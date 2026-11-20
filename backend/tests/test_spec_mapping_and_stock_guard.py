import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

import delivery_reliability
import dropshipping
import idempotency
import rate_limiter
import seller_api
from db_manager import DBManager

ORDER = "100"
ACCOUNT = "shop"
ITEM = "900"


@pytest.fixture
def orders(tmp_path, monkeypatch):
    database = DBManager(str(tmp_path / "orders.db"))
    monkeypatch.setattr(delivery_reliability, "DB_PATH", tmp_path / "delivery.db")
    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "idem.db")
    monkeypatch.setattr(rate_limiter, "circuit_blocked", lambda account: (False, 0))
    monkeypatch.setattr(rate_limiter, "gate_write", lambda account: (True, 0))
    monkeypatch.setattr(seller_api, "fetch_orders_by_ids", lambda *args: {
        ORDER: {"order_status": "pending_ship", "in_refund": False},
    })
    assert database.save_cookie(ACCOUNT, "offline", 1)
    assert database.save_cookie("other", "offline", 1)
    assert database.insert_or_update_order(
        ORDER, item_id=ITEM, cookie_id=ACCOUNT, order_status="pending_ship",
        receiver_name="测试收件人", receiver_phone="13699998888",
        receiver_address="北京市朝阳区测试街道1号",
    )
    try:
        yield database
    finally:
        database.conn.close()


def enroll(orders, *, lines=None):
    from dropship_sku import OrderLine

    orders.save_spec_mapping(ITEM, "white", "颜色:白", "goods", "white-sku")
    orders.save_spec_mapping(ITEM, "black", "颜色:黑", "goods", "black-sku")
    if lines is None:
        lines = [OrderLine("white", 2, Decimal("12.00")), OrderLine("black", 1, Decimal("12.00"))]
    assert dropshipping.begin_order(orders, ORDER, ACCOUNT, lines=lines)


@pytest.fixture
def adapter(monkeypatch):
    from dropship_sku import StockQuote

    adapter = Mock()
    adapter.quote_sku.return_value = StockQuote(8, Decimal("9.00"))
    adapter.place_sku_order.side_effect = lambda account, task, address, **fields: f"buy-{task}"
    adapter.get_pickup.return_value = None
    adapter.cancel_order.return_value = True
    ship = Mock()
    monkeypatch.setattr(dropshipping, "ship_physical_order", ship)
    return adapter, ship


def sync(orders, adapter, **kwargs):
    dropshipping.sync_account(orders, "offline", ACCOUNT, adapter=adapter, **kwargs)


def test_exact_mapping_lookup_migration_and_wal(orders):
    orders.save_spec_mapping(ITEM, "hash", "颜色:白", "goods", "sku-1")
    orders.save_spec_mapping(ITEM, "hash", "颜色:白", "goods", "sku-2", stock_guard=False)
    orders.save_spec_mapping("another-item", "hash", "颜色:黑", "different-goods", "sku-3")
    migrated = DBManager(orders.db_path)
    try:
        match = migrated.get_spec_mapping(ITEM, "hash")
        assert (match["upstream_goods_id"], match["upstream_sku_id"], match["stock_guard"]) == ("goods", "sku-2", 0)
        assert migrated.get_spec_mapping("another-item", "hash")["stock_guard"] == 1
        assert migrated.get_spec_mapping(ITEM, "HASH") is None
        assert migrated.conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        migrated.conn.close()


def test_incomplete_spec_lines_cannot_fall_back_to_a_default_purchase(orders):
    from dropship_sku import OrderLine

    orders.save_spec_mapping(ITEM, "white", "颜色:白", "goods", "white-sku")
    assert dropshipping.begin_order(orders, ORDER, ACCOUNT) is None
    assert dropshipping.begin_order(orders, ORDER, ACCOUNT, lines=[
        OrderLine("white", 1, Decimal("12.00")), OrderLine("missing", 1, Decimal("12.00")),
    ]) is None
    assert orders.get_order_by_id(ORDER)["dropship_status"] == ""
    assert orders.get_dropship_tasks(ORDER, ACCOUNT) == []


@pytest.mark.parametrize("shared_tracking", [True, False])
def test_split_purchases_snapshotted_skus_and_waits_for_all_pickups(orders, adapter, shared_tracking):
    supplier, ship = adapter
    enroll(orders)
    assert orders.get_dropship_tasks(ORDER, "other") == []
    orders.save_spec_mapping(ITEM, "white", "颜色:白", "goods", "replacement")
    for _ in range(2):
        sync(orders, supplier)
    assert [(call.args[1], call.kwargs) for call in supplier.place_sku_order.call_args_list] == [
        ("100#1", {"upstream_goods_id": "goods", "upstream_sku_id": "white-sku", "quantity": 2,
                   "max_unit_cost": Decimal("10.00")}),
        ("100#2", {"upstream_goods_id": "goods", "upstream_sku_id": "black-sku", "quantity": 1,
                   "max_unit_cost": Decimal("10.00")}),
    ]
    assert orders.get_order_by_id(ORDER)["dropship_status"] == "placed"
    packages = {"buy-100#1": ("YTO", "YT-1")}
    supplier.get_pickup.side_effect = packages.get
    sync(orders, supplier)
    ship.assert_not_called()
    assert orders.get_order_by_id(ORDER)["dropship_status"] == "placed"

    packages["buy-100#2"] = ("YTO", "YT-1" if shared_tracking else "YT-2")
    for _ in range(2):
        sync(orders, supplier)
    state = orders.get_order_by_id(ORDER)
    assert state["dropship_status"] == "shipped"
    assert state["order_status"] == ("shipped" if shared_tracking else "pending_ship")
    assert [task["express_no"] for task in orders.get_dropship_tasks(ORDER, ACCOUNT)] == [
        "YT-1", packages["buy-100#2"][1],
    ]
    assert ship.call_count == int(shared_tracking)


@pytest.mark.parametrize(("stock", "available", "cost", "reason", "zero_count"), [
    (3, True, "9.00", "low_stock", 1),
    (100, False, "9.00", "sku_unavailable", 1),
    (8, True, "10.01", "margin_below_minimum", 0),
])
def test_stock_and_margin_preflight_blocks_all_purchases(
    orders, adapter, stock, available, cost, reason, zero_count,
):
    from dropship_sku import StockQuote

    supplier, ship = adapter
    enroll(orders)
    supplier.quote_sku.side_effect = [
        StockQuote(8, Decimal("9.00")), StockQuote(stock, Decimal(cost), available),
    ] * 2
    set_stock = Mock(return_value=True)
    for _ in range(2):
        sync(orders, supplier, set_stock=set_stock)
    supplier.place_sku_order.assert_not_called()
    ship.assert_not_called()
    assert orders.get_order_by_id(ORDER)["dropship_status"] == "pending_buy"
    assert [alert["reason"] for alert in orders.get_spec_stock_alerts(ACCOUNT)] == [reason]
    assert set_stock.call_count == zero_count
    if zero_count:
        set_stock.assert_called_once_with(ACCOUNT, ITEM, "black", 0)


def test_refund_cancels_all_unshipped_child_orders(orders, adapter):
    supplier, ship = adapter
    enroll(orders)
    sync(orders, supplier)
    assert orders.insert_or_update_order(ORDER, cookie_id=ACCOUNT, order_status="refunding")
    assert orders.get_order_by_id(ORDER)["dropship_status"] == "refund_intercepted"
    sync(orders, supplier)
    assert [call.args for call in supplier.cancel_order.call_args_list] == [("buy-100#1",), ("buy-100#2",)]
    assert orders.get_order_by_id(ORDER)["dropship_status"] == "cancelled"
    ship.assert_not_called()


def test_stock_poll_without_orders_rearms_after_recovery(orders, adapter):
    from dropship_sku import StockQuote, ZeroStockGuard

    supplier, _ = adapter
    assert orders.save_item_info(ACCOUNT, ITEM, {"title": "测试商品"})
    orders.save_spec_mapping(ITEM, "black", "颜色:黑", "goods", "black-sku")
    set_stock = Mock(return_value=True)
    supplier.quote_sku.return_value = StockQuote(3, Decimal("9.00"))

    class SingleTick(asyncio.Event):
        async def wait(self):
            self.set()
            return True

    asyncio.run(dropshipping.run_scheduler(orders, SingleTick(), {}, adapter=supplier, set_stock=set_stock))
    set_stock.assert_called_once_with(ACCOUNT, ITEM, "black", 0)
    guard = ZeroStockGuard(orders, supplier, set_stock)
    guard.poll()
    assert set_stock.call_count == 1

    supplier.quote_sku.return_value = StockQuote(8, Decimal("9.00"))
    guard.poll()
    alert, = orders.get_spec_stock_alerts(ACCOUNT)
    assert alert["active"] == 0
    supplier.quote_sku.return_value = StockQuote(2, Decimal("9.00"))
    guard.poll()
    assert set_stock.call_count == 2
    alert, = orders.get_spec_stock_alerts(ACCOUNT)
    assert (alert["active"], alert["zeroed"]) == (1, 1)
    supplier.place_sku_order.assert_not_called()


def test_stock_poll_rearms_when_the_upstream_sku_changes(orders, adapter):
    from dropship_sku import StockQuote, ZeroStockGuard

    supplier, _ = adapter
    assert orders.save_item_info(ACCOUNT, ITEM, {"title": "测试商品"})
    orders.save_spec_mapping(ITEM, "black", "颜色:黑", "goods", "black-sku")
    supplier.quote_sku.return_value = StockQuote(3, Decimal("9.00"))
    set_stock = Mock(return_value=True)
    guard = ZeroStockGuard(orders, supplier, set_stock)
    guard.poll()
    orders.save_spec_mapping(ITEM, "black", "改名", "goods", "black-sku")
    guard.poll()
    set_stock.assert_called_once_with(ACCOUNT, ITEM, "black", 0)

    orders.save_spec_mapping(ITEM, "black", "改名", "goods", "replacement")
    guard.poll()
    supplier.quote_sku.assert_called_with("goods", "replacement")
    assert set_stock.call_count == 2


def test_stock_guard_simulates_only_the_bound_platform_sku(orders, adapter, monkeypatch):
    from dropship_sku import StockQuote, make_zero_stock_simulator

    supplier, _ = adapter
    network = Mock(side_effect=AssertionError("库存模拟不应请求网络"))
    monkeypatch.setattr(seller_api.requests, "post", network)
    assert orders.save_item_info(ACCOUNT, ITEM, {"title": "测试商品"})
    orders.save_spec_mapping(ITEM, "black", "颜色:黑", "goods", "supplier-sku")
    supplier.quote_sku.return_value = StockQuote(3, Decimal("9.00"))
    send = Mock(side_effect=[False, True, True])
    set_stock = make_zero_stock_simulator(orders, send)

    class SingleTick(asyncio.Event):
        async def wait(self):
            self.set()
            return True

    def run_tick():
        asyncio.run(dropshipping.run_scheduler(orders, SingleTick(), {}, adapter=supplier, set_stock=set_stock))

    run_tick()
    send.assert_not_called()
    orders.save_spec_mapping(ITEM, "black", "颜色:黑", "goods", "supplier-sku",
                             platform_sku_id="51283940182")
    run_tick()
    assert orders.get_spec_stock_alerts(ACCOUNT)[0]["zeroed"] == 0
    run_tick()
    run_tick()
    assert send.call_count == 2
    send.assert_called_with(
        "mtop.taobao.idle.item.stock.update",
        {"itemId": ITEM, "skuStocks": [{"skuId": "51283940182", "quantity": 0}]},
        version="1.0",
    )
    assert orders.get_spec_stock_alerts(ACCOUNT)[0]["zeroed"] == 1

    orders.save_spec_mapping(ITEM, "black", "颜色:黑", "goods", "supplier-sku",
                             platform_sku_id="51283940183")
    run_tick()
    assert send.call_count == 3
    assert send.call_args.args[1]["skuStocks"] == [{"skuId": "51283940183", "quantity": 0}]
    network.assert_not_called()


def test_uncertain_supplier_submission_stops_retries_and_keeps_refund_intercepted(orders, adapter):
    supplier, ship = adapter
    enroll(orders)
    supplier.place_sku_order.side_effect = TimeoutError
    for _ in range(2):
        sync(orders, supplier)
    supplier.place_sku_order.assert_called_once()
    assert orders.insert_or_update_order(ORDER, cookie_id=ACCOUNT, order_status="refunding")
    sync(orders, supplier)
    assert [task["status"] for task in orders.get_dropship_tasks(ORDER, ACCOUNT)] == ["placing", "cancelled"]
    assert orders.get_order_by_id(ORDER)["dropship_status"] == "refund_intercepted"
    supplier.cancel_order.assert_not_called()
    ship.assert_not_called()
