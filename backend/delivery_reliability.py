"""
发货可靠性（C3）

解决的问题不是"多发"，而是**少发/发一半**：

    买家付了钱，卡密发到第 2 张时连接断了。
    旧的发货流程在发送**之前**就把订单标成"已发货"（mark_delivery_sent +
    system_shipped=1），于是：
      - 第 3 张永远发不出去；
      - 买家再发消息也不会重试（判重认为发过了）；
      - 除了日志里一行 error，没有任何人能发现。

三层结构：

    delivery_task   订单级租约   —— 同一订单在**任何进程**里只有一个执行者
    delivery_cards  卡券级幂等   —— (order_id, cookie_id, idx) 唯一，每张卡各自记账
    resend_scan     补发扫描     —— failed 且未超过次数的卡，交给调用方重发

三条硬规矩：

1. **先记账再发，发完再盖章。** 每张卡的 payload 在发送前落库，
   重发时发的是**同一份内容**——不是再去卡池取一张新的（那样会重复消耗库存，
   买家还可能拿到两张不同的卡）。
2. **submission_unknown 绝不自动重发。** 超时/断连导致"到底发没发"不知道时，
   自动重发可能让买家拿到两张卡。这时只允许人工回读后放行。
3. `done` 的卡永远不再发。这是幂等的最后一道，也是重启后仍然有效的那道。

当前 `sent` 的含义仅是 `websocket.send()` 正常返回，即本进程已经把完整帧交给
连接层。源码和日志中尚无平台回执与发送侧 `mid`/UUID 的关联证据，所以不能把
`sent` 解释成"平台确认送达"。

数据落在 `data/delivery_reliability.db`（含卡密明文，本机敏感，随 data/ 一起
不进任何外传包）。测试用 DELIVERY_RELIABILITY_DB 指到临时目录。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlite_runtime import connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = Path(os.environ.get("DELIVERY_RELIABILITY_DB") or (DATA_DIR / "delivery_reliability.db"))
CONFIG_PATH = Path(os.environ.get("DELIVERY_RELIABILITY_CONFIG") or (DATA_DIR / "delivery_reliability.json"))

DEFAULT_CONFIG: Dict[str, Any] = {
    # 兼容旧配置。当前没有可关联的平台回执，websocket.send 异常后无法证明
    # 第一次是否已写出，因此发货路径不再做进程内自动重试。
    "retry_inline": False,
    "inline_retries": 0,
    "inline_backoff_seconds": 2,
    # 跨进程补发（下一个触发点发现这张卡还是 failed，就补）
    "auto_resend": True,
    "max_attempts": 3,             # 一张卡最多尝试几次（含首次）
    "resend_delay_minutes": 10,    # 失败后多久才补，避免和平台状态同步抢跑
    # 连着失败这么多次就停手、只报警
    "alert_after_attempts": 3,
    "lease_seconds": 300,
    "keep_days": 90,
}

SELF_SERVICE_RESEND_HINT = "【防漏提示】如遇网络延迟未显示，可直接回复【1】或【提取】，系统将自动为您补发。"

_CFG_LOCK = threading.Lock()
_CFG_CACHE: Dict[str, Any] = {}
_DB_INIT_LOCK = threading.RLock()


class DeliverySubmissionNotAttempted(RuntimeError):
    """发送动作尚未提交到 WebSocket，可安全记账为 failed。"""


class DeliverySubmissionUnknown(RuntimeError):
    """WebSocket 写入结果不确定，必须记账为 unknown，禁止自动重发。"""


def _connect_with_retry(timeout_seconds: float = 30.0) -> sqlite3.Connection:
    """
    打开账本连接。

    `connect_sqlite` 会先执行 `PRAGMA journal_mode=WAL`。SQLite 的 busy_timeout
    对这个 PRAGMA 并不总能生效，另一个进程正在写时可能直接返回 locked；
    这里只对 locked 做有界重试，其余数据库错误立即抛出。
    """
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    delay = 0.05
    while True:
        try:
            return connect_sqlite(DB_PATH)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(0.5, delay * 2)


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect_with_retry()
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delivery_cards (
            order_id   TEXT NOT NULL,
            cookie_id  TEXT NOT NULL,
            idx        INTEGER NOT NULL,
            item_id    TEXT DEFAULT '',
            chat_id    TEXT DEFAULT '',
            buyer_id   TEXT DEFAULT '',
            spec       TEXT DEFAULT '',        -- 订单规格原文（拆解 39 §八-1）
            kind       TEXT DEFAULT 'text',
            payload    TEXT DEFAULT '',
            status     TEXT NOT NULL,          -- pending|sending|sent|failed|unknown
            attempts   INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '',
            lease_owner TEXT DEFAULT '',
            lease_token TEXT DEFAULT '',       -- 每次抢占换一个（拆解 8）
            lease_until REAL DEFAULT 0,
            sent_at    TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (order_id, cookie_id, idx)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_status ON delivery_cards(status, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_order ON delivery_cards(order_id, cookie_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delivery_order_observations (
            order_id TEXT NOT NULL,
            cookie_id TEXT NOT NULL,
            status TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            poll_started_ns INTEGER NOT NULL,
            PRIMARY KEY (order_id, cookie_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delivery_pickups (
            order_id TEXT NOT NULL, cookie_id TEXT NOT NULL, buyer_id TEXT NOT NULL,
            collected_at TEXT, collected_ip TEXT,
            PRIMARY KEY (order_id, cookie_id)
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS pickup_requests (ip TEXT NOT NULL, requested_at INTEGER NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pickup_requests_ip ON pickup_requests(ip, requested_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pickup_requests_time ON pickup_requests(requested_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pickup_ip_state (
            ip TEXT PRIMARY KEY, failures INTEGER NOT NULL,
            banned_until INTEGER NOT NULL, updated_at INTEGER NOT NULL
        )
    """)
    # 买家主动回复“1/提取”的自助补发请求。请求键负责挡住同一条平台消息的
    # 重复推送，账号+买家+时间负责挡住买家短时间连发不同消息造成的重复补发。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delivery_self_service_requests (
            cookie_id  TEXT NOT NULL,
            request_key TEXT NOT NULL,
            buyer_id   TEXT NOT NULL,
            chat_id    TEXT DEFAULT '',
            order_id   TEXT DEFAULT '',
            status     TEXT NOT NULL,
            card_count INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (cookie_id, request_key)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dssr_buyer "
        "ON delivery_self_service_requests(cookie_id, buyer_id, updated_at)"
    )
    # 自助补发还要按“账号 + 买家 + 原订单 + 卡位”记录进度。请求账本只能挡住
    # 十分钟内的连发；买家十分钟后再次回复“1”时会生成新请求，若这里不留
    # 逐卡进度，前半批已经成功的卡会被整批重发。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delivery_self_service_cards (
            cookie_id   TEXT NOT NULL,
            buyer_id    TEXT NOT NULL,
            order_id    TEXT NOT NULL,
            idx         INTEGER NOT NULL,
            status      TEXT NOT NULL,       -- sending|sent|failed|unknown
            attempts    INTEGER DEFAULT 0,
            last_error  TEXT DEFAULT '',
            lease_token TEXT DEFAULT '',
            lease_until REAL DEFAULT 0,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (cookie_id, buyer_id, order_id, idx)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dssc_order "
        "ON delivery_self_service_cards(cookie_id, buyer_id, order_id, updated_at)"
    )
    # 老库补列（拆解 8）：lease_token 是后加的，CREATE TABLE IF NOT EXISTS 不会给已有表加列。
    # 只加列、不改数据、不锁库，和 care_actions 加 rechecks 是同一套做法。
    try:
        import migrate
        migrate.ensure_columns(conn, "delivery_cards", {
            "lease_token": "TEXT DEFAULT ''",
            # 拆解 39 §八-1：账本原来只有商品号，没有规格。规格配错时
            # 发货页看不出买家买的是哪一款，只能回订单表翻。
            "spec": "TEXT DEFAULT ''",
            # 自助补发必须依据首次发送完成时间，不能拿可能被后续状态更新
            # 改写的 updated_at 代替。
            "sent_at": "TEXT DEFAULT ''",
        })
    except Exception:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dc_sent_at ON delivery_cards(cookie_id, buyer_id, sent_at)")
    # 平台"确认发货"是另一本账（拆解 7）。内容发成功 ≠ 平台已发货：
    # 前者是买家拿到货，后者是平台状态机往前走一步。两件事各自会失败，必须分开重试，
    # 否则"确认失败但内容成功"的单会永远挂在待发货里，没人知道。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS platform_ship (
            order_id   TEXT NOT NULL,
            cookie_id  TEXT NOT NULL,
            item_id    TEXT DEFAULT '',
            attempts   INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '',
            done       INTEGER DEFAULT 0,
            stopped    INTEGER DEFAULT 0,      -- 1=不再自动重试（拆解 9）
            stop_reason TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (order_id, cookie_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ps_pending ON platform_ship(done, updated_at)")
    # 老库补列（拆解 9）：stopped / stop_reason 是后加的，同 lease_token 的做法。
    try:
        import migrate
        migrate.ensure_columns(conn, "platform_ship", {
            "stopped": "INTEGER DEFAULT 0",
            "stop_reason": "TEXT DEFAULT ''",
        })
    except Exception:
        pass
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 配置

def get_config() -> Dict[str, Any]:
    with _CFG_LOCK:
        if _CFG_CACHE:
            return dict(_CFG_CACHE)
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8") or "{}")
            if isinstance(raw, dict):
                for k in DEFAULT_CONFIG:
                    if k in raw:
                        cfg[k] = raw[k]
    except Exception:
        pass
    with _CFG_LOCK:
        _CFG_CACHE.update(cfg)
    return cfg


def save_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_config()
    for k, v in (patch or {}).items():
        if k in DEFAULT_CONFIG:
            cfg[k] = v
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    with _CFG_LOCK:
        _CFG_CACHE.clear()
        _CFG_CACHE.update(cfg)
    return cfg


# ---------------------------------------------------------------- 计划

def parse_content(delivery_content: str) -> Dict[str, str]:
    """把 `__IMAGE_SEND__<card_id>|<url>` 拆成 kind/payload；其余当文本。"""
    s = str(delivery_content or "")
    if s.startswith("__IMAGE_SEND__"):
        return {"kind": "image", "payload": s[len("__IMAGE_SEND__"):]}
    return {"kind": "text", "payload": s}


def is_self_service_resend_trigger(send_message: Any) -> bool:
    """只匹配独立的“1”或“提取”，避免把普通聊天里的数字 1 当成补发指令。"""
    return str(send_message or "").strip() in ("1", "提取")


def append_self_service_resend_hint(delivery_content: Any) -> str:
    """在文本发货内容末尾追加一次防漏提示，图片内容保持不变。"""
    content = str(delivery_content or "")
    if content.startswith("__IMAGE_SEND__"):
        return content
    if content.rstrip().endswith(SELF_SERVICE_RESEND_HINT):
        return content
    content = content.replace(SELF_SERVICE_RESEND_HINT, "").rstrip()
    if not content.strip():
        return SELF_SERVICE_RESEND_HINT
    return f"{content}\n\n{SELF_SERVICE_RESEND_HINT}"


def ensure_order(order_id: str, cookie_id: str, contents: List[str],
                 item_id: str = "", chat_id: str = "", buyer_id: str = "",
                 spec: str = "") -> int:
    """
    把这单的 N 张卡登记进账本（幂等，重复调用不覆盖已 sent 的卡）。

    返回登记的行数。已有内容保持原样，补货时只填入原来没有内容的位置。
    """
    if not order_id:
        return 0
    conn = _conn()
    created = 0
    try:
        for idx, content in enumerate(contents):
            if not content:
                continue
            meta = parse_content(content)
            now = _now()
            cur = conn.execute(
                "INSERT INTO delivery_cards "
                "(order_id, cookie_id, idx, item_id, chat_id, buyer_id, spec, kind, payload, "
                " status, attempts, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?, 'pending', 0, ?, ?) "
                "ON CONFLICT(order_id, cookie_id, idx) DO UPDATE SET "
                "item_id=excluded.item_id, chat_id=excluded.chat_id, buyer_id=excluded.buyer_id, spec=excluded.spec, "
                "kind=excluded.kind, payload=excluded.payload, status='pending', attempts=0, last_error='', "
                "updated_at=excluded.updated_at WHERE delivery_cards.payload='' AND delivery_cards.status='failed'",
                (str(order_id), str(cookie_id), idx, item_id[:64], chat_id[:64], buyer_id[:64],
                 str(spec or "")[:120], meta["kind"], meta["payload"], now, now))
            created += cur.rowcount
        conn.commit()
        return created
    finally:
        conn.close()


def record_missing(order_id: str, cookie_id: str, start_idx: int, count: int,
                   reason: str = "发货内容获取失败", item_id: str = "",
                   chat_id: str = "", buyer_id: str = "", spec: str = "") -> int:
    """
    内容没取到的卡也要落库。

    原来这种情况只有一行 warning 日志：订单被标成已发货，这几张卡等于凭空蒸发。
    记成 failed + attempts 拉满（没有内容可发，重试多少次都没用），
    这样它会出现在"待人工处理"里，而不是消失。
    """
    if not order_id or count <= 0:
        return 0
    conn = _conn()
    now = _now()
    n = 0
    try:
        for i in range(start_idx, start_idx + count):
            cur = conn.execute(
                "INSERT OR IGNORE INTO delivery_cards "
                "(order_id, cookie_id, idx, item_id, chat_id, buyer_id, spec, kind, payload, "
                " status, attempts, last_error, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,'text','', 'failed', ?, ?, ?, ?)",
                (str(order_id), str(cookie_id), i, item_id[:64], chat_id[:64], buyer_id[:64],
                 str(spec or "")[:120],
                 int(get_config()["max_attempts"]), reason[:200], now, now))
            n += cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def mark_shortage(order_id: str, cookie_id: str, need: int, have: int,
                  reason: str = "库存不足") -> int:
    """
    库存不够时**整单停发**（嗷嗷鱼 Q23 的口径：一张都不发，绝不发一半）。

    为什么不能发一半：买家买 3 件只收到 1 张卡，剩下 2 件要么退款要么吵；
    而已经发出去的那张卡是**一次性消耗品**，退款也收不回来。
    所以缺货的处理是"停 + 通知人"，不是"能发几张发几张"。

    两种行分开标：
      - idx < have   有内容的卡：attempts 不动，补货后 force_retry 还能发出去
      - idx >= have  没内容的卡：attempts 拉满，重试无意义，只能人工补发/退款
    """
    if not order_id:
        return 0
    need, have = int(need or 0), max(0, int(have or 0))
    if need <= 0:
        return 0
    conn = _conn()
    now = _now()
    max_attempts = int(get_config()["max_attempts"])
    n = 0
    try:
        cur = conn.execute(
            "UPDATE delivery_cards SET status='failed', last_error=?, updated_at=? "
            "WHERE order_id=? AND cookie_id=? AND idx>=0 AND idx<? "
            "AND status IN ('pending','failed')",
            (str(reason)[:200], now, str(order_id), str(cookie_id), min(need, have)))
        n += cur.rowcount
        for i in range(min(need, have), need):
            cur = conn.execute(
                "UPDATE delivery_cards SET status='failed', last_error=?, attempts=?, updated_at=? "
                "WHERE order_id=? AND cookie_id=? AND idx=? AND status IN ('pending','failed')",
                (str(reason)[:200], max_attempts, now, str(order_id), str(cookie_id), i))
            n += cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def cards(order_id: str, cookie_id: str) -> List[Dict[str, Any]]:
    conn = _conn()
    try:
        return [dict(row) for row in _order_cards(conn, order_id, cookie_id)]
    finally:
        conn.close()


def _order_cards(conn: sqlite3.Connection, order_id: str, cookie_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM delivery_cards WHERE order_id=? AND cookie_id=? ORDER BY idx",
        (str(order_id), str(cookie_id)),
    ).fetchall()


# ---------------------------------------------------------------- 逐卡抢占

def uses_pickup(order_id: str, cookie_id: str) -> bool:
    conn = _conn()
    try:
        return conn.execute(
            "SELECT 1 FROM delivery_pickups WHERE order_id=? AND cookie_id=?",
            (order_id, cookie_id),
        ).fetchone() is not None
    finally:
        conn.close()


def claim_card(order_id: str, cookie_id: str, idx: int, owner: str = "",
               lease_seconds: Optional[int] = None, allow_failed: bool = True) -> Dict[str, Any]:
    """
    抢一张卡。返回 {"claimed": bool, "reason": ..., "card": dict|None}

    reason：
      new / retry       → 可以发
      running           → 别人正在发（租约未过期）
      unknown           → 上一租约已过期或结果未确认，禁止自动接管
      sent              → 已经发过了，跳过（这就是幂等）
      exhausted         → 次数用完了
    """
    cfg = get_config()
    lease = int(lease_seconds or cfg["lease_seconds"])
    max_attempts = int(cfg["max_attempts"])
    owner = owner or f"pid{os.getpid()}-{uuid.uuid4().hex[:6]}"
    # 每次抢占换一个新 token（拆解 8，照 `flyfish-dev/shop` 的 `xy_job.lease_token`）。
    # 为什么光有 lease_until 不够：A 的租约过期 → B 接管开始发 → 这时 A 的 ws.send
    # 才姗姗来迟地抛异常，A 手里那条"失败"的结论会盖掉 B 正在进行的发送。
    # 更坏的是 A 的 mark_failed 会把状态改回 failed，补发扫描立刻把这张卡
    # 再交给第三个 worker —— 同一张卡同时有两个人以为"只有我在发"。
    token = uuid.uuid4().hex[:12]
    now_ts = time.time()
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM delivery_cards WHERE order_id=? AND cookie_id=? AND idx=?",
            (str(order_id), str(cookie_id), int(idx))).fetchone()
        if row is None:
            return {"claimed": False, "reason": "missing", "card": None}
        d = dict(row)
        st = d["status"]
        if st in ("sent", "collected"):
            return {"claimed": False, "reason": "sent", "card": d}
        if st == "unknown":
            return {"claimed": False, "reason": "unknown", "card": d}
        if st == "sending" and float(d["lease_until"] or 0) > now_ts:
            return {"claimed": False, "reason": "running", "card": d}
        if st == "sending":
            # 租约过期意味着上一位执行者可能已经写出，只是没来得及结算。
            # 认领路径必须自己完成从 sending 到 unknown 的原子迁移，不能等
            # 外部先调用 reap_expired_leases()，否则自动发货路径会绕过安全语义。
            error = "租约过期未回报（进程可能崩在发送中途）"
            conn.execute(
                "UPDATE delivery_cards SET status='unknown', last_error=?, "
                "lease_owner='', lease_token='', lease_until=0, updated_at=? "
                "WHERE order_id=? AND cookie_id=? AND idx=?",
                (error, _now(), str(order_id), str(cookie_id), int(idx)))
            conn.commit()
            d.update({
                "status": "unknown",
                "last_error": error,
                "lease_owner": "",
                "lease_token": "",
                "lease_until": 0,
            })
            return {"claimed": False, "reason": "unknown", "card": d}
        attempts = int(d["attempts"] or 0)
        if attempts >= max_attempts and st != "sending":
            return {"claimed": False, "reason": "exhausted", "card": d}
        if st == "failed" and not allow_failed:
            return {"claimed": False, "reason": "failed", "card": d}

        conn.execute(
            "UPDATE delivery_cards SET status='sending', attempts=attempts+1, "
            "lease_owner=?, lease_token=?, lease_until=?, updated_at=? "
            "WHERE order_id=? AND cookie_id=? AND idx=?",
            (owner, token, now_ts + lease, _now(), str(order_id), str(cookie_id), int(idx)))
        conn.commit()
        d.update({"status": "sending", "attempts": attempts + 1,
                  "lease_owner": owner, "lease_token": token,
                  "lease_until": now_ts + lease})
        # 已经试过的（failed / manual_retry）都记 retry，便于日志区分
        return {"claimed": True,
                "reason": "retry" if (st in ("failed", "manual_retry") or attempts > 0) else "new",
                "card": d}
    finally:
        conn.close()


def mark_sent(order_id: str, cookie_id: str, idx: int, note: str = "",
              token: str = "") -> bool:
    return _set_status(order_id, cookie_id, idx, "sent", note, token=token)


def mark_failed(order_id: str, cookie_id: str, idx: int, error: str = "",
                token: str = "") -> bool:
    return _set_status(order_id, cookie_id, idx, "failed", error, token=token)


def mark_unknown(order_id: str, cookie_id: str, idx: int, error: str = "",
                 token: str = "") -> bool:
    """结果未知：通道超时/连接中断前已经写出去了。禁止自动重发。"""
    return _set_status(order_id, cookie_id, idx, "unknown", error, token=token)


def record_send_exception(order_id: str, cookie_id: str, idx: int, exc: BaseException,
                          token: str = "") -> bool:
    """按发送阶段把异常落账。

    只有 `DeliverySubmissionNotAttempted` 能证明调用发生在 websocket 写出之前。
    websockets 的 send 调用没有应用层回执，其余异常一律按 unknown 处理，宁可
    人工核对，也不能自动重发同一张卡密。
    """
    message = str(exc)[:300]
    if isinstance(exc, DeliverySubmissionNotAttempted):
        return mark_failed(order_id, cookie_id, idx, message, token=token)
    return mark_unknown(order_id, cookie_id, idx, message, token=token)


def _set_status(order_id: str, cookie_id: str, idx: int, status: str, note: str = "",
                token: str = "") -> bool:
    """
    收尾写状态。`token` 非空时加租约守卫（拆解 8）。

    守卫写在 SQL 里而不是先 SELECT 再判断：读-判-写之间有窗口，
    两个进程可以同时看到"我还是持有者"。只有 `WHERE lease_token=?` 是原子的。
    返回 False = 这次结论已经过期（别人接管了），调用方**不该**把它当成失败重试，
    而是当作"我的活已经被别人接手了"直接放手。
    """
    now = _now()
    sql = ("UPDATE delivery_cards SET status=?, last_error=?, lease_owner='', "
           "lease_token='', lease_until=0, updated_at=?, "
           "sent_at=CASE WHEN ?='sent' AND status<>'sent' THEN ? ELSE sent_at END "
           "WHERE order_id=? AND cookie_id=? AND idx=? AND status<>'collected'")
    args: list = [
        status,
        str(note or "")[:300],
        now,
        status,
        now,
        str(order_id),
        str(cookie_id),
        int(idx),
    ]
    if token:
        sql += " AND lease_token=?"
        args.append(str(token))
    conn = _conn()
    try:
        cur = conn.execute(sql, tuple(args))
        conn.commit()
        if cur.rowcount > 0:
            return True
        if not token:
            return False
        # 没写进去有两种可能，必须分开：别人接管了（该放手）vs 这单已经是这个状态了
        # （同一次调用的重复执行，属于幂等成功）。只看 rowcount 会把后者误报成
        # "租约被抢"，在日志里制造不存在的事故。
        row = conn.execute(
            "SELECT status FROM delivery_cards WHERE order_id=? AND cookie_id=? AND idx=?",
            (str(order_id), str(cookie_id), int(idx))).fetchone()
        return bool(row) and str(row[0]) == status
    finally:
        conn.close()


def release_card(order_id: str, cookie_id: str, idx: int, reason: str = "",
                 token: str = "") -> bool:
    """把 sending 退回 pending（例如发送前发现 ws 已关闭，明确没发出去）。"""
    return _set_status(order_id, cookie_id, idx, "pending", reason, token=token)


def defer_unsent_card(order_id: str, cookie_id: str, idx: int, token: str) -> bool:
    """暂停发生在发送前，保留原内容并退回这次未使用的发送次数。"""
    if not token:
        return False
    conn = _conn()
    try:
        cursor = conn.execute(
            "UPDATE delivery_cards SET status='failed', attempts=max(0, attempts-1), "
            "last_error=?, lease_owner='', lease_token='', lease_until=0, updated_at=? "
            "WHERE order_id=? AND cookie_id=? AND idx=? AND status='sending' AND lease_token=?",
            ("账号暂停，本件尚未发送", _now(), order_id, cookie_id, idx, token))
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


# ---------------------------------------------------------------- 汇总

def record_platform_order_observations(
    cookie_id: str, order_statuses: Dict[str, str], poll_started_ns: int,
) -> None:
    """Record seller order observations independently of card delivery state."""
    if not order_statuses:
        return
    observed_at = _now()
    conn = _conn()
    try:
        with conn:
            conn.executemany("""
                INSERT INTO delivery_order_observations
                    (order_id, cookie_id, status, observed_at, poll_started_ns)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (order_id, cookie_id) DO UPDATE SET
                    observed_at = CASE WHEN status = excluded.status THEN observed_at
                                       ELSE excluded.observed_at END,
                    status = excluded.status,
                    poll_started_ns = excluded.poll_started_ns
                WHERE excluded.poll_started_ns > poll_started_ns
                """, (
                    (order_id, cookie_id, status, observed_at, poll_started_ns)
                    for order_id, status in order_statuses.items()
                ))
    finally:
        conn.close()


def _order_completion(conn: sqlite3.Connection, order_id: str, cookie_id: str) -> Optional[Dict[str, str]]:
    row = conn.execute(
        "SELECT observed_at FROM delivery_order_observations "
        "WHERE order_id=? AND cookie_id=? AND status='completed'",
        (str(order_id), str(cookie_id)),
    ).fetchone()
    return {"source": "sold.get", "observed_at": row["observed_at"]} if row else None


def summary(order_id: str, cookie_id: str) -> Dict[str, Any]:
    conn = _conn()
    try:
        conn.execute("BEGIN")
        rows = _order_cards(conn, order_id, cookie_id)
        completion = _order_completion(conn, order_id, cookie_id)
    finally:
        conn.close()
    by: Dict[str, int] = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    total = len(rows)
    sent = by.get("sent", 0) + by.get("collected", 0)
    all_sent = bool(total) and sent == total
    return {
        "total": total,
        "sent": sent,
        "failed": by.get("failed", 0),
        "unknown": by.get("unknown", 0),
        "pending": by.get("pending", 0) + by.get("sending", 0) + by.get("manual_retry", 0),
        "all_sent": all_sent,
        "by_status": by,
        "sent_meaning": "websocket_write_completed",
        "platform_receipt_verified": False,
        "platform_order_completion": completion,
        "delivery_reconciled": all_sent and completion is not None,
        # 只剩"内容没取到"那类，重试也没用，必须人工处理
        "need_human": [r["idx"] for r in rows if r["status"] in ("failed", "unknown")],
    }


def resend_candidates(limit: int = 50) -> List[Dict[str, Any]]:
    """
    可以补发的卡：人工放行的 manual_retry，或 failed、还有次数、且已过冷却。

    明确排除 unknown —— 那次到底发没发不知道，补发可能让买家拿到两张。
    """
    cfg = get_config()
    cutoff = (datetime.now() - timedelta(minutes=int(cfg["resend_delay_minutes"]))).isoformat(timespec="seconds")
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT card.* FROM delivery_cards AS card "
            "WHERE (card.status='manual_retry' OR "
            "      (card.status='failed' AND card.attempts < ? AND card.updated_at <= ?)) "
            "AND card.payload <> '' AND card.lease_until = 0 "
            "AND NOT EXISTS (SELECT 1 FROM delivery_cards AS missing "
            "WHERE missing.order_id=card.order_id AND missing.cookie_id=card.cookie_id AND missing.payload='') "
            "ORDER BY card.updated_at LIMIT ?",
            (int(cfg["max_attempts"]), cutoff, int(limit))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def force_retry(order_id: str, cookie_id: str, idx: int, reason: str = "人工要求补发") -> bool:
    """
    人工点"再补一次"：把次数清零，置为 manual_retry 供补发扫描立即领取。

    只有人能决定这一步 —— 因为 unknown 的卡可能平台已经收到，
    自动重发会让买家拿到两张卡，这是唯一需要人判断的地方。

    两条硬拦截：`sent` 不回退（已经给买家了），`payload=''` 不放行
    （缺货停发时留下的空位没有人内容，放回去只会发一条空消息，
    这种单的正确出路是退款或手动补发）。
    """
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE delivery_cards SET status='manual_retry', attempts=0, last_error=?, "
            "lease_owner='', lease_token='', lease_until=0, updated_at=? "
            "WHERE order_id=? AND cookie_id=? AND idx=? AND status NOT IN ('sent','collected') "
            "AND payload IS NOT NULL AND payload <> ''",
            (str(reason or "")[:300], _now(), str(order_id), str(cookie_id), int(idx)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def unresolved(limit: int = 100) -> List[Dict[str, Any]]:
    """所有"没发成、需要人看"的卡（failed / unknown），新的在前。"""
    conn = _conn()
    try:
        rows = conn.execute(
            """
            WITH unresolved AS (
                SELECT order_id, cookie_id, idx, item_id, chat_id, buyer_id, spec, kind, status,
                       attempts, last_error, updated_at, length(payload) AS payload_len
                FROM delivery_cards WHERE status IN ('failed','unknown')
                ORDER BY updated_at DESC LIMIT ?
            ), totals AS (
                SELECT order_id, cookie_id, count(*) AS order_total,
                       sum(status IN ('sent','collected')) AS order_sent,
                       sum(status IN ('failed','unknown')) AS order_unresolved
                FROM delivery_cards
                WHERE (order_id, cookie_id) IN (SELECT order_id, cookie_id FROM unresolved)
                GROUP BY order_id, cookie_id
            )
            SELECT unresolved.*, order_total, order_sent, order_unresolved
            FROM unresolved JOIN totals USING (order_id, cookie_id)
            ORDER BY updated_at DESC
            """, (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def locate_context(order_id: str, cookie_id: str = "", idx: int = -1) -> Optional[Dict[str, Any]]:
    """
    异常订单的会话定位上下文（只读）。

    用途：发货失败/未知的行，帮人快速找到"去哪个账号、哪个会话里问买家"。
    边界（按交接文档）：本机**没有**经过验证的闲鱼网页直达会话入口，所以这里
    不编造跳转链接，只返回已存标识 + 人工查找步骤。等拿到真实点击记录再补直达。

    - idx < 0：整单定位，优先取第一条有 chat_id 的卡；
    - 缺 chat_id 时依次回落到 buyer_id / order_id，并把缺什么写进 missing。
    """
    conn = _conn()
    try:
        conn.execute("BEGIN")
        if idx is not None and int(idx) >= 0:
            row = conn.execute(
                "SELECT * FROM delivery_cards WHERE order_id=? AND cookie_id=? AND idx=?",
                (str(order_id), str(cookie_id), int(idx))).fetchone()
            if row is None:
                return None
            rows = [row]
        else:
            rows = conn.execute(
                "SELECT * FROM delivery_cards WHERE order_id=? "
                + ("AND cookie_id=? " if cookie_id else "")
                + "ORDER BY (chat_id<>'') DESC, idx ASC",
                ((str(order_id), str(cookie_id)) if cookie_id else (str(order_id),))).fetchall()
            if not rows:
                return None
        base = dict(rows[0])
        totals = conn.execute(
            "SELECT count(*) AS order_total, sum(status IN ('sent','collected')) AS order_sent, "
            "sum(status IN ('failed','unknown')) AS order_unresolved "
            "FROM delivery_cards WHERE order_id=? AND cookie_id=?",
            (str(order_id), base["cookie_id"])).fetchone()
        completion = _order_completion(conn, order_id, base["cookie_id"])
        chat_id = base.get("chat_id") or ""
        buyer_id = base.get("buyer_id") or ""
        missing = []
        if not chat_id:
            missing.append("chat_id")
        if not buyer_id:
            missing.append("buyer_id")
        if chat_id:
            steps = [
                "打开对应账号的闲鱼 App → 消息",
                f"搜索会话编号 {chat_id}（或从订单进入买家会话）",
                "若搜不到，用订单号在「我卖出的」里打开该订单，再点「联系买家」",
            ]
        elif buyer_id:
            steps = [
                "打开对应账号的闲鱼 App → 消息",
                f"搜索买家 ID {buyer_id}",
                f"仍找不到时用订单号 {order_id} 在「我卖出的」打开订单，再点「联系买家」",
            ]
        else:
            steps = [
                "这条卡没有记录会话编号和买家 ID",
                f"用订单号 {order_id} 在对应账号的「我卖出的」里找到订单",
                "从订单页点「联系买家」进入会话，再人工处理",
            ]
        return {
            "order_id": str(order_id),
            "cookie_id": base["cookie_id"],
            "idx": int(base.get("idx") or 0),
            "item_id": base.get("item_id") or "",
            # 规格为空时前端显示「未提供」——不猜、不回订单表替人补
            "spec": base.get("spec") or "",
            "chat_id": chat_id,
            "buyer_id": buyer_id,
            "status": base.get("status") or "",
            "updated_at": base.get("updated_at") or "",
            "has_chat_id": bool(chat_id),
            "missing": missing,
            "order_total": int(totals["order_total"] or 0) if totals else 0,
            "order_sent": int(totals["order_sent"] or 0) if totals else 0,
            "order_unresolved": int(totals["order_unresolved"] or 0) if totals else 0,
            "platform_order_completion": completion,
            "delivery_reconciled": completion is not None and totals["order_total"] == totals["order_sent"],
            # 直达入口尚未验证：留空而不是猜一个 URL，避免把人带到错误页面
            "deep_link": "",
            "entry_verified": False,
            "manual_steps": steps,
        }
    finally:
        conn.close()


def reap_expired_leases() -> int:
    """租约过期的 sending → **unknown**，不是退回 pending（拆解 2 修正）。

    原来退回 pending 等于"进程死了就自动重发"。但这个状态恰恰是最不确定的：
    进程可能死在 ws.send **之后**、mark_sent **之前**，消息已经到买家手机上了。
    自动重发 = 买家拿到两张卡（卡密是钱）。

    上游 `UniversalECommerceBot/core/fulfillment_store.py:recover_interrupted()`
    的做法值得抄：判定持有者已死后不是改回待发，而是直接置 `unknown`，
    让负责收口的人去看一眼。我们的页面上 unknown 本来就有三个出口
    （已确认送达 / 再补一次 / 放弃），所以这只是换了目标状态，不新增交互。

    真正"明确没发出去"的场景走 `release_card()`（发送前发现 ws 已关闭），
    那条路退回 pending 是对的。
    """
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE delivery_cards SET status='unknown', lease_owner='', lease_token='', "
            "lease_until=0, last_error=?, updated_at=? "
            "WHERE status='sending' AND lease_until > 0 AND lease_until < ?",
            ("租约过期未回报（进程可能崩在发送中途）", _now(), time.time()))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------- 买家自助补发

def begin_self_service_request(request_key: str, cookie_id: str, buyer_id: str,
                               chat_id: str = "", cooldown_minutes: int = 10) -> Dict[str, Any]:
    """
    原子抢占一次买家自助补发。

    返回 `allowed=True` 时调用方获得本次发送权，必须用
    `finish_self_service_request()` 收尾。相同 request_key 永远只放行一次；
    同账号同买家在冷却窗口内的第二次请求也不会放行，避免平台重推或买家连发
    “1/提取”导致同一批卡密发送两遍。
    """
    key = str(request_key or "").strip()
    cookie = str(cookie_id or "").strip()
    buyer = str(buyer_id or "").strip()
    if not key or not cookie or not buyer:
        return {"allowed": False, "reason": "invalid_request"}

    cooldown = max(0, int(cooldown_minutes))
    cutoff = (datetime.now() - timedelta(minutes=cooldown)).isoformat(timespec="seconds")
    now = _now()
    # 连接初始化里的 PRAGMA journal_mode 也要求短暂独占；把连接、写事务和
    # 提交放在同一把进程内锁里，避免两个回调同时打开库时先撞上 WAL 初始化。
    # 跨进程去重仍由 BEGIN IMMEDIATE 和主键保证。
    with _DB_INIT_LOCK:
        conn = _conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            duplicate = conn.execute(
                "SELECT status, order_id, card_count, last_error, created_at, updated_at "
                "FROM delivery_self_service_requests "
                "WHERE cookie_id=? AND request_key=?",
                (cookie, key),
            ).fetchone()
            if duplicate is not None:
                return {
                    "allowed": False,
                    "reason": "duplicate",
                    "request": dict(duplicate),
                }

            recent = conn.execute(
                "SELECT status, order_id, card_count, last_error, created_at, updated_at "
                "FROM delivery_self_service_requests "
                "WHERE cookie_id=? AND buyer_id=? AND updated_at>=? "
            "AND status IN ('sending','sent','no_delivery','unknown','failed') "
                "ORDER BY updated_at DESC LIMIT 1",
                (cookie, buyer, cutoff),
            ).fetchone()
            if recent is not None:
                return {
                    "allowed": False,
                    "reason": "cooldown",
                    "request": dict(recent),
                    "cooldown_minutes": cooldown,
                }

            conn.execute(
                "INSERT INTO delivery_self_service_requests "
                "(cookie_id, request_key, buyer_id, chat_id, order_id, status, "
                " card_count, last_error, created_at, updated_at) "
                "VALUES (?,?,?,?,'', 'sending',0,'',?,?)",
                (cookie, key, buyer, str(chat_id or "")[:128], now, now),
            )
            conn.commit()
            return {
                "allowed": True,
                "reason": "acquired",
                "request_key": key,
                "cooldown_minutes": cooldown,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def finish_self_service_request(request_key: str, cookie_id: str, status: str,
                                order_id: str = "", card_count: int = 0,
                                last_error: str = "") -> bool:
    """结算一次自助补发请求；不触碰原 `delivery_cards`。"""
    allowed_statuses = {"sent", "no_delivery", "unknown", "failed"}
    final_status = str(status or "").strip()
    if final_status not in allowed_statuses:
        raise ValueError(f"不支持的自助补发状态: {final_status}")

    key = str(request_key or "").strip()
    cookie = str(cookie_id or "").strip()
    if not key or not cookie:
        return False
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE delivery_self_service_requests SET "
            "order_id=?, status=?, card_count=?, last_error=?, updated_at=? "
            "WHERE cookie_id=? AND request_key=?",
            (
                str(order_id or "")[:64],
                final_status,
                max(0, int(card_count or 0)),
                str(last_error or "")[:300],
                _now(),
                cookie,
                key,
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def claim_self_service_card(cookie_id: str, buyer_id: str, order_id: str,
                            idx: int, owner: str = "") -> Dict[str, Any]:
    """
    为一次自助补发原子领取一个卡位。

    `sent` 表示这张原卡已经成功补发过一次，永不再领。`unknown` 可能已经到买家
    手上，也禁止自动重发。只有 `failed` 或从未存在的卡位可以领取。
    """
    cookie = str(cookie_id or "").strip()
    buyer = str(buyer_id or "").strip()
    order = str(order_id or "").strip()
    card_idx = int(idx)
    if not cookie or not buyer or not order or card_idx < 0:
        return {"claimed": False, "reason": "invalid_request", "card": None}

    now = _now()
    now_ts = time.time()
    token = uuid.uuid4().hex
    with _DB_INIT_LOCK:
        conn = _conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM delivery_self_service_cards "
                "WHERE cookie_id=? AND buyer_id=? AND order_id=? AND idx=?",
                (cookie, buyer, order, card_idx),
            ).fetchone()
            if existing is not None:
                status = str(existing["status"] or "")
                if status in ("sent", "unknown"):
                    conn.rollback()
                    return {
                        "claimed": False,
                        "reason": status,
                        "card": dict(existing),
                    }
                if (
                    status == "sending"
                    and float(existing["lease_until"] or 0) >= now_ts
                ):
                    conn.rollback()
                    return {
                        "claimed": False,
                        "reason": "in_flight",
                        "card": dict(existing),
                    }
                if status == "sending":
                    conn.execute(
                        "UPDATE delivery_self_service_cards SET "
                        " status='unknown', lease_token='', lease_until=0, "
                        " last_error=?, updated_at=? "
                        "WHERE cookie_id=? AND buyer_id=? AND order_id=? AND idx=?",
                        (
                            "自助补发租约过期，发送结果未知",
                            now,
                            cookie,
                            buyer,
                            order,
                            card_idx,
                        ),
                    )
                    conn.commit()
                    return {
                        "claimed": False,
                        "reason": "unknown",
                        "card": dict(existing),
                    }
                if status != "failed":
                    conn.rollback()
                    return {
                        "claimed": False,
                        "reason": "invalid_state",
                        "card": dict(existing),
                    }
                conn.execute(
                    "UPDATE delivery_self_service_cards SET "
                    " status='sending', attempts=attempts+1, last_error='', "
                    " lease_token=?, lease_until=?, updated_at=? "
                    "WHERE cookie_id=? AND buyer_id=? AND order_id=? AND idx=?",
                    (
                        token,
                        now_ts + float(get_config()["lease_seconds"]),
                        now,
                        cookie,
                        buyer,
                        order,
                        card_idx,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO delivery_self_service_cards "
                    "(cookie_id, buyer_id, order_id, idx, status, attempts, "
                    " lease_token, lease_until, created_at, updated_at) "
                    "VALUES (?,?,?,?,'sending',1,?,?,?,?)",
                    (
                        cookie,
                        buyer,
                        order,
                        card_idx,
                        token,
                        now_ts + float(get_config()["lease_seconds"]),
                        now,
                        now,
                    ),
                )
            card = conn.execute(
                "SELECT * FROM delivery_self_service_cards "
                "WHERE cookie_id=? AND buyer_id=? AND order_id=? AND idx=?",
                (cookie, buyer, order, card_idx),
            ).fetchone()
            conn.commit()
            return {
                "claimed": True,
                "reason": "acquired",
                "card": dict(card) if card is not None else None,
                "owner": str(owner or ""),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def finish_self_service_card(cookie_id: str, buyer_id: str, order_id: str,
                             idx: int, status: str, *, token: str,
                             last_error: str = "") -> bool:
    """按租约结算一个自助补发卡位；迟到的旧租约不能覆盖新结论。"""
    final_status = str(status or "").strip()
    if final_status not in {"sent", "failed", "unknown"}:
        raise ValueError(f"不支持的自助补发卡状态: {final_status}")
    cookie = str(cookie_id or "").strip()
    buyer = str(buyer_id or "").strip()
    order = str(order_id or "").strip()
    lease_token = str(token or "").strip()
    if not cookie or not buyer or not order or not lease_token:
        return False
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE delivery_self_service_cards SET "
            " status=?, last_error=?, lease_token='', lease_until=0, updated_at=? "
            "WHERE cookie_id=? AND buyer_id=? AND order_id=? AND idx=? "
            "AND status='sending' AND lease_token=?",
            (
                final_status,
                str(last_error or "")[:300] if final_status != "sent" else "",
                _now(),
                cookie,
                buyer,
                order,
                int(idx),
                lease_token,
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def latest_sent_delivery(cookie_id: str, buyer_id: str,
                         within_hours: int = 2) -> Optional[Dict[str, Any]]:
    """
    找到当前账号、当前买家两小时内最近一单已发出卡密。

    以真实 `sent_at` 选出最近一单，再只取其中 `sent` 且 payload 非空的卡。
    `unknown` 可能已经到买家手上，绝不纳入自动补发；空 payload 没有内容可发。
    成功卡必须从索引 0 开始连续，避免把订单中不相邻的卡位拼成一次错误补发。
    """
    cookie = str(cookie_id or "").strip()
    buyer = str(buyer_id or "").strip()
    if not cookie or not buyer:
        return None

    hours = max(0, int(within_hours))
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    conn = _conn()
    try:
        conn.execute("BEGIN")
        latest_order = conn.execute(
            """
            SELECT order_id,
                   MAX(sent_at) AS sent_at
            FROM delivery_cards
            WHERE cookie_id=? AND buyer_id=?
              AND status='sent'
              AND NOT EXISTS (SELECT 1 FROM delivery_pickups p
                              WHERE p.order_id=delivery_cards.order_id AND p.cookie_id=delivery_cards.cookie_id)
              AND TRIM(COALESCE(payload, ''))<>''
              AND TRIM(COALESCE(sent_at, ''))<>''
              AND sent_at>=?
            GROUP BY order_id
            ORDER BY sent_at DESC, order_id DESC
            LIMIT 1
            """,
            (cookie, buyer, cutoff),
        ).fetchone()
        if latest_order is None:
            conn.rollback()
            return None

        rows = conn.execute(
            "SELECT * FROM delivery_cards "
            "WHERE cookie_id=? AND buyer_id=? AND order_id=? "
            "AND status='sent' AND TRIM(COALESCE(payload, ''))<>'' "
            "AND NOT EXISTS (SELECT 1 FROM delivery_pickups p "
            "WHERE p.order_id=delivery_cards.order_id AND p.cookie_id=delivery_cards.cookie_id) "
            "ORDER BY idx",
            (cookie, buyer, latest_order["order_id"]),
        ).fetchall()
        cards_in_order = [dict(row) for row in rows]
        if (
            not cards_in_order
            or [card["idx"] for card in cards_in_order] != list(range(len(cards_in_order)))
        ):
            conn.rollback()
            return None

        conn.rollback()
        first = cards_in_order[0]
        return {
            "order_id": first["order_id"],
            "cookie_id": first["cookie_id"],
            "buyer_id": first["buyer_id"],
            "chat_id": first["chat_id"] or "",
            "item_id": first["item_id"] or "",
            "spec": first["spec"] or "",
            "sent_at": latest_order["sent_at"],
            "card_count": len(cards_in_order),
            "cards": cards_in_order,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------- 平台发货账（拆解 7）

def note_platform_ship(order_id: str, cookie_id: str, item_id: str = "",
                       error: str = "", policy: str = "retry") -> bool:
    """
    登记一条"内容已发完、但平台确认发货还没成功"的记录（或累加一次失败）。

    只在**内容全部发出去之后**调用。顺序反过来就是上游项目的 bug：
    先确认发货、再发内容，内容失败时订单已经显示发货，买家什么都没收到。

    `policy` 是处置分级（见 `mtop_response.retry_policy`），决定这一笔记什么账：
      - `retry`   普通失败：attempts +1，到上限转人工；
      - `refresh` 登录态失效：**不加次数**，等保活刷新后自然重试。
                 把"我没登录"算成"平台不肯发货"，5 次就把单送进死信，是假故障；
      - `stop`    平台已是终态 / 签名错 / 风控：`stopped=1`，不再自动重试，
                 也不再占用重试次数（它根本没资格叫失败）。
    """
    if not order_id:
        return False
    pol = str(policy or "retry")
    inc = 0 if pol in ("stop", "refresh") else 1
    stopped = 1 if pol == "stop" else 0
    reason = str(error or "")[:300] if stopped else ""
    conn = _conn()
    now = _now()
    try:
        conn.execute(
            "INSERT INTO platform_ship (order_id, cookie_id, item_id, attempts, last_error, "
            " done, stopped, stop_reason, created_at, updated_at) VALUES (?,?,?,?,?,0,?,?,?,?) "
            "ON CONFLICT(order_id, cookie_id) DO UPDATE SET "
            " attempts=platform_ship.attempts + ?, "
            " last_error=excluded.last_error, "
            " item_id=CASE WHEN excluded.item_id<>'' THEN excluded.item_id ELSE platform_ship.item_id END, "
            " stopped=MAX(platform_ship.stopped, excluded.stopped), "
            " stop_reason=CASE WHEN excluded.stopped=1 THEN excluded.stop_reason "
            "             ELSE platform_ship.stop_reason END, "
            " updated_at=excluded.updated_at",
            (str(order_id), str(cookie_id), str(item_id or ""),
             inc, str(error or "")[:300], stopped, reason, now, now, inc))
        conn.commit()
        return True
    finally:
        conn.close()


def mark_platform_shipped(order_id: str, cookie_id: str, *,
                          confirmed_item_id: Optional[str] = None) -> bool:
    """销账。收到平台成功结果时，即使没有待补记录也保存确认凭据。"""
    conn = _conn()
    try:
        now = _now()
        if confirmed_item_id is None:
            cur = conn.execute(
                "UPDATE platform_ship SET done=1, stopped=0, stop_reason='', last_error='', updated_at=? "
                "WHERE order_id=? AND cookie_id=? AND done=0",
                (now, str(order_id), str(cookie_id)))
        else:
            cur = conn.execute(
                "INSERT INTO platform_ship (order_id, cookie_id, item_id, done, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(order_id, cookie_id) DO UPDATE SET "
                "done=1, stopped=0, stop_reason='', last_error='', updated_at=excluded.updated_at, "
                "item_id=COALESCE(NULLIF(excluded.item_id, ''), platform_ship.item_id) "
                "WHERE platform_ship.done=0",
                (str(order_id), str(cookie_id), str(confirmed_item_id), now, now))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def pending_platform_shipments(cookie_id: str = "", limit: int = 20,
                               max_attempts: int = 5) -> List[Dict[str, Any]]:
    """内容已发完、平台还没确认发货、且还**值得**再试的订单（最久没试的优先）。"""
    conn = _conn()
    try:
        sql = ("SELECT * FROM platform_ship WHERE done=0 AND stopped=0 AND attempts < ?")
        args: List[Any] = [int(max_attempts)]
        if cookie_id:
            sql += " AND cookie_id=?"
            args.append(str(cookie_id))
        sql += " ORDER BY updated_at LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]
    finally:
        conn.close()


def platform_ship_rows(state: str = "pending", cookie_id: str = "", limit: int = 50,
                       max_attempts: int = 5) -> List[Dict[str, Any]]:
    """
    页面用：把三本不同的账分开列，别混成一句"有 6 单没确认"。

    - `pending` 还会自动重试的
    - `stopped` 平台已终态/签名错/风控，已停止重试（需要人看一眼，但**不是故障**）
    - `stuck`   普通失败撞到次数上限，真卡住了（最该处理的一类）
    """
    where = {
        "pending": "done=0 AND stopped=0 AND attempts < ?",
        "stopped": "done=0 AND stopped=1",
        "stuck": "done=0 AND stopped=0 AND attempts >= ?",
        "done": "done=1",
    }.get(str(state or "pending"), "done=0 AND stopped=0 AND attempts < ?")
    conn = _conn()
    try:
        sql = (f"SELECT order_id, cookie_id, item_id, attempts, last_error, stopped, "
               f"stop_reason, created_at, updated_at FROM platform_ship WHERE {where}")
        args: List[Any] = []
        if "?" in where:
            args.append(int(max_attempts))
        if cookie_id:
            sql += " AND cookie_id=?"
            args.append(str(cookie_id))
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]
    finally:
        conn.close()


def retry_platform_ship(order_id: str, cookie_id: str) -> bool:
    """人工"再试一次"：把次数和停止标记清零，让心跳重新捡起来。

    这是 `stopped` 的唯一出口。没有它，一旦平台判了终态（或我们误判），
    这单就永远停在页面上，只能删库。
    """
    conn = _conn()
    try:
        cur = conn.execute(
            "UPDATE platform_ship SET attempts=0, stopped=0, stop_reason='', updated_at=? "
            "WHERE order_id=? AND cookie_id=? AND done=0",
            (_now(), str(order_id), str(cookie_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def platform_ship_stats(max_attempts: int = 5) -> Dict[str, Any]:
    """四个口径分开：已确认 / 还会自动重试 / 已停止 / 卡死。

    `pending` 只算**真的还会重试**的；把卡死的也算进 pending，会让页面天天显示
    "有 3 单待补"，但一单都不会再动 —— 数字好看，事故照旧。
    """
    conn = _conn()
    try:
        mx = int(max_attempts)
        q = lambda sql, *a: int(conn.execute(sql, a).fetchone()["n"] or 0)
        done = q("SELECT COUNT(*) n FROM platform_ship WHERE done=1")
        stopped = q("SELECT COUNT(*) n FROM platform_ship WHERE done=0 AND stopped=1")
        pending = q("SELECT COUNT(*) n FROM platform_ship "
                    "WHERE done=0 AND stopped=0 AND attempts < ?", mx)
        stuck_n = q("SELECT COUNT(*) n FROM platform_ship "
                    "WHERE done=0 AND stopped=0 AND attempts >= ?", mx)
        stuck = [dict(r) for r in conn.execute(
            "SELECT order_id, cookie_id, item_id, attempts, last_error, updated_at "
            "FROM platform_ship WHERE done=0 AND stopped=0 AND attempts >= ? "
            "ORDER BY updated_at DESC LIMIT 30", (mx,)).fetchall()]
    finally:
        conn.close()
    return {"done": done, "pending": pending, "stopped": stopped,
            "stuck_count": stuck_n, "stuck": stuck}


def stats(days: int = 7) -> Dict[str, Any]:
    cutoff = (datetime.now() - timedelta(days=int(days))).isoformat(timespec="seconds")
    conn = _conn()
    try:
        by = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM delivery_cards WHERE updated_at >= ? GROUP BY status",
            (cutoff,)).fetchall()}
        orders = conn.execute(
            "SELECT COUNT(DISTINCT order_id) n FROM delivery_cards WHERE updated_at >= ?",
            (cutoff,)).fetchone()["n"]
        recent = [dict(r) for r in conn.execute(
            "SELECT order_id, cookie_id, idx, kind, status, attempts, last_error, updated_at "
            "FROM delivery_cards WHERE status IN ('failed','unknown') "
            "ORDER BY updated_at DESC LIMIT 30").fetchall()]
        # 清理老数据（sent 的卡留 90 天足够追溯，卡密明文不该长期留着）
        keep_after = (datetime.now() - timedelta(days=int(get_config()["keep_days"]))).isoformat(timespec="seconds")
        conn.execute("DELETE FROM delivery_cards WHERE status IN ('sent','collected') AND updated_at < ?", (keep_after,))
        conn.execute("DELETE FROM platform_ship WHERE done=1 AND updated_at < ?", (keep_after,))
        conn.execute("DELETE FROM delivery_order_observations WHERE observed_at < ?", (keep_after,))
        conn.commit()
    finally:
        conn.close()
    return {"config": get_config(), "by_status": by, "orders": int(orders or 0),
            "unresolved": recent, "platform_ship": platform_ship_stats()}


__all__ = ["get_config", "save_config", "parse_content", "ensure_order", "record_missing",
           "mark_shortage",
           "cards", "claim_card", "mark_sent", "mark_failed", "mark_unknown",
           "record_send_exception", "DeliverySubmissionNotAttempted",
           "DeliverySubmissionUnknown", "release_card", "force_retry", "unresolved", "summary",
           "resend_candidates", "reap_expired_leases",
           "SELF_SERVICE_RESEND_HINT", "is_self_service_resend_trigger",
           "append_self_service_resend_hint",
           "begin_self_service_request", "finish_self_service_request",
           "claim_self_service_card", "finish_self_service_card",
           "latest_sent_delivery",
           "note_platform_ship", "mark_platform_shipped", "pending_platform_shipments",
           "platform_ship_rows", "retry_platform_ship", "platform_ship_stats",
           "stats", "DEFAULT_CONFIG", "DB_PATH", "CONFIG_PATH"]
