"""Passport 静默续期与 Cookie 原子回写的回归测试。"""

import json
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

import session_renewal as renewal


class FakeResponse:
    def __init__(self, payload, set_cookie=None, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}
        if set_cookie:
            self.headers["set-cookie"] = set_cookie

    def json(self):
        return self._payload


class FakeDatabase:
    def __init__(self, cookies=None, statuses=None):
        self.cookies = cookies if cookies is not None else {}
        self.statuses = statuses if statuses is not None else {}
        self.updates = []

    def get_all_cookies(self):
        return dict(self.cookies)

    def get_all_cookie_status(self):
        return dict(self.statuses)

    def update_cookie_value(self, cookie_id, cookie_value):
        self.updates.append((cookie_id, cookie_value))
        if cookie_id in self.cookies:
            self.cookies[cookie_id] = cookie_value
            return True
        return False


def test_build_silent_payload_matches_verified_request_contract():
    cookie_map = {
        "unb": "2219399967630",
        "XSRF-TOKEN": "csrf-value",
        "cookie2": "cookie2-value",
        "cna": "device-value",
    }

    params, data = renewal.build_silent_payload(cookie_map)

    assert params == {
        "documentReferer": "https%3A%2F%2Fwww.goofish.com%2F",
        "appEntrance": "xianyu_sdkSilent",
        "appName": "xianyu",
        "fromSite": "0",
        "ltl": "true",
    }
    assert data == {
        "hid": "2219399967630",
        "ltl": "true",
        "appName": "xianyu",
        "appEntrance": "web",
        "_csrf_token": "csrf-value",
        "umidToken": "",
        "hsiz": "cookie2-value",
        "mainPage": "false",
        "isMobile": "false",
        "lang": "zh_CN",
        "returnUrl": "",
        "fromSite": "77",
        "isIframe": "true",
        "documentReferer": "https://www.goofish.com/",
        "defaultView": "hasLogin",
        "umidTag": "SERVER",
        "deviceId": "device-value",
    }


def test_expiry_markers_parse_seconds_and_milliseconds():
    status = renewal.cookie_expiry_status(
        "sdkSilent=not-a-timestamp; "
        "havana_lgc_exp=1700000000; "
        "cookie3_bak_exp=1700000000000",
        now=1700000100,
    )

    assert status["havana_lgc_exp"]["present"] is True
    assert status["havana_lgc_exp"]["expiry_epoch"] == 1700000000
    assert status["havana_lgc_exp"]["expired"] is True
    assert status["cookie3_bak_exp"]["expiry_epoch"] == 1700000000
    assert status["cookie3_bak_exp"]["expired"] is True
    assert status["sdkSilent"]["parseable"] is False
    assert status["sdkSilent"]["expired"] is None


def test_successful_renewal_updates_cookie_only_after_order_verification(monkeypatch):
    database = FakeDatabase(cookies={"acc1": "unb=1; cookie2=c2"})
    captured = {}

    def post(params, data, cookie_map, timeout):
        captured["params"] = params
        captured["data"] = data
        captured["cookie_map"] = cookie_map
        captured["timeout"] = timeout
        return FakeResponse(
            {"content": {"success": True}},
            set_cookie="cookie2=c2-new; Path=/; HttpOnly",
        )

    verify_calls = []

    def verify(cookie):
        verify_calls.append(cookie)
        return True

    renewer = renewal.SessionRenewal(
        database=database, post=post, verify=verify, timeout=7
    )
    result = renewer.renew("acc1", "unb=1; cookie2=c2")

    assert result["ok"] is True
    assert result["renewed"] is True
    assert "cookie2=c2-new" in result["cookie"]
    assert verify_calls == [result["cookie"]]
    assert database.updates == [("acc1", result["cookie"])]
    assert captured["params"]["appEntrance"] == "xianyu_sdkSilent"
    assert captured["data"]["hsiz"] == "c2"
    assert captured["timeout"] == 7


def test_verification_failure_keeps_original_cookie(tmp_path):
    database = FakeDatabase(cookies={"acc1": "unb=1; cookie2=c2"})

    def post(params, data, cookie_map, timeout):
        return FakeResponse(
            {"content": {"success": True}},
            set_cookie="cookie2=c2-new; Path=/",
        )

    alerts = []
    renewer = renewal.SessionRenewal(
        database=database,
        post=post,
        verify=lambda cookie: False,
        alert=lambda title, content: alerts.append((title, content)),
    )
    result = renewer.renew("acc1", "unb=1; cookie2=c2")

    assert result["ok"] is False
    assert result["cookie"] == "unb=1; cookie2=c2"
    assert database.updates == []
    assert alerts


def test_captcha_or_risk_response_never_overwrites_cookie():
    database = FakeDatabase(cookies={"acc1": "unb=1; cookie2=c2"})

    def post(params, data, cookie_map, timeout):
        return FakeResponse(
            {"content": {"success": False}, "ret": ["RGV587_ERROR::请完成滑块验证"]},
            set_cookie="cookie2=should-not-be-saved; Path=/",
        )

    alerts = []
    renewer = renewal.SessionRenewal(
        database=database,
        post=post,
        verify=lambda cookie: True,
        alert=lambda title, content: alerts.append((title, content)),
    )
    result = renewer.renew("acc1", "unb=1; cookie2=c2")

    assert result["ok"] is False
    assert "RGV587" in result["error"]
    assert database.updates == []
    assert alerts


def test_scheduler_only_scans_enabled_accounts(monkeypatch):
    database = FakeDatabase(
        cookies={"acc1": "unb=1", "acc2": "unb=2"},
        statuses={"acc1": True, "acc2": False},
    )
    renewer = renewal.SessionRenewal(database=database, post=lambda *a: None)
    called = []

    def fake_renew(cookie_id, cookie, now=None):
        called.append(cookie_id)
        return {"ok": True, "renewed": True, "cookie_id": cookie_id}

    renewer.renew = fake_renew
    summary = renewer.scan_once(now=1000.0)

    assert called == ["acc1"]
    assert summary["scanned"] == 1
    assert summary["renewed"] == 1


def test_default_verify_uses_read_only_sold_orders(monkeypatch):
    import seller_api

    calls = {}

    def fake_sold_orders(cookie, **kwargs):
        calls["cookie"] = cookie
        calls["kwargs"] = kwargs
        return {"items": [], "total_count": 0, "next_page": False}

    monkeypatch.setattr(seller_api, "get_sold_orders", fake_sold_orders)

    assert renewal._verify_order_access("unb=1; cookie2=c2") is True
    assert calls["kwargs"]["page_number"] == 1
    assert calls["kwargs"]["rows_per_page"] == 1


def test_atomic_cookie_update_preserves_other_columns_and_wal(tmp_path):
    from db_manager import DBManager

    database = DBManager(str(tmp_path / "renewal.db"))
    try:
        assert database.save_cookie("acc1", "unb=1; cookie2=old")
        assert database.update_cookie_remark("acc1", "keep-me")

        assert database.update_cookie_value("acc1", "unb=1; cookie2=new") is True
        detail = database.get_cookie_details("acc1")

        assert detail["value"] == "unb=1; cookie2=new"
        assert detail["remark"] == "keep-me"
        mode = database.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
    finally:
        database.conn.close()


def test_reply_server_and_example_config_wire_four_hour_renewal():
    server_source = (BACKEND / "reply_server.py").read_text(encoding="utf-8")
    example = (BACKEND.parent / "global_config.example.yml").read_text(encoding="utf-8")

    assert "session_renewal" in server_source
    assert 'config.get("SESSION_RENEWAL.enabled")' in server_source
    assert "SESSION_RENEWAL:" in example
    assert "interval_hours: 4" in example
    assert renewal.DEFAULT_INTERVAL_HOURS == 4
