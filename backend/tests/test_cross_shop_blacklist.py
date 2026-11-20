import asyncio
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

import db_manager
import dropshipping
import rate_limiter


@pytest.fixture
def database(tmp_path, monkeypatch):
    manager = db_manager.DBManager(str(tmp_path / "shop.db"))
    monkeypatch.setattr(db_manager, "db_manager", manager)
    monkeypatch.setattr(rate_limiter, "CIRCUIT_FILE", tmp_path / "risk.json")
    for shop in ("shop-a", "shop-b", "shop-c"):
        assert manager.save_cookie(shop, "offline")
    yield manager
    manager.conn.close()


def test_only_explicit_shared_entries_block_across_accounts(database):
    assert database.conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert database.record_refund_blacklist("buyer", "shop-a", "refunded-order")
    assert database.get_cross_shop_blacklist_entry("buyer") is None
    assert not database.should_block_refund_buyer_delivery("buyer", "shop-b", "item")
    assert database.record_cross_shop_blacklist("buyer", "shop-a", "已核实退款滥用", 19.9)
    assert database.get_cross_shop_blacklist_entry("BUYER") is None
    assert database.record_cross_shop_blacklist("buyer", "missing-shop", "错误来源", None) is False
    shared = database.get_cross_shop_blacklist_entry("buyer")
    assert shared["trigger_account_id"] == "shop-a"
    assert shared["reason"] == "已核实退款滥用"
    assert shared["refund_amount"] == 19.9
    assert shared["created_at"]
    assert database.delete_cross_shop_blacklist_entry("buyer")
    assert database.get_cross_shop_blacklist_entry("buyer") is None
    assert database.is_buyer_refund_blacklisted("buyer", "shop-a")


def test_auto_and_manual_delivery_stop_before_allocating_cards(database, monkeypatch):
    from XianyuAutoAsync import XianyuLive

    assert database.record_cross_shop_blacklist("buyer", "shop-a", "已核实滥用", None)
    order = {"cookie_id": "shop-b", "item_id": "item", "buyer_id": "buyer",
             "order_status": "pending_ship"}
    monkeypatch.setattr(database, "get_order_by_id", Mock(return_value=order))
    monkeypatch.setattr(database, "get_item_info", Mock(return_value={"auto_delivery_enabled": True}))
    account = SimpleNamespace(
        cookie_id="shop-b",
        _order_locks=defaultdict(asyncio.Lock),
        _is_auto_delivery_trigger=lambda *_: True,
        _extract_order_id=lambda *_, **__: "order",
        _prepare_delivery_contents=AsyncMock(),
        _safe_str=str,
    )
    asyncio.run(XianyuLive._handle_auto_delivery(
        account, None, {}, "用户名", "buyer", "item", "chat", "now",
    ))
    account._prepare_delivery_contents.assert_not_awaited()
    manual = asyncio.run(XianyuLive.deliver_order_manually(account, "order", "item", "buyer", "chat"))
    assert manual["reason_code"] == "cross_shop_blacklist"
    assert not manual["success"]


def test_dropship_blocks_new_purchase_and_shipment_but_preserves_refunds(database, tmp_path, monkeypatch):
    import delivery_reliability
    import idempotency

    assert database.record_cross_shop_blacklist("buyer", "shop-a", "已核实退款滥用", 9.9)
    for order_id, buyer, status in (("new", "buyer", "pending_ship"),
                                    ("ship", "buyer", "pending_ship"),
                                    ("refund", "buyer", "refunding"),
                                    ("other", "other-buyer", "pending_ship")):
        assert database.insert_or_update_order(
            order_id, cookie_id="shop-b", item_id="item", buyer_id=buyer, order_status=status,
        )
    with database.lock, database.conn:
        database.conn.execute("UPDATE orders SET dropship_status='shipped', express_no='YT123', express_company='YTO'")
        database.conn.execute("UPDATE orders SET dropship_status='pending_buy' WHERE order_id='new'")
    ship = Mock()
    reject = Mock()
    adapter = Mock()
    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "idempotency.db")
    monkeypatch.setattr(delivery_reliability, "DB_PATH", tmp_path / "delivery.db")
    monkeypatch.setattr(rate_limiter, "gate_write", lambda *_: (True, 0))
    monkeypatch.setattr(dropshipping, "ship_physical_order", ship)
    monkeypatch.setattr(dropshipping, "_reject_refund", reject)
    fetch = Mock(return_value={"other": {"order_status": "pending_ship", "buyer_id": "other-buyer"}})
    monkeypatch.setattr(dropshipping.seller_api, "fetch_orders_by_ids", fetch)

    dropshipping.sync_account(
        database, "offline", "shop-b", adapter=adapter, seller_eid="offline", refund_refuse_reason="offline",
    )
    adapter.place_order.assert_not_called()
    fetch.assert_called_once_with("offline", "shop-b", ["other"])
    assert [call.args[0] for call in ship.call_args_list] == ["other"]
    assert [call.args[1]["order_id"] for call in reject.call_args_list] == ["refund"]
    assert database.get_dropship_order("new", "shop-b")["dropship_status"] == "pending_buy"
    assert database.get_dropship_order("ship", "shop-b")["order_status"] == "pending_ship"


def test_admin_api_and_manual_ship_precheck(database, monkeypatch):
    import reply_server
    from fastapi.testclient import TestClient

    monkeypatch.setattr(reply_server, "db_manager", database)
    user = {"username": "shop-user", "user_id": 1, "is_admin": False}
    monkeypatch.setitem(reply_server.app.dependency_overrides, reply_server.get_current_user, lambda: user)
    client = TestClient(reply_server.app)
    payload = {"buyer_id": "buyer", "trigger_account_id": "shop-a", "reason": "已核实滥用", "refund_amount": 9.9}
    params = {"buyer_id": "buyer"}
    assert client.post("/cross-shop-blacklist", json=payload).status_code == 403
    assert client.get("/cross-shop-blacklist", params=params).status_code == 403
    user.update(username=reply_server.ADMIN_USERNAME, is_admin=True)
    assert client.post("/cross-shop-blacklist", json=payload).status_code == 201
    assert client.get("/cross-shop-blacklist", params=params).json()["item"]["trigger_account_id"] == "shop-a"
    assert database.insert_or_update_order(
        "order", cookie_id="shop-b", item_id="item", buyer_id="buyer", order_status="pending_ship",
    )
    for mode in ("full_delivery", "status_only"):
        result = client.post("/api/orders/manual-ship", json={"order_ids": ["order"], "ship_mode": mode})
        assert result.status_code == 200
        assert result.json()["results"][0]["reason_code"] == "cross_shop_blacklist"
    assert client.delete("/cross-shop-blacklist", params=params).status_code == 200
    assert client.get("/cross-shop-blacklist", params=params).status_code == 404


def test_previously_issued_link_cannot_release_uncollected_cards(database, tmp_path, monkeypatch):
    import delivery_reliability as ledger
    from pickup_service import PickupError, PickupService

    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "delivery.db")
    assert database.insert_or_update_order(
        "order", cookie_id="shop-c", item_id="item", buyer_id="buyer", order_status="pending_ship",
    )
    ledger.ensure_order("order", "shop-c", ["offline"], item_id="item", buyer_id="buyer")
    assert ledger.mark_sent("order", "shop-c", 0)
    pickup = PickupService(database.db_path, "https://pickup.example/pickup", b"x" * 32)
    link = pickup.issue_link("order", "shop-c", "buyer")
    token = parse_qs(urlsplit(link).fragment)["pickup_token"][0]

    assert database.record_cross_shop_blacklist("buyer", "shop-a", "已核实滥用", None)
    with pytest.raises(PickupError) as error:
        pickup.access("order", token, "192.0.2.5", collect=True)
    assert error.value.status == 409
    assert ledger.cards("order", "shop-c")[0]["status"] == "sent"
    assert database.delete_cross_shop_blacklist_entry("buyer")
    assert pickup.access("order", token, "192.0.2.5", collect=True)["status"] == "collected"
