"""Passport 会话静默续期。

请求契约唯一来源：`utils/refresh_util.py` 的 `XianyuApis.hasLogin()`。
不猜测参数名、Cookie 名或返回结构。

安全底线：
1. 续期成功不等于登录态可用，必须再做一次只读订单查询验真；
2. 验真通过后才原子更新数据库里的 Cookie；
3. 失败、验证码、风控一律保留原会话并告警，绝不静默覆盖。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Callable, Dict, Optional

import requests
from loguru import logger

RENEW_URL = "https://passport.goofish.com/newlogin/silentHasLogin.do"
RENEW_PARAMS = {
    "documentReferer": "https%3A%2F%2Fwww.goofish.com%2F",
    "appEntrance": "xianyu_sdkSilent",
    "appName": "xianyu",
    "fromSite": "0",
    "ltl": "true",
}
EXPIRY_COOKIE_NAMES = ("sdkSilent", "havana_lgc_exp", "cookie3_bak_exp")
DEFAULT_INTERVAL_HOURS = 4
DEFAULT_LEAD_SECONDS = 6 * 3600
DEFAULT_TIMEOUT_SECONDS = 20


def parse_cookie_map(cookie: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for part in str(cookie or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        result[name.strip()] = value.strip()
    return result


def build_silent_payload(cookie_map: Dict[str, str]):
    """严格对齐 hasLogin() 的 params 与 data。"""
    params = dict(RENEW_PARAMS)
    data = {
        "hid": cookie_map.get("unb", ""),
        "ltl": "true",
        "appName": "xianyu",
        "appEntrance": "web",
        "_csrf_token": cookie_map.get("XSRF-TOKEN", ""),
        "umidToken": "",
        "hsiz": cookie_map.get("cookie2", ""),
        "mainPage": "false",
        "isMobile": "false",
        "lang": "zh_CN",
        "returnUrl": "",
        "fromSite": "77",
        "isIframe": "true",
        "documentReferer": "https://www.goofish.com/",
        "defaultView": "hasLogin",
        "umidTag": "SERVER",
        "deviceId": cookie_map.get("cna", ""),
    }
    return params, data


def _parse_expiry_epoch(raw: Optional[str]) -> Optional[int]:
    text = str(raw or "").strip()
    if not text or text.lower() in ("0", "-1", "null", "none"):
        return None
    if not text.isdigit():
        return None
    value = int(text)
    if value <= 0:
        return None
    if value >= 10**12:
        value //= 1000
    return value


def cookie_expiry_status(cookie: str, *, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """按 Cookie 名读取原始过期属性。

    只有能解析成 10 位 Unix 秒或 13 位 Unix 毫秒时才给出 expired；
    其他值一律标记 parseable=False / expired=None，不猜解码方式。
    """
    current = time.time() if now is None else float(now)
    jar = parse_cookie_map(cookie)
    result: Dict[str, Dict[str, Any]] = {}
    for name in EXPIRY_COOKIE_NAMES:
        present = name in jar
        raw = jar.get(name, "")
        epoch = _parse_expiry_epoch(raw) if present else None
        result[name] = {
            "present": present,
            "parseable": epoch is not None,
            "expiry_epoch": epoch,
            "expired": (epoch <= current) if epoch is not None else None,
        }
    return result


def _merge_set_cookies(response: Any, cookie: str) -> str:
    try:
        headers = getattr(response, "headers", {}) or {}
        items = []
        if hasattr(headers, "getlist"):
            items = headers.getlist("set-cookie") or []
        elif headers.get("set-cookie"):
            raw_headers = headers.get("set-cookie")
            if isinstance(raw_headers, str):
                items = re.split(r",(?=\s*[^;=,\s]+=)", raw_headers)
            else:
                items = list(raw_headers)
    except Exception:
        return cookie
    if not items:
        return cookie

    jar = parse_cookie_map(cookie)
    changed = False
    for raw in items:
        pair = str(raw).split(";", 1)[0].strip()
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name = name.strip()
        if not name:
            continue
        jar[name] = value.strip()
        changed = True
    if not changed:
        return cookie
    return "; ".join(f"{name}={value}" for name, value in jar.items())


def _default_post(params, data, cookie_map, timeout):
    session = requests.Session()
    session.cookies.update(cookie_map)
    return session.post(RENEW_URL, params=params, data=data, timeout=timeout)


def _verify_order_access(cookie: str) -> bool:
    """只读订单查询验真。能拿到 items 字段才算登录态可用。"""
    import seller_api

    result = seller_api.get_sold_orders(cookie, page_number=1, rows_per_page=1)
    return isinstance(result, dict) and "items" in result


def _default_alert(title: str, content: str) -> None:
    logger.warning("{}: {}", title, content)
    try:
        import notify_service

        notify_service.send_event("account", title, content, level="warn")
    except Exception as exc:
        logger.warning("会话续期告警通知失败，已跳过: {}", type(exc).__name__)


class SessionRenewal:
    def __init__(
        self,
        database=None,
        *,
        post: Optional[Callable] = None,
        verify: Optional[Callable[[str], bool]] = None,
        alert: Optional[Callable[[str, str], None]] = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        interval_hours: int = DEFAULT_INTERVAL_HOURS,
        lead_seconds: int = DEFAULT_LEAD_SECONDS,
    ):
        self.database = database
        self.post = post or _default_post
        self.verify = verify or _verify_order_access
        self.alert = alert or _default_alert
        self.timeout = max(1, int(timeout))
        self.interval_hours = max(1, int(interval_hours))
        self.lead_seconds = max(0, int(lead_seconds))

    def renew(
        self, cookie_id: str, cookie: str, *, now: Optional[float] = None
    ) -> Dict[str, Any]:
        cookie_id = str(cookie_id or "")
        original = str(cookie or "")
        markers = cookie_expiry_status(original, now=now)
        params, data = build_silent_payload(parse_cookie_map(original))

        try:
            response = self.post(
                params, data, parse_cookie_map(original), self.timeout
            )
        except Exception as exc:
            message = f"续期请求异常: {type(exc).__name__}"
            self.alert("会话静默续期失败", f"账号 {cookie_id} {message}，保留原会话")
            return self._result(False, cookie_id, original, original, message, markers)

        status_code = int(getattr(response, "status_code", 0) or 0)
        try:
            raw = response.json()
        except Exception:
            message = f"续期响应非 JSON（HTTP {status_code}）"
            self.alert("会话静默续期失败", f"账号 {cookie_id} {message}，保留原会话")
            return self._result(False, cookie_id, original, original, message, markers)

        body_text = json.dumps(raw, ensure_ascii=False) if isinstance(raw, dict) else str(raw)
        risk = False
        try:
            from mtop_response import RISK_MARKERS, has_marker

            risk = has_marker(body_text, RISK_MARKERS)
        except Exception:
            risk = False

        success = (
            isinstance(raw, dict)
            and isinstance(raw.get("content"), dict)
            and raw["content"].get("success") is True
        )
        if not success or status_code == 429 or risk:
            reason = body_text[:200]
            self.alert(
                "会话静默续期未通过",
                f"账号 {cookie_id} 续期失败或触发验证，保留原会话: {reason}",
            )
            return self._result(False, cookie_id, original, original, reason, markers)

        renewed_cookie = _merge_set_cookies(response, original)
        if not self.verify(renewed_cookie):
            message = "续期后只读订单验真失败"
            self.alert(
                "会话静默续期验真失败",
                f"账号 {cookie_id} {message}，保留原会话",
            )
            return self._result(False, cookie_id, original, original, message, markers)

        if renewed_cookie == original:
            return self._result(
                True, cookie_id, original, original, "续期成功，未下发新 Cookie", markers
            )

        if not self._persist(cookie_id, renewed_cookie):
            message = "Cookie 原子回写失败"
            self.alert(
                "会话静默续期回写失败",
                f"账号 {cookie_id} {message}，保留原会话",
            )
            return self._result(False, cookie_id, original, original, message, markers)

        return self._result(
            True, cookie_id, original, renewed_cookie, "", markers, renewed=True
        )

    def _persist(self, cookie_id: str, new_cookie: str) -> bool:
        if self.database is None:
            return False
        updater = getattr(self.database, "update_cookie_value", None)
        if not callable(updater):
            return False
        try:
            updated = bool(updater(cookie_id, new_cookie))
        except Exception as exc:
            logger.warning("会话续期 Cookie 写库异常（{}）", type(exc).__name__)
            return False
        if not updated:
            return False
        try:
            import cookie_manager as cm

            manager = getattr(cm, "manager", None)
            if manager is not None:
                manager.cookies[cookie_id] = new_cookie
        except Exception as exc:
            logger.warning("会话续期运行时 Cookie 更新失败（{}）", type(exc).__name__)
        return True

    @staticmethod
    def _result(
        ok: bool,
        cookie_id: str,
        original: str,
        cookie: str,
        error: str,
        markers: Dict[str, Dict[str, Any]],
        *,
        renewed: bool = False,
    ) -> Dict[str, Any]:
        return {
            "ok": bool(ok),
            "cookie_id": cookie_id,
            "renewed": bool(renewed),
            "cookie": cookie if ok else original,
            "error": str(error or ""),
            "markers": markers,
        }

    def scan_once(self, *, now: Optional[float] = None) -> Dict[str, int]:
        summary = {"scanned": 0, "renewed": 0, "failed": 0, "skipped": 0}
        if self.database is None:
            return summary
        cookies = self.database.get_all_cookies() or {}
        statuses = self.database.get_all_cookie_status() or {}
        for cookie_id, cookie in cookies.items():
            if statuses.get(cookie_id, True) is False:
                summary["skipped"] += 1
                continue
            summary["scanned"] += 1
            result = self.renew(cookie_id, cookie, now=now)
            if result.get("ok") is True:
                summary["renewed"] += 1
            else:
                summary["failed"] += 1
        return summary


async def run_scheduler(database, stop):
    from config import config

    settings = config.get("SESSION_RENEWAL") or {}
    if settings.get("enabled") is not True:
        return
    try:
        interval_hours = max(1, int(settings.get("interval_hours", DEFAULT_INTERVAL_HOURS)))
        lead_seconds = max(0, int(settings.get("lead_seconds", DEFAULT_LEAD_SECONDS)))
        timeout = max(1, int(settings.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)))
    except (TypeError, ValueError):
        logger.error("会话静默续期配置无效")
        return

    scheduler = SessionRenewal(
        database,
        timeout=timeout,
        interval_hours=interval_hours,
        lead_seconds=lead_seconds,
    )
    interval_seconds = interval_hours * 3600
    while not stop.is_set():
        try:
            await asyncio.to_thread(scheduler.scan_once)
        except Exception as exc:
            logger.warning("会话静默续期扫描中断（{}）", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), interval_seconds)
        except asyncio.TimeoutError:
            continue


__all__ = [
    "SessionRenewal",
    "RENEW_URL",
    "RENEW_PARAMS",
    "EXPIRY_COOKIE_NAMES",
    "DEFAULT_INTERVAL_HOURS",
    "build_silent_payload",
    "cookie_expiry_status",
    "parse_cookie_map",
    "run_scheduler",
]
