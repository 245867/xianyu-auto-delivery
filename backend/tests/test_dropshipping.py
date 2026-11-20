import asyncio
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

import delivery_reliability
import goofish_open
import idempotency
import rate_limiter
import seller_api


@pytest.fixture
def orders(tmp_path, monkeypatch):
    from db_manager import DBManager

    database = DBManager(str(tmp_path / "orders.db"))
    monkeypatch.setattr(delivery_reliability, "DB_PATH", tmp_path / "delivery.db")
    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "idempotency.db")
    monkeypatch.setattr(rate_limiter, "circuit_blocked", lambda cookie_id: (False, 0))
    monkeypatch.setattr(rate_limiter, "gate_write", lambda cookie_id: (True, 0))
    assert database.save_cookie("shop", "unb=1", 1)
    assert database.save_cookie("other", "unb=2", 1)
    assert database.insert_or_update_order(
        "100", item_id="200", buyer_id="buyer", order_status="pending_ship", cookie_id="shop",
        receiver_name="陈女士", receiver_phone="+86 136-9999-8888",
        receiver_address="北京市朝阳区望京SOHO塔1-2003",
    )
    try:
        yield database
    finally:
        database.conn.close()


def test_migrates_existing_orders_and_preserves_wal(orders):
    from db_manager import DBManager

    fields = ("upstream_order_id", "express_no", "express_company", "dropship_status")
    with orders.conn:
        for field in fields:
            orders.conn.execute(f"ALTER TABLE orders DROP COLUMN {field}")
    for _ in range(2):
        migrated = DBManager(orders.db_path)
        try:
            row = migrated.get_order_by_id("100")
            assert row["cookie_id"] == "shop"
            assert all(row[field] == "" for field in fields)
            assert migrated.conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        finally:
            migrated.conn.close()


def test_address_and_account_gate_registration(orders):
    from dropshipping import begin_order

    assert begin_order(orders, "100", "other") is None
    assert orders.insert_or_update_order("100", receiver_phone="脱敏号码")
    assert begin_order(orders, "100", "shop") is None
    assert orders.get_order_by_id("100")["dropship_status"] == ""
    assert orders.insert_or_update_order("100", receiver_phone="+86 136-9999-8888")
    address = begin_order(orders, "100", "shop")
    assert (address["name"], address["phone"]) == ("陈女士", "13699998888")
    assert (address["province"], address["district"], address["street"]) == (
        "北京市", "朝阳区", "望京SOHO塔1-2003",
    )
    assert address["full_address"] == "北京市朝阳区望京SOHO塔1-2003"
    assert begin_order(orders, "100", "shop") is None


@pytest.mark.parametrize("placement_first", [True, False])
def test_refund_intercepts_placement_even_when_ack_arrives_late(orders, placement_first):
    from dropshipping import begin_order, record_cancellation, record_placement, record_pickup

    assert begin_order(orders, "100", "shop")
    if placement_first:
        assert record_placement(orders, "100", "shop", "upstream")
    assert orders.insert_or_update_order("100", cookie_id="shop", order_status="refunding")
    if not placement_first:
        assert orders.get_order_by_id("100")["dropship_status"] == "cancelled"
        assert record_placement(orders, "100", "shop", "upstream")
    row = orders.get_order_by_id("100")
    assert (row["dropship_status"], row["upstream_order_id"]) == ("refund_intercepted", "upstream")
    assert orders.pending_dropship_cancellations("shop") == [("100", "upstream")]
    assert orders.pending_dropship_cancellations("other") == []
    assert not orders.insert_or_update_order("100", cookie_id="other", order_status="cancelled")
    assert record_placement(orders, "100", "shop", "upstream")
    assert not record_pickup(orders, "100", "shop", "different-upstream", "YUNDA", "000123")
    assert not record_cancellation(orders, "100", "other", "upstream")
    assert record_cancellation(orders, "100", "shop", "upstream")
    assert not record_pickup(orders, "100", "shop", "upstream", "YUNDA", "000123")
    assert orders.get_order_by_id("100")["dropship_status"] == "cancelled"
    assert orders.pending_dropship_cancellations("shop") == []


def in_transit(orders):
    from dropshipping import begin_order, record_placement, record_pickup

    assert begin_order(orders, "100", "shop")
    assert record_placement(orders, "100", "shop", "upstream")
    assert not record_pickup(orders, "100", "shop", "upstream", "", "000123")
    assert record_pickup(orders, "100", "shop", "upstream", "YUNDA", "000123")
    assert not record_pickup(orders, "100", "shop", "upstream", "YUNDA", "000456")


@pytest.mark.parametrize(("ret", "confirmed"), [
    (["SUCCESS::ok"], True),
    (["FAIL_SUCCESS::unexpected"], False),
    (["SUCCESS::ok", "FAIL_SYS_USER_VALIDATE"], False),
])
def test_logistics_submission_commits_only_on_success(orders, monkeypatch, ret, confirmed):
    from dropshipping import sync_account

    in_transit(orders)
    monkeypatch.setattr(rate_limiter, "trip_circuit", Mock())
    monkeypatch.setattr(seller_api, "fetch_orders_by_ids", lambda *args, **kwargs: {
        "100": {"order_id": "100", "order_status": "pending_ship", "in_refund": False},
    })
    submit = Mock(return_value={
        "api": "mtop.taobao.idle.order.ship", "v": "1.0", "ret": ret,
        "data": {"orderId": "100", "success": True, "isSuccess": "true"},
    })
    monkeypatch.setattr(seller_api, "_call", submit)
    sync_account(orders, "test MTOP cookie", "shop", mtop_ship_version="")
    submit.assert_not_called()
    sync_account(orders, "test MTOP cookie", "other", mtop_ship_version="1.0")
    submit.assert_not_called()
    for _ in range(2):
        sync_account(orders, "test MTOP cookie", "shop", mtop_ship_version="1.0")
    submit.assert_called_once_with(
        "mtop.taobao.idle.order.ship",
        {"orderId": "100", "expressCode": "YUNDA", "expressNo": "000123", "subOrderList": []},
        "test MTOP cookie", version="1.0", timeout=20, retry_expired_token=False,
    )
    order = orders.get_order_by_id("100")
    assert order["order_status"] == ("shipped" if confirmed else "pending_ship")
    assert order["dropship_status"] == "shipped"
    assert bool(delivery_reliability.platform_ship_rows("done", "shop")) is confirmed


def test_physical_order_is_blocked_by_virtual_delivery_entries(orders, monkeypatch):
    import db_manager
    from dropshipping import begin_order
    from XianyuAutoAsync import XianyuLive

    assert begin_order(orders, "100", "shop")
    monkeypatch.setattr(db_manager, "db_manager", orders)
    live = XianyuLive.__new__(XianyuLive)
    live.cookie_id = "shop"
    live._order_locks = defaultdict(asyncio.Lock)
    live.last_delivery_time = {}
    live.delivery_cooldown = 600
    assert not live.can_auto_delivery("100")
    result = asyncio.run(live.deliver_order_manually("100", "200", "buyer", "chat"))
    assert result["reason_code"] == "physical_dropship"
    assert not asyncio.run(live._confirm_platform_shipped("100", "200", force=True))


def test_concurrent_timeout_does_not_resubmit_logistics(orders, monkeypatch):
    from dropshipping import sync_account

    in_transit(orders)
    monkeypatch.setattr(seller_api, "fetch_orders_by_ids", lambda *args, **kwargs: {
        "100": {"order_id": "100", "order_status": "pending_ship", "in_refund": False},
    })
    submit = Mock(side_effect=TimeoutError)
    monkeypatch.setattr(seller_api, "_call", submit)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda version: sync_account(
            orders, "test MTOP cookie", "shop", mtop_ship_version=version,
        ), ["1.0"] * 3))
    submit.assert_called_once()
    key = idempotency.make_key("dropship_ship", "shop", "100")
    assert idempotency.get(key)["status"] == "submission_unknown"
    assert orders.get_order_by_id("100")["order_status"] == "pending_ship"
    assert delivery_reliability.platform_ship_rows("stopped", "shop")


@pytest.mark.parametrize("refund_pending", [False, True])
def test_open_circuit_skips_platform_reads(orders, monkeypatch, refund_pending):
    from dropshipping import sync_account

    in_transit(orders)
    if refund_pending:
        assert orders.insert_or_update_order("100", cookie_id="shop", order_status="refunding")
    sold = Mock()
    aftersale = Mock()
    monkeypatch.setattr(seller_api, "fetch_orders_by_ids", sold)
    monkeypatch.setattr(goofish_open, "call", aftersale)
    monkeypatch.setattr(rate_limiter, "circuit_blocked", lambda cookie_id: (True, 60))
    sync_account(orders, "test MTOP cookie", "shop", mtop_ship_version="1.0",
                 seller_eid="seller", refund_refuse_reason="未沟通协商一致")
    sold.assert_not_called()
    aftersale.assert_not_called()


def test_platform_refund_flag_blocks_logistics_and_intercepts_placement(orders, monkeypatch):
    from dropshipping import begin_order, record_placement, sync_account
    from db_manager import db_manager as default_database
    import db_manager
    import order_reconcile

    assert begin_order(orders, "100", "shop")
    assert record_placement(orders, "100", "shop", "upstream")
    monkeypatch.setattr(db_manager, "db_manager", orders)
    assert db_manager.db_manager is not default_database
    monkeypatch.setattr(seller_api, "fetch_sold_order_pages", lambda *args, **kwargs: {
        "items": [{"commonData": {"orderId": "100", "orderStatus": "待发货", "inRefund": True}}],
        "pages_read": 1, "failed_pages": [], "has_more": False,
    })
    assert order_reconcile.reconcile("", "shop", notify_missing=False)["ok"]
    assert orders.get_order_by_id("100")["dropship_status"] == "refund_intercepted"
    assert orders.get_order_by_id("100")["order_status"] == "refunding"
    submit = Mock()
    monkeypatch.setattr(seller_api, "_call", submit)
    sync_account(orders, "test MTOP cookie", "shop", mtop_ship_version="1.0")
    submit.assert_not_called()


@pytest.mark.parametrize(("detail_change", "expected_writes"), [
    ({}, 1),
    ({"seller_eid": "other-seller"}, 0),
    ({"refund_type": 2}, 0),
    ({"refund_status": 5}, 0),
    ({"goods_status": 2}, 0),
])
def test_in_transit_refusal_uses_verified_refund_and_explanation(
    orders, monkeypatch, detail_change, expected_writes,
):
    from dropshipping import sync_account

    in_transit(orders)
    assert orders.insert_or_update_order("100", cookie_id="shop", order_status="refunding")
    details = {
        "refund_no": "refund-1", "order_no": "100", "seller_eid": "seller",
        "refund_status": 1, "refund_type": 1, "goods_status": 1, **detail_change,
    }
    refused = []

    def call(action, body, allow_write=False):
        if action == "aftersale.detail":
            assert body == {"order_no": "100"}
            return {"code": 0, "data": dict(details)}
        if action == "aftersale.reject_reasons":
            return {"code": 0, "data": {"refuse_reason": ["未沟通协商一致"]}}
        assert action == "aftersale.reject" and allow_write
        refused.append(body)
        details["refund_status"] = 6
        return {}

    monkeypatch.setattr(goofish_open, "call", call)
    for _ in range(2):
        sync_account(orders, "test MTOP cookie", "shop", seller_eid="seller",
                     refund_refuse_reason="未沟通协商一致")
    assert len(refused) == expected_writes
    if expected_writes:
        assert refused == [{
            "order_no": "100", "refuse_reason": "未沟通协商一致",
            "refuse_explain": "货物已发出，请收到后拒签退回",
        }]
        key = idempotency.make_key("dropship_refund", "shop", "100", "refund-1")
        assert idempotency.get(key)["status"] == "done"
    assert orders.get_order_by_id("100")["order_status"] == "refunding"
    assert goofish_open.ALL_APIS["aftersale.reject"] == "/api/open/trade/refund/operate/refused"
