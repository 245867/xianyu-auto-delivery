"""
Dispute Evidence Pack - 恶意退款全自动取证反制与平台仲裁凭证包组装矩阵
《拆解 58：商业闭源「恶意退款全自动取证反制与平台仲裁凭证包组装矩阵」》

核心目标：
1. 三重铁证模型聚合提取 (物流签收轨迹 + 交付核销回执 + IM 对话意向留痕)
2. 标准化平台小二申诉公文生成器 (结构化履约事实、交付核销证明、买家确认留痕、平台规则抗辩)
3. 刚性自动反制状态机决策 (驳回举证 / 在途引导拒签 / 转人工风险告警 / 正常退款支持)
"""

import hashlib
import json
import re
from typing import Dict, Any, List, Optional, Tuple, Union


# 状态机动作常量
ACTION_REJECT_WITH_PROOF = "REJECT_WITH_PROOF"         # 证据确凿，自动组装凭证包驳回并申请小二裁决
ACTION_REJECT_IN_TRANSIT = "REJECT_IN_TRANSIT"         # 货物运输在途，驳回退款并告知拒签流程
ACTION_REQUIRE_MANUAL_REVIEW = "REQUIRE_MANUAL_REVIEW" # 存疑/缺关键证据，转人工客服跟进并触发告警
ACTION_ACCEPT_REFUND = "ACCEPT_REFUND"                 # 尚未履约/未发货，正常支持退款

# 决策原因常量
REASON_DELIVERED_CLAIMED_FRAUD = "DELIVERED_CLAIMED_FRAUD" # 已妥投签收或已核销使用，驳回恶意仅退款
REASON_IN_TRANSIT_REFUSAL = "IN_TRANSIT_REFUSAL"           # 商品已发货运输中，驳回并引导拒签
REASON_INSUFFICIENT_PROOF = "INSUFFICIENT_PROOF"           # 凭据不全或存疑，转人工客服
REASON_UNFULFILLED_AGREED = "UNFULFILLED_AGREED"           # 未发货未履约，合规同意退款

# 物流签收关键词正则
SIGN_PATTERN = re.compile(
    r"(已签收|本人签收|代收点签收|丰巢|菜鸟驿站|兔喜|快递超市|门卫|家门口|已妥投|签收人[:：]\s*(\S+))",
    re.IGNORECASE,
)

# 物流在途关键词正则
TRANSIT_PATTERN = re.compile(
    r"(已揽收|揽件|运输中|在途|发往|到达|派送中|正在派件|分拨中心|转运中)",
    re.IGNORECASE,
)

# 买家 IM 认可/收到/使用防抵赖关键词正则
CHAT_CONFIRM_PATTERN = re.compile(
    r"(收到了|收到啦|收到货|拿到了|好用|挺好用|可以了|可以用|能用|能用了|充上了|已充值|到账了|已到账|充好|谢谢|多谢|感谢|没有问题|没问题|全对|密码对|已激活|激活了|五星好评|已好评|速度很快)",
    re.IGNORECASE,
)


def mask_sensitive_code(code: str) -> str:
    """对卡密/凭据等敏感串进行脱敏处理，保留首尾供平台小二核实。"""
    raw = str(code or "").strip()
    if not raw:
        return ""
    length = len(raw)
    if length <= 6:
        return raw[:1] + "****" + raw[-1:] if length > 2 else "****"
    prefix_len = min(5, length // 3)
    suffix_len = min(4, length // 3)
    return raw[:prefix_len] + "****" + raw[-suffix_len:]


def extract_logistics_evidence(
    order_id: str,
    tracking_info: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """
    第一重铁证：提取物流承运商真实签收轨迹。
    支持传入轨迹列表或轨迹包装字典。
    """
    result: Dict[str, Any] = {
        "order_id": str(order_id or ""),
        "has_logistics": False,
        "carrier_name": "",
        "tracking_number": "",
        "is_delivered": False,
        "is_in_transit": False,
        "sign_time": "",
        "sign_station": "",
        "signer": "",
        "trace_summary": "暂无物流轨迹",
        "raw_traces_count": 0,
    }

    if not tracking_info:
        return result

    carrier = ""
    mail_no = ""
    traces: List[Dict[str, Any]] = []

    if isinstance(tracking_info, dict):
        carrier = tracking_info.get("carrier") or tracking_info.get("carrier_name") or tracking_info.get("express_company") or ""
        mail_no = tracking_info.get("tracking_number") or tracking_info.get("mail_no") or tracking_info.get("waybill_code") or ""
        raw_traces = tracking_info.get("traces") or tracking_info.get("trace_list") or []
        if isinstance(raw_traces, list):
            traces = raw_traces
    elif isinstance(tracking_info, list):
        traces = tracking_info

    result["carrier_name"] = str(carrier or "").strip()
    result["tracking_number"] = str(mail_no or "").strip()
    result["raw_traces_count"] = len(traces)

    if not traces and not (carrier or mail_no):
        return result

    result["has_logistics"] = True

    # 扫描轨迹节点，寻找签收与在途信息
    delivered = False
    in_transit = False
    sign_time = ""
    signer = ""
    sign_station = ""
    last_desc = ""

    for trace in traces:
        if not isinstance(trace, dict):
            continue
        desc = str(trace.get("desc") or trace.get("context") or trace.get("content") or "").strip()
        time_str = str(trace.get("time") or trace.get("ftime") or trace.get("created_at") or "").strip()
        if desc:
            last_desc = desc

        # 检查是否命中签收
        sign_match = SIGN_PATTERN.search(desc)
        if sign_match:
            delivered = True
            sign_time = time_str or sign_time
            
            # 优先提取具体签收人
            signer_match = re.search(r"签收人[:：]\s*(\S+)", desc)
            if signer_match:
                signer = signer_match.group(1).strip()
            elif "本人" in desc:
                signer = "本人签收"
            elif any(k in desc for k in ("代收", "驿站", "丰巢", "快递超市", "门卫")):
                signer = "代收网点/自提柜签收"
            else:
                signer = sign_match.group(0)

            # 提取自提网点/代收点名称
            station_match = re.search(r"【([^】]+(驿站|自提|丰巢|超市|生活)[^】]*)】", desc)
            if station_match:
                sign_station = station_match.group(1).strip()
            elif "菜鸟驿站" in desc:
                sign_station = "菜鸟驿站"
            elif "丰巢" in desc:
                sign_station = "丰巢快递柜"

        # 检查在途
        if not delivered and TRANSIT_PATTERN.search(desc):
            in_transit = True

    # 如果有显式快递单号且未标记 delivered，但列表非空
    if not delivered and traces and not in_transit:
        in_transit = True

    result["is_delivered"] = delivered
    result["is_in_transit"] = in_transit and not delivered
    result["sign_time"] = sign_time
    result["signer"] = signer
    result["sign_station"] = sign_station

    if delivered:
        result["trace_summary"] = f"已签收妥投 ({sign_time or '已确认'} {signer or ''}) | 承运: {result['carrier_name'] or '快递'} [{result['tracking_number'] or '-'}]"
    elif in_transit:
        result["trace_summary"] = f"运输在途中 ({last_desc[:40] if last_desc else '物流运输中'}) | 承运: {result['carrier_name'] or '快递'} [{result['tracking_number'] or '-'}]"
    else:
        result["trace_summary"] = f"物流已建单待揽收 | 运单号: {result['tracking_number'] or '-'}"

    return result


def extract_fulfillment_evidence(
    order_id: str,
    voucher_record: Optional[Dict[str, Any]] = None,
    card_record: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    第二重铁证：提取虚拟卡密发放或慢充代充核销凭证。
    """
    result: Dict[str, Any] = {
        "order_id": str(order_id or ""),
        "has_fulfillment": False,
        "fulfillment_type": "none",
        "fulfilled_at": "",
        "voucher_code_masked": "",
        "voucher_hash": "",
        "channel_order_id": "",
        "verification_status": "unverified",
        "is_fulfilled": False,
        "detail_summary": "无交付记录",
    }

    record = voucher_record or card_record
    if not record or not isinstance(record, dict):
        return result

    raw_code = str(record.get("card_code") or record.get("voucher_code") or record.get("text_content") or "").strip()
    channel_id = str(record.get("channel_order_id") or record.get("upstream_order_id") or record.get("serial_no") or "").strip()
    fulfilled_at = str(record.get("fulfilled_at") or record.get("sent_at") or record.get("created_at") or "").strip()
    status = str(record.get("status") or record.get("verification_status") or "").strip().lower()

    if not (raw_code or channel_id or fulfilled_at):
        return result

    result["has_fulfillment"] = True
    result["fulfilled_at"] = fulfilled_at
    result["channel_order_id"] = channel_id

    # 识别交付类型
    if channel_id and not raw_code:
        result["fulfillment_type"] = "recharge"
    elif raw_code:
        result["fulfillment_type"] = "card_key"
    else:
        result["fulfillment_type"] = "custom_service"

    if raw_code:
        result["voucher_code_masked"] = mask_sensitive_code(raw_code)
        result["voucher_hash"] = hashlib.sha256(raw_code.encode("utf-8")).hexdigest()[:16]

    # 判断核销/交付状态
    is_success = status in ("success", "recharged", "delivered", "fulfilled", "completed", "used", "active", "1")
    # 如果有卡密发出或有充值流水号且未标记 failed，视作已交付
    if is_success or (raw_code and status != "failed") or (channel_id and status != "failed"):
        result["is_fulfilled"] = True
        result["verification_status"] = "success"
    else:
        result["is_fulfilled"] = False
        result["verification_status"] = status or "pending"

    if result["fulfillment_type"] == "card_key":
        result["detail_summary"] = f"虚拟卡密已发放 (脱敏凭据: {result['voucher_code_masked']}, 哈希存证: {result['voucher_hash']}, 发送时间: {fulfilled_at or '已发送'})"
    elif result["fulfillment_type"] == "recharge":
        result["detail_summary"] = f"充值渠道已成功代充 (渠道流水号: {channel_id}, 交付时间: {fulfilled_at or '已完成'})"
    else:
        result["detail_summary"] = f"履约交付已完成 (状态: {result['verification_status']}, 时间: {fulfilled_at})"

    return result


def extract_chat_confirmation_evidence(
    messages: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    第三重铁证：扫描买家 IM 私聊历史，精准提取防抵赖意向言论。
    """
    result: Dict[str, Any] = {
        "has_chat_records": False,
        "confirmed_by_buyer": False,
        "matched_keywords": [],
        "evidence_snippets": [],
        "confirmation_summary": "买家未在私聊中明确确认收货或使用",
    }

    if not messages or not isinstance(messages, list):
        return result

    result["has_chat_records"] = True
    matched_kws: List[str] = []
    snippets: List[Dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        # 区分发送方：若是卖家发的信息，跳过
        role = str(msg.get("sender_role") or msg.get("sender") or msg.get("role") or "").lower()
        is_buyer = msg.get("is_buyer")
        if is_buyer is False or role in ("seller", "system", "admin", "service"):
            continue

        text = str(msg.get("content") or msg.get("text") or msg.get("message") or "").strip()
        time_str = str(msg.get("created_at") or msg.get("timestamp") or msg.get("time") or "").strip()

        if not text:
            continue

        matches = CHAT_CONFIRM_PATTERN.findall(text)
        if matches:
            for kw in matches:
                kw_str = kw if isinstance(kw, str) else kw[0]
                if kw_str and kw_str not in matched_kws:
                    matched_kws.append(kw_str)

            snippets.append({
                "time": time_str,
                "text": text[:100],
                "matched": [k if isinstance(k, str) else k[0] for k in matches],
            })

    if snippets:
        result["confirmed_by_buyer"] = True
        result["matched_keywords"] = matched_kws
        result["evidence_snippets"] = snippets
        kws_repr = "、".join(matched_kws[:5])
        result["confirmation_summary"] = f"买家在私聊中明确确认收货/可用 (命中关键词: {kws_repr}, 共 {len(snippets)} 处防抵赖发言记录)"

    return result


def assemble_arbitration_statement(
    order_info: Dict[str, Any],
    logistics_ev: Dict[str, Any],
    fulfillment_ev: Dict[str, Any],
    chat_ev: Dict[str, Any],
    refund_reason: str = "",
) -> str:
    """
    标准化平台小二仲裁申诉公文生成器。
    根据三重铁证结构体，组装符合淘系/闲鱼小二官方审理偏好的标准化抗辩公文。
    """
    order_id = str(order_info.get("order_id") or order_info.get("id") or "").strip()
    item_title = str(order_info.get("item_title") or order_info.get("title") or "商品").strip()
    amount = str(order_info.get("amount") or order_info.get("price") or "0.00").strip()
    buyer_id = str(order_info.get("buyer_id") or order_info.get("buyer_name") or "买家").strip()
    reason_desc = str(refund_reason or order_info.get("refund_reason") or "买家申请退款").strip()

    sections: List[str] = [
        "【阿里巴巴闲鱼官方小二争议仲裁申诉凭证包】",
        f"订单编号：{order_id} | 争议商品：{item_title} | 订单金额：¥{amount} | 买方：{buyer_id}",
        f"买方退款诉由：{reason_desc}",
        "",
        "一、交易履约事实记录：",
    ]

    # 1. 履约记录
    if logistics_ev.get("has_logistics"):
        sections.append(f"• 承运快递：{logistics_ev.get('carrier_name') or '正规承运物流'}，运单号：{logistics_ev.get('tracking_number') or '-'}")
        sections.append(f"• 轨迹节点：{logistics_ev.get('trace_summary')}")
        if logistics_ev.get("is_delivered"):
            sections.append(f"• 签收详情：买家已于 {logistics_ev.get('sign_time') or '约定时间'} 正常签收（签收人: {logistics_ev.get('signer') or '本人'}）。包裹已安全妥投，不存在未收到货情况。")
    elif fulfillment_ev.get("has_fulfillment"):
        sections.append(f"• 系统交付方式：{fulfillment_ev.get('fulfillment_type')}")
        sections.append(f"• 交付明细：{fulfillment_ev.get('detail_summary')}")
    else:
        sections.append("• 订单已在约定时间内按照买家要求完成发货与履约处理。")

    # 2. 交付与核销证明
    sections.append("")
    sections.append("二、交付核销铁证凭据：")
    if fulfillment_ev.get("has_fulfillment"):
        sections.append(f"• 交付时间：{fulfillment_ev.get('fulfilled_at') or '订单支付后即时交付'}")
        if fulfillment_ev.get("voucher_code_masked"):
            sections.append(f"• 卡密凭证脱敏留痕：{fulfillment_ev.get('voucher_code_masked')}（SHA-256 存证哈希: {fulfillment_ev.get('voucher_hash')}）")
        if fulfillment_ev.get("channel_order_id"):
            sections.append(f"• 第三方平台充值流水号：{fulfillment_ev.get('channel_order_id')}（已核销充值到账）")
        sections.append(f"• 凭证状态：已成功核销（状态: {fulfillment_ev.get('verification_status')}），无法二次销售回收。")
    elif logistics_ev.get("is_delivered"):
        sections.append("• 电子面单系统完整记录派件及签收流水，签收轨迹不可篡改，已构成完全履约事实。")
    else:
        sections.append("• 系统已留存发货回执与交付单据，订单已进入实际履行阶段。")

    # 3. 买家沟通留痕
    sections.append("")
    sections.append("三、买家沟通确认与防抵赖事实：")
    if chat_ev.get("confirmed_by_buyer"):
        sections.append(f"• {chat_ev.get('confirmation_summary')}")
        snippets = chat_ev.get("evidence_snippets", [])
        for i, snip in enumerate(snippets[:3], 1):
            time_part = f"[{snip.get('time')}] " if snip.get("time") else ""
            sections.append(f"  证据 {i}: {time_part}买家发言「{snip.get('text')}」")
        sections.append("• 买家发言与签收状态均证实其已实际占有/激活并认可商品，目前发起的仅退款纯属恶意白嫖行为。")
    else:
        sections.append("• 卖家发货前后多次主动沟通指引，买家在此期间未提出任何物流破损或无法使用异议。")

    # 4. 卖家法理诉求
    sections.append("")
    sections.append("四、卖家合规申诉与驳回诉求：")
    if logistics_ev.get("is_delivered") or (fulfillment_ev.get("is_fulfilled") and chat_ev.get("confirmed_by_buyer")):
        sections.append("依据《闲鱼平台争议处理规则》及电商交易法相关条款，买家已完整接收并确认/消耗了标的商品。")
        sections.append("现买家企图通过‘未收到货/虚假质量问题’仅退款达到无偿占有之目的，已严重侵害卖家合法权益。")
        sections.append("恳请平台小二秉公裁决：刚性驳回买家恶意退款申请，判定交易成功并将货款结算至卖家账户！")
    elif logistics_ev.get("is_in_transit"):
        sections.append("商品已依约发出且物流正全速在途配送，尚未妥投。根据平台规则，运输在途不支持直接仅退款。")
        sections.append("卖家已驳回退款申请；若买家确不需要，请买家在快递派件时直接当面‘拒收’，待包裹真实退回发货地后方可办理售后退款。")
    else:
        sections.append("卖家已依规履约并留存完整凭证，请买家遵守平台诚信交易原则，并请小二介入依法支持卖家。")

    return "\n".join(sections)


def decide_dispute_action(
    order_status: str,
    logistics_ev: Dict[str, Any],
    fulfillment_ev: Dict[str, Any],
    chat_ev: Dict[str, Any],
) -> Tuple[str, str]:
    """
    刚性反制决策状态机：
    输入当前订单状态与三重铁证，输出自动反制动作与法理原因。
    """
    st = str(order_status or "").strip().lower()

    is_delivered = bool(logistics_ev.get("is_delivered"))
    is_in_transit = bool(logistics_ev.get("is_in_transit"))
    is_fulfilled = bool(fulfillment_ev.get("is_fulfilled"))
    confirmed_by_buyer = bool(chat_ev.get("confirmed_by_buyer"))

    # 1. 证据确凿：妥投签收，或虚拟交付核销且有买家私聊确认，或卡密已发送
    if is_delivered or (is_fulfilled and confirmed_by_buyer) or (fulfillment_ev.get("fulfillment_type") == "card_key" and is_fulfilled):
        return (ACTION_REJECT_WITH_PROOF, REASON_DELIVERED_CLAIMED_FRAUD)

    # 2. 实物包裹正在途中
    if is_in_transit:
        return (ACTION_REJECT_IN_TRANSIT, REASON_IN_TRANSIT_REFUSAL)

    # 3. 订单已进入已发货或已完成，但无跟踪轨迹且买家未确认，存疑转人工
    if st in ("shipped", "completed", "pending_confirm") or is_fulfilled:
        return (ACTION_REQUIRE_MANUAL_REVIEW, REASON_INSUFFICIENT_PROOF)

    # 4. 处于未发货、待付款或处理中，未发生履约行为，支持退款
    if st in ("processing", "waiting", "wait_buyer_pay", "pending_ship", "pending"):
        return (ACTION_ACCEPT_REFUND, REASON_UNFULFILLED_AGREED)

    # 默认兜底：转人工客服审核
    return (ACTION_REQUIRE_MANUAL_REVIEW, REASON_INSUFFICIENT_PROOF)


BUYER_CONFIRM_KEYWORDS = (
    "收到了",
    "好用",
    "可以了",
    "已经充上了",
    "挺好的",
    "密码对的",
)


def _parse_amount(value: Any) -> float:
    text = str(value or "").replace("¥", "").replace("￥", "").replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    try:
        return float(match.group(0))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _logistics_from_order(order: Dict[str, Any]) -> Dict[str, Any]:
    express_no = str(order.get("express_no") or "").strip()
    express_company = str(order.get("express_company") or "").strip()
    dropship_status = str(order.get("dropship_status") or "").strip().lower()
    delivered = dropship_status in ("signed", "delivered", "received")
    in_transit = dropship_status == "shipped" and not delivered
    return {
        "express_no": express_no,
        "express_company": express_company,
        "dropship_status": dropship_status,
        "has_logistics": bool(express_no or express_company),
        "delivered": delivered,
        "in_transit": in_transit,
    }


def _delivery_from_cards(cards: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    sent_statuses = {"sent", "done", "delivered", "claimed", "collected"}
    vouchers: List[Dict[str, Any]] = []
    sent_count = 0
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        status = str(card.get("status") or "").strip().lower()
        if status not in sent_statuses:
            continue
        sent_count += 1
        payload = str(card.get("payload") or "")
        vouchers.append({
            "idx": card.get("idx"),
            "status": status,
            "voucher_code_masked": mask_sensitive_code(payload),
            "voucher_hash": (
                hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
                if payload else ""
            ),
            "sent_at": str(card.get("sent_at") or ""),
        })
    return {
        "sent_count": sent_count,
        "has_delivery": sent_count > 0,
        "vouchers": vouchers,
    }


def _chat_from_conversations(database, order: Dict[str, Any]) -> Dict[str, Any]:
    account_id = str(order.get("cookie_id") or order.get("account_id") or "")
    buyer_id = str(order.get("buyer_id") or "")
    item_id = str(order.get("item_id") or "")
    params: List[Any] = [account_id, buyer_id]
    sql = """SELECT content, created_at FROM ai_conversations
             WHERE cookie_id=? AND user_id=? AND role='user'"""
    if item_id:
        sql += " AND item_id=?"
        params.append(item_id)
    sql += " ORDER BY created_at ASC"
    with database.lock:
        cursor = database.conn.cursor()
        cursor.row_factory = None
        rows = cursor.execute(sql, tuple(params)).fetchall()
    quotes: List[str] = []
    for row in rows or []:
        content = str(row[0] or "").strip()
        if not content:
            continue
        if any(keyword in content for keyword in BUYER_CONFIRM_KEYWORDS):
            quotes.append(content)
    return {
        "matched": bool(quotes),
        "quotes": quotes,
        "message_count": len(rows or []),
    }


def dispute_evidence_pack(database, order, cards_reader=None, now=None):
    """聚合物流、交付与买家确认三类证据，返回可持久化的申诉凭证包。"""
    order = dict(order or {})
    order_id = str(order.get("order_id") or order.get("id") or "")
    account_id = str(order.get("cookie_id") or order.get("account_id") or "")
    cards: List[Dict[str, Any]] = []
    if callable(cards_reader):
        cards = cards_reader(order_id, account_id) or []
    else:
        try:
            import delivery_reliability

            cards = delivery_reliability.cards(order_id, account_id)
        except Exception:
            cards = []
    logistics = _logistics_from_order(order)
    delivery = _delivery_from_cards(cards)
    chat = _chat_from_conversations(database, order)
    evidence = {
        "order": {
            "order_id": order_id,
            "item_id": str(order.get("item_id") or ""),
            "buyer_id": str(order.get("buyer_id") or ""),
            "account_id": account_id,
            "amount": _parse_amount(order.get("amount")),
            "order_status": str(order.get("order_status") or ""),
            "created_at": str(order.get("created_at") or ""),
        },
        "logistics": logistics,
        "delivery": delivery,
        "chat": chat,
    }
    risk_flags: List[str] = []
    if not logistics["has_logistics"] and not delivery["has_delivery"]:
        risk_flags.append("missing_delivery_evidence")
    if not chat["matched"]:
        risk_flags.append("missing_chat_confirmation")
    auto_refuse = bool(
        logistics["delivered"]
        or logistics["in_transit"]
        or delivery["has_delivery"]
        or chat["matched"]
    )
    statement_text = _assemble_statement(evidence, now)
    return {
        "refund_amount": evidence["order"]["amount"],
        "refund_reason": str(order.get("refund_reason") or "买家申请退款"),
        "evidence": evidence,
        "statement_text": statement_text,
        "auto_refuse": auto_refuse,
        "risk_flags": risk_flags,
    }


def _assemble_statement(evidence: Dict[str, Any], now=None) -> str:
    order = evidence["order"]
    logistics = evidence["logistics"]
    delivery = evidence["delivery"]
    chat = evidence["chat"]
    collected_at = (
        now.strftime("%Y-%m-%d %H:%M:%S")
        if hasattr(now, "strftime") else str(now or "")
    )
    lines = [
        "【交易履约事实】",
        f"订单号：{order['order_id']}｜商品：{order['item_id']}｜"
        f"金额：¥{order['amount']:.2f}｜买家：{order['buyer_id']}",
    ]
    if logistics["has_logistics"]:
        lines.append(
            f"物流：{logistics['express_company'] or '承运商'}，"
            f"运单号 {logistics['express_no'] or '-'}，"
            f"状态 {logistics['dropship_status'] or '已发货'}"
        )
    else:
        lines.append("物流：本单为虚拟交付，无实物运单。")
    lines.append("【交付核销证明】")
    if delivery["has_delivery"]:
        lines.append(
            f"系统已记录 {delivery['sent_count']} 份卡密/服务交付回执，"
            "逐卡记账不可抵赖，凭据原文已脱敏并留存 SHA-256 哈希。"
        )
    else:
        lines.append("系统未记录到已完成的卡密或服务交付回执。")
    lines.append("【买家沟通确保证明】")
    if chat["matched"]:
        lines.append("买家在私聊中明确表示已收到并使用商品，原始发言如下：")
        for quote in chat["quotes"][:5]:
            lines.append(f"· 买家发言「{quote}」")
    else:
        lines.append("未检索到买家确认收货或确认可用的私聊发言。")
    lines.append("【卖家诉求声明】")
    if evidence["order"]["order_status"] == "refunding" and (
        delivery["has_delivery"] or logistics["delivered"] or chat["matched"]
    ):
        lines.append(
            "卖家已完整履约，买家在已获得商品或服务后申请退款，"
            "恳请平台小二依据凭证驳回该退款申请。"
        )
    else:
        lines.append("卖家已提交履约凭证，恳请平台小二依据事实公正裁决。")
    if collected_at:
        lines.append(f"取证时间：{collected_at}")
    return "\n".join(lines)
