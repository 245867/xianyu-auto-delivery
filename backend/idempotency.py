"""
幂等表（A2）

解决的问题：同一件事被触发两次，就会发两次货、发两条商品、擦两次。
光靠内存标记不够（重启即失忆），所以落一张 SQLite 小表：

    op_idempotency(
      key        主键（动作+账号+业务标识的稳定摘要）
      action     publish / delivery / polish / review ...
      status     pending | done | failed | submission_unknown
      note       结果摘要（itemId、错误码等，不含凭据）
      created_at / updated_at
    )

约定：
- `claim()` 是原子的（INSERT OR IGNORE），谁先抢到谁执行；
- `claim_leased()` 在此基础上加**租约**：进程中途死掉，租约到期后任务可被接管，
  不会永久卡在 pending（这是 C3 发货"发到一半崩了"的兜底）；
- `submission_unknown` 是**禁止重发**状态：提交阶段断网/超时，平台可能已经收了，
  必须人工回读核对后用 `resolve()` 放行（Gate 0 第 6 条）；
- 记录只保留 30 天，避免无限增长。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from sqlite_runtime import connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 测试用 IDEMPOTENCY_DB 指到临时目录，绝不能在测试里碰真实幂等表
DB_PATH = Path(os.environ.get("IDEMPOTENCY_DB") or (PROJECT_ROOT / "data" / "idempotency.db"))
KEEP_DAYS = 30

STATUSES = ("pending", "done", "failed", "submission_unknown")

# 活跃态：这两个状态下的行如果租约过期，说明干活的人已经没了
ACTIVE = ("pending",)


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS op_idempotency (
            key        TEXT PRIMARY KEY,
            action     TEXT NOT NULL,
            status     TEXT NOT NULL,
            note       TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_idem_action_time "
                 "ON op_idempotency(action, updated_at)")
    # 人工核对留痕（append-only）：submission_unknown 被核对时，先把原记录
    # 快照写进这张表，再改/删幂等行。这样"平台没收到 → 允许重发"删掉的只是
    # 幂等键，原始发布记录、核对结果、理由、时间都还在。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS op_verify_log (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            key                TEXT NOT NULL,
            action             TEXT DEFAULT '',
            outcome            TEXT NOT NULL,
            reason             TEXT DEFAULT '',
            matched_item_id    TEXT DEFAULT '',
            original_status    TEXT DEFAULT '',
            original_note      TEXT DEFAULT '',
            original_created_at TEXT DEFAULT '',
            original_updated_at TEXT DEFAULT '',
            created_at         TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_verify_key_time "
                 "ON op_verify_log(key, created_at)")
    # 老库补列：attempts / lease_owner / lease_until。
    # 用 ADD COLUMN 而不是重建表 —— 升级时不能动用户已有数据。
    import migrate
    migrate.ensure_columns(conn, "op_idempotency", {
        "attempts": "INTEGER DEFAULT 0",
        "lease_owner": "TEXT DEFAULT ''",
        "lease_until": "REAL DEFAULT 0",
    }, commit=False)
    conn.commit()
    return conn


def make_key(action: str, *parts: Any) -> str:
    """稳定键：动作 + 业务要素（账号/标题/图片/订单号）的 sha1。"""
    raw = "|".join(str(p or "") for p in parts)
    return f"{action}:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:24]}"


def _row(conn: sqlite3.Connection, key: str) -> Optional[Dict[str, Any]]:
    cur = conn.execute("SELECT * FROM op_idempotency WHERE key=?", (key,))
    row = cur.fetchone()
    return dict(row) if row else None


def get(key: str) -> Optional[Dict[str, Any]]:
    conn = _conn()
    try:
        return _row(conn, key)
    finally:
        conn.close()


def precheck(key: str) -> Dict[str, Any]:
    """
    只读预检（B5）：先看这条业务是否已经提交过，**再决定要不要花配额**。

    为什么要单独一个函数：旧顺序是"先取配额、再查幂等"，重复点击同一关键词的
    第二次会把 60 秒的发布配额白吃掉，然后报"限流"而不是"这条已经提交过"——
    用户看到的原因完全是错的，真正该做的事（去核对/改标题）也被藏起来了。

    返回 {"exists", "blocked", "record"}。**绝不做任何写入**，可以放心放在
    限流之前调用（并发下仍由 claim() 兜底，这里只负责把常见情况提前说清楚）。
    """
    rec = get(key)
    return {"exists": bool(rec),
            "blocked": bool(rec and rec.get("status") == "submission_unknown"),
            "record": rec}


def claim(key: str, action: str = "", note: str = "") -> Dict[str, Any]:
    """
    抢占一个幂等键。
    返回 {"claimed": bool, "record": dict|None, "blocked": bool}
    blocked=True 表示这个键处于 submission_unknown，禁止再发。
    """
    now = datetime.now().isoformat(timespec="seconds")
    conn = _conn()
    try:
        existing = _row(conn, key)
        if existing:
            return {"claimed": False, "record": existing,
                    "blocked": existing["status"] == "submission_unknown"}
        cur = conn.execute(
            "INSERT OR IGNORE INTO op_idempotency (key, action, status, note, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)", (key, action or key.split(":")[0], "pending", note[:300], now, now))
        conn.commit()
        if cur.rowcount <= 0:
            # 先查后插之间，别的线程刚把这一行插进去 → 我方这条 INSERT 被静默忽略。
            # 必须按 rowcount 判赢家：否则两个线程都拿到 claimed=True，所谓"原子
            # claim"只是摆设（用屏障让两个线程同时读到空行即可复现，claim_leased 同病）。
            row = _row(conn, key)
            return {"claimed": False, "record": row,
                    "blocked": bool(row and row["status"] == "submission_unknown")}
        row = _row(conn, key)
        return {"claimed": True, "record": row, "blocked": False}
    finally:
        conn.close()


def _lease_refusal(existing: Optional[Dict[str, Any]], now_ts: float,
                   allow_failed: bool) -> Optional[Dict[str, Any]]:
    """
    这条现有行能不能抢：不能抢就返回原因，能抢返回 None。

    预检与"UPDATE 没抢到之后的重读"共用一份判定——两处各写一套，迟早有一处
    漏掉新状态（比如以后加了 cancelled），输家就会拿到一个自相矛盾的回答。
    """
    if existing is None:
        return {"claimed": False, "record": None, "blocked": False, "reason": "conflict"}
    st = existing.get("status")
    if st == "done":
        return {"claimed": False, "record": existing, "blocked": False, "reason": "done"}
    if st == "submission_unknown":
        return {"claimed": False, "record": existing, "blocked": True, "reason": "unknown"}
    if st == "failed" and not allow_failed:
        return {"claimed": False, "record": existing, "blocked": False, "reason": "failed"}
    try:
        lease_until = float(existing.get("lease_until") or 0)
    except Exception:
        lease_until = 0.0
    if st == "pending" and lease_until > now_ts:
        return {"claimed": False, "record": existing, "blocked": False, "reason": "running"}
    if st not in ("pending", "failed"):
        # 没写接管规则的状态一律拒绝，不默认放行
        return {"claimed": False, "record": existing, "blocked": False,
                "reason": str(st or "unknown_status")}
    return None


def claim_leased(key: str, action: str = "", note: str = "",
                 owner: str = "", lease_seconds: int = 300,
                 allow_failed: bool = False) -> Dict[str, Any]:
    """
    带租约的抢占。给"发一条货"这种**必须知道到底发没发**的动作用。

    owner + lease_seconds 的意义：抢到的人负责在租约内干完并回调 finish；
    进程崩了租约自然过期，下一次扫描能接管，不会留下一个永远 pending 的孤儿。

    返回 reason：
      new / lease_taken_over   → claimed=True
      running                  → 别人正在干，别插一脚
      done                     → 已经干完了
      unknown（blocked=True）  → 提交结果未知，**禁止自动重发**，要人工回读
      failed                   → 上次失败；allow_failed=True 时才允许再抢
    """
    now_ts = time.time()
    now = datetime.now().isoformat(timespec="seconds")
    conn = _conn()
    try:
        existing = _row(conn, key)
        if existing is None:
            cur = conn.execute(
                "INSERT OR IGNORE INTO op_idempotency "
                "(key, action, status, note, created_at, updated_at, attempts, lease_owner, lease_until) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (key, action or key.split(":")[0], "pending", note[:300], now, now,
                 1, owner, now_ts + max(1, int(lease_seconds))))
            conn.commit()
            if cur.rowcount > 0:
                return {"claimed": True, "record": _row(conn, key),
                        "blocked": False, "reason": "new"}
            # 并发下别人先插进去了（INSERT OR IGNORE 静默跳过）。这时不能报 new：
            # 两个"抢到了"就是两条货。重新读那一行，按下面的状态机判 running/done/…
            existing = _row(conn, key)
            block = _lease_refusal(existing, now_ts, allow_failed)
            if block:
                return block

        block = _lease_refusal(existing, now_ts, allow_failed)
        if block:
            return block

        # pending 且租约过期（或 failed 且允许重试）→ 接管。
        # **WHERE 里必须带上"读到的状态（与租约仍过期）"**：两个 owner 同时读到
        # 同一条过期 pending 时，先到的那条 UPDATE 把它改成新租约，后到的那条
        # 就命中 0 行——由数据库判赢家，而不是各读各的然后都以为抢到了。
        st = existing["status"]
        lease_next = now_ts + max(1, int(lease_seconds))
        note_val = (note or existing.get("note") or "")[:300]
        if st == "pending":
            cur = conn.execute(
                "UPDATE op_idempotency SET status='pending', attempts=COALESCE(attempts,0)+1, "
                "lease_owner=?, lease_until=?, updated_at=?, note=? "
                "WHERE key=? AND status='pending' AND COALESCE(lease_until,0) <= ?",
                (owner, lease_next, now, note_val, key, now_ts))
        else:  # failed 且 allow_failed
            cur = conn.execute(
                "UPDATE op_idempotency SET status='pending', attempts=COALESCE(attempts,0)+1, "
                "lease_owner=?, lease_until=?, updated_at=?, note=? "
                "WHERE key=? AND status='failed'",
                (owner, lease_next, now, note_val, key))
        conn.commit()
        if cur.rowcount <= 0:
            # 没抢到：重读最新状态再回答（通常是 running），不要拿旧判断搪塞
            fresh = _row(conn, key)
            return _lease_refusal(fresh, time.time(), allow_failed) or {
                "claimed": False, "record": fresh, "blocked": False, "reason": "conflict"}
        return {"claimed": True, "record": _row(conn, key), "blocked": False,
                "reason": "lease_taken_over" if st == "pending" else "retry"}
    finally:
        conn.close()


def heartbeat(key: str, owner: str, lease_seconds: int = 300) -> bool:
    """续租。长任务每处理完一个子项调一次。owner 不匹配就不续（防止抢回来的任务被原主人续命）。"""
    now_ts = time.time()
    now = datetime.now().isoformat(timespec="seconds")
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE op_idempotency SET lease_until=?, updated_at=? "
            "WHERE key=? AND lease_owner=? AND status='pending'",
            (now_ts + max(1, int(lease_seconds)), now, key, owner))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def reap_expired(grace_seconds: int = 0) -> int:
    """
    把已过期租约清掉，让下一轮扫描可以接管。
    只动 pending，不动 done/unknown —— 那两个状态不该被"回收"。
    """
    cutoff = time.time() - max(0, int(grace_seconds))
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE op_idempotency SET lease_owner='', lease_until=0 "
            "WHERE status='pending' AND lease_until > 0 AND lease_until < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def finish(key: str, status: str = "done", note: str = "") -> bool:
    """
    标记结局。**不校验租约所有权**——按 key 无条件改，并清掉租约字段。

    现状证据：`finish()` 的六个调用点（一键发布 ×2、模板批量 ×2、僵尸重发 ×2）
    用的都是 `claim()`，键上没有"另一个人正在干"的概念，所以这个缺口目前
    打不到人；唯一的租约用户 `claim_leased()` 还没有生产调用方。
    等它接上生产链路时再加 `owner=` 校验（老主人拿着过期租约回调 finish，
    会把新主人刚接管的行改成 done/unknown）；本轮只记影响，不改接口。
    """
    if status not in STATUSES:
        raise ValueError(f"未知状态：{status}")
    now = datetime.now().isoformat(timespec="seconds")
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE op_idempotency SET status=?, note=?, updated_at=?, "
            "lease_owner='', lease_until=0 WHERE key=?",
            (status, note[:500], now, key))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def resolve(key: str, note: str = "人工回读确认已完成") -> bool:
    """把 submission_unknown 人工放行（核对平台确实没有/已完成后再调用）。"""
    return finish(key, "done", note)


# 人工核对的三种结果。必须由操作员明确选择，不做自动推断。
# - confirmed    平台上确实已有同一笔（可能已改价/待审核）→ 关闭为 done，禁止重发
# - not_received 平台确认没有收到 → 归档原记录后释放幂等键，允许重发
# - still_unknown 核对未完成（如待审核、分页没翻完）→ 保持 unknown，仅留痕
VERIFY_OUTCOMES = ("confirmed", "not_received", "still_unknown")


def verify_unknown(key: str, outcome: str, reason: str = "",
                   matched_item_id: str = "",
                   allow_pending: bool = False) -> Dict[str, Any]:
    """
    人工核对 submission_unknown 的**单条**出口（拆解 38 快鱼队列重置的收敛版）。

    约定：
    - 默认只有 submission_unknown 能核对；其他状态直接拒绝，避免被当成通用改状态接口。
    - 批处理恢复可显式传 `allow_pending=True`，处理“claim 后进程中断”留下的
      pending。调用方必须先确认该批次没有活动的 worker。
    - 理由必填：留痕要有"为什么这么判"，同标题同价格只是辅助证据，不是判据。
    - 先快照原记录到 op_verify_log，再改/删幂等行；原发布记录永不被静默丢弃。
    - 不做批量：一次只处理一个 key，避免一次错误放行多条未知记录。
    """
    if outcome not in VERIFY_OUTCOMES:
        return {"ok": False, "error": f"未知核对结果：{outcome}"}
    reason = (reason or "").strip()
    if not reason:
        return {"ok": False, "error": "核对理由必填（用于留痕）"}
    conn = _conn()
    try:
        row = _row(conn, key)
        if not row:
            return {"ok": False, "error": "记录不存在或已超过 30 天保留期"}
        original_status = str(row.get("status") or "")
        allowed_statuses = ("submission_unknown", "pending") if allow_pending else (
            "submission_unknown",
        )
        if original_status not in allowed_statuses:
            return {"ok": False,
                    "error": f"当前状态是 {row.get('status')}，只有 submission_unknown 需要人工核对"}
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO op_verify_log (key, action, outcome, reason, matched_item_id, "
            "original_status, original_note, original_created_at, original_updated_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (key, row.get("action", ""), outcome, reason[:300],
             (matched_item_id or "")[:80], original_status,
             row.get("note", "")[:300], row.get("created_at", ""),
             row.get("updated_at", ""), now))
        conn.commit()
    finally:
        conn.close()

    if outcome == "confirmed":
        suffix = f" itemId={matched_item_id}" if matched_item_id else ""
        finish(key, "done", note=f"人工核对：已上架{suffix}；{reason}"[:500])
        new_status = "done"
    elif outcome == "not_received":
        # 删的是幂等键（允许重发），原记录已进 op_verify_log
        release(key)
        new_status = "released"
    else:
        new_status = "submission_unknown"

    return {"ok": True, "key": key, "outcome": outcome,
            "previous_status": original_status, "new_status": new_status,
            "reason": reason, "matched_item_id": matched_item_id,
            "logged_at": now}


def verify_log(key: str = "", limit: int = 20) -> list:
    """人工核对留痕（新的在上）。按 key 查单条，留空查最近全部。"""
    conn = _conn()
    try:
        sql = "SELECT * FROM op_verify_log"
        args: list = []
        if key:
            sql += " WHERE key=?"
            args.append(key)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def release(key: str) -> bool:
    """删掉记录，允许重来（仅用于确定平台没收到的情况）。"""
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM op_idempotency WHERE key=?", (key,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def recent(limit: int = 30, action: str = "") -> list:
    cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    conn = _conn()
    try:
        conn.execute("DELETE FROM op_idempotency WHERE updated_at < ?", (cutoff,))
        conn.commit()
        sql = "SELECT * FROM op_idempotency"
        args: list = []
        if action:
            sql += " WHERE action=?"
            args.append(action)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def stats() -> Dict[str, Any]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM op_idempotency GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}
    finally:
        conn.close()


__all__ = ["make_key", "precheck", "claim", "claim_leased", "heartbeat", "reap_expired",
           "finish", "resolve", "release", "get", "recent",
           "verify_unknown", "verify_log", "VERIFY_OUTCOMES",
           "stats", "STATUSES", "DB_PATH"]
