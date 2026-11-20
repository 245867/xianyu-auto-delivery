"""
订单状态归一化（唯一来源）
==========================
这个文件存在的唯一理由：**状态文案的匹配顺序会改变业务结果**，而这种顺序
散落在多个文件里就一定会写错。2026-11-09 实测到一次真实错误：

    "退款完成，钱款已原路退返"

原逻辑按优先级 退款成功 → 关闭/取消 → 完成/交易成功 → 退款 判断，
「退款完成」先命中了「完成」，被归成 `completed`。而 C1 的自动评价正好挂在
`completed` 分支上 —— 等于**给已经退款的买家发好评**。

修法不是补一个特例，而是把「退款族」整族排到最前面，并且：
`退款` 与 `完成/成功/原路退返` 同时出现时，一律归 `cancelled`（钱退了，交易没了）。

**改这里的顺序前先想清楚**：这个表从上往下是"先否决、后认可"。
退款是终态否决，必须比"完成"更早命中。

来源对照：`23Star/xianyu-super-butler` 的 `utils/order_status_rules.py`
（它踩过同样的坑：注里明确写「必须排在『已发货』之前，否则『已发货退款』
会先命中 shipped 而被当成有效订单计入营收」）。
"""

from typing import Any, Dict, Optional

VALID_STATUSES = frozenset({
    "processing", "pending_ship", "shipped", "completed",
    "refunding", "cancelled", "refund_cancelled", "unknown",
})

# 平台状态码（两套编码都在用，合并成一张表）
STATUS_CODE_MAP: Dict[str, str] = {
    # 卖家端 merchant.* / MTOP 数值码
    "1": "processing",     # 处理中
    "2": "pending_ship",   # 待发货
    "3": "shipped",        # 已发货
    "4": "completed",      # 已完成/交易成功
    "5": "refunding",      # 退款中
    "6": "cancelled",      # 已取消/已关闭
    "7": "refunding",      # 退款申请中
    "8": "cancelled",      # 退款成功（订单关闭）
    "9": "refunding",      # 退款协商中
    "10": "cancelled",     # 退款关闭
    "11": "completed",
    "12": "cancelled",
}

# 已经是终态、不需要再轮询的状态
STABLE_STATUSES = frozenset({"shipped", "completed", "cancelled", "refund_cancelled"})

# ---------------------------------------------------------------------------
# 顺序即语义。**不要重排**，除非你同时改了 test_order_status_rules.py。
# ---------------------------------------------------------------------------
STATUS_TEXT_RULES: tuple = (
    # ---- 第一层：退款终态（钱退了 = 交易没了，无论文案里有没有"完成/成功"）
    ("退款成功", "cancelled"),
    ("退款完成", "cancelled"),
    ("钱款已原路退返", "cancelled"),
    ("已原路退返", "cancelled"),
    ("退款已到账", "cancelled"),
    ("极速退款成功", "cancelled"),

    # ---- 第二层：订单关闭 / 取消
    ("买家取消了订单", "cancelled"),
    ("卖家取消了订单", "cancelled"),
    ("你关闭了订单", "cancelled"),
    ("订单已关闭", "cancelled"),
    ("交易关闭", "cancelled"),
    ("交易已关闭", "cancelled"),
    ("超时未付款", "cancelled"),

    # ---- 第三层：退款处理中（必须在"已发货/完成"之前）
    ("未发货退款", "refunding"),
    ("已发货退款", "refunding"),
    ("退货退款中", "refunding"),
    ("退货退款", "refunding"),
    ("等待卖家处理退款", "refunding"),
    ("退款申请中", "refunding"),
    ("申请退款", "refunding"),
    ("退款中", "refunding"),
    ("退款协商", "refunding"),

    # ---- 第四层：完成（只在没有退款字样的前提下才算数）
    ("买家确认收货，交易成功", "completed"),
    ("你已确认收货，交易成功", "completed"),
    ("交易成功", "completed"),
    ("交易完成", "completed"),
    ("订单完成", "completed"),

    # ---- 第五层：已发货
    ("卖家已发货，待买家确认收货", "shipped"),
    ("已发货，待买家确认收货", "shipped"),
    ("请等待买家确认收货", "shipped"),
    ("待买家确认收货", "shipped"),
    ("卖家已发货", "shipped"),
    ("你已发货", "shipped"),
    ("已发货", "shipped"),

    # ---- 第六层：已付款待发货
    ("买家已付款，请尽快发货", "pending_ship"),
    ("等待卖家发货", "pending_ship"),
    ("等待你发货", "pending_ship"),
    ("我已付款", "pending_ship"),
    ("买家已付款", "pending_ship"),
    ("付款完成", "pending_ship"),
    ("已付款，待发货", "pending_ship"),
    ("待发货", "pending_ship"),

    # ---- 第七层：拍下未付款 / 处理中
    ("我已拍下，待付款", "processing"),
    ("待付款", "processing"),
    ("处理中", "processing"),
)

# 出现这些字样时，任何"完成/成功"都不算数（退款语义优先）
_REFUND_MARKERS = ("退款", "退货", "原路退返", "退返")


def detect_status_from_text(text: Any) -> str:
    """按上表顺序匹配文案。先命中者胜。"""
    t = str(text or "").strip()
    if not t:
        return "unknown"
    # 硬保险：文案里出现退款字样时，先走退款判定，不给"完成"任何机会
    if any(m in t for m in _REFUND_MARKERS):
        if any(k in t for k in ("成功", "完成", "已退", "到账", "原路退返", "退返")):
            return "cancelled"
        return "refunding"
    for keyword, status in STATUS_TEXT_RULES:
        if keyword in t:
            return status
    return "unknown"


def normalize_status(status_code: Any, status_text: Any = "") -> str:
    """状态码优先；码不认识才用文案。码认识但文案是退款终态时，文案赢。"""
    code = str(status_code or "").strip()
    text = str(status_text or "").strip()
    by_code = STATUS_CODE_MAP.get(code) or (code if code in VALID_STATUSES else "")

    # 文案里明确是退款终态 → 覆盖状态码（平台码有时滞后，还停在 4=已完成）
    if text and any(m in text for m in _REFUND_MARKERS):
        by_text = detect_status_from_text(text)
        if by_text in ("cancelled", "refunding"):
            return by_text

    if by_code:
        return by_code
    return detect_status_from_text(text or code)


def get_status(order: Any) -> str:
    """从库里的一行订单取归一化状态（兼容新旧字段名）。"""
    if not isinstance(order, dict):
        return "unknown"
    return normalize_status(
        order.get("order_status") or order.get("status") or "unknown",
        order.get("status_text") or "",
    )


def is_stable(status: Any) -> bool:
    return normalize_status(status) in STABLE_STATUSES


def is_completed(status: Any) -> bool:
    """真正的"交易完成"：不是退款、不是关闭。C1 交易后动作只能挂这个。"""
    return normalize_status(status) == "completed"


# 允许发货的状态：只有"还没发出去"和"状态不明但可能待发"这两种。
DELIVERABLE_STATUSES = frozenset({"pending_ship", "processing", "unknown"})
CROSS_SHOP_BLACKLIST_REASON = "买家命中跨店黑名单，已暂停发货"

# 明确不许发货的状态及理由。**拒绝的理由必须能给人看**——发货被拦下时，
# 用户需要知道是"这单退款了"还是"这单已经发过了"，否则他以为是程序坏了。
_NOT_DELIVERABLE_REASON: Dict[str, str] = {
    "shipped": "订单已发货（系统已标或平台已确认），重复发货会重发卡密",
    "completed": "订单已交易完成，不需要再发货",
    "cancelled": "订单已关闭或退款成功，发货会造成钱货两失",
    "refund_cancelled": "订单已退款关闭，发货会造成钱货两失",
    "refunding": "订单正在退款中，钱可能已经退了，先不要发货",
}


def delivery_check(status: Any) -> Dict[str, Any]:
    """
    发货前检查：这个订单还该不该发货。

    为什么单独一个函数、而不是让两个发货入口各自写 if：
    手动发货（`reply_server.manual_ship_orders`）和自动发货（`_auto_delivery`）
    是两条独立代码路径。历史上它们各写一套判断，结果一边加了检查另一边没加。
    这里收敛成单点，并配一条"结构测试"断言两个入口都调了它。

    返回 {"ok": bool, "status": 归一化状态, "reason": 拒绝原因}。
    **状态取不到时按 unknown 放行**——与"规则查询失败"不同：
    订单状态来自本机库，取不到通常是这单还没入库（首次自动发货就是这种情况），
    此时拦截会让新订单永远发不出去。真正的兜底靠 can_auto_delivery 的判重。
    """
    st = normalize_status(status)
    if st in DELIVERABLE_STATUSES:
        return {"ok": True, "status": st, "reason": ""}
    return {"ok": False, "status": st,
            "reason": _NOT_DELIVERABLE_REASON.get(st, f"订单状态为 {st}，不允许发货")}


def cross_shop_block_reason(database, buyer_id: Optional[str]) -> str:
    if buyer_id and database.get_cross_shop_blacklist_entry(buyer_id) is not None:
        return CROSS_SHOP_BLACKLIST_REASON
    return ""


def explain(text: Any) -> Optional[Dict[str, str]]:
    """调试用：说明一句话为什么被判成这个状态。"""
    t = str(text or "").strip()
    if not t:
        return None
    result = detect_status_from_text(t)
    for keyword, status in STATUS_TEXT_RULES:
        if keyword in t and status == result:
            return {"text": t, "status": result, "matched": keyword}
    if any(m in t for m in _REFUND_MARKERS):
        return {"text": t, "status": result, "matched": "退款族硬规则"}
    return {"text": t, "status": result, "matched": ""}
