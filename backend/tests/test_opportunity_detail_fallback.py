"""详情接口风控时的需求信号降级测试。"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import opportunity_detail
import opportunity_guard
import opportunity_publisher
import rate_limiter


COOKIE = f"{opportunity_publisher.TOKEN_COOKIE_NAME}=token_123"


def test_detail_risk_response_has_explicit_error_kind(monkeypatch):
    class Response:
        headers = {}

    monkeypatch.setattr(
        opportunity_detail,
        "_mtop_request",
        lambda *args, **kwargs: (
            {"ret": ["FAIL_SYS_USER_VALIDATE::需要验证"]},
            Response(),
        ),
    )

    result = opportunity_detail.fetch_item_detail(COOKIE, "123", "app")

    assert result["ok"] is False
    assert result["error_kind"] == "risk"


def test_enrich_stops_after_first_risk_and_keeps_search_label(monkeypatch):
    items = [
        {
            "itemId": "1",
            "title": "搜索卡片带真实想要数",
            "want_count": 1087,
            "want_source": "search",
            "want_stale": False,
        },
        {
            "itemId": "2",
            "title": "缺数据商品 A",
            "want_count": 0,
            "want_source": None,
            "want_stale": True,
        },
        {
            "itemId": "3",
            "title": "缺数据商品 B",
            "want_count": None,
            "want_source": None,
            "want_stale": True,
        },
    ]
    calls = []

    def risk_detail(cookie_str, item_id, app_key, cookie_id=""):
        calls.append(item_id)
        return {
            "ok": False,
            "error": "RGV587_ERROR::触发平台风控",
            "error_kind": "risk",
            "cookie_str": cookie_str,
        }

    monkeypatch.setattr(opportunity_detail, "fetch_item_detail", risk_detail)
    monkeypatch.setattr(rate_limiter, "acquire", lambda *args, **kwargs: (True, 0))
    monkeypatch.setattr(rate_limiter, "jitter", lambda *args, **kwargs: None)
    monkeypatch.setattr(opportunity_guard, "check_allowed", lambda: (True, ""))
    recorded = []
    monkeypatch.setattr(
        opportunity_guard,
        "record_failure",
        lambda text: recorded.append(text) or {"risk_signal": "RGV587"},
    )

    stats = opportunity_detail.enrich_items(
        items,
        COOKIE,
        "app",
        limit=3,
    )

    assert calls == ["1"]
    assert stats["requested"] == 1
    assert stats["failed"] == 1
    assert stats["stopped_by_risk"] is True
    assert stats["search_fallback"] == 1
    assert recorded and "RGV587" in recorded[0]

    assert items[0]["want_count"] == 1087
    assert items[0]["want_source"] == "search"
    assert items[1]["want_count"] is None
    assert items[1]["want_source"] == "unknown"
    assert items[2]["want_count"] is None
    assert items[2]["want_source"] == "unknown"
