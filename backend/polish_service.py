"""
自动擦亮服务（补齐①）—— 移植自 zhinianboke/xianyu-auto-reply
`scheduler/app/services/scheduler/polish_task.py`（AGPL-3.0，与本项目底座同源血统）。

擦亮链路（与上游逐段对齐）：
1. Cookie 解析出 MTOP token（_m_h5_tk，取 "_" 首段）；
2. POST https://h5api.m.goofish.com/h5/mtop.taobao.idle.item.polish/1.0/
   参数 jsv=2.7.2 / v=2.0 / type=originaljson / accountSite=xianyu / sessionOption=AutoLoginOnly；
3. data={"itemId": ...} 紧凑 JSON，签名 MD5(token & t & appKey & data)；
4. 成功判断 ret[0] == "SUCCESS::调用成功"；"宝贝已经擦亮过了"/"IDLEITEM_POLISH_AGAIN" 视为成功；
5. TOKEN_EXOIRED/TOKEN_EXPIRED 时合并响应 set-cookie 回写 CookieManager 并重试（最多 3 次）。

本地化改动：
- 上游 appKey 硬编码，本模块读 global_config.yml 的 APP_CONFIG.app_key（闲鱼签名固定值，禁改）；
- 上游 MySQL 记录擦亮日志，本模块用 data/polish_log.json + data/polish_state.json（SQLite 无需迁移）；
- 调度：守护线程每 10 分钟检查，每天配置时刻（默认 09:30）跑一次全量擦亮；
  逐商品随机间隔 8–15 秒、账号间暂停 30 秒防风控；单账号失败率 > 50% 视为风控信号中止该账号。
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

import rate_limiter
from platform_guard import (
    PlatformGuardBlocked,
    admit as guard_admit,
    observe as guard_observe,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
STATE_FILE = DATA_DIR / "polish_state.json"
LOG_FILE = DATA_DIR / "polish_log.json"

TOKEN_COOKIE_NAME = "_m_h5_tk"
POLISH_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idle.item.polish/1.0/"
MAX_TOKEN_RETRY = 3

# 每日调度默认配置（可用 global_config.yml AUTO_POLISH 节覆盖：enabled / hour / minute）
DEFAULTS = {"enabled": True, "hour": 9, "minute": 30}

POLISH_HEADERS = {
    'accept': 'application/json',
    'accept-language': 'en,zh-CN;q=0.9,zh;q=0.8',
    'cache-control': 'no-cache',
    'content-type': 'application/x-www-form-urlencoded',
    # 官方页面同源请求必须带 Origin，缺失会被判 FAIL_SYS_PARAMINVALID_ERROR
    'origin': 'https://www.goofish.com',
    'pragma': 'no-cache',
    'priority': 'u=1, i',
    'sec-ch-ua': '"Google Chrome";v="141", "Not=A?Brand";v="8", "Not A(Brand";v="141"',
    'sec-ch-ua-arch': '"x64"',
    'sec-ch-ua-bitness': '"64"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Win32"',
    'sec-ch-ua-platform-version': '"10.0.0"',
    'sec-fetch-dest': 'empty',
    'sec-fetch-mode': 'cors',
    'sec-fetch-site': 'same-site',
    'Referer': 'https://www.goofish.com/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}

_lock = threading.Lock()
_running = threading.Event()  # 手动/定时任务互斥
_scheduler_started = False


# ---------------------------------------------------------------- 状态与日志

def _load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: Path, data) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_settings() -> Dict[str, Any]:
    """读取 AUTO_POLISH 配置（enabled/hour/minute），缺省每日 09:30。"""
    settings = dict(DEFAULTS)
    try:
        from config import config
        section = config.get("AUTO_POLISH", {}) or {}
        if isinstance(section, dict):
            settings["enabled"] = bool(section.get("enabled", DEFAULTS["enabled"]))
            settings["hour"] = int(section.get("hour", DEFAULTS["hour"]))
            settings["minute"] = int(section.get("minute", DEFAULTS["minute"]))
    except Exception:
        pass
    return settings


def _append_log(entry: Dict[str, Any]) -> None:
    with _lock:
        logs = _load_json(LOG_FILE, [])
        logs.append(entry)
        _save_json(LOG_FILE, logs[-300:])


def _update_state(**kv) -> None:
    with _lock:
        state = _load_json(STATE_FILE, {})
        state.update(kv)
        _save_json(STATE_FILE, state)


def get_status() -> Dict[str, Any]:
    state = _load_json(STATE_FILE, {})
    logs = _load_json(LOG_FILE, [])
    today = date.today().isoformat()
    return {
        "settings": get_settings(),
        "running": _running.is_set(),
        "last_run": state.get("last_run", ""),
        "ran_today": state.get("last_run_date", "") == today,
        "last_summary": state.get("last_summary", {}),
        "recent_logs": logs[-20:],
    }


# ---------------------------------------------------------------- 擦亮核心

def _extract_token(cookie_str: str) -> str:
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        if name.strip() == TOKEN_COOKIE_NAME:
            value = value.strip()
            return value.split("_")[0] if "_" in value else value
    return ""


def _merge_response_cookies(resp: requests.Response, cookie_str: str) -> str:
    """合并响应 set-cookie（令牌过期时服务端会下发新 _m_h5_tk），返回新 Cookie 串。"""
    try:
        new_cookies = {}
        for cookie in resp.headers.get("set-cookie", "").split(","):
            # set-cookie 可能是多条合并，按分号取首段 name=value
            first = cookie.split(";")[0]
            if "=" in first:
                name, _, value = first.partition("=")
                new_cookies[name.strip()] = value.strip()
        if not new_cookies:
            return cookie_str
        existing = {}
        for part in (cookie_str or "").split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                existing[name.strip()] = value.strip()
        existing.update(new_cookies)
        return "; ".join(f"{k}={v}" for k, v in existing.items())
    except Exception:
        return cookie_str


def _persist_cookie(cookie_id: str, new_cookie_str: str) -> None:
    """把刷新后的 Cookie 回写 cookie_manager + 数据库（沿用平台自身的更新通道）。"""
    try:
        import cookie_manager as cm
        if getattr(cm, "manager", None) is not None:
            cm.manager.update_cookie(cookie_id, new_cookie_str, save_to_db=True)
    except Exception:
        pass


def _is_risk_failure(message: Any) -> tuple:
    """这条失败是不是**平台风控**，而不是"某个商品擦不了"。

    只认 `mtop_response.RISK_MARKERS` 那张表（RGV587 / USER_VALIDATE / PUNISH /
    哎哟喂 / 被挤爆 / 风控）。分不清的就不熔断：网络超时、非 JSON 响应、
    `IDLEITEM_NOT_FOUND` 这类重试有意义，把整店停摆反而是更大的损失。

    返回 `(是否风控, 原文前 120 字)`；`mtop_response` 读不到时按"不是风控"处理。
    """
    text = str(message or "").strip()
    if not text:
        return False, ""
    try:
        import mtop_response
        return bool(mtop_response.has_marker(text, mtop_response.RISK_MARKERS)), text[:120]
    except Exception:
        return False, text[:120]


def polish_item(cookie_str: str, item_id: str, app_key: str,
                retry_count: int = 0) -> Dict[str, Any]:
    """
    擦亮单个商品（上游同款：TOKEN_EXPIRED 用响应新 Cookie 重试，最多 3 次）。
    返回 {"success": bool, "message": str, "cookie_str": str}
    """
    try:
        token = _extract_token(cookie_str)
        if not token:
            return {"success": False, "message": f"Cookie中没有找到{TOKEN_COOKIE_NAME}",
                    "cookie_str": cookie_str}

        t = str(int(time.time() * 1000))
        params = {
            'jsv': '2.7.2',
            'appKey': app_key,
            't': t,
            'sign': '',
            'v': '1.0',
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'api': 'mtop.taobao.idle.item.polish',
            'sessionOption': 'AutoLoginOnly',
        }
        data_val = json.dumps({"itemId": str(item_id)}, separators=(',', ':'))
        params['sign'] = hashlib.md5(
            f"{token}&{t}&{app_key}&{data_val}".encode()).hexdigest()

        headers = dict(POLISH_HEADERS)
        headers['cookie'] = cookie_str.strip()

        try:
            guard_admit(cookie_str, scope="polish", write=True, sleep=True)
        except PlatformGuardBlocked as exc:
            return {"success": False, "message": str(exc), "cookie_str": cookie_str}
        resp = requests.post(POLISH_URL, params=params, data={'data': data_val},
                             headers=headers, timeout=30)
        guard_observe(
            cookie_str,
            text=str(getattr(resp, "text", "") or ""),
            status_code=int(getattr(resp, "status_code", 0) or 0),
        )
        try:
            result = resp.json()
        except Exception:
            return {"success": False, "message": f"响应非JSON: {resp.text[:120]}",
                    "cookie_str": cookie_str}

        new_cookie_str = _merge_response_cookies(resp, cookie_str)

        ret = result.get('ret') or []
        ret_msg = ret[0] if ret else '未知错误'
        if ret and ret[0] == 'SUCCESS::调用成功':
            return {"success": True, "message": "擦亮成功", "cookie_str": new_cookie_str}
        if ('宝贝已经擦亮过了' in ret_msg or 'IDLEITEM_POLISH_AGAIN' in ret_msg
                or 'POLISH_DUPLICATE' in ret_msg):
            return {"success": True, "message": "今天已经擦亮过了", "cookie_str": new_cookie_str}

        # 令牌过期：用新 Cookie 重试（上游拼写 TOKEN_EXOIRED 原样兼容）
        if any(('TOKEN_EXOIRED' in str(r)) or ('TOKEN_EXPIRED' in str(r)) for r in ret):
            if retry_count < MAX_TOKEN_RETRY - 1:
                time.sleep(0.5)
                return polish_item(new_cookie_str, item_id, app_key, retry_count + 1)

        return {"success": False, "message": str(ret_msg), "cookie_str": new_cookie_str}
    except Exception as e:
        return {"success": False, "message": f"擦亮异常: {e}", "cookie_str": cookie_str}


# ---------------------------------------------------------------- 全量批次

def _resolve_cookies() -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    try:
        import cookie_manager as cm
        if getattr(cm, "manager", None) is not None:
            cookies = dict(getattr(cm.manager, "cookies", {}) or {})
    except Exception:
        cookies = {}
    if not cookies:
        try:
            from db_manager import db_manager
            cookies = db_manager.get_all_cookies() or {}
        except Exception:
            cookies = {}
    return cookies


def run_polish_batch(cookie_id: Optional[str] = None) -> Dict[str, Any]:
    """
    全量擦亮（或指定账号）：逐商品随机间隔 8–15s，账号间暂停 30s。
    运行期间置 _running，手动与定时互斥。返回批次摘要。
    """
    if _running.is_set():
        return {"ok": False, "error": "已有擦亮任务在运行中，请稍后再试"}
    _running.set()
    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary = {"batch_id": batch_id, "started": datetime.now().isoformat(),
               "accounts": {}, "total_success": 0, "total_fail": 0}
    try:
        # 商品类网页 MTOP 接口签名必须用 Web APP_KEY（IM Key 会报参数非法）
        try:
            from opportunity_publisher import WEB_APP_KEY as app_key
        except Exception:
            app_key = "34839810"

        all_cookies = _resolve_cookies()
        if cookie_id:
            all_cookies = {cookie_id: all_cookies.get(cookie_id, "")} if cookie_id in all_cookies else {}
        if not all_cookies:
            summary["error"] = "没有可用账号（请先扫码登录）"
            return summary

        from db_manager import db_manager
        first_account = True
        for cid, cookie_str in all_cookies.items():
            if not cookie_str:
                continue
            if not first_account:
                time.sleep(30)  # 账号间暂停防风控
            first_account = False

            items = db_manager.get_items_by_cookie(cid) or []
            if not items:
                summary["accounts"][cid] = {"items": 0, "success": 0, "fail": 0,
                                            "note": "无在售商品（先同步商品）"}
                continue

            ok_n, fail_n = 0, 0
            stop_note = ""
            risk_flag = False
            for it in items:
                item_id = str(it.get("item_id") or "").strip()
                if not item_id:
                    continue
                # A2 统一限流：单账号擦亮频次 + 日上限（200）都从这里取
                ok_quota, retry_in = rate_limiter.acquire("polish", cid)
                if not ok_quota:
                    # 记在 stop_note 里，循环后统一落到账户摘要——以前这里先写一次
                    # summary、出了循环又被覆盖，"触发限流"这句提示其实从来没显示过。
                    stop_note = f"触发限流（{retry_in}s 后恢复），本轮中止"
                    break
                result = polish_item(cookie_str, item_id, app_key)
                # Cookie 刷新则回写并沿用
                if result.get("cookie_str") and result["cookie_str"] != cookie_str:
                    cookie_str = result["cookie_str"]
                    _persist_cookie(cid, cookie_str)
                if result.get("success"):
                    ok_n += 1
                else:
                    fail_n += 1
                _append_log({"batch": batch_id, "time": datetime.now().isoformat(),
                             "cookie_id": cid, "item_id": item_id,
                             "success": bool(result.get("success")),
                             "message": result.get("message", "")[:200]})
                # 风控观测统一出口：单条就撞墙，就别再把剩下的商品挨个试一遍。
                # 熔断落盘后，这个账号的发布 / 发货 / 擦亮都会一起停下（拆解 3）。
                if not result.get("success"):
                    is_risk, risk_text = _is_risk_failure(result.get("message"))
                    if is_risk:
                        risk_flag = True
                        stop_note = "收到平台风控信号，已熔断该账号并中止本轮"
                        secs = 0
                        try:
                            secs = rate_limiter.trip_circuit(
                                cid, reason=f"擦亮触发风控：{risk_text}")
                        except Exception:
                            pass
                        try:
                            from notify_service import notify_risk_trip
                            notify_risk_trip(cid, "擦亮", risk_text, secs)
                        except Exception:
                            pass
                        break
                rate_limiter.jitter(8, 15)  # 逐商品随机间隔（8–15s）

            summary["accounts"][cid] = {"items": len(items), "success": ok_n, "fail": fail_n}
            if stop_note:
                summary["accounts"][cid]["note"] = stop_note
            if risk_flag:
                summary["accounts"][cid]["risk"] = True
            summary["total_success"] += ok_n
            summary["total_fail"] += fail_n

            # 风控信号（兜底）：单账号失败率过半则记录告警（不中断其他账号）。
            # 拿不到明确的平台风控文本时，只能靠这条粗口径提示人工看一眼。
            if not stop_note and items and fail_n > len(items) * 0.5:
                summary["accounts"][cid]["warning"] = "失败率超50%，疑似风控/登录失效，请检查该账号"

        summary["finished"] = datetime.now().isoformat()
        _update_state(last_run=datetime.now().isoformat(),
                      last_run_date=date.today().isoformat(),
                      last_summary=summary)
        # 通知推送（用户"只看"模式：批次结果直接推到手机/群里）
        try:
            from notify_service import notify_polish_summary
            notify_polish_summary(summary)
        except Exception:
            pass
        return summary
    finally:
        _running.clear()


def run_polish_batch_async(cookie_id: Optional[str] = None) -> str:
    """后台线程跑批次，立即返回批次号（供 API 手动触发）。"""
    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    threading.Thread(target=run_polish_batch, args=(cookie_id,),
                     daemon=True, name=f"polish-{batch_id}").start()
    return batch_id


# ---------------------------------------------------------------- 每日调度

def _scheduler_loop() -> None:
    import task_watchdog as _wd
    _wd.register("polish-scheduler", 660, "每日擦亮调度（每 10 分钟检查一次）")
    while True:
        try:
            settings = get_settings()
            now = datetime.now()
            state = _load_json(STATE_FILE, {})
            ran_today = state.get("last_run_date", "") == date.today().isoformat()
            due = (now.hour == settings["hour"] and now.minute >= settings["minute"])
            if settings["enabled"] and due and not ran_today and not _running.is_set():
                run_polish_batch()
        except Exception:
            pass
        _wd.beat("polish-scheduler")
        time.sleep(600)  # 每 10 分钟检查一次


def start_scheduler() -> None:
    """启动每日擦亮守护线程（幂等）。"""
    global _scheduler_started
    with _lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    threading.Thread(target=_scheduler_loop, daemon=True,
                     name="polish-scheduler").start()


__all__ = ["polish_item", "run_polish_batch", "run_polish_batch_async",
           "get_status", "get_settings", "start_scheduler"]
