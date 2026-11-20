"""
IM 限时议价倒计时逼单引擎（拆解 55）
=====================================

议价被接受后生成 15 分钟会话：

- 会话有效期内买家创建待付款订单 → 调用 `apply_concession` 对该单改价；
- 到期仍未成交 → 置为 `expired`，调用 `restore_price` 恢复原价并
  `release_stock` 释放库存锁定；
- 平台改价/库存接口的请求参数尚未取得样本，因此三个平台动作全部由调用方注入，
  引擎只负责状态机与幂等，不在内部拼协议。
"""

import asyncio
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from loguru import logger

from config import config

SESSION_TTL_SECONDS = 900


def _default_alert(title: str, content: str) -> None:
    logger.warning("{}: {}", title, content)
    try:
        import notify_service

        notify_service.send_event("risk", title, content, level="warn")
    except Exception as exc:
        logger.warning("限时议价预警通知失败，已跳过: {}", type(exc).__name__)


def _moment(value) -> datetime:
    moment = value if isinstance(value, datetime) else datetime.now(timezone.utc)
    if moment.utcoffset() is None:
        raise ValueError("议价时间须包含时区")
    return moment.astimezone(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


class BargainCloser:
    def __init__(self, database, *, apply_concession, restore_price, release_stock,
                 ttl_seconds: int = SESSION_TTL_SECONDS, alert=None):
        if type(ttl_seconds) is not int or ttl_seconds < 60:
            raise ValueError("议价会话时长必须为不小于 60 秒的整数")
        self.database = database
        self.apply_concession = apply_concession
        self.restore_price = restore_price
        self.release_stock = release_stock
        self.ttl_seconds = ttl_seconds
        self.alert = alert or _default_alert

    def create_session(self, account_id: str, buyer_id: str, item_id: str,
                       original_price: float, concession_price: float,
                       now=None) -> Dict[str, Any]:
        account_id = str(account_id or "").strip()
        buyer_id = str(buyer_id or "").strip()
        item_id = str(item_id or "").strip()
        if not account_id or not buyer_id or not item_id:
            raise ValueError("议价会话缺少账号/买家/商品")
        try:
            original = float(original_price)
            concession = float(concession_price)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("议价价格无效") from exc
        if (not math.isfinite(original) or not math.isfinite(concession)
                or original <= 0 or concession <= 0 or concession >= original):
            raise ValueError("议价价格必须为正数，且优惠价低于原价")

        moment = _moment(now)
        stamp = _stamp(moment)
        existing = self.database.find_active_bargain_session(
            account_id, buyer_id, item_id, stamp)
        if existing is not None:
            return existing

        expires_at = _stamp(moment + timedelta(seconds=self.ttl_seconds))
        return self.database.create_bargain_session(
            uuid.uuid4().hex, account_id, buyer_id, item_id,
            original, concession, expires_at, created_at=stamp,
        )

    def _restore(self, session: Dict[str, Any]) -> bool:
        ok = True
        if callable(self.restore_price):
            try:
                ok = bool(self.restore_price(
                    session["account_id"], session["item_id"],
                    float(session["original_price"]),
                )) and ok
            except Exception as exc:
                logger.warning("会话 {} 恢复原价异常：{}",
                               session["session_id"], type(exc).__name__)
                ok = False
        else:
            logger.warning("会话 {} 未配置恢复原价通道，仅记录到期", session["session_id"])

        if callable(self.release_stock):
            try:
                ok = bool(self.release_stock(
                    session["account_id"], session["item_id"],
                )) and ok
            except Exception as exc:
                logger.warning("会话 {} 释放库存异常：{}",
                               session["session_id"], type(exc).__name__)
                ok = False
        else:
            logger.warning("会话 {} 未配置释放库存通道，仅记录到期", session["session_id"])
        return ok

    def scan_once(self, now=None) -> Dict[str, int]:
        moment = _moment(now)
        stamp = _stamp(moment)
        result = {"expired": 0, "consumed": 0}

        for session in self.database.list_due_bargain_sessions(stamp):
            if not self._restore(session):
                self.alert("限时议价到期恢复失败",
                           f"会话 {session['session_id']} 恢复原价/库存失败，下轮重试")
                continue
            if self.database.mark_bargain_session_expired(session["session_id"]):
                result["expired"] += 1

        if not callable(self.apply_concession):
            if self.database.list_bargain_sessions("active"):
                logger.warning("限时议价未配置订单改价通道，成交联动暂停")
            return result

        for session in self.database.list_bargain_sessions("active"):
            if session["expires_at"] <= stamp:
                continue
            order = self.database.find_unpaid_order_for_bargain(
                session["account_id"], session["buyer_id"], session["item_id"],
                session["created_at"],
            )
            if order is None:
                continue
            if not self.database.consume_bargain_session(session["session_id"]):
                continue
            try:
                applied = bool(self.apply_concession(
                    order["order_id"], session["account_id"],
                    float(session["concession_price"]),
                ))
            except Exception as exc:
                logger.warning("订单 {} 应用议价优惠异常：{}",
                               order["order_id"], type(exc).__name__)
                applied = False
            if applied:
                result["consumed"] += 1
            else:
                self.database.fail_bargain_session(session["session_id"])
                self.alert("限时议价改价失败",
                           f"订单 {order['order_id']} 未能应用优惠价，会话已标记 failed")

        return result


async def run_scheduler(database, stop, *, apply_concession=None,
                        restore_price=None, release_stock=None):
    settings = config.get("BARGAIN_CLOSER") or {}
    try:
        interval = max(30, int(settings.get("scan_interval_seconds", 60)))
        ttl_seconds = int(settings.get("ttl_seconds", SESSION_TTL_SECONDS))
    except (TypeError, ValueError):
        logger.error("限时议价调度配置无效")
        return
    closer = BargainCloser(
        database,
        apply_concession=apply_concession,
        restore_price=restore_price,
        release_stock=release_stock,
        ttl_seconds=ttl_seconds,
    )
    while not stop.is_set():
        try:
            await asyncio.to_thread(closer.scan_once)
        except Exception as exc:
            logger.warning("限时议价扫描中断（{}）", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except asyncio.TimeoutError:
            continue


def build_platform_channels():
    """
    生产链路只绑定订单级改价（order.editPrice）：本引擎从不修改挂牌价，
    因此 restore_price 保持为空；库存预占由闲鱼下单自动完成，本地仅靠
    会话状态流转释放，无需额外的 release_stock 调用。
    开放平台未启用/未配置时不返回任何写动作，调度器只推进会话状态。
    """
    try:
        import goofish_open
        cfg = goofish_open.get_config()
    except Exception:
        return {}
    if not cfg.get("enabled") or not goofish_open.is_configured(cfg):
        return {}

    def apply_concession(order_id, _account_id, price):
        goofish_open.edit_order_price(str(order_id), float(price), allow_write=True)
        return True

    return {"apply_concession": apply_concession}
