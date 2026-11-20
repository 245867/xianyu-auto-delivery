"""
服务型商品复盘（P2）
====================
对照拆解 23 的结论：服务型的强信号在**对话**里，不在搜索页。我们能算的是
咨询条数/人数、有回复率、未命中主题；拆解 23 同时点名四个缺口——首响耗时、
退款原因码、服务工时、服务单类型——本模块**不编数**，缺的字段一律
`missing` + 原因展示。

数据来源：
- `faq_kb.db` 的 questions 表（只读）：咨询条数、人数、答上率、未命中原文；
- `db_manager` 的 orders / item_info（只读）：服务单、完成/退款/状态不明、复购；
- 服务单识别：显式 item_id 列表，或商品标题命中服务关键词（默认见 CONFIG）。

测试可注入 questions/orders/item_titles，完全不碰真实库。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlite_runtime import connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = Path(os.environ.get("SERVICE_REVIEW_CONFIG") or (DATA_DIR / "service_review.json"))
FAQ_DB_PATH = Path(os.environ.get("FAQ_DB") or (DATA_DIR / "faq_kb.db"))

DEFAULT_CONFIG: Dict[str, Any] = {
    # 标题命中任一关键词即视为服务型商品（可按自店实际改）
    "service_keywords": ["代写", "点评", "润色", "服务", "咨询", "定制", "辅导"],
    # 显式指定服务商品 item_id（优先于关键词）
    "service_item_ids": [],
}

ANSWERED = frozenset({"hit", "ai", "manual", "default", "fallback"})


def get_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {})
    except Exception:
        pass
    return cfg


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_config()
    if isinstance(patch.get("service_keywords"), list):
        cfg["service_keywords"] = [str(x).strip() for x in patch["service_keywords"] if str(x).strip()]
    if isinstance(patch.get("service_item_ids"), list):
        cfg["service_item_ids"] = [str(x).strip() for x in patch["service_item_ids"] if str(x).strip()]
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


# ---------------------------------------------------------------- 默认数据源（只读）

def _since(days: int) -> str:
    return (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")


def _load_questions(days: int, cookie_id: str = "") -> List[Dict[str, Any]]:
    if not FAQ_DB_PATH.exists():
        return []
    try:
        conn = connect_sqlite(FAQ_DB_PATH)
        conn.row_factory = sqlite3.Row
        sql = ("SELECT id, cookie_id, chat_id, user_id, item_id, raw, outcome, created_at "
               "FROM questions WHERE created_at>=?")
        args: list = [_since(days)]
        if cookie_id:
            sql += " AND cookie_id=?"
            args.append(cookie_id)
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def _load_orders(cookie_id: str = "", limit: int = 1000) -> List[Dict[str, Any]]:
    try:
        from db_manager import db_manager
        if cookie_id:
            return db_manager.get_orders_by_cookie(cookie_id, limit=limit) or []
        return db_manager.get_all_orders(limit=limit) or []
    except Exception:
        return []


def _load_title(cookie_id: str, item_id: str) -> str:
    try:
        from db_manager import db_manager
        info = db_manager.get_item_info(cookie_id, item_id) or {}
        return str(info.get("item_title") or info.get("title") or "")
    except Exception:
        return ""


def _is_service(cookie_id: str, item_id: str, title: str,
                keywords: Iterable[str], explicit: Iterable[str]) -> bool:
    if item_id and item_id in set(explicit):
        return True
    if title:
        return any(k in title for k in keywords)
    return False


def _pct(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


# ---------------------------------------------------------------- 复盘

def review(days: int = 30, cookie_id: str = "", *, now: Optional[datetime] = None,
           questions: Optional[List[Dict[str, Any]]] = None,
           orders: Optional[List[Dict[str, Any]]] = None,
           item_titles: Optional[Dict[Tuple[str, str], str]] = None,
           config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    产出服务型 SKU 复盘。所有比率都带口径；缺字段用 missing_fields 明示。
    `questions/orders/item_titles` 可注入（测试用），默认只读真实库。
    """
    cfg = config or get_config()
    keywords = cfg.get("service_keywords") or []
    explicit = cfg.get("service_item_ids") or []
    now_dt = now or datetime.now()
    days = max(1, int(days))

    qs = questions if questions is not None else _load_questions(days, cookie_id)
    if cookie_id:
        qs = [q for q in qs if str(q.get("cookie_id") or "") == cookie_id]
    os_ = orders if orders is not None else _load_orders(cookie_id)
    if cookie_id:
        os_ = [o for o in os_ if str(o.get("cookie_id") or "") == cookie_id]

    # 标题：显式注入 → 逐单只读补齐（上限保护：订单 1000 单内，唯一 item 数通常很小）
    titles = dict(item_titles or {})
    missing_title_cache: Dict[Tuple[str, str], str] = {}
    for o in os_:
        cid = str(o.get("cookie_id") or "")
        iid = str(o.get("item_id") or "")
        key = (cid, iid)
        if key in titles or not iid:
            continue
        if key not in missing_title_cache:
            missing_title_cache[key] = _load_title(cid, iid)
        titles[key] = missing_title_cache[key]

    service_orders: List[Dict[str, Any]] = []
    for o in os_:
        cid = str(o.get("cookie_id") or "")
        iid = str(o.get("item_id") or "")
        title = titles.get((cid, iid), "")
        if _is_service(cid, iid, title, keywords, explicit):
            service_orders.append({**o, "_title": title})

    # ---- 咨询（问题侧）
    service_item_ids = {str(o.get("item_id") or "") for o in service_orders if o.get("item_id")}
    linked_qs = [q for q in qs if str(q.get("item_id") or "") in service_item_ids] if service_item_ids else []
    unlinked_qs = [q for q in qs if str(q.get("item_id") or "") not in service_item_ids]
    consult_total = len(linked_qs)
    consult_users = {str(q.get("user_id") or q.get("chat_id") or "") for q in linked_qs}
    consult_users.discard("")
    answered = sum(1 for q in linked_qs if str(q.get("outcome") or "") in ANSWERED)
    unanswered = [q for q in linked_qs if str(q.get("outcome") or "") not in ANSWERED]

    # ---- 订单侧
    def st(o):
        return str(o.get("status") or o.get("order_status") or "unknown")

    total = len(service_orders)
    completed = sum(1 for o in service_orders if st(o) == "completed")
    refunding = sum(1 for o in service_orders if st(o) == "refunding")
    shipped = sum(1 for o in service_orders if st(o) == "shipped")
    unknown = sum(1 for o in service_orders if st(o) == "unknown")
    buyers: Dict[str, int] = {}
    for o in service_orders:
        b = str(o.get("buyer_id") or "")
        if b:
            buyers[b] = buyers.get(b, 0) + 1
    repeat_buyers = {b: n for b, n in buyers.items() if n >= 2}

    # ---- 缺失字段（拆解 23 点名、我们确实没有的）
    missing_fields = [
        {"field": "首响耗时", "reason": "问题与回复没有同一个消息 id/时间对，无法算首响"},
        {"field": "交付工时", "reason": "没有服务单工时记录（人工交付未计时）"},
        {"field": "退款原因码", "reason": "账本只记 refund 类型，没有平台原因码"},
        {"field": "服务单类型", "reason": "订单表没有服务类型字段（代写/点评/加急无法区分）"},
    ]

    return {
        "ok": True,
        "days": days,
        "cookie_id": cookie_id,
        "config": cfg,
        "generated_at": now_dt.isoformat(timespec="seconds"),
        "services": {
            "orders_total": total,
            "orders_completed": completed,
            "orders_shipped": shipped,
            "orders_refunding": refunding,
            "orders_unknown": unknown,
            "completion_rate": _pct(completed, total),
            "refund_rate": _pct(refunding, total),
            "buyers": len(buyers),
            "repeat_buyers": len(repeat_buyers),
            "repeat_rate": _pct(len(repeat_buyers), len(buyers)),
            "gmv_yuan": round(sum(_num(o.get("amount")) for o in service_orders), 2),
            "order_list": service_orders[:200],
        },
        "consult": {
            "questions_total": consult_total,
            "consultants": len(consult_users),
            "answered": answered,
            "answer_rate": _pct(answered, consult_total),
            "unanswered": unanswered[:50],
            "unanswered_count": len(unanswered),
            "questions_unlinked": len(unlinked_qs),
            "frequency_per_buyer": _pct(consult_total, len(consult_users)) if consult_users else 0.0,
            "conversion": _pct(total, len(consult_users)) if consult_users else 0.0,
            "note": "转化率口径：服务单数 ÷ 有商品标识的咨询人数；问题未带 item_id 时单列不并入",
        },
        "missing_fields": missing_fields,
        "notes": [
            "样本口径：咨询取最近 N 天、商品标题命中服务关键词（或显式 item_id）的提问与订单",
            "首响耗时/交付工时/退款原因码/服务单类型：本机无数据，按缺失展示，不估算",
            "复购按 buyer_id 在服务单里出现 ≥2 次计；买家未留 ID 的不计入分母",
        ],
    }


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["get_config", "save_config", "review", "DEFAULT_CONFIG"]
