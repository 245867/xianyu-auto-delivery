"""
待付款订单三阶智能催付调度器（扫描 + 状态推进）

职责：
- 幂等建立催付账本并推进三阶催付状态机
- 买家回复熔断、夜间静默、频次硬顶、终态转换
- 超时结案释放库存（可选通道）
- 仅在消息发送成功后推进阶段；失败或无通道保持原状态
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from loguru import logger

import order_chaser

SendFn = Optional[Callable[[str, str, str, str], bool]]
ReleaseFn = Optional[Callable[[str, str, str], bool]]


def _moment(now: Optional[datetime]) -> datetime:
    return now if now is not None else datetime.now()


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except Exception:
        return None


def _order_age_minutes(order: Dict[str, Any], chase: Dict[str, Any], now: datetime) -> float:
    created = _parse_dt(order.get("created_at")) or _parse_dt(chase.get("created_at")) or now
    age = (now - created).total_seconds() / 60.0
    return age if age >= 0 else 0.0


def _is_paid_or_closed(status: Any) -> bool:
    normalized = str(status or "").strip().lower()
    return normalized not in {
        "processing",
        "waiting",
        "wait_buyer_pay",
        "unpaid",
        "waitbuyerpay",
        "pending",
    }


class OrderChaseScheduler:
    def __init__(
        self,
        database,
        send_message_fn: SendFn = None,
        release_stock_fn: ReleaseFn = None,
        *,
        stage_1_delay_min: float = 3.0,
        stage_2_delay_min: float = 10.0,
        timeout_expire_min: float = 30.0,
        max_chase_attempts: int = 2,
        quiet_start_hour: int = 23,
        quiet_end_hour: int = 8,
    ):
        self.database = database
        self.send_message_fn = send_message_fn
        self.release_stock_fn = release_stock_fn
        self.stage_1_delay_min = float(stage_1_delay_min)
        self.stage_2_delay_min = float(stage_2_delay_min)
        self.timeout_expire_min = float(timeout_expire_min)
        self.max_chase_attempts = int(max_chase_attempts)
        self.quiet_start_hour = int(quiet_start_hour)
        self.quiet_end_hour = int(quiet_end_hour)

    def _try_send(self, account_id: str, buyer_id: str, item_id: str, text: str) -> bool:
        if not callable(self.send_message_fn):
            return False
        try:
            return bool(self.send_message_fn(account_id, buyer_id, item_id, text))
        except Exception as exc:
            logger.warning("订单催付消息发送异常（{}）", type(exc).__name__)
            return False

    def _try_release(self, order_id: str, account_id: str, item_id: str) -> bool:
        if not callable(self.release_stock_fn):
            return True
        try:
            return bool(self.release_stock_fn(order_id, account_id, item_id))
        except Exception as exc:
            logger.warning("订单催付释放库存异常（{}）", type(exc).__name__)
            return False

    def scan_once(self, now: Optional[datetime] = None) -> Dict[str, int]:
        moment = _moment(now)
        stamp = _stamp(moment)
        result = {"converted": 0, "chased": 0, "expired": 0, "suspended": 0}

        # 幂等建立待付款订单的催付账本
        for order in self.database.list_unpaid_orders_for_chase(limit=200):
            try:
                self.database.ensure_unpaid_order_chase(
                    order_id=order.get("order_id"),
                    account_id=order.get("account_id"),
                    item_id=order.get("item_id"),
                    buyer_id=order.get("buyer_id"),
                    order_amount=order.get("amount"),
                    created_at=order.get("order_created_at"),
                )
            except Exception as exc:
                logger.warning("建立催付账本失败（{}）", type(exc).__name__)

        for chase in self.database.list_unpaid_orders_for_chase(limit=200):
            if str(chase.get("chase_state") or "") != "active":
                continue

            order_id = str(chase.get("order_id") or "")
            account_id = str(chase.get("account_id") or "")
            buyer_id = str(chase.get("buyer_id") or "")
            item_id = str(chase.get("item_id") or "")
            chase_stage = int(chase.get("chase_stage") or 0)
            last_chased_at = chase.get("last_chased_at")

            order_created_at = chase.get("order_created_at")
            age_minutes = _order_age_minutes(
                {"created_at": order_created_at}, chase, moment
            )
            since = chase.get("chase_created_at") or order_created_at
            buyer_has_replied = False
            if account_id and buyer_id and since:
                try:
                    buyer_has_replied = bool(
                        self.database.has_buyer_message_since(account_id, buyer_id, str(since))
                    )
                except Exception:
                    buyer_has_replied = False

            is_night_time = order_chaser.is_night_quiet_hours(
                moment, self.quiet_start_hour, self.quiet_end_hour
            )
            is_terminal_status = _is_paid_or_closed(chase.get("order_status"))
            action, reason = order_chaser.decide_chase_action(
                order_age_minutes=age_minutes,
                chase_count=chase_stage,
                buyer_has_replied=buyer_has_replied,
                is_terminal_status=is_terminal_status,
                is_night_time=is_night_time,
                stage_1_delay_min=self.stage_1_delay_min,
                stage_2_delay_min=self.stage_2_delay_min,
                timeout_expire_min=self.timeout_expire_min,
                max_chase_attempts=self.max_chase_attempts,
            )
            logger.debug("催付决策 order={} action={} reason={}", order_id, action, reason)

            if action == "SKIP_TERMINATED":
                if self.database.update_unpaid_order_chase(order_id, chase_state="converted"):
                    result["converted"] += 1
                continue

            if action == "SKIP_BUYER_TALKING":
                if self.database.update_unpaid_order_chase(order_id, chase_state="suspended"):
                    result["suspended"] += 1
                continue

            if action in ("CHASE_STAGE_1", "CHASE_STAGE_2"):
                stage = 1 if action == "CHASE_STAGE_1" else 2
                text = order_chaser.get_chase_message(stage)
                if self._try_send(account_id, buyer_id, item_id, text):
                    self.database.update_unpaid_order_chase(
                        order_id, chase_stage=stage, last_chased_at=stamp
                    )
                    result["chased"] += 1
                continue

            if action == "EXPIRE_RELEASE":
                released = self._try_release(order_id, account_id, item_id)
                if released and self.database.update_unpaid_order_chase(
                        order_id, chase_state="expired"):
                    result["expired"] += 1
                continue

            # SKIP_NIGHT_SILENT / WAIT_NEXT_WINDOW / SKIP_MAX_ATTEMPTS / 其他：保持原状态

        return result


def _default_send(account_id: str, buyer_id: str, item_id: str, text: str) -> bool:
    try:
        from XianyuAutoAsync import XianyuLive
    except Exception as exc:
        logger.warning("催付发送通道不可用（{}）", type(exc).__name__)
        return False

    instance = XianyuLive.get_instance(account_id)
    if instance is None:
        logger.warning("账号 {} 不在线，待付款催付暂不发送", account_id)
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    async def _send():
        return await instance.send_msg_once(buyer_id, item_id, text)

    try:
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(_send(), loop)
            return bool(future.result(timeout=30))
        return bool(asyncio.run(_send()))
    except Exception as exc:
        logger.warning("待付款催付发送失败（{}）", type(exc).__name__)
        return False


async def run_scheduler(database, stop, *, send_message_fn: SendFn = None, release_stock_fn: ReleaseFn = None):
    try:
        from config import config

        settings = config.get("ORDER_CHASER") or {}
    except Exception:
        return
    if settings.get("enabled") is not True:
        return
    try:
        interval = max(30, int(settings.get("scan_interval_seconds", 60)))
        stage_1 = float(settings.get("stage_1_delay_min", 3.0))
        stage_2 = float(settings.get("stage_2_delay_min", 10.0))
        timeout_expire = float(settings.get("timeout_expire_min", 30.0))
        max_attempts = int(settings.get("max_chase_attempts", 2))
        quiet_start = int(settings.get("quiet_start_hour", 23))
        quiet_end = int(settings.get("quiet_end_hour", 8))
    except (TypeError, ValueError):
        logger.error("待付款催付调度配置无效")
        return

    scheduler = OrderChaseScheduler(
        database,
        send_message_fn=send_message_fn or _default_send,
        release_stock_fn=release_stock_fn,
        stage_1_delay_min=stage_1,
        stage_2_delay_min=stage_2,
        timeout_expire_min=timeout_expire,
        max_chase_attempts=max_attempts,
        quiet_start_hour=quiet_start,
        quiet_end_hour=quiet_end,
    )
    while not stop.is_set():
        try:
            await asyncio.to_thread(scheduler.scan_once)
        except Exception as exc:
            logger.warning("待付款催付扫描中断（{}）", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except asyncio.TimeoutError:
            continue

