import asyncio
import json
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

import delivery_reliability
import dropshipping
import idempotency
import platform_ship
import rate_limiter
import seller_api
from dropship_sku import OrderLine, StockQuote

ORDER_ID = "3316574319266035695"
ACCOUNT = "offline-shop"
COOKIE = f"{seller_api.TOKEN_COOKIE}=offline_123"
SHIP_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idle.order.ship/1.0/"
MULTI_SKU_ORDER = {
    "orderId": ORDER_ID,
    "itemId": "1100839041022",
    "itemTitle": "车载香薰出风口专用高档持久清香",
    "priceVO": {"totalPrice": "19.90"},
    "orderStatus": "PAID",
    "skuId": "51283940182",
    "skuProperties": [
        {"propertyName": "颜色分类", "propertyValue": "深邃黑（配2枚香芯）"},
        {"propertyName": "套餐规格", "propertyValue": "官方标配"},
    ],
    "subOrders": [
        {"subOrderId": f"{ORDER_ID}-1", "skuId": "51283940182", "buyNum": 1},
        {"subOrderId": f"{ORDER_ID}-2", "skuId": "51283940183", "buyNum": 1},
    ],
}


@pytest.fixture
def orders(tmp_path, monkeypatch):
    from db_manager import DBManager

    database = DBManager(str(tmp_path / "orders.db"))
    monkeypatch.setattr(delivery_reliability, "DB_PATH", tmp_path / "delivery.db")
    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "idempotency.db")
    monkeypatch.setattr(rate_limiter, "circuit_blocked", lambda cookie_id: (False, 0))
    monkeypatch.setattr(rate_limiter, "gate_write", lambda cookie_id: (True, 0))
    assert database.save_cookie(ACCOUNT, COOKIE, 1)
    assert database.insert_or_update_order(
        ORDER_ID, item_id="100", buyer_id="offline-buyer", order_status="pending_ship",
        cookie_id=ACCOUNT, receiver_name="测试收件人", receiver_phone="+86 136-9999-8888",
        receiver_address="北京市朝阳区测试街道1号",
    )
    try:
        yield database
    finally:
        database.conn.close()


class MockDropshipAdapter:
    def __init__(self):
        self.orders = {}
        self.pickups = {}
        self.cancellations = set()

    def place_order(self, cookie_id, order_id, address):
        assert address["full_address"] == "北京市朝阳区测试街道1号"
        return self.orders.setdefault((cookie_id, order_id), f"MOCK-{order_id}")

    def simulate_pickup(self, upstream_order_id, *, express_no=None):
        assert upstream_order_id in self.orders.values()
        assert upstream_order_id not in self.cancellations
        return self.pickups.setdefault(
            upstream_order_id, ("圆通", express_no or f"MOCK-TRACK-{upstream_order_id}"),
        )

    def get_pickup(self, upstream_order_id):
        return self.pickups.get(upstream_order_id)

    def cancel_order(self, upstream_order_id):
        assert upstream_order_id in self.orders.values()
        if upstream_order_id in self.pickups:
            return False
        self.cancellations.add(upstream_order_id)
        return True


@pytest.fixture
def platform(monkeypatch):
    response = Mock(status_code=200, headers={})
    response.json.return_value = {
        "api": "mtop.taobao.idle.order.ship",
        "v": "1.0",
        "ret": ["SUCCESS::调用成功"],
        "data": {"success": True, "orderId": ORDER_ID, "isSuccess": "true"},
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(seller_api.requests, "post", post)
    monkeypatch.setattr(seller_api, "_app_key", lambda: "offline-app")
    monkeypatch.setattr(seller_api, "fetch_orders_by_ids", lambda cookie, account, ids: {
        ORDER_ID: {"order_id": ORDER_ID, "order_status": "pending_ship", "in_refund": False},
    })
    return post


class SingleSchedulerTick(asyncio.Event):
    async def wait(self):
        self.set()
        return True


def run_tick(database, adapter):
    asyncio.run(dropshipping.run_scheduler(database, SingleSchedulerTick(), {}, adapter=adapter))


def test_scheduler_purchases_then_submits_carrier_pickup_once(orders, platform):
    adapter = MockDropshipAdapter()
    assert dropshipping.begin_order(orders, ORDER_ID, ACCOUNT)
    assert orders.get_order_by_id(ORDER_ID)["dropship_status"] == "pending_buy"
    run_tick(orders, adapter)
    placed = orders.get_order_by_id(ORDER_ID)
    assert placed["dropship_status"] == "placed"
    upstream = placed["upstream_order_id"]
    assert upstream == adapter.orders[(ACCOUNT, ORDER_ID)]
    platform.assert_not_called()

    adapter.simulate_pickup(upstream, express_no="YT1234567890")
    for _ in range(2):
        run_tick(orders, adapter)
    platform.assert_called_once()
    request = platform.call_args
    assert request.args == (SHIP_URL,)
    assert request.kwargs["params"]["v"] == "1.0"
    assert request.kwargs["allow_redirects"] is False
    assert json.loads(request.kwargs["data"]["data"]) == {
        "orderId": ORDER_ID, "expressCode": "YTO", "expressNo": "YT1234567890",
        "subOrderList": [],
    }
    shipped = orders.get_order_by_id(ORDER_ID)
    assert (shipped["dropship_status"], shipped["order_status"]) == ("shipped", "shipped")
    assert shipped["express_no"] == "YT1234567890"
    assert len(delivery_reliability.platform_ship_rows("done", ACCOUNT)) == 1
    assert orders.conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)


@pytest.mark.parametrize("broken_sub_orders", ["missing", "duplicate", "quantity", "different_item"])
def test_scheduler_aggregates_distinct_labels_using_exact_platform_skus(
    orders, platform, monkeypatch, broken_sub_orders,
):
    item_id = MULTI_SKU_ORDER["itemId"]
    assert orders.insert_or_update_order(ORDER_ID, item_id=item_id, cookie_id=ACCOUNT)
    sold = deepcopy(MULTI_SKU_ORDER)
    sold["subOrders"].reverse()
    parsed = seller_api.parse_sold_order(sold)
    assert (parsed["order_id"], parsed["item_id"], parsed["order_status"], parsed["quantity"]) == (
        ORDER_ID, item_id, "pending_ship", "2",
    )
    monkeypatch.setattr(seller_api, "fetch_orders_by_ids", lambda *args: {
        ORDER_ID: seller_api.parse_sold_order(sold),
    })
    orders.save_spec_mapping(item_id, "black", "黑", "supplier-goods", "supplier-black",
                             platform_sku_id="51283940182")
    orders.save_spec_mapping(item_id, "white", "白", "supplier-goods", "supplier-white",
                             platform_sku_id="51283940183")
    assert dropshipping.begin_order(orders, ORDER_ID, ACCOUNT, lines=[
        OrderLine("black", 1, Decimal("9.90")), OrderLine("white", 1, Decimal("10.00")),
    ])

    adapter = Mock()
    adapter.quote_sku.return_value = StockQuote(20, Decimal("5.00"))
    adapter.place_sku_order.side_effect = lambda account, task_id, address, **fields: task_id
    pickups = {f"{ORDER_ID}#1": ("圆通", "YT9876543210")}
    adapter.get_pickup.side_effect = pickups.get
    run_tick(orders, adapter)
    assert adapter.place_sku_order.call_count == 2
    run_tick(orders, adapter)
    platform.assert_not_called()

    pickups[f"{ORDER_ID}#2"] = ("中通", "ZT1234567890")
    if broken_sub_orders == "missing":
        del sold["subOrders"]
    elif broken_sub_orders == "duplicate":
        sold["subOrders"][1]["subOrderId"] = sold["subOrders"][0]["subOrderId"]
    elif broken_sub_orders == "quantity":
        sold["subOrders"][1]["buyNum"] = 2
    else:
        sold["itemId"] = "unrelated-item"
    run_tick(orders, adapter)
    platform.assert_not_called()
    assert orders.get_order_by_id(ORDER_ID)["order_status"] == "pending_ship"

    sold = deepcopy(MULTI_SKU_ORDER)
    sold["subOrders"].reverse()
    del platform.return_value.json.return_value["data"]["success"]
    for _ in range(2):
        run_tick(orders, adapter)
    platform.assert_called_once()
    assert platform.call_args.args == (SHIP_URL,)
    assert json.loads(platform.call_args.kwargs["data"]["data"]) == {
        "orderId": ORDER_ID,
        "expressCode": "YTO",
        "expressNo": "YT9876543210",
        "subOrderList": [
            {"subOrderId": f"{ORDER_ID}-1", "expressCode": "YTO", "expressNo": "YT9876543210"},
            {"subOrderId": f"{ORDER_ID}-2", "expressCode": "ZTO", "expressNo": "ZT1234567890"},
        ],
    }
    assert orders.get_order_by_id(ORDER_ID)["order_status"] == "shipped"
    assert len(delivery_reliability.platform_ship_rows("done", ACCOUNT)) == 1


@pytest.mark.parametrize("sub_orders", [
    (("same-id", "YTO", "YT123"), ("same-id", "ZTO", "ZT123")),
    (("valid-id", "unknown-carrier", "TRACK"),),
])
def test_platform_ship_refuses_invalid_sub_order_labels(platform, sub_orders):
    with pytest.raises(ValueError):
        platform_ship.ship_physical_order(ORDER_ID, "YTO", "YT123", COOKIE, ACCOUNT, sub_orders=sub_orders)
    platform.assert_not_called()


def test_refund_intercepts_purchase_and_applies_supplier_cancellation(orders, platform):
    adapter = MockDropshipAdapter()
    assert dropshipping.begin_order(orders, ORDER_ID, ACCOUNT)
    run_tick(orders, adapter)
    upstream = orders.get_order_by_id(ORDER_ID)["upstream_order_id"]

    assert orders.insert_or_update_order(ORDER_ID, cookie_id=ACCOUNT, order_status="refunding")
    assert orders.get_order_by_id(ORDER_ID)["dropship_status"] == "refund_intercepted"
    assert orders.pending_dropship_cancellations(ACCOUNT) == [(ORDER_ID, upstream)]

    for _ in range(2):
        run_tick(orders, adapter)
    cancelled = orders.get_order_by_id(ORDER_ID)
    assert (cancelled["dropship_status"], cancelled["order_status"]) == ("cancelled", "refunding")
    assert adapter.cancellations == {upstream}
    assert orders.pending_dropship_cancellations(ACCOUNT) == []
    platform.assert_not_called()


def test_refund_during_supplier_purchase_cancels_late_order(orders, platform):
    class RefundDuringPlacement(MockDropshipAdapter):
        def place_order(self, cookie_id, order_id, address):
            upstream = super().place_order(cookie_id, order_id, address)
            assert orders.insert_or_update_order(order_id, cookie_id=cookie_id, order_status="refunding")
            return upstream

    adapter = RefundDuringPlacement()
    assert dropshipping.begin_order(orders, ORDER_ID, ACCOUNT)
    run_tick(orders, adapter)
    upstream = adapter.orders[(ACCOUNT, ORDER_ID)]
    assert orders.pending_dropship_cancellations(ACCOUNT) == [(ORDER_ID, upstream)]
    run_tick(orders, adapter)
    assert adapter.cancellations == {upstream}
    assert orders.get_order_by_id(ORDER_ID)["dropship_status"] == "cancelled"
    platform.assert_not_called()


@pytest.mark.parametrize(("name", "code"), [
    ("圆通", "YTO"), ("中通", "ZTO"), ("申通", "STO"), ("韵达", "YUNDA"),
    ("顺丰", "SF"), ("极兔", "JT"), ("邮政 EMS", "EMS"),
])
def test_platform_ship_accepts_exact_carrier_names_and_codes(platform, name, code):
    for carrier in (name, code):
        platform_ship.ship_physical_order(ORDER_ID, carrier, "OFFLINE123", COOKIE, ACCOUNT)
        assert json.loads(platform.call_args.kwargs["data"]["data"])["expressCode"] == code


def test_platform_ship_rejects_unknown_carrier_or_version(platform):
    with pytest.raises(ValueError):
        platform_ship.ship_physical_order(ORDER_ID, "JT", "OFFLINE123", COOKIE, ACCOUNT, version="2.0")
    with pytest.raises(ValueError):
        platform_ship.ship_physical_order(ORDER_ID, "JTU", "OFFLINE123", COOKIE, ACCOUNT)
    platform.assert_not_called()


@pytest.mark.parametrize(("key", "value"), [
    ("orderId", "different-order"), ("success", False), ("isSuccess", "false"),
])
def test_platform_ship_requires_matching_order_and_business_success(platform, key, value):
    platform.return_value.json.return_value["data"][key] = value
    with pytest.raises(seller_api.SellerApiError):
        platform_ship.ship_physical_order(ORDER_ID, "YTO", "OFFLINE123", COOKIE, ACCOUNT)
