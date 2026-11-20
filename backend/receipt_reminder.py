"""
收货提醒（P1）：只读预演 + 按订单去重
=====================================
背景（拆解 28）：买家不确认收货时，我们现有的 `waiting` 态只会**重读自己**
（6h × 6 次 = 36 小时后放弃评价/小红花），缺的是"发货 24h 后还没确认，
就发一句话催一下"这个**往上推买家**的动作。鱼店长与闲管家都把这件事做成
带时间/间隔/次数/排除条件的流程，商业目的是加快回款。

边界（拆解 27）：发送通道没有回执校验，**收货提醒也一样**。所以本轮只做
只读预演——扫出"该提醒谁、为什么该/不该提醒"，绝不真发。真实发送等
"自己拍一单买 2 件"的探针确认 `sent` 语义后再启用（与发货回执共用同一单）。

本模块只碰自己的 `data/receipt_reminder.db`（去重账），订单数据只从
db_manager 只读获取。测试用 `RECEIPT_REMINDER_DB` 指到临时目录。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sqlite_runtime import connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = Path(os.environ.get("RECEIPT_REMINDER_DB") or (DATA_DIR / "receipt_reminder.db"))
CONFIG_PATH = Path(os.environ.get("RECEIPT_REMINDER_CONFIG") or (DATA_DIR / "receipt_reminder.json"))

DEFAULT_CONFIG: Dict[str, Any] = {
    # 总开关保持关：本轮只预演，真发要等真实探针验收后再由用户打开
    "enabled": False,
    # 发货后多少小时还没确认收货才提醒（鱼店长同类功能的最小旋钮）
    "delay_hours": 24,
    # 同一订单最多提醒几次（默认只提醒一次，避免骚扰）
    "max_per_order": 1,
    # 缺会话编号时是否仍列出候选（列出，但标记跳过原因，方便补录）
    "include_missing_chat": False,
}

# 不提醒的状态：已完成为买家已收货；退款/取消是售后问题；unknown 状态不明。
SKIP_STATUSES = {
    "completed": "已确认收货/交易完成",
    "refunding": "退款中",
    "cancelled": "已取消",
    "refund_cancelled": "退款后交易关闭",
    "unknown": "订单状态不明",
}

# 发送闸：没有验证过的回执语义之前，一律不开真发。
SEND_ENABLED = False


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
    for key in DEFAULT_CONFIG:
        if key in patch:
            try:
                if isinstance(DEFAULT_CONFIG[key], bool):
                    cfg[key] = bool(patch[key])
                elif isinstance(DEFAULT_CONFIG[key], int):
                    cfg[key] = int(patch[key])
            except Exception:
                continue
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS receipt_reminders (
            order_id   TEXT NOT NULL,
            cookie_id  TEXT NOT NULL,
            status     TEXT NOT NULL,       -- reminded | skipped | failed
            reason     TEXT DEFAULT '',
            attempts   INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (order_id, cookie_id)
        )
    """)
    conn.commit()
    return conn


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now()


def _parse_time(value: Any) -> Optional[datetime]:
    """订单时间可能是 'YYYY-MM-DD HH:MM:SS' 或带 T 的 ISO，统一解析。"""
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
    """只读拉订单。失败时返回空表，不抛异常（预演页面不该因读订单失败而崩）。"""
    try:
        from db_manager import db_manager
        if cookie_id:
            return db_manager.get_orders_by_cookie(cookie_id, limit=limit) or []
        return db_manager.get_all_orders(limit=limit) or []
    except Exception:
        return []


def _get_chat_id(order_id: str) -> str:
    try:
        from db_manager import db_manager
        row = db_manager.get_order_by_id(order_id) or {}
        return str(row.get("chat_id") or "")
    except Exception:
        return ""


def _reminder_rows() -> Dict[str, Dict[str, Any]]:
    conn = _conn()
    try:
        return {f"{r['order_id']}|{r['cookie_id']}": dict(r)
                for r in conn.execute("SELECT * FROM receipt_reminders").fetchall()}
    finally:
        conn.close()


def _account_state(cookie_id: str) -> Optional[str]:
    """
    账号级跳过原因（只读）。返回 None = 可以提醒。

    为什么必须加（拆解 39 §八-4）：鱼店长把"挂机 + 智能答复有效期"写成提醒的前置
    条件——提醒要靠在线会话发出去。我们原来的预演只数订单，不看账号：
    账号被后台关掉、熔断中或连接断了，候选照样列出来，人照着点也是白发。

    判定顺序（先便宜后贵，任何一步读不到就当作"不知道"而不是"正常"）：
      1. 账号启用开关（cookie_manager.cookie_status）
      2. 连接状态（有实例才看；没实例 = 没有运行中的连接）
      3. 账号级熔断（rate_limiter.circuit_blocked）
    """
    cid = str(cookie_id or "")
    if not cid:
        return None
    try:
        from cookie_manager import manager as cm
    except Exception:
        cm = None
    if cm is not None:
        try:
            if getattr(cm, "cookie_status", {}).get(cid) is False:
                return "账号已暂停（后台已关闭该账号）"
            instances = getattr(cm, "instances", None)
            if instances is not None:
                inst = instances.get(cid)
                if inst is None:
                    if instances:
                        return "账号离线（没有运行中的连接）"
                else:
                    state = getattr(inst, "connection_state", None)
                    name = getattr(state, "value", state)
                    if name in ("disconnected", "closed"):
                        return f"账号离线（连接状态 {name}）"
                    if name == "failed":
                        return "账号连接失败（等待重连）"
                    if name in ("connecting", "reconnecting"):
                        return f"账号重连中（{name}）"
        except Exception:
            pass
    try:
        import rate_limiter
        blocked, remain = rate_limiter.circuit_blocked(cid)
        if blocked:
            minutes = max(1, int(remain) // 60) if remain else 0
            return f"账号熔断中（剩余约 {minutes} 分钟）" if minutes else "账号熔断中"
    except Exception:
        pass
    return None


def scan(cookie_id: str = "", *, now: Optional[datetime] = None,
         orders: Optional[Iterable[Dict[str, Any]]] = None,
         chat_ids: Optional[Dict[str, str]] = None,
         account_states: Optional[Dict[str, str]] = None,
         limit: int = 1000) -> Dict[str, Any]:
    """
    只读预演：返回 candidates / skipped，两边的每一行都带原因。

    - `orders` 可注入（测试用）；默认从 db_manager 只读拉。
    - `chat_ids` 可注入 order_id→chat_id（测试用）；默认逐单查 orders.chat_id。
    - 不做任何写操作；去重只读 `receipt_reminders`。
    """
    cfg = get_config()
    now_dt = _now(now)
    delay = timedelta(hours=max(0, int(cfg.get("delay_hours") or 0)))
    max_per_order = max(1, int(cfg.get("max_per_order") or 1))

    if orders is None:
        orders = _load_orders(cookie_id=cookie_id, limit=limit)
    reminders = _reminder_rows()

    candidates: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for o in orders:
        order_id = str(o.get("order_id") or o.get("id") or "")
        if not order_id:
            continue
        cid = str(o.get("cookie_id") or cookie_id or "")
        status = str(o.get("status") or o.get("order_status") or "unknown")
        base = {"order_id": order_id, "cookie_id": cid,
                "item_id": str(o.get("item_id") or ""),
                "buyer_id": str(o.get("buyer_id") or ""),
                "status": status}

        def skip(reason: str) -> None:
            skipped.append({**base, "reason": reason})

        if status in SKIP_STATUSES:
            skip(SKIP_STATUSES[status])
            continue
        if status not in ("shipped", "pending_ship", "processing"):
            skip(f"不在可提醒状态（{status}）")
            continue

        if status == "shipped":
            changed = _parse_time(o.get("updated_at") or o.get("created_at"))
            if changed is None:
                skip("没有可用的发货时间")
                continue
            waited = now_dt - changed
            if waited < delay:
                remain = delay - waited
                hours = int(remain.total_seconds() // 3600)
                mins = int((remain.total_seconds() % 3600) // 60)
                skip(f"还没到提醒时间（约 {hours} 小时 {mins} 分后）")
                continue
        else:
            # pending_ship/processing 是"还没发货"，不是"催收货"的对象
            skip(f"尚未发货（{status}）")
            continue

        key = f"{order_id}|{cid}"
        rec = reminders.get(key)
        if rec and rec.get("status") == "reminded" and int(rec.get("attempts") or 0) >= max_per_order:
            skip(f"已提醒过 {rec.get('attempts')} 次（上限 {max_per_order}）")
            continue

        # 账号级跳过（拆解 39 §八-4）：提醒要靠在线会话发出去，
        # 账号暂停/离线时列出来也没人发得出去。放在去重之后、会话编号之前：
        # 已提醒过的不再报账号状态，未提醒的才需要知道"为什么这轮列不出来"。
        account_reason = (account_states or {}).get(cid)
        if account_reason is None and account_states is None:
            account_reason = _account_state(cid)
        if account_reason:
            skip(account_reason)
            continue

        chat_id = (chat_ids or {}).get(order_id, None)
        if chat_id is None:
            chat_id = _get_chat_id(order_id)
        if not chat_id and not cfg.get("include_missing_chat"):
            skip("缺少会话编号（无法发送，先补录或人工处理）")
            continue

        age_hours = round(max(0.0, (now_dt - _parse_time(
            o.get("updated_at") or o.get("created_at"))).total_seconds()) / 3600, 1)
        candidates.append({**base, "chat_id": chat_id,
                           "amount": o.get("amount"), "quantity": o.get("quantity"),
                           "updated_at": o.get("updated_at") or o.get("created_at"),
                           "waited_hours": age_hours,
                           "reminded_before": int((rec or {}).get("attempts") or 0),
                           "action": "preview_only"})

    return {
        "ok": True,
        "dry_run": True,
        "send_enabled": SEND_ENABLED,
        "send_block_reason": "" if SEND_ENABLED else
            "发送回执尚未验证（拆解 27/28 共用一单探针），本轮只预演不发送",
        "config": cfg,
        "scanned": len(orders) if hasattr(orders, "__len__") else None,
        "candidates": candidates,
        "skipped": skipped,
        "generated_at": now_dt.isoformat(timespec="seconds"),
    }


def mark_reminded(order_id: str, cookie_id: str, reason: str = "") -> bool:
    """
    记一次"已提醒"（**只应在真实发送成功后调用**）。
    发送路径本轮不开放，留这个函数是为了让去重逻辑可测、也为后续接真实发送。
    """
    if not order_id or not cookie_id:
        return False
    now = datetime.now().isoformat(timespec="seconds")
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO receipt_reminders (order_id, cookie_id, status, reason, attempts, "
            "created_at, updated_at) VALUES (?,?, 'reminded', ?, 1, ?, ?) "
            "ON CONFLICT(order_id, cookie_id) DO UPDATE SET "
            " status='reminded', reason=excluded.reason, attempts=attempts+1, "
            " updated_at=excluded.updated_at",
            (str(order_id), str(cookie_id), str(reason or "")[:200], now, now))
        conn.commit()
        return True
    finally:
        conn.close()


def recent(limit: int = 100) -> List[Dict[str, Any]]:
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM receipt_reminders ORDER BY updated_at DESC LIMIT ?",
            (int(limit),)).fetchall()]
    finally:
        conn.close()


__all__ = ["get_config", "save_config", "scan", "mark_reminded", "recent",
           "SEND_ENABLED", "DEFAULT_CONFIG", "DB_PATH"]
