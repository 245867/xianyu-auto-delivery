"""
回复发件箱（拆解 10：`falses00/Falses-Goofish-GuardAgent` 的 `core/reply_outbox.py`）
=================================================================================

**我们原来的状态：回复是"生成即发"，只有一张内存里的去重表。**

```python
self.processed_message_ids = {}      # 进程内，重启即失忆
...
if message_id in self.processed_message_ids: return
```

这张表挡住了同一进程内的重复推送，但挡不住两件事：

1. **重启/断线重连**。内存清空，平台把最近的消息重推一遍，买家就收到两条一样的回复。
2. **崩在"发送"中间**。日志里既没有"已发"，也没有"没发"，下次只能凭猜。

发商品、发货都有落盘幂等（`idempotency.py`）+ 结果未知态（`submission_unknown`），
唯独回复这条最高频的链路没有。而回复是**对买家说的话**：重复发送不是"多一条日志"，
是卖家在买家眼里像个复读机；静默丢失则是买家问了没人理。

## 它怎么做的

`ReplyOutbox` 是一张 `UNIQUE dedupe_key` 的 SQLite 表，状态机：

```
pending ──claim──> sending ──> sent | skipped
                     └───────> failed
```

关键在于 **`sending` 这个中间态**：`claim_for_send()` 用 `BEGIN IMMEDIATE` 把
"读状态 + 改状态"做成一个事务，所以两个 worker 不可能同时看到 `pending` 并各发一条；
同一个 key 的第二次 claim 会拿到 `already_sending` 直接退出。它的注释写得很清楚：
**"端到端 exactly-once 仍然依赖远端平台的幂等或回执"**——发件箱能保证的是
"本机最多一个正在发"，不是"平台上只有一条"。

## 我们抄了什么，改了什么

抄：状态机、`UNIQUE(cookie_id, source_msg_id)`、`BEGIN IMMEDIATE` 的抢占、
`skipped` 也是终态（被合规闸拦下的回复，重放时不该再放行一次）。

改（两处，都是和我们已有的铁律对齐）：

1. **过期的 `sending` 判 `unknown`，不自动重发。** 它 300 秒后重新 claim 再发一次，
   接受重复。我们不行：C3 那条铁律是"结果未知永不自动补"（买家可能已经收到了），
   回复同理。过期 `sending` 一律落 `unknown`，等人在页面上看一眼。
2. **只有 `pending` 会自动补发。** `pending` 的含义是"登记了但一个字节都还没发出去"
   （崩在生成之后、发送之前），补发绝对安全。`failed` / `unknown` 都要人工放行——
   和 `platform_ship` 的 `stopped` 需要 `retry_platform_ship()` 一样：**每个停止态都要留一个人的出口。**

## 表

    reply_outbox(
      cookie_id, source_msg_id,   -- 唯一键：同一账号下的同一条买家消息
      chat_id, item_id, user_id,  -- 回放时要用的路由信息
      reply_text, payload_kind, payload,  -- 要发的东西（text / image / image_text）
      status, attempts, last_error,
      created_at, updated_at, sent_at
    )
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlite_runtime import connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 测试用 REPLY_OUTBOX_DB 指到临时目录，绝不能在测试里碰真实发件箱
DB_PATH = Path(os.environ.get("REPLY_OUTBOX_DB") or (PROJECT_ROOT / "data" / "reply_outbox.db"))

KEEP_DAYS = 30
LEASE_SECONDS = 300

# 状态机
# held = 三档里的 draft（拆解 39 §八-3）：内容涉及售后/退款/承诺，
# 默认扣下不发，等人放行或弃用。它不是"发送失败"，所以不能混进 failed。
STATUSES = ("pending", "sending", "sent", "failed", "unknown", "skipped", "held")
# 终态：不会再自动发，也不会被 claim
TERMINAL = ("sent", "skipped")
# 停止态：不会自动补，但人可以放行
HOLD = ("unknown", "failed", "held")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reply_outbox (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            cookie_id     TEXT NOT NULL,
            source_msg_id TEXT NOT NULL,
            chat_id       TEXT DEFAULT '',
            item_id       TEXT DEFAULT '',
            user_id       TEXT DEFAULT '',
            reply_text    TEXT DEFAULT '',
            payload_kind  TEXT DEFAULT 'text',
            payload       TEXT DEFAULT '',
            tier          TEXT DEFAULT '',       -- auto|draft|block（拆解 39 §八-3）
            status        TEXT NOT NULL DEFAULT 'pending',
            attempts      INTEGER DEFAULT 0,
            last_error    TEXT DEFAULT '',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            sent_at       TEXT DEFAULT '',
            UNIQUE (cookie_id, source_msg_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reply_outbox_status "
                 "ON reply_outbox(status, updated_at)")
    # 老库补列：用 ADD COLUMN 不动已有数据（同 care_actions / platform_ship 的做法）
    import migrate
    migrate.ensure_columns(conn, "reply_outbox", {
        "payload_kind": "TEXT DEFAULT 'text'",
        "payload": "TEXT DEFAULT ''",
        "chat_id": "TEXT DEFAULT ''",
        "item_id": "TEXT DEFAULT ''",
        "user_id": "TEXT DEFAULT ''",
        "sent_at": "TEXT DEFAULT ''",
        # 拆解 39 §八-3：回复三档（auto/draft/block）。这里只存结论，
        # 判定在 reply_guard.classify()，发件箱不重复实现规则。
        "tier": "TEXT DEFAULT ''",
    }, commit=False)
    return conn


def _row(conn: sqlite3.Connection, cookie_id: str, source_msg_id: str) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM reply_outbox WHERE cookie_id=? AND source_msg_id=?",
        (str(cookie_id or ""), str(source_msg_id or "")))
    row = cur.fetchone()
    return dict(row) if row else None


def _stale_before(lease_seconds: int) -> str:
    return (datetime.now() - timedelta(seconds=max(0, int(lease_seconds)))).isoformat(timespec="seconds")


def get(cookie_id: str, source_msg_id: str) -> Optional[Dict[str, Any]]:
    conn = _conn()
    try:
        return _row(conn, cookie_id, source_msg_id)
    finally:
        conn.close()


def status_of(cookie_id: str, source_msg_id: str, lease_seconds: int = LEASE_SECONDS) -> str:
    """
    这条消息现在是什么状态（不存在返回 ''）。

    `sending` 超过租约就说明干活的人已经没了：转成 `unknown` 并返回 `unknown`，
    而不是返回 `sending` 让上层以为"别人还在发"。**转 unknown 之后不会自动重发。**
    """
    conn = _conn()
    try:
        row = _row(conn, cookie_id, source_msg_id)
        if not row:
            return ""
        if row["status"] == "sending" and (row["updated_at"] or "") <= _stale_before(lease_seconds):
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE reply_outbox SET status='unknown', last_error=?, updated_at=? "
                "WHERE cookie_id=? AND source_msg_id=? AND status='sending'",
                ("发送中崩溃，结果未知（不自动重发）", _now(), cookie_id, source_msg_id))
            conn.execute("COMMIT")
            return "unknown"
        return row["status"]
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def begin(cookie_id: str, source_msg_id: str, chat_id: str = "", item_id: str = "",
          user_id: str = "", reply_text: str = "", payload_kind: str = "text",
          payload: str = "", tier: str = "", lease_seconds: int = LEASE_SECONDS) -> Dict[str, Any]:
    """
    登记一条待发回复并抢占发送权（pending/failed → sending，attempts+1）。

    返回 {"ok", "status", "reason", "record"}：
      ok=True            抢到了，可以发
      reason=in_flight   别人正在发（租约内）
      reason=done        已经发过 / 已跳过
      reason=unknown     结果未知，等人工
    """
    now = _now()
    stale = _stale_before(lease_seconds)
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn, cookie_id, source_msg_id)
        if not row:
            conn.execute(
                "INSERT INTO reply_outbox (cookie_id, source_msg_id, chat_id, item_id, user_id,"
                " reply_text, payload_kind, payload, tier, status, attempts, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,'pending',0,?,?)",
                (str(cookie_id or ""), str(source_msg_id or ""), str(chat_id or ""), str(item_id or ""),
                 str(user_id or ""), str(reply_text or ""), str(payload_kind or "text"),
                 str(payload or ""), str(tier or ""), now, now))
            row = _row(conn, cookie_id, source_msg_id)

        status = row["status"]
        if status in TERMINAL:
            conn.execute("COMMIT")
            return {"ok": False, "status": status, "reason": "done", "record": row}
        if status == "unknown":
            conn.execute("COMMIT")
            return {"ok": False, "status": status, "reason": "unknown", "record": row}
        if status == "held":
            # draft 档被扣下的行：只有人在页面上放行（→ pending）才会再发。
            conn.execute("COMMIT")
            return {"ok": False, "status": status, "reason": "held", "record": row}
        if status == "sending":
            if (row["updated_at"] or "") > stale:
                conn.execute("COMMIT")
                return {"ok": False, "status": status, "reason": "in_flight", "record": row}
            # 租约过期：转 unknown，绝不自动重发（与 C3 的"未知不补"一致）
            conn.execute(
                "UPDATE reply_outbox SET status='unknown', last_error=?, updated_at=? "
                "WHERE cookie_id=? AND source_msg_id=? AND status='sending'",
                ("发送中崩溃，结果未知（不自动重发）", now, cookie_id, source_msg_id))
            conn.execute("COMMIT")
            return {"ok": False, "status": "unknown", "reason": "unknown",
                    "record": _row(conn, cookie_id, source_msg_id)}

        # pending / failed → sending
        conn.execute(
            "UPDATE reply_outbox SET status='sending', attempts=attempts+1,"
            " reply_text=CASE WHEN ?<>'' THEN ? ELSE reply_text END,"
            " payload_kind=?, payload=CASE WHEN ?<>'' THEN ? ELSE payload END,"
            " chat_id=CASE WHEN ?<>'' THEN ? ELSE chat_id END,"
            " item_id=CASE WHEN ?<>'' THEN ? ELSE item_id END,"
            " user_id=CASE WHEN ?<>'' THEN ? ELSE user_id END,"
            " tier=CASE WHEN ?<>'' THEN ? ELSE tier END,"
            " last_error='', updated_at=? "
            "WHERE cookie_id=? AND source_msg_id=? AND status IN ('pending','failed')",
            (str(reply_text or ""), str(reply_text or ""), str(payload_kind or "text"),
             str(payload or ""), str(payload or ""),
             str(chat_id or ""), str(chat_id or ""),
             str(item_id or ""), str(item_id or ""),
             str(user_id or ""), str(user_id or ""),
             str(tier or ""), str(tier or ""),
             now, cookie_id, source_msg_id))
        conn.execute("COMMIT")
        return {"ok": True, "status": "sending", "reason": "claimed",
                "record": _row(conn, cookie_id, source_msg_id)}
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def finish(cookie_id: str, source_msg_id: str, status: str, error: str = "") -> Optional[Dict[str, Any]]:
    """
    收尾：sent / skipped / failed / unknown。

    `sent` 之后不会再被 claim；`failed` / `unknown` 只能由人放行（`release`）。
    """
    status = str(status or "")
    if status not in ("sent", "skipped", "failed", "unknown", "held"):
        raise ValueError(f"不支持的收尾状态: {status}")
    now = _now()
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn, cookie_id, source_msg_id)
        if not row:
            conn.execute("COMMIT")
            return None
        # 已被别人收尾（例如两次发送）就不要覆盖结论。
        # 两个例外：`unknown`/`held` → `skipped`，那是人看过之后说"不用再发了"。
        if row["status"] in TERMINAL or \
                (row["status"] in ("unknown", "held") and status != "skipped"):
            conn.execute("COMMIT")
            return row
        conn.execute(
            "UPDATE reply_outbox SET status=?, last_error=?, updated_at=?,"
            " sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END "
            "WHERE cookie_id=? AND source_msg_id=?",
            (status, str(error or "")[:300], now, status, now, cookie_id, source_msg_id))
        conn.execute("COMMIT")
        return _row(conn, cookie_id, source_msg_id)
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def release(cookie_id: str, source_msg_id: str) -> bool:
    """
    人工放行：`failed` / `unknown` / `held` → `pending`，让补发链路再发一次。

    这是停止态**唯一**的出口。没有它，一次误判就只能删数据库。
    `held` 是 draft 档（售后/承诺类）默认扣下的行，放行等于人说"这句可以发"。
    """
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE reply_outbox SET status='pending', last_error='', updated_at=? "
            "WHERE cookie_id=? AND source_msg_id=? AND status IN ('failed','unknown','held')",
            (_now(), cookie_id, source_msg_id))
        return cur.rowcount == 1
    finally:
        conn.close()


def hold(cookie_id: str, source_msg_id: str, chat_id: str = "", item_id: str = "",
         user_id: str = "", reply_text: str = "", payload_kind: str = "text",
         payload: str = "", tier: str = "draft", reason: str = "") -> Dict[str, Any]:
    """
    扣下不发：登记成 `held`（三档里的 draft），等人放行或弃用。

    与 `begin()` 分开是因为语义不同：begin 是"我现在要发"，hold 是"我不发，
    但这句回复值得留给人看"。**held 不会被心跳补发**（补发只认 pending）。
    """
    now = _now()
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn, cookie_id, source_msg_id)
        if not row:
            conn.execute(
                "INSERT INTO reply_outbox (cookie_id, source_msg_id, chat_id, item_id, user_id,"
                " reply_text, payload_kind, payload, tier, status, attempts, last_error,"
                " created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,'held',0,?,?,?)",
                (str(cookie_id or ""), str(source_msg_id or ""), str(chat_id or ""),
                 str(item_id or ""), str(user_id or ""), str(reply_text or ""),
                 str(payload_kind or "text"), str(payload or ""), str(tier or "draft"),
                 str(reason or "")[:300], now, now))
        elif row["status"] in ("pending", "failed", "unknown", "held"):
            # 不覆盖 sent / skipped / sending 的结论。
            conn.execute(
                "UPDATE reply_outbox SET status='held', tier=?, reply_text=CASE WHEN ?<>'' "
                "THEN ? ELSE reply_text END, last_error=?, updated_at=? "
                "WHERE cookie_id=? AND source_msg_id=?",
                (str(tier or "draft"), str(reply_text or ""), str(reply_text or ""),
                 str(reason or "")[:300], now, cookie_id, source_msg_id))
        conn.execute("COMMIT")
        return {"ok": True, "status": "held", "record": _row(conn, cookie_id, source_msg_id)}
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def pending_rows(cookie_id: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    """能自动补发的行：只有 `pending`（一个字节都没发出去过）。"""
    conn = _conn()
    try:
        if cookie_id:
            cur = conn.execute(
                "SELECT * FROM reply_outbox WHERE status='pending' AND cookie_id=? "
                "ORDER BY id LIMIT ?", (cookie_id, max(1, int(limit))))
        else:
            cur = conn.execute(
                "SELECT * FROM reply_outbox WHERE status='pending' ORDER BY id LIMIT ?",
                (max(1, int(limit)),))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def rows(status: str = "", cookie_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    conn = _conn()
    try:
        where, params = [], []
        if status:
            where.append("status=?")
            params.append(status)
        if cookie_id:
            where.append("cookie_id=?")
            params.append(cookie_id)
        sql = "SELECT * FROM reply_outbox"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def rows_by_tier(tier: str = "draft", limit: int = 30) -> List[Dict[str, Any]]:
    """按三档筛（拆解 39 §八-3）：页面要能单独看"需要人工收尾"的那批。"""
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM reply_outbox WHERE tier=? ORDER BY updated_at DESC LIMIT ?",
            (str(tier or ""), max(1, int(limit)))).fetchall()]
    finally:
        conn.close()


def stats() -> Dict[str, int]:
    conn = _conn()
    try:
        out = {s: 0 for s in STATUSES}
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM reply_outbox GROUP BY status"):
            out[r["status"]] = int(r["n"])
        out["hold"] = out.get("failed", 0) + out.get("unknown", 0)
        # 三档计数（空 tier = 加列前的老记录，不归任何一档）
        for r in conn.execute("SELECT tier, COUNT(*) AS n FROM reply_outbox "
                              "WHERE tier<>'' GROUP BY tier"):
            out[f"tier_{r['tier']}"] = int(r["n"])
        return out
    finally:
        conn.close()


def reap_stale(lease_seconds: int = LEASE_SECONDS) -> int:
    """把超过租约的 `sending` 全部转 `unknown`（不重发），返回条数。"""
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE reply_outbox SET status='unknown', last_error=?, updated_at=? "
            "WHERE status='sending' AND updated_at<=?",
            ("发送中崩溃，结果未知（不自动重发）", _now(), _stale_before(lease_seconds)))
        return cur.rowcount
    finally:
        conn.close()


def purge(keep_days: int = KEEP_DAYS) -> int:
    """清理旧终态记录。pending / sending / unknown 不删——它们还有账要看。"""
    before = (datetime.now() - timedelta(days=max(1, int(keep_days)))).isoformat(timespec="seconds")
    conn = _conn()
    try:
        cur = conn.execute(
            "DELETE FROM reply_outbox WHERE status IN ('sent','skipped','failed') AND updated_at<?",
            (before,))
        return cur.rowcount
    finally:
        conn.close()


__all__ = [
    "STATUSES", "TERMINAL", "HOLD", "LEASE_SECONDS",
    "get", "status_of", "begin", "hold", "finish", "release",
    "pending_rows", "rows", "rows_by_tier", "stats", "reap_stale", "purge",
]
