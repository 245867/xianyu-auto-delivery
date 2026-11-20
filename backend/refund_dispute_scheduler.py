"""恶意退款取证、驳回与仲裁状态机。"""

import asyncio
from datetime import datetime
from typing import Any, Dict

from loguru import logger

import goofish_open
from dispute_evidence_pack import dispute_evidence_pack


DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_MAX_PER_SCAN = 50
DEFAULT_REFUSE_REASON = ""
DEFAULT_REFUSE_EXPLANATION = "商品已实际发货/已核销，买家已实际获得商品或服务"


def _default_alert(title: str, content: str) -> None:
    logger.warning("{}: {}", title, content)
    try:
        import notify_service

        notify_service.send_event("risk", title, content, level="warn")
    except Exception as exc:
        logger.warning("退款争议预警通知失败，已跳过: {}", type(exc).__name__)


def _refusal_succeeded(response) -> bool:
    if not isinstance(response, dict):
        return False
    if response.get("ok") is True:
        return True
    if response.get("code") in (0, "0"):
        data = response.get("data")
        if isinstance(data, dict) and data.get("success") is False:
            return False
        return True
    return False


class RefundDisputeScheduler:
    def __init__(self, database, evidence_pack=None, refuse_refund=None,
                 *, auto_refuse=True, alert=None, max_per_scan=50,
                 refuse_reason=DEFAULT_REFUSE_REASON,
                 refuse_explanation=DEFAULT_REFUSE_EXPLANATION):
        self.database = database
        self.evidence_pack = evidence_pack or (
            lambda order: dispute_evidence_pack(database, order)
        )
        self.refuse_refund = refuse_refund or goofish_open.refuse_order_refund
        self.auto_refuse = bool(auto_refuse)
        self.alert = alert or _default_alert
        self.max_per_scan = max(1, int(max_per_scan))
        self.refuse_reason = str(refuse_reason or DEFAULT_REFUSE_REASON)
        self.refuse_explanation = str(
            refuse_explanation or DEFAULT_REFUSE_EXPLANATION
        )

    def scan_once(self, now=None):
        result = {
            "scanned": 0,
            "collected": 0,
            "refused": 0,
            "won": 0,
            "lost": 0,
        }
        current = datetime.now() if now is None else now
        orders = self.database.list_refund_dispute_candidates(self.max_per_scan)
        active = {
            row["order_id"]: row
            for row in self.database.list_active_disputes(self.max_per_scan)
        }
        for order in orders:
            order_id = str(order.get("order_id") or "")
            if not order_id:
                continue
            result["scanned"] += 1
            case = active.get(order_id)
            if case is None:
                pack = self.evidence_pack(order)
                self.database.upsert_dispute_case(
                    order_id,
                    order.get("cookie_id"),
                    order.get("item_id"),
                    order.get("buyer_id"),
                    pack.get("refund_amount", order.get("amount")),
                    pack.get("refund_reason", ""),
                    "evidence_collected",
                    pack.get("evidence", {}),
                    pack.get("statement_text", ""),
                    created_at=current.strftime("%Y-%m-%d %H:%M:%S"),
                )
                result["collected"] += 1
                case = self.database.get_dispute_case(order_id)

            state = case.get("dispute_state")
            if state == "evidence_collected":
                pack = self._case_pack(case)
                if not (self.auto_refuse and pack.get("auto_refuse") is True):
                    self.alert(
                        "退款争议证据不足",
                        f"订单 {order_id} 暂不自动驳回，风险: {pack.get('risk_flags')}",
                    )
                    continue
                if not self.refuse_reason:
                    self.alert(
                        "退款争议未配置驳回原因",
                        f"订单 {order_id} 已取证，但 REFUND_DISPUTE.refuse_reason 为空，"
                        "请填写平台支持的拒绝原因后再启用自动驳回",
                    )
                    continue
                try:
                    response = self.refuse_refund(
                        order_id,
                        self.refuse_reason,
                        self.refuse_explanation,
                        allow_write=True,
                    )
                except Exception as exc:
                    logger.warning("退款争议驳回调用失败: {}", type(exc).__name__)
                    response = {"ok": False}
                if not _refusal_succeeded(response):
                    self.alert(
                        "退款争议驳回失败",
                        f"订单 {order_id} 驳回失败，将保留证据后重试",
                    )
                    continue
                self.database.upsert_dispute_case(
                    order_id,
                    case.get("account_id"),
                    case.get("item_id"),
                    case.get("buyer_id"),
                    case.get("refund_amount"),
                    case.get("refund_reason"),
                    "refused",
                    pack.get("evidence", {}),
                    pack.get("statement_text", case.get("statement_text", "")),
                    created_at=case.get("created_at"),
                )
                result["refused"] += 1
            elif state == "refused":
                self.database.upsert_dispute_case(
                    order_id,
                    case.get("account_id"),
                    case.get("item_id"),
                    case.get("buyer_id"),
                    case.get("refund_amount"),
                    case.get("refund_reason"),
                    "arbitrating",
                    case.get("evidence_json"),
                    case.get("statement_text"),
                    created_at=case.get("created_at"),
                )

        for case in self.database.list_active_disputes(self.max_per_scan):
            order_id = str(case.get("order_id") or "")
            order = self.database.get_order_by_id(order_id)
            if order is None:
                continue
            status = str(order.get("order_status") or "")
            if status in ("pending_ship", "completed", "refund_cancelled"):
                self.database.upsert_dispute_case(
                    order_id, case.get("account_id"), case.get("item_id"),
                    case.get("buyer_id"), case.get("refund_amount"),
                    case.get("refund_reason"), "won", case.get("evidence_json"),
                    case.get("statement_text"),
                    created_at=case.get("created_at"),
                )
                result["won"] += 1
            elif status == "cancelled":
                self.database.upsert_dispute_case(
                    order_id, case.get("account_id"), case.get("item_id"),
                    case.get("buyer_id"), case.get("refund_amount"),
                    case.get("refund_reason"), "lost", case.get("evidence_json"),
                    case.get("statement_text"),
                    created_at=case.get("created_at"),
                )
                result["lost"] += 1
        return result

    @staticmethod
    def _case_pack(case: Dict[str, Any]) -> Dict[str, Any]:
        import json

        try:
            evidence = json.loads(case.get("evidence_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        return {
            "evidence": evidence,
            "statement_text": case.get("statement_text", ""),
            "refund_amount": case.get("refund_amount"),
            "refund_reason": case.get("refund_reason", ""),
            "auto_refuse": bool(
                evidence.get("delivery", {}).get("sent_count", 0) > 0
                or evidence.get("logistics", {}).get("delivered") is True
                or evidence.get("logistics", {}).get("in_transit") is True
                or evidence.get("chat", {}).get("matched") is True
            ),
            "risk_flags": [],
        }


async def run_scheduler(database, stop):
    from config import config

    settings = config.get("REFUND_DISPUTE") or {}
    if settings.get("enabled") is not True:
        return
    try:
        interval = max(30, int(settings.get("scan_interval_seconds", DEFAULT_INTERVAL_SECONDS)))
        max_per_scan = max(1, int(settings.get("max_per_scan", DEFAULT_MAX_PER_SCAN)))
    except (TypeError, ValueError):
        logger.error("退款争议调度配置无效")
        return
    scheduler = RefundDisputeScheduler(
        database,
        auto_refuse=settings.get("auto_refuse", True) is True,
        max_per_scan=max_per_scan,
        refuse_reason=settings.get("refuse_reason", DEFAULT_REFUSE_REASON),
        refuse_explanation=settings.get(
            "refuse_explanation", DEFAULT_REFUSE_EXPLANATION
        ),
    )
    while not stop.is_set():
        try:
            await asyncio.to_thread(scheduler.scan_once)
        except Exception as exc:
            logger.warning("退款争议扫描中断（{}）", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except asyncio.TimeoutError:
            continue
