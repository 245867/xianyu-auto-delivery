"""Physical dropship order transitions and platform synchronization."""

import asyncio
import time
from decimal import Decimal

import delivery_reliability
import goofish_open
import idempotency
import rate_limiter
import seller_api
from loguru import logger
from dropship_sku import MINIMUM_MARGIN, OrderLine, ZeroStockGuard, upstream_sku
from order_status_rules import cross_shop_block_reason
from platform_ship import EXPRESS_CODES, PHYSICAL_SHIP_VERSION, ship_physical_order
from shipping_address_parser import parse_shipping_address

REFUND_EXPLANATION = "货物已发出，请收到后拒签退回"
REFUND_WAITING_FOR_SELLER = 1
REFUND_REJECTED = 6
REFUND_ONLY = 1
GOODS_IN_TRANSIT = (1, 6)


def _update(database, order, status, **fields):
    return database.update_dropship_order(
        order["order_id"], order["cookie_id"], order["version"], status, **fields,
    )


def _shipping_address(order):
    address = parse_shipping_address(
        order["receiver_name"], order["receiver_phone"], order["receiver_address"],
        order["receiver_city"] or "",
    )
    if not address["valid"] or not all(address[field] for field in ("province", "city", "district")):
        return None
    return address


def begin_order(database, order_id: str, cookie_id: str, *, lines=None):
    """Enroll a paid physical order and return its normalized address to the caller."""
    order = database.get_dropship_order(order_id, cookie_id)
    if not order or order["dropship_status"] or order["system_shipped"] or order["order_status"] != "pending_ship":
        return None
    address = _shipping_address(order)
    if not address:
        return None
    if lines is not None:
        lines = tuple(lines)
        if not lines or not all(isinstance(line, OrderLine) for line in lines):
            return None
        if order["quantity"] and order["quantity"] != str(sum(line.quantity for line in lines)):
            return None
        return address if database.create_dropship_tasks(order_id, cookie_id, order["version"], lines) else None
    if database.has_spec_mappings(order["item_id"]):
        return None
    return address if _update(database, order, "pending_buy") else None


def record_placement(database, order_id: str, cookie_id: str, upstream_order_id: str) -> bool:
    """Persist the supplier's confirmed purchase, including a late response after refund."""
    if not upstream_order_id or database.get_dropship_tasks(order_id, cookie_id):
        return False
    for _ in range(3):
        order = database.get_dropship_order(order_id, cookie_id)
        if not order or not order["dropship_status"]:
            return False
        if order["upstream_order_id"]:
            return order["upstream_order_id"] == upstream_order_id
        if order["dropship_status"] == "pending_buy" and order["order_status"] == "pending_ship":
            status = "placed"
        elif order["dropship_status"] == "cancelled":
            status = "refund_intercepted"
        else:
            return False
        if _update(database, order, status, upstream_order_id=upstream_order_id):
            return True
    return False


def record_pickup(database, order_id: str, cookie_id: str, upstream_order_id: str,
                  express_company: str, express_no: str) -> bool:
    """Accept logistics reported by the supplier after verified carrier pickup."""
    if not all(isinstance(value, str) and value and value == value.strip()
               for value in (upstream_order_id, express_company, express_no)):
        return False
    if database.get_dropship_tasks(order_id, cookie_id):
        return False
    for _ in range(3):
        order = database.get_dropship_order(order_id, cookie_id)
        if not order or order["upstream_order_id"] != upstream_order_id:
            return False
        if order["dropship_status"] == "shipped":
            return (order["express_company"], order["express_no"]) == (express_company, express_no)
        if order["dropship_status"] not in ("placed", "refund_intercepted"):
            return False
        if not order["express_no"] and _update(
            database, order, "shipped", express_company=express_company, express_no=express_no,
        ):
            return True
    return False


def record_cancellation(database, order_id: str, cookie_id: str, upstream_order_id: str) -> bool:
    """Close the cancellation request after the supplier confirms cancellation."""
    if not upstream_order_id or database.get_dropship_tasks(order_id, cookie_id):
        return False
    for _ in range(3):
        order = database.get_dropship_order(order_id, cookie_id)
        if not order or order["upstream_order_id"] != upstream_order_id:
            return False
        if order["dropship_status"] == "cancelled":
            return True
        if order["dropship_status"] != "refund_intercepted":
            return False
        if _update(database, order, "cancelled"):
            return True
    return False


def _sync_upstream(database, order, cookie_str, adapter):
    order_id, cookie_id = order["order_id"], order["cookie_id"]
    status = order["dropship_status"]
    if status == "pending_buy":
        if order["order_status"] != "pending_ship" or not cookie_str or database.has_spec_mappings(order["item_id"]):
            return
        if cross_shop_block_reason(database, order.get("buyer_id")):
            return
        active = seller_api.fetch_orders_by_ids(cookie_str, cookie_id, [order_id]).get(order_id)
        if not active or active["order_status"] != "pending_ship" or active.get("in_refund"):
            return
        if cross_shop_block_reason(database, active.get("buyer_id")):
            return
        if active.get("sub_orders", ()) != ():
            return
        latest = database.get_dropship_order(order_id, cookie_id)
        if not latest or latest["version"] != order["version"] or not database.get_cookie_status(cookie_id):
            return
        address = _shipping_address(latest)
        if address and not cross_shop_block_reason(database, latest.get("buyer_id")):
            upstream_order_id = adapter.place_order(cookie_id, order_id, address)
            record_placement(database, order_id, cookie_id, upstream_order_id)
        return

    upstream_order_id = order["upstream_order_id"]
    if not upstream_order_id:
        return
    if status == "refund_intercepted" and adapter.cancel_order(upstream_order_id):
        record_cancellation(database, order_id, cookie_id, upstream_order_id)
        return
    pickup = adapter.get_pickup(upstream_order_id)
    if pickup:
        express_company, express_no = pickup
        record_pickup(database, order_id, cookie_id, upstream_order_id, express_company, express_no)


def _update_task(database, order, task, status, **fields):
    return database.update_dropship_task(
        order["order_id"], order["cookie_id"], task["position"], task["status"], status, **fields,
    )


def _platform_sub_order_ids(order, tasks, active):
    rows = active.get("sub_orders", ())
    if rows == () and not any(task["platform_sku_id"] for task in tasks):
        return ()
    if (
        not rows or len(rows) != len(tasks)
        or active.get("order_id") != order["order_id"] or active.get("item_id") != order["item_id"]
    ):
        return None
    by_spec = {(sku_id, quantity): sub_order_id for sub_order_id, sku_id, quantity in rows}
    if len(by_spec) != len(rows):
        return None
    ids = tuple(by_spec.pop((task["platform_sku_id"], task["quantity"]), "") for task in tasks)
    return ids if all(ids) else None


def _sync_task_logistics(database, order, tasks, adapter):
    refunding = order["order_status"] in ("refunding", "cancelled")
    for task in tasks:
        if refunding and task["status"] == "pending_buy":
            _update_task(database, order, task, "cancelled")
        if task["status"] != "placed":
            continue
        upstream_id = task["upstream_order_id"]
        if refunding and adapter.cancel_order(upstream_id) is True:
            _update_task(database, order, task, "cancelled")
            continue
        pickup = adapter.get_pickup(upstream_id)
        if isinstance(pickup, (tuple, list)) and len(pickup) == 2:
            company, tracking = pickup
            if all(isinstance(value, str) and value and value == value.strip() for value in pickup):
                _update_task(database, order, task, "shipped",
                             express_company=EXPRESS_CODES.get(company, company), express_no=tracking)


def _purchase_tasks(database, order, tasks, cookie_str, guard):
    if order["order_status"] != "pending_ship" or not cookie_str:
        return
    if cross_shop_block_reason(database, order.get("buyer_id")):
        return
    order_id, cookie_id = order["order_id"], order["cookie_id"]
    pending = [task for task in tasks if task["status"] == "pending_buy"]
    if not pending or any(task["status"] == "placing" for task in tasks):
        return
    active = seller_api.fetch_orders_by_ids(cookie_str, cookie_id, [order_id]).get(order_id)
    if not active or active["order_status"] != "pending_ship" or active.get("in_refund"):
        return
    if cross_shop_block_reason(database, active.get("buyer_id")):
        return
    if _platform_sub_order_ids(order, tasks, active) is None:
        return
    if not guard.check_purchase(cookie_id, order["item_id"], pending):
        return
    latest = database.get_dropship_order(order_id, cookie_id)
    if not latest or latest["version"] != order["version"]:
        return
    address = _shipping_address(latest)
    if not address:
        return
    for task in pending:
        if (cross_shop_block_reason(database, latest.get("buyer_id"))
                or cross_shop_block_reason(database, active.get("buyer_id"))):
            return
        if not database.get_cookie_status(cookie_id) or not _update_task(database, order, task, "placing"):
            return
        goods_id, sku_id = upstream_sku(task)
        upstream_id = guard.adapter.place_sku_order(
            cookie_id, task["task_id"], address, upstream_goods_id=goods_id, upstream_sku_id=sku_id,
            quantity=task["quantity"], max_unit_cost=Decimal(task["unit_price"]) - guard.minimum_margin,
        )
        if not isinstance(upstream_id, str) or not upstream_id or upstream_id != upstream_id.strip():
            logger.warning("【{}】采购任务 {} 未收到采购单号，待核对", cookie_id, task["task_id"])
            return
        if not _update_task(database, order, {**task, "status": "placing"}, "placed", upstream_order_id=upstream_id):
            return


def _aggregate_tasks(database, order_id, cookie_id):
    order = database.get_dropship_order(order_id, cookie_id)
    if not order or order["dropship_status"] not in ("pending_buy", "placed", "refund_intercepted"):
        return
    tasks = database.get_dropship_tasks(order_id, cookie_id)
    if not tasks:
        return
    states = {task["status"] for task in tasks}
    if states == {"shipped"}:
        first = tasks[0]
        _update(database, order, "shipped", upstream_order_id=first["upstream_order_id"],
                express_company=first["express_company"], express_no=first["express_no"])
    elif states == {"cancelled"} and order["dropship_status"] == "refund_intercepted":
        _update(database, order, "cancelled")
    elif states.intersection(("placed", "shipped")) and order["dropship_status"] == "pending_buy":
        _update(database, order, "placed")


def _ship(database, order, cookie_str, version):
    order_id, cookie_id = order["order_id"], order["cookie_id"]
    key = idempotency.make_key("dropship_ship", cookie_id, order_id)
    prior = idempotency.get(key)
    if prior and prior["status"] != "done":
        return
    if not prior:
        blocked = cross_shop_block_reason(database, order.get("buyer_id"))
        if blocked:
            logger.warning("【{}】订单 {}：{}", cookie_id, order_id, blocked)
            return
        if not order["express_no"] or not order["express_company"]:
            return
        active = seller_api.fetch_orders_by_ids(cookie_str, cookie_id, [order_id]).get(order_id)
        if not active or active["order_status"] != "pending_ship" or active.get("in_refund"):
            return
        if cross_shop_block_reason(database, active.get("buyer_id")):
            return
        tasks = database.get_dropship_tasks(order_id, cookie_id)
        if any(task["status"] != "shipped" for task in tasks):
            return
        sub_order_ids = _platform_sub_order_ids(order, tasks, active)
        if sub_order_ids is None or (
            not sub_order_ids and len({(task["express_company"], task["express_no"]) for task in tasks}) > 1
        ):
            delivery_reliability.note_platform_ship(
                order_id, cookie_id, order["item_id"] or "",
                "实物子单面单无法与平台子单逐一核对", policy="stop",
            )
            return
        sub_orders = tuple(
            (sub_order_id, task["express_company"], task["express_no"])
            for sub_order_id, task in zip(sub_order_ids, tasks)
        )
        latest = database.get_dropship_order(order_id, cookie_id)
        if not latest or latest["version"] != order["version"]:
            return
        if not rate_limiter.gate_write(cookie_id)[0] or not idempotency.claim(key)["claimed"]:
            return
        latest = database.get_dropship_order(order_id, cookie_id)
        if (not latest or latest["version"] != order["version"] or not database.get_cookie_status(cookie_id)
                or cross_shop_block_reason(database, latest.get("buyer_id"))
                or cross_shop_block_reason(database, active.get("buyer_id"))):
            idempotency.release(key)
            return
        try:
            ship_physical_order(order_id, order["express_company"], order["express_no"],
                                cookie_str, cookie_id, version=version, sub_orders=sub_orders)
        except Exception as exc:
            idempotency.finish(key, "submission_unknown", type(exc).__name__)
            delivery_reliability.note_platform_ship(
                order_id, cookie_id, order["item_id"] or "",
                "实物发货未获成功回执，待平台核对", policy="stop",
            )
            logger.warning("【{}】订单 {} 实物发货待核对（{}）", cookie_id, order_id, type(exc).__name__)
            return
        idempotency.finish(key, "done")
    delivery_reliability.mark_platform_shipped(order_id, cookie_id, confirmed_item_id=order["item_id"] or "")
    latest = database.get_dropship_order(order_id, cookie_id)
    if latest and latest["order_status"] == "pending_ship" and latest["dropship_status"] == "shipped":
        _update(database, latest, "shipped", order_status="shipped")


def _refund_details(order_id, seller_eid):
    response = goofish_open.call("aftersale.detail", {"order_no": order_id})
    detail = response.get("data")
    if (response.get("code") in (0, "0") and isinstance(detail, dict)
            and detail.get("order_no") == order_id and detail.get("seller_eid") == seller_eid
            and isinstance(detail.get("refund_no"), str) and detail["refund_no"]):
        return detail
    return {}


def _can_refuse(detail):
    return (detail.get("refund_status") == REFUND_WAITING_FOR_SELLER
            and detail.get("refund_type") == REFUND_ONLY
            and detail.get("goods_status") in GOODS_IN_TRANSIT)


def _reject_refund(database, order, seller_eid, reason):
    order_id, cookie_id = order["order_id"], order["cookie_id"]

    def still_eligible():
        latest = database.get_dropship_order(order_id, cookie_id)
        return (latest and latest["version"] == order["version"]
                and all((order["upstream_order_id"], order["express_company"], order["express_no"])))

    reject_platform_refund(database, order_id, cookie_id, seller_eid, reason,
                           action="dropship_refund", explanation=REFUND_EXPLANATION,
                           can_refuse=_can_refuse, still_eligible=still_eligible)


def reject_platform_refund(database, order_id, cookie_id, seller_eid, reason, *,
                           action, explanation, can_refuse, still_eligible):
    detail = _refund_details(order_id, seller_eid)
    if not detail:
        return
    refund_no = detail["refund_no"]
    key = idempotency.make_key(action, cookie_id, order_id, refund_no)
    prior = idempotency.get(key)
    if prior:
        if detail.get("refund_status") == REFUND_REJECTED:
            if prior["status"] != "done":
                idempotency.finish(key, "done")
            return "rejected"
        return "submission_unknown" if prior["status"] != "done" else None
    if not can_refuse(detail) or not still_eligible():
        return
    response = goofish_open.call("aftersale.reject_reasons", {"order_no": order_id})
    data = response.get("data")
    reasons = data.get("refuse_reason") if isinstance(data, dict) else None
    if response.get("code") not in (0, "0") or not isinstance(reasons, list) or reason not in reasons:
        return
    detail = _refund_details(order_id, seller_eid)
    if not still_eligible() or detail.get("refund_no") != refund_no or not can_refuse(detail):
        return
    if not rate_limiter.gate_write(cookie_id)[0] or not idempotency.claim(key)["claimed"]:
        return
    if not still_eligible() or not database.get_cookie_status(cookie_id):
        idempotency.release(key)
        return
    try:
        goofish_open.call("aftersale.reject", {
            "order_no": order_id,
            "refuse_reason": reason,
            "refuse_explain": explanation,
        }, allow_write=True)
        detail = _refund_details(order_id, seller_eid)
    except goofish_open.OpenPlatformError as exc:
        if exc.kind in ("disabled", "config"):
            idempotency.release(key)
            return
        if exc.kind == "risk":
            rate_limiter.trip_circuit(cookie_id, reason="退款处理触发平台限流")
        detail = {}
    except Exception:
        detail = {}
    if detail.get("refund_no") == refund_no and detail.get("refund_status") == REFUND_REJECTED:
        idempotency.finish(key, "done")
        return "rejected"
    else:
        idempotency.finish(key, "submission_unknown", "待核对官方售后详情")
        logger.warning("【{}】订单 {} 退款驳回待平台核对", cookie_id, order_id)
        return "submission_unknown"


def sync_account(database, cookie_str: str, cookie_id: str, *, mtop_ship_version: str = PHYSICAL_SHIP_VERSION,
                 seller_eid: str = "", refund_refuse_reason: str = "", adapter=None, set_stock=None,
                 minimum_margin: Decimal = MINIMUM_MARGIN) -> None:
    """Sync one account. The SKU adapter must enforce max_unit_cost at purchase time."""
    ship_enabled = mtop_ship_version == PHYSICAL_SHIP_VERSION and bool(cookie_str)
    if adapter is None and not ship_enabled and not (seller_eid and refund_refuse_reason):
        return
    guard = ZeroStockGuard(database, adapter, set_stock, minimum_margin=minimum_margin) if adapter is not None else None
    for order_id in database.dropship_orders_to_sync(cookie_id, include_upstream=adapter is not None):
        try:
            if not database.get_cookie_status(cookie_id) or rate_limiter.circuit_blocked(cookie_id)[0]:
                return
            order = database.get_dropship_order(order_id, cookie_id)
            if adapter is not None and order and order["dropship_status"] in ("pending_buy", "placed", "refund_intercepted"):
                tasks = database.get_dropship_tasks(order_id, cookie_id)
                if tasks:
                    try:
                        _sync_task_logistics(database, order, tasks, adapter)
                        latest = database.get_dropship_order(order_id, cookie_id)
                        if latest:
                            _purchase_tasks(database, latest, database.get_dropship_tasks(order_id, cookie_id),
                                            cookie_str, guard)
                    finally:
                        _aggregate_tasks(database, order_id, cookie_id)
                else:
                    _sync_upstream(database, order, cookie_str, adapter)
                order = database.get_dropship_order(order_id, cookie_id)
            if not order or order["dropship_status"] != "shipped":
                continue
            if order["order_status"] == "pending_ship" and ship_enabled:
                if not database.is_account_automation_silent(cookie_id):
                    _ship(database, order, cookie_str, mtop_ship_version)
            elif order["order_status"] == "refunding" and seller_eid and refund_refuse_reason:
                _reject_refund(database, order, seller_eid, refund_refuse_reason)
        except goofish_open.OpenPlatformError as exc:
            if exc.kind == "risk":
                rate_limiter.trip_circuit(cookie_id, reason="售后查询触发平台限流")
                return
            logger.warning("【{}】订单 {} 售后查询失败（{}）", cookie_id, order_id, exc.kind)
        except Exception as exc:
            logger.warning("【{}】订单 {} 代拍同步失败（{}）", cookie_id, order_id, type(exc).__name__)


async def run_scheduler(database, stop: asyncio.Event, settings: dict, *, adapter=None, set_stock=None) -> None:
    """Run physical logistics synchronization off the service event loop."""
    version = settings.get("mtop_ship_version", PHYSICAL_SHIP_VERSION)
    reason = settings.get("refund_refuse_reason") or ""
    seller_eids = settings.get("seller_eids") or {}
    if adapter is None and version != PHYSICAL_SHIP_VERSION and not (reason and seller_eids):
        return
    minimum_margin = Decimal(str(settings.get("minimum_margin", MINIMUM_MARGIN))) if adapter is not None else MINIMUM_MARGIN
    guard = ZeroStockGuard(database, adapter, set_stock, minimum_margin=minimum_margin) if adapter is not None else None
    next_stock_poll = 0.0
    while not stop.is_set():
        try:
            if guard is not None and time.monotonic() >= next_stock_poll:
                await asyncio.to_thread(guard.poll)
                next_stock_poll = time.monotonic() + 900
            account_ids = await asyncio.to_thread(database.dropship_sync_accounts, include_upstream=adapter is not None)
            for cookie_id in account_ids:
                if stop.is_set():
                    return
                cookie_str = await asyncio.to_thread(database.get_cookie, cookie_id)
                if cookie_str:
                    await asyncio.to_thread(
                        sync_account, database, cookie_str, cookie_id, mtop_ship_version=version,
                        seller_eid=seller_eids.get(cookie_id, ""), refund_refuse_reason=reason, adapter=adapter,
                        set_stock=set_stock, minimum_margin=minimum_margin,
                    )
        except Exception as exc:
            logger.warning("代拍调度失败（{}）", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=300)
        except asyncio.TimeoutError:
            continue
