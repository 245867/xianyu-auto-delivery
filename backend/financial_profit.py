"""
逐单全要素边际利润账本（拆解 56）
==================================

收入来自 `orders.amount`；上游成本来自 `dropship_tasks`（数量 × 采购单价）。
卡密慢充订单在 `voucher_async_reconcile` 中没有成本字段，成本只能由调用方
通过 `voucher_cost` 注入；未注入时按 0 计，不做任何推测。

结算口径：
- 订单存在即为 `pending`（在途资金）；
- `orders.order_status == 'completed'` 转为 `settled`，并固定 `settled_at`；
- `cancelled` / `refund_cancelled` 视为货款已退回，收入按 0 计。
"""

import asyncio
import math
from datetime import datetime, timezone

from loguru import logger

from config import config

LOSS_ALERT = "LOSS_ALERT"
CLOSED_STATUSES = frozenset({"cancelled", "refund_cancelled"})
SETTLED_ORDER_STATUS = "completed"


def parse_amount(value) -> float:
    """把 orders.amount 这类文本金额转成元；空值/占位符按 0 计。"""
    if value is None:
        return 0.0
    text = str(value).strip()
    for token in ("¥", "￥", ","):
        text = text.replace(token, "")
    text = text.strip()
    if text in ("", "N/A", "None", "null"):
        return 0.0
    try:
        return round(float(text), 2)
    except (TypeError, ValueError):
        return 0.0


def _stamp(moment=None) -> str:
    if moment is None:
        moment = datetime.now(timezone.utc)
    if moment.utcoffset() is None:
        raise ValueError("财务核算时间须包含时区")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _default_alert(event: str, detail: str) -> None:
    logger.warning("{} {}", event, detail)
    try:
        import notify_service

        notify_service.send_event("risk", event, detail, level="warning")
    except Exception as exc:
        logger.warning("财务预警通知发送失败，已跳过: {}", type(exc).__name__)


class FinancialProfitLedger:
    def __init__(self, database, *, platform_fee_rate: float = 0.0,
                 shipping_cost: float = 0.0, voucher_cost=None,
                 alert=None, scan_days: int = 30):
        try:
            rate = float(platform_fee_rate)
            shipping = float(shipping_cost)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("财务参数无效") from exc
        if not math.isfinite(rate) or rate < 0 or rate >= 1:
            raise ValueError("平台费率须在 [0, 1) 内")
        if not math.isfinite(shipping) or shipping < 0:
            raise ValueError("运费成本不能为负数")
        if type(scan_days) is not int or scan_days < 1:
            raise ValueError("核算窗口须为不小于 1 天的整数")
        self.database = database
        self.platform_fee_rate = rate
        self.shipping_cost = round(shipping, 2)
        self.voucher_cost = voucher_cost
        self.alert = alert or _default_alert
        self.scan_days = scan_days

    def _upstream_cost(self, order_id: str, is_voucher: bool) -> float:
        cost = self.database.get_dropship_cost_for_order(order_id)
        if cost <= 0 and is_voucher and callable(self.voucher_cost):
            try:
                cost = float(self.voucher_cost(order_id) or 0)
            except Exception as exc:
                logger.warning("订单 {} 卡密成本解析失败: {}", order_id, type(exc).__name__)
                cost = 0.0
        return round(max(cost, 0.0), 2)

    def collect(self, order_id: str, now=None):
        order = self.database.get_order_by_id(order_id)
        if order is None:
            return None
        moment_stamp = _stamp(now)
        status = order.get("order_status") or "unknown"
        revenue = parse_amount(order.get("amount"))
        if status in CLOSED_STATUSES:
            revenue = 0.0

        is_voucher = self.database.is_voucher_profit_order(order_id)
        upstream = self._upstream_cost(order_id, is_voucher)
        shipping = self.shipping_cost
        fee = round(revenue * self.platform_fee_rate, 2)
        net = round(revenue - upstream - shipping - fee, 2)
        margin = round(net / revenue, 4) if revenue > 0 else 0.0
        settlement = "settled" if status == SETTLED_ORDER_STATUS else "pending"

        previous = self.database.get_financial_profit_record(order_id)
        record = {
            "order_id": order_id,
            "account_id": order.get("cookie_id"),
            "item_id": order.get("item_id"),
            "revenue": revenue,
            "upstream_cost": upstream,
            "shipping_cost": shipping,
            "platform_fee": fee,
            "net_profit": net,
            "margin_rate": margin,
            "settlement_status": settlement,
            "created_at": moment_stamp,
            "settled_at": moment_stamp if settlement == "settled" else None,
        }
        self.database.upsert_financial_profit_record(record)

        if net < 0 and (previous is None or float(previous["net_profit"]) >= 0):
            self.alert(
                LOSS_ALERT,
                f"订单 {order_id} 净利 {net:.2f} 元"
                f"（收入 {revenue:.2f} / 上游 {upstream:.2f} / 运费 {shipping:.2f}"
                f" / 平台费 {fee:.2f}）",
            )
        return self.database.get_financial_profit_record(order_id)

    def scan_once(self, now=None):
        result = {"scanned": 0, "settled": 0, "loss_alerts": 0}
        for order in self.database.list_orders_for_profit_ledger(self.scan_days):
            order_id = order["order_id"]
            before = self.database.get_financial_profit_record(order_id)
            try:
                record = self.collect(order_id, now=now)
            except Exception as exc:
                logger.warning("订单 {} 利润核算失败: {}", order_id, type(exc).__name__)
                continue
            if record is None:
                continue
            result["scanned"] += 1
            if record["settlement_status"] == "settled":
                result["settled"] += 1
            if (record["net_profit"] < 0
                    and (before is None or float(before["net_profit"]) >= 0)):
                result["loss_alerts"] += 1
        return result


async def run_scheduler(database, stop):
    settings = config.get("FINANCIAL_PROFIT") or {}
    try:
        interval = max(60, int(settings.get("scan_interval_seconds", 300)))
        rate = float(settings.get("platform_fee_rate", 0.0) or 0.0)
        shipping = float(settings.get("shipping_cost_per_order", 0.0) or 0.0)
        scan_days = int(settings.get("scan_days", 30) or 30)
    except (TypeError, ValueError):
        logger.error("财务利润核算配置无效")
        return
    try:
        ledger = FinancialProfitLedger(
            database, platform_fee_rate=rate, shipping_cost=shipping,
            scan_days=scan_days,
        )
    except ValueError as exc:
        logger.error("财务利润核算配置无效: {}", exc)
        return
    while not stop.is_set():
        try:
            await asyncio.to_thread(ledger.scan_once)
        except Exception as exc:
            logger.warning("财务利润核算中断（{}）", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except asyncio.TimeoutError:
            continue
