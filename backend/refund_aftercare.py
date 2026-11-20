"""
退款后处置账本（方向一：跨领域同构）
====================================

**问题的形状**

虚拟商品的退款和实物不一样：实物退款还能等买家寄回来，卡密发出去就**没了**。
我们原来的处理是一条状态变更 —— 订单从 `refunding` 走到 `cancelled`，写进 orders 表，
然后什么都不会发生。卖家想知道"这个月退款亏了多少、哪些单是钱货两失"，
只能自己去翻订单列表，一单一看。

欧美做数字商品发货的那批产品（Gumroad / Lemon Squeezy / SendOwl）把这件事拆成
两本账：**授权**和**履约**。退款发生时，能撤销的撤销（license / 设备绑定），
不能撤销的记成核销（write-off）。卡密属于后者 —— 没有"撤销"这个动作，
但这不妨碍我们把它记成一张账单。

**我们照做，但只做能做的部分**

- **不改发货逻辑**。退款该不该发货，三闸门和 `delivery_block_rules` 早就管了；
  这里只处理"已经发生"的退款。
- **不自动拉黑**。`refund_blacklist` 有自己的开关和口径，自动写会把卖家的
  判断权拿走。这里只在页面上给一条"是否拉黑"的提示。
- **损失按订单金额记，标注为上限**。我们不知道单张卡密的进货成本，
  所以 `loss` 是"这一单最多亏了多少"，不是净亏 —— 宁可写清楚口径，
  也不要给一个看起来很精确、其实是猜的数字。

**两种退款要分开**

- `refunding`（退款中）：买家可能撤销，先记账，不动任何东西；
- `cancelled`（已关闭）：平台只有"退款完成"才会走到这里，但**普通关单也会**。
  所以关闭只在"已经有一条退款记录"时更新，否则不认 —— 不然会把没付款的
  关单也算成退款损失。
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlite_runtime import connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = Path(os.environ.get("REFUND_AFTERCARE_DB") or (DATA_DIR / "refund_aftercare.db"))

REFUND_STATES = ("refunding", "cancelled")
_MONEY = re.compile(r"-?\d+(?:\.\d+)?")


def _money(text: Any) -> float:
    """订单金额是 TEXT（"12.50" / "¥12.50" / "12.5元"），统一成 float。"""
    m = _MONEY.search(str(text or "").replace(",", ""))
    try:
        return float(m.group(0)) if m else 0.0
    except Exception:
        return 0.0


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS refunds (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            cookie_id   TEXT NOT NULL,
            order_id    TEXT NOT NULL,
            item_id     TEXT DEFAULT '',
            buyer_id    TEXT DEFAULT '',
            status      TEXT DEFAULT '',        -- refunding|cancelled
            amount      REAL DEFAULT 0,         -- 订单金额（损失上限）
            sent_cards  INTEGER DEFAULT 0,      -- 已经发出去的卡
            total_cards INTEGER DEFAULT 0,      -- 账本里一共有几张
            loss        REAL DEFAULT 0,         -- 记成损失的数（未发货 = 0）
            recoverable INTEGER DEFAULT 0,      -- 1 = 还没发出去，停发即可
            action      TEXT DEFAULT '',        -- 给人看的一句话
            resolved    INTEGER DEFAULT 0,      -- 人处理完了没有
            note        TEXT DEFAULT '',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            UNIQUE (cookie_id, order_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_refund_created ON refunds(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_refund_open ON refunds(resolved, recoverable)")
    conn.commit()
    return conn


@contextmanager
def _tx():
    """短连接：自动提交，并且**一定关闭**（sqlite 的 `with conn` 只管事务，不管关闭）。"""
    conn = _conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _read_cards(order_id: str, cookie_id: str) -> List[Dict[str, Any]]:
    try:
        import delivery_reliability as dr
        return dr.cards(order_id, cookie_id) or []
    except Exception as e:
        logger.warning(f"读取发货账本失败（退款照记，卡数按 0 算）: {e}")
        return []


def _read_order(order_id: str) -> Dict[str, Any]:
    try:
        from db_manager import db_manager
        return db_manager.get_order_by_id(order_id) or {}
    except Exception as e:
        logger.warning(f"读取订单失败（退款照记，金额按 0 算）: {e}")
        return {}


def _classify(sent: int, total: int, amount: float) -> Dict[str, Any]:
    """
    退款时这单还能不能救：
      - 一张都没发出去 → 停发即可，损失 0，可回收
      - 已经发了 k 张    → 卡密是一次性消耗品，收不回来；按订单金额记上限
    """
    if sent <= 0:
        return {"recoverable": 1, "loss": 0.0,
                "action": "还没发出去，停发即可，无损失" if total else "账本里没有卡，确认是否走的是别的发货方式"}
    return {"recoverable": 0, "loss": round(float(amount or 0), 2),
            "action": f"已发出 {sent}/{total} 张，卡密一次性，收不回来；人工判断是否拉黑/协商"}


def record(order_id: str, cookie_id: str, status: str = "refunding", context: str = "") -> Dict[str, Any]:
    """
    退款发生（`refunding`）时记一笔。幂等：同一单重复来只更新，不重复记账/通知。
    永不抛异常 —— 调用点在订单状态写入之后，退款记账绝不能让状态写入失败。
    """
    try:
        order = _read_order(order_id)
        cards = _read_cards(order_id, cookie_id)
        sent = len([c for c in cards if str(c.get("status") or "") == "sent"])
        total = len(cards)
        amount = _money(order.get("amount"))
        cls = _classify(sent, total, amount)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with _tx() as conn:
            old = conn.execute(
                "SELECT id, sent_cards, loss FROM refunds WHERE cookie_id=? AND order_id=?",
                (str(cookie_id), str(order_id))).fetchone()
            conn.execute(
                """INSERT INTO refunds
                   (cookie_id, order_id, item_id, buyer_id, status, amount,
                    sent_cards, total_cards, loss, recoverable, action, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cookie_id, order_id) DO UPDATE SET
                     status=excluded.status, amount=excluded.amount,
                     sent_cards=excluded.sent_cards, total_cards=excluded.total_cards,
                     loss=excluded.loss, recoverable=excluded.recoverable,
                     action=excluded.action, updated_at=excluded.updated_at""",
                (str(cookie_id), str(order_id), str(order.get("item_id") or ""),
                 str(order.get("buyer_id") or ""), str(status or "refunding"), amount,
                 sent, total, cls["loss"], cls["recoverable"], cls["action"], now, now))
            new_id = conn.execute(
                "SELECT id FROM refunds WHERE cookie_id=? AND order_id=?",
                (str(cookie_id), str(order_id))).fetchone()["id"]

        fresh = old is None
        if fresh and sent > 0:
            try:
                import notify_service
                notify_service.send_event(
                    "risk", "💸 退款：已发货",
                    f"订单 {order_id} 退款，已发出 {sent} 张卡密（一次性，收不回）。"
                    f"订单金额 {amount:.2f}，记损失上限。{('上下文：' + context) if context else ''}")
            except Exception as e:
                logger.warning(f"退款通知发送失败（不影响记账）: {e}")
            logger.warning(f"退款：订单 {order_id} 已发 {sent}/{total} 张，金额 {amount:.2f}")

        return {"ok": True, "id": new_id, "fresh": fresh, "sent_cards": sent,
                "total_cards": total, "loss": cls["loss"],
                "recoverable": bool(cls["recoverable"]), "action": cls["action"]}
    except Exception as e:
        logger.error(f"退款记账失败（不影响订单状态）: {e}")
        return {"ok": False, "error": str(e)[:200]}


def close(order_id: str, cookie_id: str, context: str = "") -> Dict[str, Any]:
    """
    订单关闭。**只有已经记过退款**的才更新 —— 普通关单（没付款/超时）不是退款，
    跟着记会把损失算多。
    """
    try:
        with _tx() as conn:
            row = conn.execute(
                "SELECT id,sent_cards,amount FROM refunds WHERE cookie_id=? AND order_id=?",
                (str(cookie_id), str(order_id))).fetchone()
            if not row:
                return {"ok": True, "skipped": "没有退款记录，按普通关单处理"}
            conn.execute(
                "UPDATE refunds SET status='cancelled', updated_at=? WHERE id=?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), row["id"]))
        return {"ok": True, "id": row["id"], "sent_cards": row["sent_cards"]}
    except Exception as e:
        logger.error(f"退款关闭记账失败（不影响订单状态）: {e}")
        return {"ok": False, "error": str(e)[:200]}


def on_status(order_id: str, cookie_id: str, new_status: str, context: str = "") -> Dict[str, Any]:
    """订单状态变更的统一入口（order_status_handler 调用）。"""
    st = str(new_status or "")
    if st == "refunding":
        return record(order_id, cookie_id, "refunding", context)
    if st == "cancelled":
        return close(order_id, cookie_id, context)
    return {"ok": True, "skipped": f"{st} 不是退款状态"}


def _since(days: int) -> str:
    return (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")


def stats(days: int = 30) -> Dict[str, Any]:
    try:
        with _tx() as conn:
            row = conn.execute(
                """SELECT COUNT(*) n,
                          COALESCE(SUM(loss),0) loss,
                          COALESCE(SUM(CASE WHEN sent_cards>0 THEN 1 ELSE 0 END),0) with_sent,
                          COALESCE(SUM(CASE WHEN recoverable=1 THEN 1 ELSE 0 END),0) recoverable,
                          COALESCE(SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END),0) open
                   FROM refunds WHERE created_at>=?""", (_since(days),)).fetchone()
        return {"ok": True, "days": int(days), "count": int(row["n"] or 0),
                "loss": round(float(row["loss"] or 0), 2), "with_sent": int(row["with_sent"] or 0),
                "recoverable": int(row["recoverable"] or 0), "open": int(row["open"] or 0)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], "count": 0, "loss": 0.0,
                "with_sent": 0, "recoverable": 0, "open": 0}


def open_items(limit: int = 50, only_loss: bool = False) -> List[Dict[str, Any]]:
    where = "resolved=0" + (" AND sent_cards>0" if only_loss else "")
    try:
        with _tx() as conn:
            rows = conn.execute(
                f"SELECT * FROM refunds WHERE {where} ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"读取待处理退款失败: {e}")
        return []


def has_record(order_id: str) -> bool:
    """该订单是否进过退款账本（退款中/已退款都算）。读不到时按“有退款”处理。"""
    try:
        with _tx() as conn:
            return conn.execute(
                "SELECT 1 FROM refunds WHERE order_id=? LIMIT 1",
                (str(order_id or ""),)).fetchone() is not None
    except Exception as e:
        logger.warning(f"查询退款账本失败，按存在退款处理: {e}")
        return True


def resolve(row_id: int, note: str = "") -> bool:
    try:
        with _tx() as conn:
            cur = conn.execute(
                "UPDATE refunds SET resolved=1, note=?, updated_at=? WHERE id=?",
                (str(note or "")[:200], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(row_id)))
            return cur.rowcount == 1
    except Exception as e:
        logger.warning(f"标记退款已处理失败: {e}")
        return False


def summary(days: int = 30) -> Dict[str, Any]:
    return {"ok": True, "stats": stats(days), "open": open_items(50, only_loss=False)}


__all__ = ["REFUND_STATES", "record", "close", "on_status", "stats",
           "open_items", "resolve", "summary"]
