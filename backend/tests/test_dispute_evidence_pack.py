import pytest
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

from dispute_evidence_pack import (
    extract_logistics_evidence,
    extract_fulfillment_evidence,
    extract_chat_confirmation_evidence,
    assemble_arbitration_statement,
    decide_dispute_action,
    mask_sensitive_code,
    ACTION_REJECT_WITH_PROOF,
    ACTION_REJECT_IN_TRANSIT,
    ACTION_REQUIRE_MANUAL_REVIEW,
    ACTION_ACCEPT_REFUND,
    REASON_DELIVERED_CLAIMED_FRAUD,
    REASON_IN_TRANSIT_REFUSAL,
    REASON_INSUFFICIENT_PROOF,
    REASON_UNFULFILLED_AGREED,
)


def test_mask_sensitive_code():
    assert mask_sensitive_code("") == ""
    assert mask_sensitive_code("12") == "****"
    assert mask_sensitive_code("ABC123") == "A****3"
    assert mask_sensitive_code("CARD-1234-5678-XXXX") == "CARD-****XXXX"


def test_extract_logistics_evidence_empty():
    ev = extract_logistics_evidence("ORDER_001", None)
    assert ev["has_logistics"] is False
    assert ev["is_delivered"] is False
    assert ev["is_in_transit"] is False
    assert "暂无物流轨迹" in ev["trace_summary"]


def test_extract_logistics_evidence_in_transit():
    data = {
        "carrier": "中通快递",
        "tracking_number": "ZT789012345",
        "traces": [
            {"time": "2026-11-20 09:00:00", "desc": "快件已揽收，正在发往北京转运中心"},
            {"time": "2026-11-20 14:00:00", "desc": "到达北京顺义分拨中心，正在进行中转派送"},
        ]
    }
    ev = extract_logistics_evidence("ORDER_002", data)
    assert ev["has_logistics"] is True
    assert ev["carrier_name"] == "中通快递"
    assert ev["tracking_number"] == "ZT789012345"
    assert ev["is_in_transit"] is True
    assert ev["is_delivered"] is False
    assert "运输在途中" in ev["trace_summary"]


def test_extract_logistics_evidence_delivered():
    data = {
        "carrier": "顺丰速运",
        "tracking_number": "SF1234567890",
        "traces": [
            {"time": "2026-11-19 10:00:00", "desc": "顺丰已揽收"},
            {"time": "2026-11-20 08:30:00", "desc": "快件已到达【菜鸟驿站】，请凭取件码取件"},
            {"time": "2026-11-20 11:20:00", "desc": "买家已在菜鸟驿站凭码完成签收，签收人：本人签收"},
        ]
    }
    ev = extract_logistics_evidence("ORDER_003", data)
    assert ev["has_logistics"] is True
    assert ev["is_delivered"] is True
    assert ev["is_in_transit"] is False
    assert ev["sign_time"] == "2026-11-20 11:20:00"
    assert ev["signer"] == "本人签收"
    assert "已签收妥投" in ev["trace_summary"]


def test_extract_fulfillment_evidence_card_key():
    voucher = {
        "card_code": "STEAM-WALLET-KEY-8888",
        "fulfilled_at": "2026-11-20 12:00:00",
        "status": "delivered",
    }
    ev = extract_fulfillment_evidence("ORDER_004", voucher_record=voucher)
    assert ev["has_fulfillment"] is True
    assert ev["fulfillment_type"] == "card_key"
    assert ev["is_fulfilled"] is True
    assert "STEAM****8888" in ev["voucher_code_masked"]
    assert len(ev["voucher_hash"]) == 16
    assert "虚拟卡密已发放" in ev["detail_summary"]


def test_extract_fulfillment_evidence_recharge():
    voucher = {
        "channel_order_id": "RECHARGE_FL_20261120001",
        "fulfilled_at": "2026-11-20 13:00:00",
        "status": "recharged",
    }
    ev = extract_fulfillment_evidence("ORDER_005", voucher_record=voucher)
    assert ev["has_fulfillment"] is True
    assert ev["fulfillment_type"] == "recharge"
    assert ev["is_fulfilled"] is True
    assert ev["channel_order_id"] == "RECHARGE_FL_20261120001"
    assert "充值渠道已成功代充" in ev["detail_summary"]


def test_extract_chat_confirmation_evidence():
    messages = [
        {"sender_role": "seller", "content": "亲，卡密已为您发送，请尽快激活哦~", "created_at": "12:01"},
        {"sender_role": "buyer", "content": "收到啦！已经充上了，能用，速度很快谢谢！", "created_at": "12:05"},
        {"sender_role": "buyer", "content": "密码对的，五星好评了！", "created_at": "12:06"},
    ]
    ev = extract_chat_confirmation_evidence(messages)
    assert ev["has_chat_records"] is True
    assert ev["confirmed_by_buyer"] is True
    assert "收到了" in ev["matched_keywords"] or "充上了" in ev["matched_keywords"] or "能用" in ev["matched_keywords"]
    assert len(ev["evidence_snippets"]) >= 2
    assert "买家在私聊中明确确认收货/可用" in ev["confirmation_summary"]


def test_assemble_arbitration_statement_delivered():
    order_info = {
        "order_id": "XY99887766",
        "item_title": "精选实物宝贝",
        "amount": "199.00",
        "buyer_id": "buyer_test_123",
        "refund_reason": "买家仅退款：未收到货",
    }
    logistics = {
        "has_logistics": True,
        "carrier_name": "顺丰速运",
        "tracking_number": "SF11223344",
        "is_delivered": True,
        "sign_time": "2026-11-20 10:00:00",
        "signer": "本人签收",
        "trace_summary": "已签收妥投",
    }
    fulfillment = {"has_fulfillment": False, "is_fulfilled": False}
    chat = {
        "confirmed_by_buyer": True,
        "confirmation_summary": "买家已确认收货",
        "evidence_snippets": [{"time": "10:15", "text": "收到了，挺好用"}]
    }

    statement = assemble_arbitration_statement(order_info, logistics, fulfillment, chat)
    assert "【阿里巴巴闲鱼官方小二争议仲裁申诉凭证包】" in statement
    assert "XY99887766" in statement
    assert "SF11223344" in statement
    assert "顺丰速运" in statement
    assert "买家发言「收到了，挺好用」" in statement
    assert "刚性驳回买家恶意退款申请" in statement


def test_decide_dispute_action_all_branches():
    # 1. 已妥投 -> 驳回
    logistics_delivered = {"is_delivered": True, "is_in_transit": False}
    empty_fulfillment = {"is_fulfilled": False, "fulfillment_type": "none"}
    empty_chat = {"confirmed_by_buyer": False}
    act, reason = decide_dispute_action("shipped", logistics_delivered, empty_fulfillment, empty_chat)
    assert act == ACTION_REJECT_WITH_PROOF
    assert reason == REASON_DELIVERED_CLAIMED_FRAUD

    # 2. 卡密已发 -> 驳回
    card_fulfillment = {"is_fulfilled": True, "fulfillment_type": "card_key"}
    act, reason = decide_dispute_action("completed", {"is_delivered": False}, card_fulfillment, empty_chat)
    assert act == ACTION_REJECT_WITH_PROOF

    # 3. 在途中 -> 驳回并引导拒签
    logistics_transit = {"is_delivered": False, "is_in_transit": True}
    act, reason = decide_dispute_action("shipped", logistics_transit, empty_fulfillment, empty_chat)
    assert act == ACTION_REJECT_IN_TRANSIT
    assert reason == REASON_IN_TRANSIT_REFUSAL

    # 4. 未发货 -> 正常同意退款
    act, reason = decide_dispute_action("pending_ship", {"is_delivered": False, "is_in_transit": False}, empty_fulfillment, empty_chat)
    assert act == ACTION_ACCEPT_REFUND
    assert reason == REASON_UNFULFILLED_AGREED

    # 5. 已发货但无轨迹且无凭证 -> 存疑转人工
    act, reason = decide_dispute_action("shipped", {"is_delivered": False, "is_in_transit": False}, empty_fulfillment, empty_chat)
    assert act == ACTION_REQUIRE_MANUAL_REVIEW
    assert reason == REASON_INSUFFICIENT_PROOF
