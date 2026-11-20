"""平台风控持久化守卫。

目的只有一个：**平台已经罚站时，进程重启不能把罚期清零再撞一次。**

四个状态：
    normal     正常
    throttled  已出现限流/429，短暂降速
    cooling    风控冷却期，拒绝请求
    blocked    硬拦截，长冷却

状态与预算分别落在：
    data/platform_guard.{cookie_id}.json
    data/platform_guard.budget.{cookie_id}.json

所有时间都是 wall-clock Unix 秒（不是进程启动后的相对秒），因此重启后
直接读磁盘即可继续原来的罚期。写入使用临时文件 + os.replace 原子替换。
状态文件损坏时按 blocked 保守处理，且**不回写、不重置**。
"""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

STATES = ("normal", "throttled", "cooling", "blocked")
DEFAULT_STATE_DIR = Path(os.environ.get("PLATFORM_GUARD_DIR") or DATA_DIR)

_LOCKS: Dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _settings() -> Dict[str, Any]:
    section: Dict[str, Any] = {}
    try:
        from config import config

        raw = config.get("PLATFORM_GUARD")
        if isinstance(raw, dict):
            section = raw
    except Exception:
        section = {}

    def _int(name: str, default: int) -> int:
        try:
            return int(section.get(name, default))
        except (TypeError, ValueError):
            return default

    def _float(name: str, default: float) -> float:
        try:
            return float(section.get(name, default))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": section.get("enabled", False) is True,
        "state_dir": str(section.get("state_dir") or "").strip(),
        "hourly_budget": max(1, _int("hourly_budget", 1200)),
        "daily_budget": max(1, _int("daily_budget", 12000)),
        "throttle_cooldown_seconds": max(1, _int("throttle_cooldown_seconds", 300)),
        "cooling_cooldown_seconds": max(1, _int("cooling_cooldown_seconds", 900)),
        "block_cooldown_seconds": max(1, _int("block_cooldown_seconds", 3600)),
        "corrupt_hold_seconds": max(1, _int("corrupt_hold_seconds", 900)),
        "jitter_min_seconds": max(0.0, _float("jitter_min_seconds", 0.05)),
        "jitter_max_seconds": max(0.0, _float("jitter_max_seconds", 0.15)),
    }


def enabled() -> bool:
    """是否启用请求入口守卫。环境变量可强制覆盖，便于排障。"""
    override = os.environ.get("PLATFORM_GUARD_ENABLED")
    if override is not None and str(override).strip() != "":
        return str(override).strip().lower() not in ("0", "false", "off", "no")
    return _settings()["enabled"]


def parse_cookie_map(cookie: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for part in str(cookie or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        result[name.strip()] = value.strip()
    return result


def account_id_from_cookie(cookie: str) -> str:
    """账号 ID 取 Cookie 里的 `unb`（与 utils/refresh_util.py 的 hid 同源）。"""
    return str(parse_cookie_map(cookie).get("unb") or "").strip()


def _safe_account_key(value: str) -> str:
    key = re.sub(r"[^0-9A-Za-z_.-]", "_", str(value or "")).strip("._")
    return key or "global"


class PlatformGuardBlocked(RuntimeError):
    def __init__(self, account_id: str, retry_seconds: int, state: str):
        self.account_id = account_id
        self.retry_seconds = int(retry_seconds)
        self.state = state
        super().__init__(
            f"平台风控守卫拦截账号 {account_id or '*'}：状态 {state}，"
            f"建议 {self.retry_seconds}s 后再试"
        )


class PlatformGuard:
    def __init__(
        self,
        account_id: str,
        *,
        state_dir: Optional[Path] = None,
        throttle_cooldown_seconds: int = 300,
        cooling_cooldown_seconds: int = 900,
        block_cooldown_seconds: int = 3600,
        corrupt_hold_seconds: int = 900,
        hourly_budget: int = 600,
        daily_budget: int = 6000,
        jitter_min_seconds: float = 0.05,
        jitter_max_seconds: float = 0.15,
        now_fn=time.time,
    ):
        self.account_id = _safe_account_key(account_id)
        self.state_dir = Path(state_dir) if state_dir else DEFAULT_STATE_DIR
        self.state_path = self.state_dir / f"platform_guard.{self.account_id}.json"
        self.budget_path = (
            self.state_dir / f"platform_guard.budget.{self.account_id}.json"
        )
        self.throttle_cooldown_seconds = max(1, int(throttle_cooldown_seconds))
        self.cooling_cooldown_seconds = max(1, int(cooling_cooldown_seconds))
        self.block_cooldown_seconds = max(1, int(block_cooldown_seconds))
        self.corrupt_hold_seconds = max(1, int(corrupt_hold_seconds))
        self.hourly_budget = max(1, int(hourly_budget))
        self.daily_budget = max(1, int(daily_budget))
        self.jitter_min_seconds = max(0.0, float(jitter_min_seconds))
        self.jitter_max_seconds = max(0.0, float(jitter_max_seconds))
        self.now_fn = now_fn

    # ------------------------------------------------------------ 状态读写

    def _now(self, now: Optional[float]) -> float:
        return float(self.now_fn() if now is None else now)

    @staticmethod
    def _fresh_state(now: float) -> Dict[str, Any]:
        return {
            "version": 1,
            "state": "normal",
            "reason": "",
            "updated_at": now,
            "cooldown_until": 0.0,
            "throttle_strikes": 0,
            "risk_strikes": 0,
            "last_success_at": 0.0,
            "transitions": [],
        }

    def _read_state(self, now: float) -> Tuple[Dict[str, Any], bool]:
        if not self.state_path.exists():
            return self._fresh_state(now), False
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state root is not an object")
            if raw.get("state") not in STATES:
                raise ValueError("unknown state")
            float(raw.get("cooldown_until") or 0)
            float(raw.get("updated_at") or 0)
            raw.setdefault("reason", "")
            raw.setdefault("throttle_strikes", 0)
            raw.setdefault("risk_strikes", 0)
            raw.setdefault("last_success_at", 0.0)
            raw.setdefault("transitions", [])
            return raw, False
        except Exception as exc:
            logger.warning(
                "平台守卫状态文件损坏，按 blocked 保守处理（{}）：{}",
                type(exc).__name__,
                self.state_path.name,
            )
            # 不读取旧时间、不回写文件。每次读取都以当前时刻重新给足保守罚期，
            # 因此只要文件仍是坏的，就绝不会因为进程重启而放行。
            return {
                "version": 1,
                "state": "blocked",
                "reason": "state file corrupted",
                "updated_at": now,
                "cooldown_until": now + self.corrupt_hold_seconds,
                "throttle_strikes": 0,
                "risk_strikes": 0,
                "last_success_at": 0.0,
                "transitions": [],
                "corrupt": True,
            }, True

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_name = ""
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp_name, path)
                    return True
                except PermissionError:
                    time.sleep(0.05 * (attempt + 1))
            logger.warning("平台守卫状态落盘被占用，本次跳过：{}", path.name)
            return False
        except Exception as exc:
            logger.warning(
                "平台守卫状态落盘失败（{}）：{}", type(exc).__name__, path.name
            )
            return False
        finally:
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def _write_state(self, state: Dict[str, Any]) -> None:
        self._write_json(self.state_path, state)

    def _read_budget(self, now: float) -> Dict[str, Any]:
        hour_start = int(now // 3600 * 3600)
        day_start = int(now // 86400 * 86400)
        fresh = {
            "version": 1,
            "hour_start": hour_start,
            "hour_count": 0,
            "day_start": day_start,
            "day_count": 0,
        }
        if not self.budget_path.exists():
            return fresh
        try:
            raw = json.loads(self.budget_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("budget root is not an object")
            raw["hour_start"] = int(raw.get("hour_start") or hour_start)
            raw["hour_count"] = max(0, int(raw.get("hour_count") or 0))
            raw["day_start"] = int(raw.get("day_start") or day_start)
            raw["day_count"] = max(0, int(raw.get("day_count") or 0))
        except Exception as exc:
            logger.warning(
                "平台守卫预算文件损坏，按新窗口重置（{}）：{}",
                type(exc).__name__,
                self.budget_path.name,
            )
            return fresh

        if raw["hour_start"] != hour_start:
            raw["hour_start"] = hour_start
            raw["hour_count"] = 0
        if raw["day_start"] != day_start:
            raw["day_start"] = day_start
            raw["day_count"] = 0
        return raw

    def _consume_budget(self, now: float) -> Tuple[bool, int]:
        with _lock_for(self.budget_path):
            budget = self._read_budget(now)
            if budget["hour_count"] >= self.hourly_budget:
                retry = max(1, int(budget["hour_start"] + 3600 - now))
                return False, retry
            if budget["day_count"] >= self.daily_budget:
                retry = max(1, int(budget["day_start"] + 86400 - now))
                return False, retry
            budget["hour_count"] += 1
            budget["day_count"] += 1
            self._write_json(self.budget_path, budget)
            return True, 0

    # ------------------------------------------------------------ 状态迁移

    def _commit(
        self,
        previous: Dict[str, Any],
        *,
        state: str,
        reason: str,
        now: float,
        cooldown_until: float,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        transitions = list(previous.get("transitions") or [])
        transitions.append(
            {
                "at": now,
                "from": previous.get("state"),
                "to": state,
                "reason": str(reason or "")[:200],
            }
        )
        merged = dict(previous)
        merged.update(
            {
                "version": 1,
                "state": state,
                "reason": str(reason or "")[:200],
                "updated_at": now,
                "cooldown_until": float(max(0.0, cooldown_until)),
                "transitions": transitions[-20:],
            }
        )
        if extra:
            merged.update(extra)
        self._write_state(merged)
        return merged

    def record_throttle(
        self, reason: str = "", *, now: Optional[float] = None
    ) -> Dict[str, Any]:
        current = self._now(now)
        previous, corrupt = self._read_state(current)
        if corrupt:
            return self.record_block(reason or "state file corrupted", now=current)
        state = previous.get("state")
        strikes = int(previous.get("throttle_strikes") or 0)
        until = float(previous.get("cooldown_until") or 0)

        if state in ("cooling", "blocked") and until <= current:
            return self._commit(
                previous,
                state="throttled",
                reason=reason,
                now=current,
                cooldown_until=current + self.throttle_cooldown_seconds,
                extra={"throttle_strikes": 1},
            )
        if state == "normal":
            return self._commit(
                previous,
                state="throttled",
                reason=reason,
                now=current,
                cooldown_until=current + self.throttle_cooldown_seconds,
                extra={"throttle_strikes": 1},
            )
        if state == "throttled":
            if until > current:
                strikes += 1
                if strikes >= 2:
                    return self._commit(
                        previous,
                        state="cooling",
                        reason=reason or "repeated throttling",
                        now=current,
                        cooldown_until=current + self.cooling_cooldown_seconds,
                        extra={"throttle_strikes": strikes},
                    )
            return self._commit(
                previous,
                state="throttled",
                reason=reason,
                now=current,
                cooldown_until=current + self.throttle_cooldown_seconds,
                extra={"throttle_strikes": strikes},
            )
        return previous

    def record_risk(
        self, reason: str = "", *, now: Optional[float] = None
    ) -> Dict[str, Any]:
        current = self._now(now)
        previous, corrupt = self._read_state(current)
        if corrupt:
            merged = dict(previous)
            merged["cooldown_until"] = current + self.block_cooldown_seconds
            merged["updated_at"] = current
            merged["reason"] = str(reason or "")[:200]
            self._write_state(merged)
            return merged

        state = previous.get("state")
        until = float(previous.get("cooldown_until") or 0)
        if state in ("normal", "throttled"):
            return self._commit(
                previous,
                state="cooling",
                reason=reason,
                now=current,
                cooldown_until=current + self.cooling_cooldown_seconds,
                extra={"risk_strikes": int(previous.get("risk_strikes") or 0) + 1},
            )
        if state == "cooling" and until > current:
            return self._commit(
                previous,
                state="blocked",
                reason=reason or "risk repeated during cooling",
                now=current,
                cooldown_until=current + self.block_cooldown_seconds,
                extra={"risk_strikes": int(previous.get("risk_strikes") or 0) + 1},
            )
        if state == "blocked":
            return self._commit(
                previous,
                state="blocked",
                reason=reason,
                now=current,
                cooldown_until=max(until, current + self.block_cooldown_seconds),
            )
        return self._commit(
            previous,
            state="cooling",
            reason=reason,
            now=current,
            cooldown_until=current + self.cooling_cooldown_seconds,
        )

    def record_block(
        self, reason: str = "", *, now: Optional[float] = None
    ) -> Dict[str, Any]:
        current = self._now(now)
        previous, _corrupt = self._read_state(current)
        until = float(previous.get("cooldown_until") or 0)
        return self._commit(
            previous,
            state="blocked",
            reason=reason,
            now=current,
            cooldown_until=max(until, current + self.block_cooldown_seconds),
        )

    def record_success(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        current = self._now(now)
        previous, corrupt = self._read_state(current)
        if corrupt:
            # 损坏文件未修复前，不允许任何成功回执把罚期清零。
            return previous
        state = previous.get("state")
        if state == "normal":
            return previous
        until = float(previous.get("cooldown_until") or 0)
        if until > current:
            return previous
        merged = self._commit(
            previous,
            state="normal",
            reason="penalty expired and probe succeeded",
            now=current,
            cooldown_until=0.0,
        )
        merged["last_success_at"] = current
        merged["throttle_strikes"] = 0
        merged["risk_strikes"] = 0
        self._write_state(merged)
        return merged

    # ------------------------------------------------------------ 请求闸门

    def jitter_seconds(self) -> float:
        low, high = sorted((self.jitter_min_seconds, self.jitter_max_seconds))
        return random.uniform(low, high)

    def acquire(
        self,
        scope: str = "read",
        *,
        write: bool = False,
        now: Optional[float] = None,
        jitter: bool = True,
        sleep: bool = False,
    ) -> Tuple[bool, int]:
        current = self._now(now)
        state, corrupt = self._read_state(current)
        until = float(state.get("cooldown_until") or 0)
        current_state = str(state.get("state") or "normal")

        if current_state in ("cooling", "blocked") and until > current:
            return False, max(1, int(until - current))
        if corrupt:
            return False, max(1, int(until - current))

        allowed, retry = self._consume_budget(current)
        if not allowed:
            return False, retry
        if jitter and sleep:
            time.sleep(self.jitter_seconds())
        return True, 0

    def snapshot(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        current = self._now(now)
        state, corrupt = self._read_state(current)
        budget = self._read_budget(current)
        until = float(state.get("cooldown_until") or 0)
        current_state = str(state.get("state") or "normal")
        retry = max(0, int(until - current)) if current_state != "normal" else 0
        return {
            "account_id": self.account_id,
            "state": current_state,
            "reason": state.get("reason") or "",
            "cooldown_until": until,
            "retry_seconds": retry,
            "updated_at": state.get("updated_at") or 0,
            "corrupt": corrupt,
            "hour_used": budget["hour_count"],
            "hour_limit": self.hourly_budget,
            "day_used": budget["day_count"],
            "day_limit": self.daily_budget,
        }


def guard_for_cookie(
    cookie: str = "",
    *,
    account_id: str = "",
    state_dir: Optional[Path] = None,
) -> PlatformGuard:
    settings = _settings()
    key = str(account_id or "").strip() or account_id_from_cookie(cookie) or "global"
    return PlatformGuard(
        key,
        state_dir=state_dir or (Path(settings["state_dir"]) if settings["state_dir"] else None),
        throttle_cooldown_seconds=settings["throttle_cooldown_seconds"],
        cooling_cooldown_seconds=settings["cooling_cooldown_seconds"],
        block_cooldown_seconds=settings["block_cooldown_seconds"],
        corrupt_hold_seconds=settings["corrupt_hold_seconds"],
        hourly_budget=settings["hourly_budget"],
        daily_budget=settings["daily_budget"],
        jitter_min_seconds=settings["jitter_min_seconds"],
        jitter_max_seconds=settings["jitter_max_seconds"],
    )


def admit(
    cookie: str = "",
    *,
    account_id: str = "",
    scope: str = "read",
    write: bool = False,
    sleep: bool = False,
    state_dir: Optional[Path] = None,
) -> Optional[PlatformGuard]:
    """请求发送前调用。未启用或放行时返回 guard，拦截时抛 PlatformGuardBlocked。"""
    if not enabled():
        return None
    guard = guard_for_cookie(cookie, account_id=account_id, state_dir=state_dir)
    allowed, retry = guard.acquire(scope, write=write, jitter=True, sleep=sleep)
    if not allowed:
        snapshot = guard.snapshot()
        raise PlatformGuardBlocked(
            guard.account_id, retry, str(snapshot.get("state") or "blocked")
        )
    return guard


RATE_LIMIT_MARKERS = ("429", "TOO_MANY", "频繁", "限流", "稍后重试")


def observe(
    cookie: str = "",
    *,
    account_id: str = "",
    text: str = "",
    status_code: int = 0,
    state_dir: Optional[Path] = None,
) -> None:
    """请求返回后调用。按返回文本推进状态，不抛业务异常。"""
    if not enabled():
        return
    try:
        guard = guard_for_cookie(cookie, account_id=account_id, state_dir=state_dir)
        body = str(text or "")[:1000]
        from mtop_response import RISK_MARKERS, has_marker

        if has_marker(body, RISK_MARKERS):
            guard.record_risk(body[:200])
        elif int(status_code or 0) == 429 or has_marker(body, RATE_LIMIT_MARKERS):
            guard.record_throttle(body[:200])
        elif "SUCCESS" in body.upper():
            guard.record_success()
    except Exception as exc:
        logger.warning("平台守卫回执处理异常，已跳过（{}）", type(exc).__name__)


__all__ = [
    "PlatformGuard",
    "PlatformGuardBlocked",
    "STATES",
    "enabled",
    "admit",
    "observe",
    "guard_for_cookie",
    "account_id_from_cookie",
    "parse_cookie_map",
]
