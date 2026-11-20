import asyncio
import base64
import json
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

import delivery_reliability as ledger
import order_reconcile
import seller_api
from sqlite_runtime import connect_sqlite


@pytest.fixture
def order_feed(tmp_path, monkeypatch):
    import db_manager

    database = db_manager.DBManager(str(tmp_path / "orders.db"))
    monkeypatch.setattr(db_manager, "db_manager", database)
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "delivery.db")
    items = []
    monkeypatch.setattr(
        seller_api, "fetch_sold_order_pages",
        lambda *args, **kwargs: {
            "items": items, "pages_read": 1, "failed_pages": [], "has_more": False,
        },
    )
    try:
        assert database.save_cookie("shop", "unb=1", 1)
        assert database.save_cookie("other", "unb=2", 1)
        yield items
    finally:
        database.conn.close()


def reconcile(items, status_text, *, in_refund=False, cookie_id="shop", register=True):
    items[:] = [{
        "commonData": {
            "orderId": "123", "orderStatus": status_text, "inRefund": in_refund,
        },
    }]
    return order_reconcile.reconcile(
        "", cookie_id, register=register, notify_missing=False,
    )


@pytest.mark.parametrize("send_first", [True, False])
def test_platform_completion_reconciles_in_either_order(order_feed, send_first):
    if send_first:
        ledger.ensure_order("123", "shop", ["test card"])
        assert ledger.mark_sent("123", "shop", 0)
        assert not ledger.summary("123", "shop")["delivery_reconciled"]

    assert reconcile(order_feed, "交易成功")["ok"]
    first = ledger.summary("123", "shop")
    assert first["platform_order_completion"]["source"] == "sold.get"
    assert first["platform_order_completion"]["observed_at"]
    assert first["delivery_reconciled"] is send_first
    assert first["platform_receipt_verified"] is False

    if not send_first:
        ledger.ensure_order("123", "shop", ["test card"])
        assert ledger.mark_sent("123", "shop", 0)
    assert reconcile(order_feed, "交易成功")["ok"]
    summary = ledger.summary("123", "shop")
    assert summary["platform_order_completion"] == first["platform_order_completion"]
    assert summary["delivery_reconciled"]
    assert summary["by_status"] == {"sent": 1}
    context = ledger.locate_context("123", "shop")
    assert context["delivery_reconciled"]
    assert context["platform_order_completion"] == summary["platform_order_completion"]
    with closing(connect_sqlite(ledger.DB_PATH)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_platform_completion_preserves_unknown_and_account_boundary(order_feed):
    ledger.ensure_order("123", "shop", ["test card"])
    assert ledger.mark_unknown("123", "shop", 0, "write outcome unknown")
    assert reconcile(order_feed, "待买家确认收货")["ok"]
    assert reconcile(order_feed, "交易成功", register=False)["ok"]
    assert reconcile(order_feed, "交易成功", cookie_id="other")["failed"] == 1
    assert reconcile(order_feed, "交易成功", in_refund=True)["ok"]
    assert ledger.summary("123", "shop")["platform_order_completion"] is None
    assert reconcile(order_feed, "交易成功")["ok"]
    summary = ledger.summary("123", "shop")
    assert summary["platform_order_completion"]
    assert not summary["delivery_reconciled"]
    assert summary["by_status"] == {"unknown": 1}
    assert ledger.summary("123", "other")["platform_order_completion"] is None


@pytest.mark.parametrize(("status_text", "in_refund"), [
    ("退款成功", False),
    ("交易关闭", False),
    ("交易成功", True),
])
def test_platform_refund_or_closure_clears_completion(order_feed, status_text, in_refund):
    ledger.ensure_order("123", "shop", ["test card"])
    assert ledger.mark_sent("123", "shop", 0)
    assert reconcile(order_feed, "交易成功")["ok"]
    assert ledger.summary("123", "shop")["delivery_reconciled"]
    assert reconcile(order_feed, status_text, in_refund=in_refund)["ok"]
    summary = ledger.summary("123", "shop")
    assert summary["platform_order_completion"] is None
    assert not summary["delivery_reconciled"]
    assert summary["by_status"] == {"sent": 1}


def test_older_poll_cannot_restore_completion_after_refund(order_feed, monkeypatch):
    fetch = seller_api.fetch_sold_order_pages

    def finish_older_poll(*args, **kwargs):
        result = fetch(*args, **kwargs)
        result["items"] = list(result["items"])
        monkeypatch.setattr(seller_api, "fetch_sold_order_pages", fetch)
        assert reconcile(order_feed, "退款成功")["ok"]
        return result

    monkeypatch.setattr(seller_api, "fetch_sold_order_pages", finish_older_poll)
    assert reconcile(order_feed, "交易成功")["ok"]
    assert ledger.summary("123", "shop")["platform_order_completion"] is None


def test_websocket_acknowledges_once_and_processes_subsequent_sync_entries(monkeypatch):
    import cookie_manager
    from XianyuAutoAsync import XianyuLive

    monkeypatch.setattr(cookie_manager, "manager", None)
    live = XianyuLive.__new__(XianyuLive)
    live.cookie_id = "shop"
    socket = AsyncMock()
    processed = []

    def record_message(message):
        processed.append(message)
        return None

    monkeypatch.setattr(live, "_extract_order_id", record_message)
    asyncio.run(live.handle_message({
        "headers": {"mid": "sync-message"},
        "body": {"syncPushPackage": {"data": [
            0,
            {"data": base64.b64encode(json.dumps({}).encode()).decode()},
            {"data": base64.b64encode(json.dumps({"3": {}}).encode()).decode()},
        ]}},
    }, socket))

    assert processed == [{}, {"3": {}}]
    socket.send.assert_awaited_once()
    assert json.loads(socket.send.await_args.args[0])["headers"]["mid"] == "sync-message"
