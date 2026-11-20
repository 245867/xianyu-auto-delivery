"""
发货相关的两个只读视图（拆解 39 §八-2 / §八-6）
================================================

1. `after_sale_review()` —— 发货后 24 小时异常回看
   嗷嗷鱼交付篇的 7 步 SOP 里有一条「发货后 24h 回看异常申请」：卡密类商品
   一旦发错，买家通常在一天内就会来申请退款，过了这个窗口再发现就只剩差评。
   我们原来只有一张 `refund_aftercare` 的账，但它记的是**退款本身**，
   看不出"这笔退款和发货有什么关系"。

   这里不改任何账，只把四件事按订单拼在一起，回答一个问题：
   **过去 N 小时里发出去的单，有哪些已经出现异常信号？**
     - 订单状态变成退款/交易关闭
     - 发货账本里还有没发出去的卡（failed / unknown）
     - 平台确认发货卡住或已停止
     - 订单更新时间落在窗口内但账本完全没有记录（可能是别的进程发的）

   局限写在返回值里：本机 orders 表**没有独立的发货时间字段**，
   只能用 `updated_at` 近似，所以这不是"发货后精确 24 小时"，是"最近 N 小时有变动的单"。

2. `first_n_order_prompts()` —— 新商品前 N 单人工抽查提示
   嗷嗷鱼说"规格配错会放大错误"：一个新品前几单如果配错规格，
   错的是**每一单**，而且卡密是一次性消耗品，退款也收不回。
   这里只做提示（不拦截、不改状态），把"这个商品才卖出第几单"摆到发货页上。

两个函数都只读：不写订单、不写账本、不发消息。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_WINDOW_HOURS = 24
DEFAULT_FIRST_N = 3

# 已发货之后才值得回看的状态（未发货的单不在"发货后回看"的范围里）
SHIPPED_STATES = ("shipped", "completed", "refunding", "refund_cancelled", "cancelled")
# 出现这些状态 = 售后已经开始了
AFTER_SALE_STATES = {
    "refunding": "退款中",
    "refund_cancelled": "退款后交易关闭",
    "cancelled": "已取消",
}


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _load_orders(cookie_id: str = "", limit: int = 1000) -> List[Dict[str, Any]]:
    try:
        from db_manager import db_manager
        if cookie_id:
            return db_manager.get_orders_by_cookie(cookie_id, limit=limit) or []
        return db_manager.get_all_orders(limit=limit) or []
    except Exception:
        return []


def _cards_by_order(cookie_id: str = "") -> Dict[str, Dict[str, Any]]:
    """从发货账本取每单的未发张数（只读）。账本不可用时返回空表，不抛异常。"""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        import delivery_reliability as dr
        conn = dr._conn()
        try:
            sql = ("SELECT order_id, cookie_id, item_id, spec, "
                   "SUM(status IN ('failed','unknown')) AS unresolved, "
                   "COUNT(*) AS total, SUM(status='sent') AS sent "
                   "FROM delivery_cards GROUP BY order_id, cookie_id")
            rows = conn.execute(sql).fetchall()
        finally:
            conn.close()
    except Exception:
        return out
    for r in rows:
        d = dict(r)
        if cookie_id and d.get("cookie_id") != cookie_id:
            continue
        out[str(d.get("order_id"))] = {
            "unresolved": int(d.get("unresolved") or 0),
            "total": int(d.get("total") or 0),
            "sent": int(d.get("sent") or 0),
            "spec": d.get("spec") or "",
            "item_id": d.get("item_id") or "",
        }
    return out


def _platform_problems(cookie_id: str = "") -> Dict[str, str]:
    """平台确认发货还没完成的单：order_id → 说明（只读，复用三本账的口径）。"""
    out: Dict[str, str] = {}
    try:
        import delivery_reliability as dr
    except Exception:
        return out
    try:
        for row in dr.platform_ship_rows("stuck", cookie_id, limit=200):
            out[str(row["order_id"])] = (
                f"平台确认发货卡住（已试 {row.get('attempts')} 次）："
                f"{(row.get('last_error') or '')[:60]}")
    except Exception:
        pass
    try:
        for row in dr.platform_ship_rows("stopped", cookie_id, limit=200):
            out.setdefault(str(row["order_id"]), (
                f"平台确认发货已停止：{(row.get('stop_reason') or '')[:60]}"))
    except Exception:
        pass
    return out


def after_sale_review(cookie_id: str = "", hours: int = DEFAULT_WINDOW_HOURS,
                      now: Optional[datetime] = None,
                      orders: Optional[Iterable[Dict[str, Any]]] = None,
                      cards: Optional[Dict[str, Dict[str, Any]]] = None,
                      platform: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    过去 N 小时有变动的已发货订单，逐个标异常信号。只读。

    `orders` / `cards` / `platform` 可注入（测试用）；默认从 db_manager 与
    delivery_reliability 只读拉取。
    """
    win = max(1, int(hours or DEFAULT_WINDOW_HOURS))
    now_dt = now or datetime.now()
    cutoff = now_dt - timedelta(hours=win)

    if orders is None:
        orders = _load_orders(cookie_id=cookie_id)
    if cards is None:
        cards = _cards_by_order(cookie_id=cookie_id)
    if platform is None:
        platform = _platform_problems(cookie_id=cookie_id)

    scanned = 0
    flagged: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {"after_sale": 0, "delivery_incomplete": 0,
                              "platform_pending": 0, "no_ledger": 0}

    for o in orders:
        order_id = str(o.get("order_id") or o.get("id") or "")
        if not order_id:
            continue
        cid = str(o.get("cookie_id") or cookie_id or "")
        if cookie_id and cid and cid != cookie_id:
            continue
        status = str(o.get("status") or o.get("order_status") or "unknown")
        if status not in SHIPPED_STATES:
            continue
        changed = _parse_time(o.get("updated_at") or o.get("created_at"))
        if changed is None or changed < cutoff:
            continue
        scanned += 1

        flags: List[Dict[str, str]] = []
        if status in AFTER_SALE_STATES:
            flags.append({"code": "after_sale",
                          "text": f"订单已进入{AFTER_SALE_STATES[status]}"})
            counts["after_sale"] += 1

        ledger = cards.get(order_id) or {}
        unresolved = int(ledger.get("unresolved") or 0)
        if unresolved > 0:
            flags.append({"code": "delivery_incomplete",
                          "text": f"账本还有 {unresolved} 张卡没发出去"
                                  f"（共 {ledger.get('total', 0)} 张）"})
            counts["delivery_incomplete"] += 1
        elif not ledger:
            flags.append({"code": "no_ledger",
                          "text": "发货账本里没有这单的记录（可能是加账本之前发的，或别的进程发的）"})
            counts["no_ledger"] += 1

        p = platform.get(order_id)
        if p:
            flags.append({"code": "platform_pending", "text": p})
            counts["platform_pending"] += 1

        if not flags:
            continue
        flagged.append({
            "order_id": order_id,
            "cookie_id": cid,
            "item_id": str(o.get("item_id") or ledger.get("item_id") or ""),
            "spec": ledger.get("spec") or "",
            "status": status,
            "updated_at": o.get("updated_at") or o.get("created_at") or "",
            "flags": flags,
            "suggested": ("先看发货账本的待处理行；退款已开始的按 refund_aftercare 的出口处理，"
                          "不要在这条路径上自动补发"),
        })

    flagged.sort(key=lambda x: len(x["flags"]), reverse=True)
    return {
        "ok": True,
        "dry_run": True,
        "window_hours": win,
        "since": cutoff.isoformat(timespec="seconds"),
        "scanned": scanned,
        "flagged": flagged,
        "counts": counts,
        "limitations": [
            "orders 表没有独立发货时间，窗口按订单 updated_at 近似，不是精确的发货后 24 小时",
            "只读：不补发、不改状态、不写退款账",
        ],
    }


def first_n_order_prompts(cookie_id: str = "", n: int = DEFAULT_FIRST_N,
                          orders: Optional[Iterable[Dict[str, Any]]] = None,
                          limit: int = 1000) -> Dict[str, Any]:
    """
    新商品前 N 单提示：某个商品的订单数还没超过 N 时，提示人工抽查。只读、不拦截。

    为什么用"订单数"而不是"上架天数"：规格配错的影响是按单累积的，
    第 4 单之后错误一般已经被买家反馈出来了；前几单才是"错了没人告诉你"的区间。
    """
    threshold = max(1, int(n or DEFAULT_FIRST_N))
    if orders is None:
        orders = _load_orders(cookie_id=cookie_id, limit=limit)

    by_item: Dict[str, List[Dict[str, Any]]] = {}
    for o in orders:
        item_id = str(o.get("item_id") or "")
        if not item_id:
            continue
        cid = str(o.get("cookie_id") or cookie_id or "")
        if cookie_id and cid and cid != cookie_id:
            continue
        by_item.setdefault(item_id, []).append(o)

    items: List[Dict[str, Any]] = []
    for item_id, rows in by_item.items():
        if len(rows) > threshold:
            continue
        rows = sorted(rows, key=lambda r: str(r.get("created_at") or r.get("updated_at") or ""),
                      reverse=True)
        items.append({
            "item_id": item_id,
            "order_count": len(rows),
            "threshold": threshold,
            "latest_order_id": str(rows[0].get("order_id") or ""),
            "latest_at": rows[0].get("created_at") or rows[0].get("updated_at") or "",
            "order_ids": [str(r.get("order_id") or "") for r in rows[:threshold]],
            "hint": (f"该商品累计只有 {len(rows)} 单（阈值 {threshold}）："
                     "建议人工核对发出的内容与规格是否和买家所选一致"),
        })
    items.sort(key=lambda x: (x["order_count"], x["item_id"]))
    return {
        "ok": True,
        "dry_run": True,
        "threshold": threshold,
        "items": items,
        "note": "只是提示，不拦截发货；超过阈值后不再出现在这张表里",
    }


__all__ = ["after_sale_review", "first_n_order_prompts",
           "DEFAULT_WINDOW_HOURS", "DEFAULT_FIRST_N"]
