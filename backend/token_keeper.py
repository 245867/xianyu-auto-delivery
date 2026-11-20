"""
Token 主动保活（A1）

要解决的问题：
- MTOP 的 token 存在 Cookie 里（`_m_h5_tk`），长时间不用会被服务端
  判过期，等到发货/发布那一刻才发现，一笔单就砸了；
- 只在"出错后重试"是被动的，且重试会和风控撞在一起。

做法（照 XianyuAssistant / goofish-cli 的经验）：
1. **轻量心跳**：每 ~10 分钟对一个只读接口发一次请求（默认
   `mtop.taobao.idle.local.poi.get`，就是发布链路第 3 步用的地址接口），
   只判断"登录态还通不通"，不做任何写操作；
2. **响应换签**：服务端在响应头下发新 Cookie 时立即合并回写（token 会被续期）；
3. **定期换 token**：每 1.5–2.5 小时随机一次，调 IM 的 token 接口换新的
   accessToken（模拟客户端行为，间隔随机，不打点）；
4. **失败不阻塞**：连续失败只告警（走通知渠道）不重试风暴；夜间也照常
   （只读请求，不违反"23–08 禁写"）。

开关：global_config.yml → TOKEN_KEEPALIVE.enabled（默认开；只读，无写操作）。
状态：data/token_keeper.json（每账号 last_ping/last_ok/last_error/next_refresh）。
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from config import config
from opportunity_publisher import (
    TOKEN_COOKIE_NAME, extract_token_from_cookie, _classify_ret, _mtop_request,
)
import polish_service

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / "data" / "token_keeper.json"
CONFIG_FILE = PROJECT_ROOT / "data" / "token_keeper_config.json"

PING_API = "mtop.taobao.idle.local.poi.get"
PING_VERSION = "1.0"
PING_SPM = "a21ybx.publish.0.0"
TOKEN_API = "mtop.taobao.idlemessage.pc.login.token"
TOKEN_VERSION = "1.0"
TOKEN_SPM = "a21ybx.im.0.0"

TICK_SECONDS = 60
DEFAULTS = {
    "enabled": True,
    "ping_minutes": 10,
    "refresh_min_hours": 1.5,
    "refresh_max_hours": 2.5,
}

_lock = threading.Lock()
_started = threading.Event()
_LOGS: List[Dict[str, Any]] = []
MAX_LOGS = 60


# ---------------------------------------------------------------- 配置/状态

def _cfg_int(key: str, default: int) -> int:
    try:
        return int(config.get(f"TOKEN_KEEPALIVE.{key}", default))
    except Exception:
        return default


def _cfg_float(key: str, default: float) -> float:
    try:
        return float(config.get(f"TOKEN_KEEPALIVE.{key}", default))
    except Exception:
        return default


def _override() -> Dict[str, Any]:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def save_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """页面上的开关/间隔写这里（优先级高于 global_config.yml，方便随时调）。"""
    cur = _override()
    if "enabled" in data:
        cur["enabled"] = bool(data["enabled"])
    if "ping_minutes" in data:
        cur["ping_minutes"] = max(1, min(120, int(data["ping_minutes"])))
    if "refresh_min_hours" in data:
        cur["refresh_min_hours"] = max(0.5, min(12.0, float(data["refresh_min_hours"])))
    if "refresh_max_hours" in data:
        cur["refresh_max_hours"] = max(0.5, min(24.0, float(data["refresh_max_hours"])))
    if cur.get("refresh_max_hours", 2.5) <= cur.get("refresh_min_hours", 1.5):
        cur["refresh_max_hours"] = float(cur.get("refresh_min_hours", 1.5)) + 0.5
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return get_config()


def get_config() -> Dict[str, Any]:
    enabled = config.get("TOKEN_KEEPALIVE.enabled", DEFAULTS["enabled"])
    cfg = {
        "enabled": bool(enabled) if enabled is not None else DEFAULTS["enabled"],
        "ping_minutes": _cfg_int("ping_minutes", DEFAULTS["ping_minutes"]),
        "refresh_min_hours": _cfg_float("refresh_min_hours", DEFAULTS["refresh_min_hours"]),
        "refresh_max_hours": _cfg_float("refresh_max_hours", DEFAULTS["refresh_max_hours"]),
    }
    cfg.update({k: v for k, v in _override().items() if k in cfg})
    cfg["refresh_max_hours"] = max(cfg["refresh_max_hours"], cfg["refresh_min_hours"] + 0.5)
    cfg["ping_api"] = PING_API
    return cfg


def _load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass


def _log(level: str, text: str) -> None:
    entry = {"time": datetime.now().isoformat(timespec="seconds"),
             "level": level, "text": text}
    _LOGS.append(entry)
    del _LOGS[:-MAX_LOGS]
    try:
        print(f"[token] {text}")
    except Exception:
        pass


# ---------------------------------------------------------------- 账号来源

def _all_cookies() -> Dict[str, str]:
    try:
        import cookie_manager as cm
        cookies = getattr(getattr(cm, "manager", None), "cookies", None) or {}
        if cookies:
            return {str(k): v for k, v in cookies.items() if v}
    except Exception:
        pass
    try:
        from db_manager import db_manager
        return {str(k): v for k, v in (db_manager.get_all_cookies() or {}).items() if v}
    except Exception:
        return {}


# ---------------------------------------------------------------- 单个账号

def _device_id(cookie_str: str) -> str:
    """按上游 generate_device_id 的规则，从 unb 生成稳定的 deviceId。"""
    unb = ""
    for part in (cookie_str or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name.strip() == "unb":
            unb = value.strip()
            break
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    out = []
    for i in range(36):
        if i in (8, 13, 18, 23):
            out.append("-")
        elif i == 14:
            out.append("4")
        elif i == 19:
            out.append(chars[(random.randint(0, 15) & 0x3) | 0x8])
        else:
            out.append(chars[random.randint(0, 15)])
    return "".join(out) + "-" + (unb or "0")


def _merge_and_persist(cookie_id: str, cookie_str: str, resp) -> str:
    if not getattr(resp, "headers", None) or not resp.headers.get("set-cookie"):
        return cookie_str
    try:
        new_cookie = polish_service._merge_response_cookies(resp, cookie_str)
        if new_cookie != cookie_str:
            polish_service._persist_cookie(cookie_id, new_cookie)
            _log("info", f"{cookie_id} 服务端下发新 Cookie，已换签")
            return new_cookie
    except Exception:
        pass
    return cookie_str


def ping(cookie_id: str, cookie_str: str) -> Dict[str, Any]:
    """只读心跳：确认登录态可用，并顺手接住服务端下发的续期 Cookie。"""
    app_key = str(config.get("APP_CONFIG.app_key", ""))
    if not extract_token_from_cookie(cookie_str):
        return {"ok": False, "error": f"Cookie 里没有 {TOKEN_COOKIE_NAME}", "need_login": True}
    try:
        raw, resp = _mtop_request(cookie_str, PING_API, {}, PING_VERSION,
                                  PING_SPM, app_key, timeout=15)
    except requests.RequestException as exc:
        return {"ok": False, "error": f"网络异常：{type(exc).__name__}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}
    new_cookie = _merge_and_persist(cookie_id, cookie_str, resp)
    ret = raw.get("ret") or []
    ret_str = " | ".join(str(r) for r in ret) if isinstance(ret, list) else str(ret)
    try:
        _classify_ret(PING_API, ret_str)
    except Exception as exc:
        text = str(exc)
        need_login = "登录" in text or "令牌" in text or "SESSION" in text or "TOKEN" in text
        return {"ok": False, "error": text[:160], "need_login": need_login,
                "cookie_str": new_cookie}
    return {"ok": True, "cookie_str": new_cookie}


def refresh_im_token(cookie_id: str, cookie_str: str) -> Dict[str, Any]:
    """向 IM 接口换一个 accessToken（模拟客户端；失败不影响主流程）。"""
    app_key = str(config.get("APP_CONFIG.app_key", ""))
    device_id = _device_id(cookie_str)
    try:
        raw, resp = _mtop_request(
            cookie_str, TOKEN_API,
            {"deviceId": device_id, "appKey": app_key, "platform": "web"},
            TOKEN_VERSION, TOKEN_SPM, app_key, timeout=15)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    new_cookie = _merge_and_persist(cookie_id, cookie_str, resp)
    ret = raw.get("ret") or []
    ret_str = " | ".join(str(r) for r in ret) if isinstance(ret, list) else str(ret)
    try:
        _classify_ret(TOKEN_API, ret_str)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "cookie_str": new_cookie}
    token = ((raw.get("data") or {}).get("accessToken")
             or (raw.get("data") or {}).get("token") or "")
    return {"ok": bool(token), "token_len": len(str(token)),
            "error": "" if token else "响应里没有 accessToken",
            "cookie_str": new_cookie}


# ---------------------------------------------------------------- 调度

def run_once(force: bool = False) -> Dict[str, Any]:
    """检查一轮：到点的做心跳/换 token。force=True 忽略到期时间。"""
    cfg = get_config()
    if not cfg["enabled"] and not force:
        return {"ok": True, "skipped": "开关关闭（TOKEN_KEEPALIVE.enabled=false）"}

    cookies = _all_cookies()
    if not cookies:
        return {"ok": True, "skipped": "没有已登录账号"}

    now = time.time()
    state = _load_state()
    result: Dict[str, Any] = {"ok": True, "accounts": {}, "alerts": []}

    for cookie_id, cookie_str in cookies.items():
        acct = state.get(cookie_id) or {}

        # 账号在风控熔断里就别再敲了（拆解 3）。心跳本身也是请求：
        # 平台正在罚站的时候，每 20 分钟去 ping 一次只会把处罚拖长。
        try:
            import rate_limiter as _rl
            _blocked, _remain = _rl.circuit_blocked(cookie_id)
        except Exception:
            _blocked, _remain = False, 0

        # 用户重新登录 / Cookie 被刷新过 → 之前的失败作废，连熔断一起解除。
        # 理由：熔断针对的是"那个登录态被盯上了"，换了一个登录态就是另一回事。
        # 上游 Usagi 用文件 mtime 判这件事，我们直接比 Cookie 内容的哈希，更准。
        _sig = hashlib.sha256(str(cookie_str or "").encode()).hexdigest()[:16]
        _prev_sig = acct.get("cookie_sig") or ""
        if _prev_sig and _prev_sig != _sig:
            acct["fail_count"] = 0
            acct["last_ok"] = True
            acct["cookie_updated_at"] = datetime.now().isoformat(timespec="seconds")
            _blocked, _remain = False, 0
            try:
                _rl.reset_circuit(cookie_id)
            except Exception:
                pass
        if _blocked:
            result["accounts"][cookie_id] = {
                "ok": bool(acct.get("last_ok", True)),
                "skipped": "风控熔断中",
                "remain_seconds": _remain,
                "last_ping": acct.get("last_ping", ""),
            }
            state[cookie_id] = acct
            continue

        ping_due = force or (now - float(acct.get("last_ping_ts") or 0)
                             >= cfg["ping_minutes"] * 60)
        refresh_due = now >= float(acct.get("next_refresh_ts") or 0)
        if not (ping_due or refresh_due):
            result["accounts"][cookie_id] = {
                "ok": acct.get("last_ok", True),
                "last_ping": acct.get("last_ping", ""),
                "next_ping_in": max(0, int(cfg["ping_minutes"] * 60
                                           - (now - float(acct.get("last_ping_ts") or 0)))),
            }
            acct["cookie_sig"] = _sig
            state[cookie_id] = acct
            continue

        info: Dict[str, Any] = {}
        if ping_due:
            p = ping(cookie_id, cookie_str)
            cookie_str = p.get("cookie_str") or cookie_str
            info.update({"ok": bool(p.get("ok")), "error": p.get("error", "")})
            acct["last_ping_ts"] = now
            acct["last_ping"] = datetime.now().isoformat(timespec="seconds")
            acct["last_ok"] = bool(p.get("ok"))
            acct["last_error"] = p.get("error", "")
            if not p.get("ok"):
                acct["fail_count"] = int(acct.get("fail_count") or 0) + 1
                result["alerts"].append(f"{cookie_id}：{p.get('error', '心跳失败')}")
                if acct["fail_count"] in (3, 10) or p.get("need_login"):
                    _notify("登录态异常", f"账号 {cookie_id} 心跳失败 {acct['fail_count']} 次："
                            f"{p.get('error', '')[:200]}\n请到应用里重新登录该账号。")
            else:
                acct["fail_count"] = 0

        # 心跳不通（风控/掉登录）时不再叠一次换 token，避免同一账号连续硬撞
        refresh_ok_to_run = info.get("ok", acct.get("last_ok", True))
        if refresh_due and refresh_ok_to_run:
            r = refresh_im_token(cookie_id, cookie_str)
            cookie_str = r.get("cookie_str") or cookie_str
            acct["last_refresh"] = datetime.now().isoformat(timespec="seconds")
            gap = random.uniform(cfg["refresh_min_hours"], cfg["refresh_max_hours"]) * 3600
            acct["next_refresh_ts"] = now + max(600.0, gap)
            info["token_refresh"] = "ok" if r.get("ok") else (r.get("error") or "failed")

        acct["cookie_sig"] = hashlib.sha256(str(cookie_str or "").encode()).hexdigest()[:16]
        state[cookie_id] = acct
        result["accounts"][cookie_id] = info
        # 账号之间随机错开 3–8 秒，避免同一秒打多个请求
        time.sleep(random.uniform(3, 8))

    _save_state(state)
    result["config"] = cfg
    return result


def _notify(title: str, content: str) -> None:
    try:
        from notify_service import send_event
        send_event("risk", title, content, level="warn")
    except Exception:
        pass


def _loop() -> None:
    _log("info", "Token 保活线程已启动")
    import task_watchdog as _wd
    _wd.register("token-keeper", TICK_SECONDS, "Token 保活轮询")
    while True:
        try:
            run_once()
        except Exception:
            _log("error", "保活轮询异常：\n" + traceback.format_exc()[-800:])
        _wd.beat("token-keeper")
        time.sleep(TICK_SECONDS)


def start_keeper() -> bool:
    """幂等启动（路由加载时调用）。"""
    if _started.is_set():
        return False
    _started.set()
    threading.Thread(target=_loop, daemon=True, name="token-keeper").start()
    return True


def get_status() -> Dict[str, Any]:
    state = _load_state()
    return {
        "config": get_config(),
        "running": _started.is_set(),
        "accounts": [{"cookie_id": k, **v} for k, v in state.items()],
        "logs": _LOGS[-20:],
    }


__all__ = ["start_keeper", "run_once", "get_status", "get_config", "save_config",
           "ping", "refresh_im_token", "STATE_FILE", "CONFIG_FILE"]
