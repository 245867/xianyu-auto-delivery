"""
统一限流器（A2）

为什么要有：写操作分散在发布、擦亮、发货、评价里，各自 sleep 一段，参数不一致、
重启就清零、也没法解释"为什么这次没跑"。这里做一个进程内共享、落盘的滑动窗口，
所有写路径统一从这里取配额。

口径（沿用竞品验证过的共识，见《开工前竞品穷尽与施工排期.md》第三节）：
    write   1 次/60s      任意写操作（兜底）
    publish 1 次/60s      发布（按账号）
    media   9 次/60s      图片上传
    search  1 次/10s      关键词搜索
    detail  12 次/60s     详情补数
    polish  1 次/8s       单商品擦亮（配合 8–15s 随机）
    delivery 1 次/3s      发货（配合 3–20s 随机 + 同买家 600s 冷却）
    daily   200 次/天     单账号日上限

用法：
    ok, retry = acquire("publish", cookie_id)
    if not ok:  # 别硬撞，告诉用户排队
        return {"ok": False, "error": f"该账号 60 秒内已发布过，请 {retry}s 后再试"}
    wait("detail", "global", max_wait=30)   # 排不上就等，最多等 max_wait 秒
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 可用 RATE_LIMIT_STATE 覆盖（测试用），避免测试污染用户真实的限流计数
STATE_FILE = Path(os.environ.get("RATE_LIMIT_STATE")
                  or (PROJECT_ROOT / "data" / "rate_limit.json"))

# scope -> (次数, 窗口秒数)
RULES: Dict[str, Tuple[int, int]] = {
    "write": (1, 60),
    "publish": (1, 60),
    "media": (9, 60),
    "search": (1, 10),
    "detail": (12, 60),
    "polish": (1, 8),
    "delivery": (1, 3),
}
DAILY_CAP = 200
DAILY_SCOPES = ("publish", "polish", "delivery", "write")

# 写操作 scope：这些动作会留下平台可见的痕迹，账号被风控时继续写只会越推越深。
# search / detail 是读，不在熔断范围内（风控期间读一读不会加刑，且复盘需要数据）。
WRITE_SCOPES = ("write", "publish", "polish", "delivery", "media")

# 熔断状态单独落一个文件：跨进程（多个账号任务各在自己的事件循环里）、重启后依然有效。
# 进程内的 fail_streak 一重启就清零，等于"每次重启都重新去撞一次墙"。
CIRCUIT_FILE = Path(os.environ.get("RATE_LIMIT_CIRCUIT")
                    or (PROJECT_ROOT / "data" / "circuit.json"))
try:
    CIRCUIT_MINUTES = max(1, int(os.environ.get("CIRCUIT_BREAK_MINUTES", "10")))
except Exception:
    CIRCUIT_MINUTES = 10
# 半开探针（L2-3）：熔断到期不等于风控解除。以前是一到期就全速恢复，几十条排队
# 的写操作同时涌上去，往往几分钟内又撞一次墙、再停 10 分钟——用户只看到"又停了"，
# 平台那边看到的是"这个号刚解封就狂发"。
# 现在到期后进入半开：每 PROBE_SPACING 秒只放行一次写，连续 CLOSE_AFTER 秒不再
# 撞墙才算真的恢复；探针再撞墙则时长翻倍（10 → 20 → 40，封顶 60 分钟）。
try:
    CIRCUIT_MAX_MINUTES = max(CIRCUIT_MINUTES,
                              int(os.environ.get("CIRCUIT_MAX_MINUTES", "60")))
except Exception:
    CIRCUIT_MAX_MINUTES = 60
try:
    PROBE_SPACING = max(1.0, float(os.environ.get("CIRCUIT_PROBE_SPACING_SECONDS", "30")))
except Exception:
    PROBE_SPACING = 30.0
try:
    CLOSE_AFTER = max(PROBE_SPACING,
                      float(os.environ.get("CIRCUIT_CLOSE_AFTER_SECONDS", "300")))
except Exception:
    CLOSE_AFTER = 300.0
# 很久没再触发（默认 6 小时）就从第一档重新开始算，不把一周前的账一直翻倍
try:
    ESCALATION_RESET_SECONDS = max(60.0, float(
        os.environ.get("CIRCUIT_ESCALATION_RESET_SECONDS", "21600")))
except Exception:
    ESCALATION_RESET_SECONDS = 21600.0

_lock = threading.Lock()
_mem: Dict[str, Dict[str, list]] = {}
_daily: Dict[str, Dict[str, Any]] = {}
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        _mem.update(raw.get("windows") or {})
        _daily.update(raw.get("daily") or {})
    except Exception:
        pass


def _save() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"windows": _mem, "daily": _daily},
                                         ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _prune(bucket: list, window: int, now: float) -> list:
    cut = now - window
    return [t for t in bucket if t > cut]


def _daily_key(scope: str, key: str) -> str:
    return f"{scope}:{key or '*'}"


def _circuit_read() -> Dict[str, Any]:
    try:
        raw = json.loads(CIRCUIT_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {}


def _circuit_write(state: Dict[str, Any]) -> None:
    try:
        CIRCUIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CIRCUIT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CIRCUIT_FILE)
    except Exception:
        pass


def circuit_blocked(key: str = "") -> Tuple[bool, int]:
    """`(是否熔断, 剩余秒数)`。按 key（账号）+ 全局取最晚的一次。"""
    st = _circuit_read()
    now = time.time()
    try:
        until = float(st.get("until") or 0)
    except Exception:
        until = 0
    try:
        until = max(until, float((st.get("accounts") or {}).get(str(key or "*")) or 0))
    except Exception:
        pass
    if until > now:
        return True, max(1, int(until - now))
    return False, 0


def _half_map(st: Dict[str, Any]) -> Dict[str, Any]:
    half = st.get("half")
    return half if isinstance(half, dict) else {}


def _until_for(st: Dict[str, Any], key: str) -> float:
    """账号与全局取较晚的一次到期时间；字段坏掉按 0 处理（不因此停业）。"""
    try:
        until = float(st.get("until") or 0)
    except Exception:
        until = 0.0
    try:
        until = max(until, float((st.get("accounts") or {}).get(key) or 0))
    except Exception:
        pass
    return until


def _gate_eval(entry: Any, now: float) -> Tuple[str, int]:
    """
    半开判定（**纯计算，不落盘**）。返回 (动作, 等待秒)：

        "open"   没触发过熔断/旧状态 → 照常放行
        "probe"  取一次探针（或第一次进入半开）
        "block"  还没到下一个探针间隔
        "close"  安静够久，结束半开

    **为什么单拎出来**：`acquire` 里要先"问一次"，等确认这笔写真的会执行了
    再去落盘占探针。判定和落盘分开，才可能在配额拒绝时不动探针。
    """
    if not isinstance(entry, dict):
        return "open", 0
    try:
        probe_at = float(entry.get("probe_at") or 0)
    except Exception:
        probe_at = 0.0
    try:
        opened = float(entry.get("opened_at") or 0)
    except Exception:
        opened = 0.0
    if not opened:
        # `opened_at` 是"进入半开/第一笔探针"的时刻，**后续放行不刷新它**。
        # 之前用 probe_at 判恢复，而每次放行都会更新 probe_at——于是"每 30 秒
        # 来一笔"的稳定流量会把关闭计时永远推后，半开永远结束不了。
        # 旧状态文件里没有 opened_at（上一版只写了 probe_at），用 probe_at 兜底，
        # 免得升级后半开卡死；两个都没有就从这一刻开始算。
        opened = probe_at or now
    if now - opened >= CLOSE_AFTER:
        return "close", 0
    if probe_at and now - probe_at < PROBE_SPACING:
        return "block", max(1, int(PROBE_SPACING - (now - probe_at)) + 1)
    return "probe", 0


def _gate_commit(st: Dict[str, Any], key_s: str, action: str, now: float) -> None:
    """把判定结果写进状态字典并落盘。只该在"这笔写确实要执行"时调用。"""
    half = _half_map(st)
    if action == "close":
        half.pop(key_s, None)
    else:
        entry = dict(half.get(key_s) or {})
        entry.setdefault("failures", 0)
        if not entry.get("opened_at"):
            entry["opened_at"] = now  # 只在第一笔探针时定下来，之后不动
        entry["probe_at"] = now
        half[key_s] = entry
    st["half"] = half
    _circuit_write(st)


def gate_write(key: str = "") -> Tuple[bool, int]:
    """
    写操作入口闸（半开探针）。返回 (是否放行, 建议等待秒数)。

    - 熔断期内：拦；
    - 熔断刚到期：每 PROBE_SPACING 秒只放行一次写，先拿一次当探针；
    - **第一笔探针之后** CLOSE_AFTER 秒没再撞墙：视为已恢复，清掉半开记录，全速恢复。

    **为什么不并进 `circuit_blocked`**：那是个纯查询，状态页、诊断、测试都在调，
    顺手改状态会让"看一眼"变成"占掉一次探针"，查得越勤越写不了。
    所以读用它，写用这个。

    **为什么探针失败不用回调**：写路径在撞墙处本来就会调 `trip_circuit`，
    这里只要认"半开期间又 trip 了"就是探针失败，接着翻倍就行。

    **这里的探针是"赌这笔写会执行"**：直接调它的都是发货/确认发货这种
    过闸之后必定动手的路径。走 `acquire` 的发布/擦亮/评价不在这里领探针，
    否则被本地配额拦下的请求会把探针白白吃掉（见 `acquire`）。
    """
    key_s = str(key or "*")
    now = time.time()
    with _lock:
        st = _circuit_read()
        until = _until_for(st, key_s)
        if until > now:
            return False, max(1, int(until - now))

        action, wait = _gate_eval(_half_map(st).get(key_s), now)
        if action == "block":
            return False, wait
        if action in ("probe", "close"):
            _gate_commit(st, key_s, action, now)
        return True, 0


def trip_circuit(key: str = "", minutes: Optional[int] = None, reason: str = "") -> int:
    """
    触发熔断。返回熔断持续秒数。

    为什么状态要落盘：账号任务跑在多个事件循环、多个进程里，内存里的
    `fail_streak` 各记各的，重启即清零——那等于"每次重启都重新去撞一次墙"。
    goofish-cli 的 `guard.py` 把这个状态单独存一个文件，跨进程共享，这里照做。

    与它的一个**故意不同**：状态文件读不出来/格式坏了时我们是**放行**，它是拒绝写。
    理由是它是个命令行工具，文件坏了用户看得见也修得了；我们是后台常驻，
    一个被磁盘写坏的 JSON 不该让整店发不了货。读坏了会在日志里喊一声。

    半开加码：同一个账号在上一次熔断尚未"确认恢复"之前又撞墙，时长翻倍，
    说明平台那边的处置还没结束，按原来的 10 分钟一轮只会反复撞。
    """
    now = time.time()
    key_s = str(key or "*")
    with _lock:
        st = _circuit_read()
        if not isinstance(st.get("accounts"), dict):
            st["accounts"] = {}
        # 顺手清掉已经过期的账号（不然这个文件会一直长）
        st["accounts"] = {k: v for k, v in st["accounts"].items()
                          if isinstance(v, (int, float)) and v > now}

        half = _half_map(st)
        entry = half.get(key_s) if isinstance(half.get(key_s), dict) else {}
        failures = int(entry.get("failures") or 0) if entry else 0
        try:
            since = float(entry.get("since") or 0)
        except Exception:
            since = 0.0
        if entry and since and now - since > ESCALATION_RESET_SECONDS:
            # 很久没犯：把上一轮整段账清掉，这次按第一档算，不再累加
            entry = {}
            failures = 0

        base = max(1, int(minutes or CIRCUIT_MINUTES))
        if entry:
            failures += 1  # 上一轮还没确认恢复就又撞了 → 加码
        mins = min(base * (2 ** failures), CIRCUIT_MAX_MINUTES)
        secs = max(60, int(mins * 60))

        if key:
            st["accounts"][key_s] = now + secs
        else:
            st["until"] = now + secs
        # opened_at 留空：等第一笔探针真的写出去时再定"开始计时的那一刻"
        half[key_s] = {"failures": failures, "probe_at": None,
                       "opened_at": None, "since": now}
        st["half"] = half
        st["reason"] = str(reason or "")[:200]
        st["at"] = now
        _circuit_write(st)
    if key:
        try:
            from db_manager import db_manager

            day = datetime.fromtimestamp(now, timezone(timedelta(hours=8))).date().isoformat()
            db_manager.record_account_health_event(key_s, day, event="platform_block")
            logger.warning("账号 {} 收到平台安全拦截，健康分扣 25", key_s)
        except Exception as exc:
            logger.error("账号 {} 平台拦截计分失败（{}）", key_s, type(exc).__name__)
    return secs


def reset_circuit(key: Optional[str] = None) -> None:
    """人工解除熔断。`key=None` 清全部（含全局）。"""
    with _lock:
        if key is None:
            _circuit_write({})
            return
        st = _circuit_read()
        accounts = st.get("accounts") if isinstance(st.get("accounts"), dict) else {}
        accounts.pop(str(key), None)
        st["accounts"] = accounts
        half = _half_map(st)
        half.pop(str(key), None)  # 人工解除 = 连同半开加码的计数一起清掉
        st["half"] = half
        _circuit_write(st)


def circuit_status() -> Dict[str, Any]:
    st = _circuit_read()
    now = time.time()
    accounts = st.get("accounts") if isinstance(st.get("accounts"), dict) else {}
    active = {k: max(0, int(float(v) - now)) for k, v in accounts.items()
              if isinstance(v, (int, float)) and v > now}
    try:
        g = max(0, int(float(st.get("until") or 0) - now))
    except Exception:
        g = 0
    half_open = {}
    for k, v in _half_map(st).items():
        if not isinstance(v, dict):
            continue
        failures = int(v.get("failures") or 0)
        try:
            opened = float(v.get("opened_at") or v.get("probe_at") or 0)
        except Exception:
            opened = 0.0
        half_open[k] = {
            "failures": failures,
            "probing": bool(v.get("probe_at")),
            # 距"安静满 CLOSE_AFTER 秒、可以结案"还剩多久；还没探过就是 None。
            # 以前这里看不出来：只要一直有流量，半开就一直是 True，页面看不出异常。
            "closes_in": (max(0, int(CLOSE_AFTER - (now - opened))) if opened else None),
            "next_minutes": min(CIRCUIT_MINUTES * (2 ** (failures + 1)),
                                CIRCUIT_MAX_MINUTES),
        }
    return {"global_seconds": g, "accounts": active,
            "reason": st.get("reason") or "", "at": st.get("at") or 0,
            "half_open": half_open}


def acquire(scope: str, key: str = "", limit: Optional[int] = None,
            window: Optional[int] = None) -> Tuple[bool, int]:
    """尝试取一个配额。返回 (是否允许, 建议等待秒数)。不阻塞。"""
    if scope not in RULES and (limit is None or window is None):
        raise ValueError(f"未知限流 scope：{scope}")
    lim, win = RULES.get(scope, (limit, window))
    lim = int(limit if limit is not None else lim)
    win = int(window if window is not None else win)
    key = str(key or "*")

    # 熔断优先于配额：账号正在罚站，给多少配额都不该动手（拆解 3）。
    # 但"优先"只体现在**报错口径**上：先问一次闸的决定，不落盘。真正领探针
    # 要等配额和日上限都过了、确定这笔写会执行——否则被本地配额拒掉的请求
    # 会把探针名额白吃掉，30 秒后的真实写反而被拦（甚至 5 分钟后被误判成"已恢复"）。
    with _lock:
        _load()
        now = time.time()
        st: Dict[str, Any] = {}
        action, wait = "open", 0
        if scope in WRITE_SCOPES:
            st = _circuit_read()
            until = _until_for(st, key)
            if until > now:
                return False, max(1, int(until - now))
            action, wait = _gate_eval(_half_map(st).get(key), now)
            if action == "block":
                return False, wait

        scope_map = _mem.setdefault(scope, {})
        bucket = _prune(scope_map.get(key) or [], win, now)
        if len(bucket) >= lim:
            retry = max(1, int(win - (now - bucket[0])) + 1)
            scope_map[key] = bucket
            _save()
            return False, retry

        # 日上限：先查后扣。以前是"先 append 再查日上限"，被日上限拒掉的那次
        # 会白占一个窗口名额；顺带也会白吃探针。
        dk = None
        rec: Dict[str, Any] = {}
        if scope in DAILY_SCOPES:
            dk = _daily_key(scope, key)
            rec = _daily.get(dk) or {}
            today = date.today().isoformat()
            if rec.get("day") != today:
                rec = {"day": today, "count": 0}
            if int(rec.get("count") or 0) >= DAILY_CAP:
                _daily[dk] = rec
                _save()
                return False, 3600

        # 到这里这笔写一定会执行：现在才领探针（或结案），再扣窗口与日计数。
        # 全部在同一把锁里完成，避免"两个线程同时看到探针可用"一起冲出去。
        if action in ("probe", "close"):
            _gate_commit(st, key, action, now)
        bucket.append(now)
        scope_map[key] = bucket
        if dk:
            rec["count"] = int(rec.get("count") or 0) + 1
            _daily[dk] = rec

        _save()
        return True, 0


def wait(scope: str, key: str = "", max_wait: int = 60,
         limit: Optional[int] = None, window: Optional[int] = None) -> bool:
    """排队等配额，最多等 max_wait 秒。返回是否拿到。"""
    deadline = time.time() + max(0, int(max_wait))
    while True:
        ok, retry = acquire(scope, key, limit=limit, window=window)
        if ok:
            return True
        if time.time() + retry > deadline:
            return False
        time.sleep(min(retry, 5))


def jitter(min_seconds: float, max_seconds: float) -> float:
    """随机间隔（擦亮 8–15s、发货 3–20s 用）。返回实际睡了多久。"""
    lo, hi = sorted((float(min_seconds), float(max_seconds)))
    delay = random.uniform(max(0.0, lo), max(0.0, hi))
    time.sleep(delay)
    return delay


def daily_count(scope: str, key: str = "") -> int:
    with _lock:
        _load()
        rec = _daily.get(_daily_key(scope, key)) or {}
        if rec.get("day") != date.today().isoformat():
            return 0
        return int(rec.get("count") or 0)


def status() -> Dict[str, Any]:
    """给页面/诊断看的当前占用情况。"""
    with _lock:
        _load()
        now = time.time()
        out = {"rules": {k: {"limit": v[0], "window": v[1]} for k, v in RULES.items()},
               "daily_cap": DAILY_CAP, "scopes": {}, "daily": {}}
        for scope, keys in _mem.items():
            lim, win = RULES.get(scope, (0, 60))
            out["scopes"][scope] = {
                k: {"used": len(_prune(v or [], win, now)), "limit": lim, "window": win}
                for k, v in keys.items() if _prune(v or [], win, now)
            }
        for k, rec in _daily.items():
            if rec.get("day") == date.today().isoformat():
                out["daily"][k] = rec.get("count", 0)
        return out


def reset(scope: str = "") -> None:
    """只给排障用：清空某个 scope（或全部）的窗口计数。"""
    with _lock:
        _load()
        if scope:
            _mem.pop(scope, None)
        else:
            _mem.clear()
            _daily.clear()
        _save()


__all__ = ["acquire", "wait", "gate_write", "jitter", "daily_count", "status", "reset",
           "circuit_blocked", "trip_circuit", "reset_circuit", "circuit_status",
           "RULES", "DAILY_CAP", "STATE_FILE", "WRITE_SCOPES", "CIRCUIT_FILE"]
