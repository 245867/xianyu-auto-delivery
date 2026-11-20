"""
交易后自动回评与好评赠品（拆解 54）
====================================

账本 `auto_review_records` 记录每单的买卖双方评价状态与赠品发放标记；
调度器只在**确认买家给了好评**后回评，退款/争议/差评一律 `RATE_SUSPENDED`，
不做"互评洗白"。

接口口径（已核实，见 care_message.py 顶部）：

- 待评价列表 `mtop.taobao.idle.merchant.rate.list` v1.0；
- 提交评价 `mtop.taobao.idle.rate.create` v4.0（v1.0 不存在）。

买家评价读取通道由 `buyer_rating` 注入，默认从待评价列表读取
`buyerRateStatus` / `rateResult`（"4"+"1" 才是好评）。
返回 `{"status": "good" | "bad" | "pending" | "unknown"}`：
`pending` 表示买家还没评，`unknown` 表示读取失败；两者都不回评。
"""

import asyncio
from typing import Any, Dict

from loguru import logger

from config import config

RATE_SUSPENDED = "RATE_SUSPENDED"
DEFAULT_FEEDBACK = "感谢支持，非常爽快的好买家！"
DEFAULT_INTERVAL_SECONDS = 600


def _default_alert(title: str, content: str) -> None:
    logger.warning("{}: {}", title, content)
    try:
        import notify_service

        notify_service.send_event("risk", title, content, level="warn")
    except Exception as exc:
        logger.warning("自动回评预警通知失败，已跳过: {}", type(exc).__name__)


def _default_has_refund(order_id: str) -> bool:
    try:
        import refund_aftercare

        return refund_aftercare.has_record(order_id)
    except Exception as exc:
        logger.warning("退款账本不可读，按存在退款处理: {}", type(exc).__name__)
        return True


class AutoReviewScheduler:
    """按订单推进：退款拦截 -> 买家评价判定 -> 自动回评 -> 好评赠品。"""

    def __init__(self, database, *, buyer_rating, find_pending, submit_rate,
                 gift_card, send_gift, feedback: str = DEFAULT_FEEDBACK,
                 max_per_scan: int = 50, alert=None, has_refund=None):
        self.database = database
        self.buyer_rating = buyer_rating
        self.find_pending = find_pending
        self.submit_rate = submit_rate
        self.gift_card = gift_card
        self.send_gift = send_gift
        self.feedback = str(feedback or DEFAULT_FEEDBACK)
        self.max_per_scan = max(1, int(max_per_scan))
        self.alert = alert or _default_alert
        self.has_refund = has_refund or _default_has_refund

    def _rating_status(self, order: Dict[str, Any]) -> str:
        if not callable(self.buyer_rating):
            return "unknown"
        try:
            result = self.buyer_rating(order)
        except Exception as exc:
            logger.warning("订单 {} 买家评价读取异常，本轮不回评：{}",
                           order.get("order_id"), type(exc).__name__)
            return "unknown"
        if not isinstance(result, dict):
            return "unknown"
        status = str(result.get("status") or "").strip().lower()
        return status if status in ("good", "bad", "pending") else "unknown"

    def _suspend(self, order_id: str, reason: str, buyer_status: str = None) -> None:
        self.database.update_auto_review_record(
            order_id, buyer_rate_status=buyer_status, seller_rate_status=RATE_SUSPENDED,
        )
        logger.warning("订单 {} 自动回评已熔断：{}", order_id, reason)

    def _handle_gift(self, order_id: str, account_id: str, buyer_id: str,
                     item_id: str) -> int:
        try:
            text = self.gift_card(account_id, item_id)
        except Exception as exc:
            logger.warning("订单 {} 读取好评赠品配置失败：{}", order_id, type(exc).__name__)
            return 0
        text = str(text or "").strip()
        if not text or not buyer_id:
            return 0
        if not self.database.claim_review_gift(order_id):
            return 0
        try:
            ok = bool(self.send_gift(account_id, buyer_id, item_id, text))
        except Exception as exc:
            logger.warning("订单 {} 好评赠品发送异常：{}", order_id, type(exc).__name__)
            ok = False
        if not ok:
            self.database.release_review_gift(order_id)
            logger.warning("订单 {} 好评赠品发送失败，已释放占位待下次重试", order_id)
            return 0
        return 1

    def scan_once(self) -> Dict[str, int]:
        result = {"scanned": 0, "rated": 0, "suspended": 0, "gifted": 0}
        if not callable(self.buyer_rating):
            logger.error("自动回评缺少买家评价读取通道，本轮不执行")
            return result

        candidates = self.database.list_auto_review_candidates(self.max_per_scan)
        for order in candidates:
            result["scanned"] += 1
            order_id = str(order.get("order_id") or "").strip()
            account_id = str(order.get("cookie_id") or "").strip()
            buyer_id = str(order.get("buyer_id") or "").strip()
            item_id = str(order.get("item_id") or "").strip()
            if not order_id or not account_id:
                logger.warning("自动回评跳过缺少订单号/账号的记录：{}", order_id or "-")
                continue

            record = self.database.ensure_auto_review_record(order_id, account_id, buyer_id)
            if record is None:
                continue

            try:
                refunded = bool(self.has_refund(order_id))
            except Exception as exc:
                logger.warning("订单 {} 退款账本查询异常，按存在退款处理：{}",
                               order_id, type(exc).__name__)
                refunded = True
            if refunded:
                if record["seller_rate_status"] != RATE_SUSPENDED:
                    self._suspend(order_id, "存在退款记录或争议售后")
                    result["suspended"] += 1
                continue

            rating = self._rating_status(order)
            if rating == "bad":
                if record["seller_rate_status"] != RATE_SUSPENDED:
                    self._suspend(order_id, "买家评价为差评风险", buyer_status="bad")
                    self.alert("自动回评已熔断",
                               f"订单 {order_id} 的买家评价存在差评风险，已禁止互评")
                    result["suspended"] += 1
                continue
            if rating == "pending":
                logger.info("订单 {} 买家尚未评价，等待下一轮", order_id)
                continue
            if rating != "good":
                logger.warning("订单 {} 买家评价状态未知，本轮不回评", order_id)
                continue

            self.database.update_auto_review_record(order_id, buyer_rate_status="good")

            if record["seller_rate_status"] == "unrated":
                if not self.database.claim_auto_review_rate(order_id):
                    logger.info("订单 {} 回评占位已被占用，跳过", order_id)
                    continue
                pending = self.find_pending(account_id, order_id)
                if not isinstance(pending, dict) or pending.get("ok") is not True:
                    self.database.release_auto_review_rate(order_id)
                    logger.warning("订单 {} 待评价列表读取失败，已撤销占位", order_id)
                    continue
                if pending.get("found") is not True:
                    self.database.update_auto_review_record(
                        order_id, seller_rate_status="skipped")
                    logger.warning("订单 {} 不在平台待评价列表，跳过回评", order_id)
                    continue
                trade_id = str(pending.get("trade_id") or "").strip()
                if not trade_id:
                    self.database.release_auto_review_rate(order_id)
                    logger.warning("订单 {} 待评价列表未返回 tradeId，已撤销占位", order_id)
                    continue
                reply = self.submit_rate(account_id, trade_id, self.feedback)
                if isinstance(reply, dict) and reply.get("ok") is True:
                    self.database.update_auto_review_record(
                        order_id, seller_rate_status="rated")
                    result["rated"] += 1
                else:
                    self.database.update_auto_review_record(
                        order_id, seller_rate_status="failed")
                    kind = reply.get("kind") if isinstance(reply, dict) else ""
                    logger.warning("订单 {} 回评失败（{}）", order_id, kind or "unknown")
                    if kind in ("risk", "auth"):
                        break

            if record["gift_sent"] is False:
                result["gifted"] += self._handle_gift(
                    order_id, account_id, buyer_id, item_id,
                )

        return result


def _pending_reader(database, app_key: str):
    def find_pending(account_id: str, order_id: str):
        import care_message

        info = database.get_cookie_details(account_id) or {}
        cookie = str(info.get("value") or "")
        if not cookie:
            return {"ok": False, "found": False, "trade_id": "", "error": "无可用 Cookie"}
        return care_message.find_in_pending(
            cookie, order_id, app_key=app_key, cookie_id=account_id,
        )

    return find_pending


def _default_buyer_rating(database, app_key: str):
    """从平台待评价列表读取 buyerRateStatus / rateResult。"""

    def buyer_rating(order: Dict[str, Any]):
        import care_message

        account_id = str(order.get("cookie_id") or "")
        order_id = str(order.get("order_id") or "")
        info = database.get_cookie_details(account_id) or {}
        cookie = str(info.get("value") or "")
        if not cookie or not order_id:
            return {"status": "unknown"}
        found = care_message.find_in_pending(
            cookie, order_id, app_key=app_key, cookie_id=account_id,
        )
        if not isinstance(found, dict) or found.get("ok") is not True:
            return {"status": "unknown"}
        if found.get("found") is not True:
            return {"status": "pending"}
        return {"status": care_message.buyer_rate_verdict(found.get("row"))}

    return buyer_rating


def _rate_submitter(database, app_key: str):
    def submit_rate(account_id: str, trade_id: str, feedback: str):
        import care_message
        import rate_limiter

        allowed, retry = rate_limiter.acquire("write", account_id)
        if not allowed:
            return {"ok": False, "kind": "rate_limited", "retry": retry}
        info = database.get_cookie_details(account_id) or {}
        cookie = str(info.get("value") or "")
        if not cookie:
            return {"ok": False, "kind": "auth", "error": "无可用 Cookie"}
        return care_message.submit_rate(
            cookie, trade_id, feedback, app_key=app_key, cookie_id=account_id,
        )

    return submit_rate


def _gift_sender(account_id: str, buyer_id: str, item_id: str, text: str) -> bool:
    try:
        from XianyuAutoAsync import XianyuLive

        instance = XianyuLive.get_instance(account_id)
        if instance is None:
            logger.warning("账号 {} 不在线，好评赠品暂不发送", account_id)
            return False
        asyncio.run(instance.send_msg_once(buyer_id, item_id, text))
        return True
    except Exception as exc:
        logger.warning("账号 {} 好评赠品发送失败：{}", account_id, type(exc).__name__)
        return False


async def run_scheduler(database, stop, *, buyer_rating=None, send_gift=None):
    settings = config.get("AUTO_REVIEW") or {}
    app_key = str(config.get("APP_CONFIG.app_key") or "")
    if not callable(buyer_rating):
        buyer_rating = _default_buyer_rating(database, app_key)
    try:
        interval = max(60, int(settings.get("scan_interval_seconds", DEFAULT_INTERVAL_SECONDS)))
        max_per_scan = max(1, int(settings.get("max_per_scan", 50)))
    except (TypeError, ValueError):
        logger.error("自动回评调度配置无效")
        return

    scheduler = AutoReviewScheduler(
        database,
        buyer_rating=buyer_rating,
        find_pending=_pending_reader(database, app_key),
        submit_rate=_rate_submitter(database, app_key),
        gift_card=database.get_item_review_gift_card,
        send_gift=send_gift or _gift_sender,
        feedback=str(settings.get("feedback") or DEFAULT_FEEDBACK),
        max_per_scan=max_per_scan,
    )

    while not stop.is_set():
        try:
            await asyncio.to_thread(scheduler.scan_once)
        except Exception as exc:
            logger.warning("自动回评扫描中断（{}）", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except asyncio.TimeoutError:
            continue
