"""Persistent fulfillment orchestration for opportunity publishing.

The module connects existing capabilities in a strict order:

publish submission -> exact itemId readback -> local item sync -> card binding
-> inventory recheck -> delivery ready.

Supplier purchasing is optional and fail-closed. A network transport is never
created here. The caller must inject a contract-verified adapter and an exact
request factory; without both, the existing local/API card fulfillment path is
preserved.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from card_text import split_codes
from sqlite_runtime import connect_sqlite
from supplier_adapters import (
    CapabilityStatus,
    ContractStatus,
    PurchaseRequest,
    PurchaseResult,
    PurchaseStatus,
    SupplierCapability,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "opportunity_fulfillment.db"

TASK_STATUSES = (
    "draft",
    "ready_to_publish",
    "inventory_not_ready",
    "publishing",
    "publish_throttled",
    "publish_blocked_night",
    "publish_blocked_guard",
    "submitted_pending_readback",
    "readback_not_visible",
    "readback_failed",
    "readback_confirmed",
    "sync_confirmed",
    "delivery_bound",
    "delivery_ready",
    "submission_unknown_review_required",
    "duplicate_submission",
    "failed",
)

DELIVERY_READY = frozenset({"delivery_ready"})
MANUAL_REVIEW = frozenset(
    {
        "submission_unknown_review_required",
        "duplicate_submission",
        "failed",
    }
)
RESUMABLE = frozenset(
    {
        "ready_to_publish",
        "inventory_not_ready",
        "publish_throttled",
        "publish_blocked_night",
        "submitted_pending_readback",
        "readback_not_visible",
        "readback_failed",
        "readback_confirmed",
        "sync_confirmed",
        "delivery_bound",
    }
)
VALID_DELIVERY = ("包邮", "按距离计费", "一口价", "无需邮寄")
SOURCE_FIELDS = (
    "title",
    "price",
    "supplier",
    "url",
    "source",
    "source_label",
    "source_url",
    "updated_at",
)


class FulfillmentValidationError(ValueError):
    """The request is not executable, but no account or card ownership failed."""


class FulfillmentOwnershipError(PermissionError):
    """The account or card does not belong to the active user."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json_loads(raw: str, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _to_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise FulfillmentValidationError(f"{field} 必须是整数") from exc


def _to_float(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise FulfillmentValidationError(f"{field} 必须是数字") from exc


def card_inventory(card: Optional[Dict[str, Any]], quantity: int = 1) -> Dict[str, Any]:
    """Return whether the card group can deliver the requested quantity."""
    if not card:
        return {"ready": False, "kind": "", "reason": "卡密组不存在"}
    if not bool(card.get("enabled")):
        return {
            "ready": False,
            "kind": str(card.get("type") or ""),
            "reason": "卡密组已停用",
        }

    kind = str(card.get("type") or "").strip()
    if kind == "data":
        available = len(set(split_codes(card.get("data_content"))))
        if not available:
            reason = "批量卡密组没有剩余内容"
        elif available < quantity:
            reason = f"卡密库存不足，需要 {quantity} 张，库存剩余 {available} 张"
        else:
            reason = ""
        return {
            "ready": available >= quantity,
            "kind": kind,
            "available": available,
            "reason": reason,
        }
    if kind == "text":
        ready = bool(str(card.get("text_content") or "").strip())
        return {
            "ready": ready,
            "kind": kind,
            "reason": "" if ready else "文本卡券内容为空",
        }
    if kind == "image":
        ready = bool(str(card.get("image_url") or "").strip())
        return {
            "ready": ready,
            "kind": kind,
            "reason": "" if ready else "图片卡券地址为空",
        }
    if kind == "api":
        config = card.get("api_config")
        if isinstance(config, str):
            config = _json_loads(config, {})
        ready = isinstance(config, dict) and bool(str(config.get("url") or "").strip())
        return {
            "ready": ready,
            "kind": kind,
            "reason": "" if ready else "API 卡券缺少 url",
        }
    return {
        "ready": False,
        "kind": kind,
        "reason": f"不支持的卡券类型：{kind or '空'}",
    }


class FulfillmentStore:
    """Small SQLite task store, intentionally separate from the main app DB."""

    def __init__(self, path: Optional[Path | str] = None):
        configured = os.environ.get("OPPORTUNITY_FULFILLMENT_DB")
        self.path = Path(path or configured or DEFAULT_DB_PATH)
        self._schema_lock = threading.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect_sqlite(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self) -> None:
        with self._schema_lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS opportunity_fulfillment_tasks (
                        id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        cookie_id TEXT NOT NULL,
                        card_id INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        stage TEXT NOT NULL DEFAULT '',
                        message TEXT NOT NULL DEFAULT '',
                        idempotency_key TEXT NOT NULL DEFAULT '',
                        item_id TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL,
                        result_json TEXT NOT NULL DEFAULT '{}',
                        item_detail TEXT NOT NULL DEFAULT '',
                        error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_opportunity_fulfillment_user_time
                        ON opportunity_fulfillment_tasks(user_id, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_opportunity_fulfillment_status
                        ON opportunity_fulfillment_tasks(status, updated_at DESC);
                    CREATE TABLE IF NOT EXISTS opportunity_fulfillment_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT '',
                        stage TEXT NOT NULL DEFAULT '',
                        message TEXT NOT NULL DEFAULT '',
                        detail_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(task_id)
                            REFERENCES opportunity_fulfillment_tasks(id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_opportunity_fulfillment_event_task
                        ON opportunity_fulfillment_events(task_id, id);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _decode_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        task = dict(row)
        task["payload"] = _json_loads(task.pop("payload_json", ""), {})
        task["result"] = _json_loads(task.pop("result_json", ""), {})
        task["item_detail"] = _json_loads(task.get("item_detail", ""), {})
        return task

    def create_task(
        self,
        *,
        user_id: int,
        cookie_id: str,
        card_id: int,
        payload: Dict[str, Any],
        status: str,
        stage: str,
        message: str,
        idempotency_key: str,
        result: Dict[str, Any],
        error: str = "",
    ) -> Dict[str, Any]:
        task_id = f"ful_{uuid.uuid4().hex}"
        stamp = _now()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO opportunity_fulfillment_tasks (
                    id, user_id, cookie_id, card_id, status, stage, message,
                    idempotency_key, item_id, payload_json, result_json,
                    item_detail, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, '{}', ?, ?, ?)
                """,
                (
                    task_id,
                    int(user_id),
                    cookie_id,
                    int(card_id),
                    status,
                    stage,
                    message,
                    idempotency_key,
                    _json_dumps(payload),
                    _json_dumps(result),
                    error,
                    stamp,
                    stamp,
                ),
            )
            conn.execute(
                """
                INSERT INTO opportunity_fulfillment_events (
                    task_id, status, stage, message, detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    status,
                    stage,
                    message,
                    _json_dumps(result),
                    stamp,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_task(task_id, user_id=user_id) or {}

    def get_task(self, task_id: str, *, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            if user_id is None:
                row = conn.execute(
                    "SELECT * FROM opportunity_fulfillment_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM opportunity_fulfillment_tasks WHERE id = ? AND user_id = ?",
                    (task_id, int(user_id)),
                ).fetchone()
            return self._decode_row(row)
        finally:
            conn.close()

    def list_tasks(self, user_id: int, limit: int = 50) -> list[Dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM opportunity_fulfillment_tasks
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (int(user_id), max(1, min(int(limit), 200))),
            ).fetchall()
            return [self._decode_row(row) for row in rows if row is not None]
        finally:
            conn.close()

    def update_task(
        self,
        task_id: str,
        *,
        status: Optional[str] = None,
        stage: Optional[str] = None,
        message: Optional[str] = None,
        item_id: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        item_detail: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        fields = []
        params: list[Any] = []
        if status is not None:
            if status not in TASK_STATUSES:
                raise ValueError(f"未知履约状态：{status}")
            fields.append("status = ?")
            params.append(status)
        if stage is not None:
            fields.append("stage = ?")
            params.append(stage)
        if message is not None:
            fields.append("message = ?")
            params.append(message)
        if item_id is not None:
            fields.append("item_id = ?")
            params.append(str(item_id))
        if result is not None:
            fields.append("result_json = ?")
            params.append(_json_dumps(result))
        if item_detail is not None:
            fields.append("item_detail = ?")
            params.append(_json_dumps(item_detail))
        if error is not None:
            fields.append("error = ?")
            params.append(error)
        stamp = _now()
        fields.append("updated_at = ?")
        params.append(stamp)

        conn = self._connect()
        try:
            conn.execute(
                f"UPDATE opportunity_fulfillment_tasks SET {', '.join(fields)} WHERE id = ?",
                (*params, task_id),
            )
            if status is not None or message:
                conn.execute(
                    """
                    INSERT INTO opportunity_fulfillment_events (
                        task_id, status, stage, message, detail_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        status or "",
                        stage or "",
                        message or "",
                        _json_dumps(result or {}),
                        stamp,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        return self.get_task(task_id)

    def events(self, task_id: str, *, user_id: int) -> list[Dict[str, Any]]:
        conn = self._connect()
        try:
            task = conn.execute(
                "SELECT id FROM opportunity_fulfillment_tasks WHERE id = ? AND user_id = ?",
                (task_id, int(user_id)),
            ).fetchone()
            if not task:
                return []
            rows = conn.execute(
                """
                SELECT id, status, stage, message, detail_json, created_at
                FROM opportunity_fulfillment_events
                WHERE task_id = ?
                ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()
            events = []
            for row in rows:
                event = dict(row)
                event["detail"] = _json_loads(event.pop("detail_json", ""), {})
                events.append(event)
            return events
        finally:
            conn.close()


class _DefaultDBProxy:
    def __getattr__(self, name: str):
        from db_manager import db_manager

        return getattr(db_manager, name)


class _DefaultIdempotencyProxy:
    def __getattr__(self, name: str):
        import idempotency

        return getattr(idempotency, name)


class _DefaultRateLimiterProxy:
    def __getattr__(self, name: str):
        import rate_limiter

        return getattr(rate_limiter, name)


class _DefaultPublishGuardProxy:
    def __getattr__(self, name: str):
        import publish_guard

        return getattr(publish_guard, name)


def _default_resolve_cookie(cookie_id: str) -> str:
    from opportunity_api import _resolve_cookie as resolve

    return resolve(cookie_id)


def _default_publisher(*args, **kwargs):
    from opportunity_publisher import publish_item

    return publish_item(*args, **kwargs)


def _default_readback(*args, **kwargs):
    from shop_diagnosis import fetch_my_items

    return fetch_my_items(*args, **kwargs)


class FulfillmentOrchestrator:
    """Advance persisted fulfillment tasks through existing application APIs."""

    def __init__(
        self,
        *,
        store: Optional[FulfillmentStore] = None,
        db: Any = None,
        publisher: Optional[Callable[..., Dict[str, Any]]] = None,
        readback: Optional[Callable[..., Dict[str, Any]]] = None,
        resolve_cookie: Optional[Callable[[str], str]] = None,
        app_key: str = "",
        idempotency_api: Any = None,
        rate_limiter_api: Any = None,
        publish_guard_api: Any = None,
        night_ban_ok: Optional[Callable[[], tuple[bool, str]]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        readback_attempts: int = 3,
        readback_delay_seconds: float = 2.0,
        supplier_adapter: Any = None,
        supplier_request_factory: Optional[
            Callable[[Dict[str, Any]], PurchaseRequest]
        ] = None,
    ):
        self.store = store or FulfillmentStore()
        self.db = db or _DefaultDBProxy()
        self.publisher = publisher or _default_publisher
        self.readback = readback or _default_readback
        self.resolve_cookie = resolve_cookie or _default_resolve_cookie
        if not app_key:
            from config import config

            app_key = str(config.get("APP_CONFIG.app_key", ""))
        self.app_key = app_key
        self.idempotency = idempotency_api or _DefaultIdempotencyProxy()
        self.rate_limiter = rate_limiter_api or _DefaultRateLimiterProxy()
        self.publish_guard = publish_guard_api or _DefaultPublishGuardProxy()
        self.night_ban_ok = night_ban_ok or self._default_night_ban_ok
        self.sleep_fn = sleep_fn
        self.readback_attempts = max(1, int(readback_attempts))
        self.readback_delay_seconds = max(0.0, float(readback_delay_seconds))
        self.supplier_adapter = supplier_adapter
        self.supplier_request_factory = supplier_request_factory
        self._active_lock = threading.Lock()
        self._active_tasks: set[str] = set()

    @staticmethod
    def _default_night_ban_ok() -> tuple[bool, str]:
        hour = datetime.now().hour
        if hour >= 23 or hour < 8:
            return False, "当前处于夜间时段（23:00-08:00），已暂停自动发布"
        return True, ""

    def _load_owned_card(
        self,
        user_id: int,
        cookie_id: str,
        card_id: int,
        *,
        require_enabled: bool,
    ) -> Dict[str, Any]:
        cookies = self.db.get_all_cookies(int(user_id)) or {}
        if cookie_id not in cookies:
            raise FulfillmentOwnershipError("闲鱼账号不属于当前用户")
        card = self.db.get_card_by_id(int(card_id), int(user_id))
        if not card:
            raise FulfillmentOwnershipError("卡密组不属于当前用户")
        if require_enabled and not bool(card.get("enabled")):
            raise FulfillmentOwnershipError("卡密组已停用")
        return card

    @staticmethod
    def _normalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        cookie_id = str(payload.get("cookie_id") or "").strip()
        if not cookie_id:
            raise FulfillmentValidationError("发布账号未指定")
        card_id = _to_int(payload.get("card_id"), "card_id")

        title = str(payload.get("title") or "").strip()
        desc = str(payload.get("desc") or payload.get("description") or "").strip()
        images = payload.get("imageUrls") or payload.get("images") or []
        if not title:
            raise FulfillmentValidationError("发布标题不能为空")
        if not desc:
            raise FulfillmentValidationError("发布描述不能为空")
        if not isinstance(images, list) or not images:
            raise FulfillmentValidationError("发布至少需要一张主图")

        price = _to_float(payload.get("price"), "price")
        post_price = _to_float(payload.get("postPrice") or 0, "postPrice")
        quantity = _to_int(payload.get("quantity") or 1, "quantity")
        if price <= 0:
            raise FulfillmentValidationError("price 必须大于 0")
        if post_price < 0:
            raise FulfillmentValidationError("postPrice 不能小于 0")
        if quantity <= 0:
            raise FulfillmentValidationError("quantity 必须大于 0")

        delivery = str(payload.get("delivery") or "无需邮寄").strip()
        if delivery not in VALID_DELIVERY:
            raise FulfillmentValidationError(f"不支持的邮寄方式：{delivery}")

        normalized: Dict[str, Any] = {
            "cookie_id": cookie_id,
            "card_id": card_id,
            "title": title,
            "desc": desc,
            "imageUrls": images,
            "price": price,
            "quantity": quantity,
            "delivery": delivery,
            "postPrice": post_price,
            "tags": payload.get("tags") if isinstance(payload.get("tags"), list) else [],
        }
        if isinstance(payload.get("category"), dict):
            normalized["category"] = payload["category"]
        source = payload.get("source")
        if isinstance(source, dict):
            normalized["source"] = {
                key: source.get(key)
                for key in SOURCE_FIELDS
                if source.get(key) is not None
            }
        return normalized

    def create_task(self, user_id: int, raw_payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = self._normalize_payload(raw_payload)
        card = self._load_owned_card(
            user_id,
            payload["cookie_id"],
            payload["card_id"],
            require_enabled=True,
        )
        inventory = card_inventory(card, payload["quantity"])

        guard = self.publish_guard.check_publish(
            payload["title"],
            payload["desc"],
            payload.get("tags") or [],
            images=len(payload["imageUrls"]),
        )
        if not guard.get("ok"):
            raise FulfillmentValidationError(
                "内容含平台违禁词：" + "、".join(guard.get("block") or [])
            )

        idem_key = self.idempotency.make_key(
            "publish",
            payload["cookie_id"],
            payload["title"],
            payload["price"],
            payload["desc"],
        )
        supplier_procurement, supplier_ready = (
            self._supplier_procurement_preflight()
        )
        result = {
            "inventory": inventory,
            "guard_warnings": guard.get("warnings") or [],
            "supplier_procurement": supplier_procurement,
        }
        if supplier_ready:
            status = (
                "ready_to_publish"
                if inventory.get("ready")
                else "inventory_not_ready"
            )
            message = (
                "已通过账号、卡密组和库存检查，等待发布"
                if inventory.get("ready")
                else str(inventory.get("reason") or "卡密组库存未就绪")
            )
            stage = "preflight"
            error = ""
        else:
            status = "failed"
            stage = "supplier_preflight"
            message = str(
                supplier_procurement.get("message")
                or "供应商采购预检未通过，未进入自动发布"
            )
            error = str(
                supplier_procurement.get("reason")
                or supplier_procurement.get("message")
                or "supplier preflight failed"
            )
        return self.store.create_task(
            user_id=int(user_id),
            cookie_id=payload["cookie_id"],
            card_id=payload["card_id"],
            payload=payload,
            status=status,
            stage=stage,
            message=message,
            idempotency_key=idem_key,
            result=result,
            error=error,
        )

    def _supplier_procurement_preflight(self) -> tuple[Dict[str, Any], bool]:
        if self.supplier_adapter is None:
            return (
                {
                    "configured": False,
                    "mode": "supplier_adapter_required",
                    "message": (
                        "尚未取得供应商下单接口契约；"
                        "当前使用已绑定的本地或 API 卡券履约"
                    ),
                },
                True,
            )

        try:
            capability = self.supplier_adapter.capability()
        except Exception as exc:
            return (
                {
                    "configured": True,
                    "mode": "supplier_adapter_invalid",
                    "message": "供应商适配器能力检查失败，未进入自动发布",
                    "reason": f"{type(exc).__name__}: {str(exc)[:300]}",
                },
                False,
            )

        if not isinstance(capability, SupplierCapability):
            return (
                {
                    "configured": True,
                    "mode": "supplier_adapter_invalid",
                    "message": "供应商适配器返回了无效的能力信息，未进入自动发布",
                    "reason": "capability result is invalid",
                },
                False,
            )

        available = (
            capability.capability_status is CapabilityStatus.AVAILABLE
            and capability.contract_status is ContractStatus.VERIFIED
        )
        procurement = {
            "configured": True,
            "mode": (
                "supplier_adapter_available"
                if available
                else "supplier_adapter_blocked"
            ),
            "message": (
                (
                    "供应商适配器与采购请求构造器已就绪"
                    if self.supplier_request_factory is not None
                    else "供应商适配器契约已就绪，但缺少采购请求构造器"
                )
                if available
                else capability.reason
            ),
            "supplier_code": capability.supplier_code,
            "supplier_label": capability.supplier_label,
            "capability_status": capability.capability_status.value,
            "contract_status": capability.contract_status.value,
            "reason": capability.reason,
            "endpoint_url": capability.endpoint_url,
            "http_method": capability.http_method,
        }
        if available and self.supplier_request_factory is None:
            procurement["mode"] = "supplier_request_factory_required"
            procurement["message"] = (
                "供应商采购请求构造器未注入，未进入自动发布"
            )
            procurement["reason"] = procurement["message"]
            return procurement, False
        return procurement, available

    def _transition(
        self,
        task: Dict[str, Any],
        status: str,
        stage: str,
        message: str,
        *,
        item_id: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        item_detail: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        updated = self.store.update_task(
            task["id"],
            status=status,
            stage=stage,
            message=message,
            item_id=item_id,
            result=result if result is not None else task.get("result") or {},
            item_detail=item_detail,
            error=error,
        )
        if updated:
            task.clear()
            task.update(updated)
        return task

    def execute_task(self, task_id: str) -> Dict[str, Any]:
        with self._active_lock:
            if task_id in self._active_tasks:
                return self.store.get_task(task_id) or {}
            self._active_tasks.add(task_id)
        try:
            return self._execute_task(task_id)
        except FulfillmentOwnershipError as exc:
            task = self.store.get_task(task_id) or {"id": task_id, "result": {}}
            return self._transition(
                task,
                "failed",
                "ownership",
                str(exc),
                error=str(exc),
            )
        except Exception as exc:
            task = self.store.get_task(task_id) or {"id": task_id, "result": {}}
            return self._transition(
                task,
                "failed",
                "exception",
                f"履约任务异常：{str(exc)[:300]}",
                error=str(exc)[:1000],
            )
        finally:
            with self._active_lock:
                self._active_tasks.discard(task_id)

    def _execute_task(self, task_id: str) -> Dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task:
            raise FulfillmentValidationError("履约任务不存在")
        if task["status"] in DELIVERY_READY or task["status"] in MANUAL_REVIEW:
            return task
        if task["status"] == "publishing" and not task.get("item_id"):
            return self._transition(
                task,
                "submission_unknown_review_required",
                "submit",
                "任务停留在发布中且没有 itemId，无法判断平台是否已接收，请人工核对",
            )

        user_id = int(task["user_id"])
        payload = task["payload"]
        card = self._load_owned_card(
            user_id,
            str(task["cookie_id"]),
            int(task["card_id"]),
            require_enabled=False,
        )
        inventory = card_inventory(card, payload["quantity"])
        result = dict(task.get("result") or {})
        result["inventory"] = inventory
        if not inventory.get("ready"):
            return self._transition(
                task,
                "inventory_not_ready",
                "inventory",
                str(inventory.get("reason") or "卡密组库存未就绪"),
                result=result,
            )

        if not task.get("item_id"):
            task = self._ensure_supplier_purchase(task)
            if task["status"] in MANUAL_REVIEW:
                return task
            published = self._publish(task)
            if published["status"] != "submitted_pending_readback":
                return published
            task = published

        return self._complete_after_publish(task)

    @staticmethod
    def _purchase_result_payload(result: PurchaseResult) -> Dict[str, Any]:
        return {
            "request_id": result.request_id,
            "idempotency_key": result.idempotency_key,
            "supplier_code": result.supplier_code,
            "status": result.status.value,
            "message": result.message,
            "contract_status": result.contract_status.value,
            "transport_called": result.transport_called,
            "total_cost": (
                str(result.total_cost)
                if result.total_cost is not None
                else None
            ),
            "margin": str(result.margin) if result.margin is not None else None,
            "supplier_order_id": result.supplier_order_id,
            "delivery_code_count": len(result.delivery_codes),
            "original_status": (
                result.original_status.value
                if result.original_status is not None
                else None
            ),
        }

    @staticmethod
    def _purchase_result_succeeded(result: PurchaseResult) -> bool:
        if not result.supplier_order_id:
            return False
        return (
            result.status is PurchaseStatus.SUCCEEDED
            or (
                result.status is PurchaseStatus.DUPLICATE
                and result.original_status is PurchaseStatus.SUCCEEDED
            )
        )

    def _ensure_supplier_purchase(self, task: Dict[str, Any]) -> Dict[str, Any]:
        procurement = (task.get("result") or {}).get("supplier_procurement") or {}
        if procurement.get("configured") is not True:
            return task

        if self.supplier_adapter is None:
            result = {
                **task.get("result", {}),
                "supplier_purchase": {
                    "status": "adapter_unavailable",
                    "message": "任务要求供应商采购，但当前服务未注入供应商适配器",
                    "transport_called": False,
                },
            }
            return self._transition(
                task,
                "failed",
                "supplier_purchase",
                result["supplier_purchase"]["message"],
                result=result,
                error=result["supplier_purchase"]["message"],
            )

        previous = (task.get("result") or {}).get("supplier_purchase")
        if isinstance(previous, dict):
            previous_status = str(previous.get("status") or "")
            previous_original = str(previous.get("original_status") or "")
            previous_order_id = str(previous.get("supplier_order_id") or "").strip()
            if previous_order_id and (
                previous_status == PurchaseStatus.SUCCEEDED.value
                or (
                    previous_status == PurchaseStatus.DUPLICATE.value
                    and previous_original == PurchaseStatus.SUCCEEDED.value
                )
            ):
                return task
            if previous_status == "in_flight":
                message = (
                    "供应商采购请求已发出，但本地没有记录到最终结果。"
                    "为避免重复采购，禁止自动重试，请先到供应商后台人工核对"
                )
                result = {
                    **task.get("result", {}),
                    "supplier_purchase": {
                        **previous,
                        "status": "submission_unknown",
                        "message": message,
                    },
                }
                return self._transition(
                    task,
                    "submission_unknown_review_required",
                    "supplier_purchase",
                    message,
                    result=result,
                    error=message,
                )

        if self.supplier_request_factory is None:
            result = {
                **task.get("result", {}),
                "supplier_purchase": {
                    "status": "request_factory_unavailable",
                    "message": "任务要求供应商采购，但当前服务未注入采购请求构造器",
                    "transport_called": False,
                },
            }
            return self._transition(
                task,
                "failed",
                "supplier_purchase",
                result["supplier_purchase"]["message"],
                result=result,
                error=result["supplier_purchase"]["message"],
            )

        try:
            request = self.supplier_request_factory(task)
        except Exception as exc:
            message = f"采购请求构造失败：{type(exc).__name__}: {str(exc)[:300]}"
            result = {
                **task.get("result", {}),
                "supplier_purchase": {
                    "status": "request_build_failed",
                    "message": message,
                    "transport_called": False,
                },
            }
            return self._transition(
                task,
                "failed",
                "supplier_purchase",
                message,
                result=result,
                error=str(exc)[:1000],
            )
        if not isinstance(request, PurchaseRequest):
            message = "采购请求构造器未返回 PurchaseRequest，未调用供应商 transport"
            result = {
                **task.get("result", {}),
                "supplier_purchase": {
                    "status": "invalid_request",
                    "message": message,
                    "transport_called": False,
                },
            }
            return self._transition(
                task,
                "failed",
                "supplier_purchase",
                message,
                result=result,
                error="invalid PurchaseRequest",
            )

        attempt = {
            "request_id": request.request_id,
            "idempotency_key": request.idempotency_key,
            "supplier_code": procurement.get("supplier_code") or "",
            "status": "in_flight",
            "message": "供应商采购请求已持久化，正在调用上游",
            "transport_called": False,
            "started_at": _now(),
        }
        task = self._transition(
            task,
            str(task["status"]),
            "supplier_purchase",
            attempt["message"],
            result={
                **task.get("result", {}),
                "supplier_purchase": attempt,
            },
        )

        try:
            purchase = self.supplier_adapter.purchase(request)
        except Exception as exc:
            message = f"供应商采购异常：{type(exc).__name__}: {str(exc)[:300]}"
            result = {
                **task.get("result", {}),
                "supplier_purchase": {
                    "status": "orchestrator_exception",
                    "message": message,
                    "transport_called": None,
                },
            }
            return self._transition(
                task,
                "failed",
                "supplier_purchase",
                message,
                result=result,
                error=str(exc)[:1000],
            )
        if not isinstance(purchase, PurchaseResult):
            message = "供应商适配器未返回 PurchaseResult，未继续发布"
            result = {
                **task.get("result", {}),
                "supplier_purchase": {
                    "status": "invalid_response",
                    "message": message,
                    "transport_called": None,
                },
            }
            return self._transition(
                task,
                "failed",
                "supplier_purchase",
                message,
                result=result,
                error="invalid PurchaseResult",
            )

        purchase_payload = self._purchase_result_payload(purchase)
        result = {
            **task.get("result", {}),
            "supplier_purchase": purchase_payload,
        }
        if not self._purchase_result_succeeded(purchase):
            unknown_statuses = {
                PurchaseStatus.SUBMISSION_UNKNOWN,
                PurchaseStatus.INVALID_RESPONSE,
            }
            if purchase.status in unknown_statuses and purchase.transport_called:
                message = (
                    f"{purchase.message or '供应商响应无法确认采购结果'}；"
                    "已停止发布，请先到供应商后台人工核对"
                )
                return self._transition(
                    task,
                    "submission_unknown_review_required",
                    "supplier_purchase",
                    message,
                    result=result,
                    error=message,
                )
            return self._transition(
                task,
                "failed",
                "supplier_purchase",
                str(purchase.message or "供应商采购未成功，已停止发布"),
                result=result,
                error=str(purchase.message or purchase.status.value)[:1000],
            )

        return self._transition(
            task,
            str(task["status"]),
            "supplier_purchase",
            "供应商采购已完成，继续发布",
            result=result,
        )

    def _publish(self, task: Dict[str, Any]) -> Dict[str, Any]:
        payload = task["payload"]
        guard = self.publish_guard.check_publish(
            payload["title"],
            payload["desc"],
            payload.get("tags") or [],
            images=len(payload["imageUrls"]),
        )
        if not guard.get("ok"):
            return self._transition(
                task,
                "publish_blocked_guard",
                "guard",
                "发布前内容检查未通过",
                result={
                    **task.get("result", {}),
                    "guard": guard,
                },
            )

        night_ok, night_reason = self.night_ban_ok()
        if not night_ok:
            return self._transition(
                task,
                "publish_blocked_night",
                "schedule",
                night_reason,
            )

        idem_key = task["idempotency_key"] or self.idempotency.make_key(
            "publish",
            payload["cookie_id"],
            payload["title"],
            payload["price"],
            payload["desc"],
        )
        precheck = self.idempotency.precheck(idem_key)
        if precheck.get("exists"):
            record = precheck.get("record") or {}
            if record.get("status") == "submission_unknown":
                return self._transition(
                    task,
                    "submission_unknown_review_required",
                    "idempotency",
                    "相同内容的上一笔发布结果未知，禁止自动重发，请先人工核对",
                    result={**task.get("result", {}), "idempotency": record},
                )
            return self._transition(
                task,
                "duplicate_submission",
                "idempotency",
                "相同内容的发布记录已存在，未再次提交",
                result={**task.get("result", {}), "idempotency": record},
            )

        rate_ok, retry_after = self.rate_limiter.acquire(
            "publish",
            payload["cookie_id"],
            limit=1,
            window=60,
        )
        if not rate_ok:
            return self._transition(
                task,
                "publish_throttled",
                "rate_limit",
                f"该账号发布过于频繁，请 {retry_after} 秒后恢复任务",
                result={
                    **task.get("result", {}),
                    "retry_after": retry_after,
                },
            )

        claim = self.idempotency.claim(
            idem_key,
            "publish",
            note=str(payload["title"])[:80],
        )
        if claim.get("blocked") or not claim.get("claimed"):
            record = claim.get("record") or {}
            status = (
                "submission_unknown_review_required"
                if claim.get("blocked")
                else "duplicate_submission"
            )
            return self._transition(
                task,
                status,
                "idempotency",
                "并发发布保护已阻止重复提交",
                result={**task.get("result", {}), "idempotency": record},
            )

        task = self._transition(
            task,
            "publishing",
            "submit",
            "正在提交到闲鱼",
        )
        cookie_str = self.resolve_cookie(str(task["cookie_id"]))
        if not cookie_str:
            self.idempotency.release(idem_key)
            return self._transition(
                task,
                "failed",
                "auth",
                "账号 Cookie 不可用，请重新登录后再创建任务",
                error="Cookie unavailable",
            )

        try:
            published = self.publisher(
                cookie_str,
                {
                    key: payload[key]
                    for key in (
                        "title",
                        "desc",
                        "imageUrls",
                        "price",
                        "quantity",
                        "delivery",
                        "postPrice",
                        "category",
                    )
                    if key in payload
                },
                self.app_key,
                cookie_id=str(task["cookie_id"]),
            )
        except Exception as exc:
            submission_status = str(getattr(exc, "submission_status", "") or "")
            if submission_status == "submission_unknown":
                self.idempotency.finish(
                    idem_key,
                    "submission_unknown",
                    note=str(exc)[:500],
                )
                return self._transition(
                    task,
                    "submission_unknown_review_required",
                    str(getattr(exc, "stage", "") or "submit"),
                    "发布结果未知，可能已提交。请先到闲鱼「我发布的」人工核对",
                    result={
                        **task.get("result", {}),
                        "publish_error": str(exc)[:500],
                    },
                    error=str(exc)[:1000],
                )
            self.idempotency.release(idem_key)
            try:
                from opportunity_guard import record_failure

                record_failure(str(exc))
            except Exception:
                pass
            return self._transition(
                task,
                "failed",
                str(getattr(exc, "stage", "") or "submit"),
                f"发布失败：{str(exc)[:300]}",
                error=str(exc)[:1000],
            )

        item_id = str(published.get("itemId") or "").strip()
        if not item_id:
            self.idempotency.finish(
                idem_key,
                "submission_unknown",
                note="发布响应缺少 itemId",
            )
            return self._transition(
                task,
                "submission_unknown_review_required",
                "submit",
                "发布响应缺少 itemId，禁止自动重发，请人工核对",
            )

        self.idempotency.finish(idem_key, "done", note=item_id)
        return self._transition(
            task,
            "submitted_pending_readback",
            "readback",
            "发布已受理，正在按准确 itemId 回读商品",
            item_id=item_id,
            result={
                **task.get("result", {}),
                "publish": published,
            },
        )

    def _complete_after_publish(self, task: Dict[str, Any]) -> Dict[str, Any]:
        item_id = str(task.get("item_id") or "").strip()
        if not item_id:
            return self._transition(
                task,
                "submission_unknown_review_required",
                "readback",
                "任务没有准确 itemId，无法安全继续",
            )

        task = self._transition(
            task,
            "submitted_pending_readback",
            "readback",
            "正在按准确 itemId 回读商品",
            item_id=item_id,
        )
        matched: Optional[Dict[str, Any]] = None
        last_error = ""
        for attempt in range(self.readback_attempts):
            try:
                listed = self.readback(
                    self.resolve_cookie(str(task["cookie_id"])),
                    self.app_key,
                    cookie_id=str(task["cookie_id"]),
                    max_pages=3,
                )
            except Exception as exc:
                last_error = str(exc)[:300]
                listed = {"ok": False, "error": last_error, "items": []}
            if listed.get("ok"):
                for item in listed.get("items") or []:
                    if str(item.get("item_id") or "") == item_id:
                        matched = item
                        break
            else:
                last_error = str(listed.get("error") or "回读失败")[:300]
            if matched:
                break
            if attempt + 1 < self.readback_attempts:
                self.sleep_fn(self.readback_delay_seconds)

        if not matched:
            status = "readback_not_visible" if listed.get("ok") else "readback_failed"
            return self._transition(
                task,
                status,
                "readback",
                (
                    f"发布已受理，但连续 {self.readback_attempts} 次回读未见准确 itemId"
                    if listed.get("ok")
                    else f"发布已受理，但回读失败：{last_error}"
                ),
                item_id=item_id,
                result={
                    **task.get("result", {}),
                    "readback": {
                        "ok": bool(listed.get("ok")),
                        "error": last_error,
                        "matched_item_id": "",
                    },
                },
            )

        task = self._transition(
            task,
            "readback_confirmed",
            "readback",
            "已按准确 itemId 回读商品",
            item_id=item_id,
            item_detail=matched,
            result={
                **task.get("result", {}),
                "readback": {
                    "ok": True,
                    "error": "",
                    "matched_item_id": item_id,
                },
            },
        )

        payload = task["payload"]
        saved = self.db.save_item_basic_info(
            str(task["cookie_id"]),
            item_id,
            item_title=str(matched.get("title") or payload["title"]),
            item_description=payload["desc"],
            item_category=str(matched.get("category") or ""),
            item_price=str(matched.get("price") or payload["price"]),
            item_detail=_json_dumps(matched),
            item_image=str(matched.get("image_url") or ""),
        )
        if not saved or not self.db.get_item_info(str(task["cookie_id"]), item_id):
            return self._transition(
                task,
                "failed",
                "sync",
                "商品已回读，但写入本地商品库失败",
                item_id=item_id,
                error="save_item_basic_info failed",
            )
        task = self._transition(
            task,
            "sync_confirmed",
            "sync",
            "商品已同步到本地商品库",
            item_id=item_id,
        )

        card = self.db.get_card_by_id(int(task["card_id"]), int(task["user_id"]))
        inventory = card_inventory(card, payload["quantity"])
        result = {**task.get("result", {}), "inventory": inventory}
        if not inventory.get("ready"):
            return self._transition(
                task,
                "inventory_not_ready",
                "inventory",
                str(inventory.get("reason") or "卡密组库存未就绪"),
                item_id=item_id,
                result=result,
            )

        bound = self.db.update_item_delivery_settings(
            str(task["cookie_id"]),
            item_id,
            None,
            True,
            int(task["card_id"]),
        )
        if not bound:
            return self._transition(
                task,
                "failed",
                "bind",
                "商品卡密组绑定失败",
                item_id=item_id,
                error="update_item_delivery_settings failed",
            )
        settings = self.db.get_item_delivery_settings(
            str(task["cookie_id"]),
            item_id,
        )
        expected_ready = (
            bool(settings.get("auto_delivery_enabled"))
            and int(settings.get("delivery_card_id") or 0) == int(task["card_id"])
        )
        if not expected_ready:
            return self._transition(
                task,
                "failed",
                "bind_verify",
                "卡密组写入后复读不一致",
                item_id=item_id,
                result={**result, "delivery_settings": settings},
                error="delivery settings verification failed",
            )
        task = self._transition(
            task,
            "delivery_bound",
            "bind",
            "商品已绑定卡密组",
            item_id=item_id,
            result={**result, "delivery_settings": settings},
        )

        return self._transition(
            task,
            "delivery_ready",
            "ready",
            "发布、回读、同步、卡密绑定和库存复检均已完成",
            item_id=item_id,
        )

    def recover_task(self, task_id: str) -> Dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task:
            raise FulfillmentValidationError("履约任务不存在")
        if task["status"] == "publishing" and not task.get("item_id"):
            with self._active_lock:
                active = task_id in self._active_tasks
            if active:
                return task
            return self._transition(
                task,
                "submission_unknown_review_required",
                "submit",
                "任务停留在发布中且没有 itemId，请先核对平台是否已经上架",
            )
        return task

    def should_execute_after_recover(self, task: Dict[str, Any]) -> bool:
        return str(task.get("status") or "") in RESUMABLE


__all__ = [
    "FulfillmentOrchestrator",
    "FulfillmentOwnershipError",
    "FulfillmentStore",
    "FulfillmentValidationError",
    "TASK_STATUSES",
    "card_inventory",
]
