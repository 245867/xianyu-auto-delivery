"""Order-level supplier procurement for depleted card inventory.

This module is deliberately fail-closed. It only calls a successfully
registered supplier adapter, persists the purchase intent before transport,
and reserves returned delivery codes in the delivery ledger before any buyer
message is sent.
"""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence

from card_text import split_codes
from sqlite_runtime import connect_sqlite
from supplier_adapters import (
    BaseSupplierAdapter,
    CostQuote,
    PurchaseRequest,
    PurchaseResult,
    PurchaseStatus,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BINDING_DB = (
    PROJECT_ROOT / "data" / "supplier_delivery_procurement.db"
)


class ProcurementStatus(str, Enum):
    PURCHASED = "purchased"
    NOT_CONFIGURED = "not_configured"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    ORDER_INVALID = "order_invalid"
    BINDING_INVALID = "binding_invalid"
    IN_FLIGHT = "in_flight"
    SUBMISSION_UNKNOWN = "submission_unknown"
    SUPPLIER_BLOCKED = "supplier_blocked"
    MARGIN_BELOW_MINIMUM = "margin_below_minimum"
    CODES_INVALID = "codes_invalid"
    CODES_CONFLICT = "codes_conflict"
    RESERVATION_FAILED = "reservation_failed"
    PERSISTENCE_FAILED = "persistence_failed"


@dataclass(frozen=True)
class SupplierDeliveryBinding:
    cookie_id: str
    item_id: str
    card_id: int
    supplier_code: str
    supplier_sku: str
    currency: str
    unit_cost: Decimal
    quote_updated_at: datetime
    minimum_total_margin: Decimal
    maximum_quote_age_seconds: int
    enabled: bool = True


@dataclass(frozen=True)
class ProcurementDecision:
    ok: bool
    status: ProcurementStatus
    message: str
    codes: tuple[str, ...] = ()
    request_id: str = ""
    idempotency_key: str = ""
    supplier_code: str = ""
    supplier_order_id: str = ""
    total_cost: Optional[Decimal] = None
    margin: Optional[Decimal] = None
    transport_called: bool = False
    alert_sent: bool = False

    def public_payload(self) -> Dict[str, Any]:
        """Return a UI/log-safe payload without delivery code contents."""
        return {
            "ok": self.ok,
            "status": self.status.value,
            "message": self.message,
            "code_count": len(self.codes),
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "supplier_code": self.supplier_code,
            "supplier_order_id": self.supplier_order_id,
            "total_cost": (
                str(self.total_cost)
                if self.total_cost is not None
                else None
            ),
            "margin": str(self.margin) if self.margin is not None else None,
            "transport_called": self.transport_called,
            "alert_sent": self.alert_sent,
        }


class CodeReservationError(RuntimeError):
    pass


def _decimal(value: Any, field_name: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is not a valid decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return parsed


def _aware_datetime(value: datetime, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _parse_order_amount(raw: Any) -> Decimal:
    text = str(raw or "").strip()
    for token in ("¥", "￥", ",", " "):
        text = text.replace(token, "")
    amount = _decimal(text, "order amount")
    if amount <= 0:
        raise ValueError("order amount must be greater than zero")
    return amount


def _decode_codes(raw: Any) -> tuple[str, ...]:
    try:
        values = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(values, list):
        return ()
    if not all(isinstance(value, str) and value.strip() for value in values):
        return ()
    return tuple(value.strip() for value in values)


def _decode_target_indices(raw: Any, quantity: int) -> Optional[tuple[int, ...]]:
    try:
        values = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(values, list) or len(values) != int(quantity):
        return None
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in values
    ):
        return None
    indices = tuple(values)
    if len(set(indices)) != len(indices):
        return None
    return indices


class SupplierBindingStore:
    """Persistent binding and per-order purchase state."""

    def __init__(self, path: Optional[Path | str] = None):
        configured = os.environ.get("SUPPLIER_DELIVERY_DB")
        self.path = Path(path or configured or DEFAULT_BINDING_DB)
        self._schema_lock = threading.Lock()
        self._ensure_schema()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect_sqlite(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._schema_lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS supplier_delivery_bindings (
                        cookie_id TEXT NOT NULL,
                        item_id TEXT NOT NULL,
                        card_id INTEGER NOT NULL,
                        supplier_code TEXT NOT NULL,
                        supplier_sku TEXT NOT NULL,
                        currency TEXT NOT NULL,
                        unit_cost TEXT NOT NULL,
                        quote_updated_at TEXT NOT NULL,
                        minimum_total_margin TEXT NOT NULL,
                        maximum_quote_age_seconds INTEGER NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (cookie_id, item_id, card_id)
                    );
                    CREATE TABLE IF NOT EXISTS supplier_delivery_purchases (
                        order_id TEXT NOT NULL,
                        cookie_id TEXT NOT NULL,
                        item_id TEXT NOT NULL,
                        card_id INTEGER NOT NULL,
                        buyer_id TEXT NOT NULL DEFAULT '',
                        quantity INTEGER NOT NULL,
                        request_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        supplier_code TEXT NOT NULL,
                        supplier_sku TEXT NOT NULL,
                        target_indices_json TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL,
                        supplier_order_id TEXT NOT NULL DEFAULT '',
                        total_cost TEXT NOT NULL DEFAULT '',
                        margin TEXT NOT NULL DEFAULT '',
                        message TEXT NOT NULL DEFAULT '',
                        codes_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (order_id, cookie_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_sdp_status
                        ON supplier_delivery_purchases(status, updated_at);
                    """
                )
                self._migrate_schema(conn)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        ordered = sorted(
            (
                (int(row[5]), str(row[1]))
                for row in rows
                if int(row[5]) > 0
            ),
            key=lambda item: item[0],
        )
        return [name for _, name in ordered]

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        expected_binding_pk = ["cookie_id", "item_id", "card_id"]
        if self._primary_key_columns(conn, "supplier_delivery_bindings") != expected_binding_pk:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DROP TABLE IF EXISTS supplier_delivery_bindings_v2")
                conn.execute(
                    """
                    CREATE TABLE supplier_delivery_bindings_v2 (
                        cookie_id TEXT NOT NULL,
                        item_id TEXT NOT NULL,
                        card_id INTEGER NOT NULL,
                        supplier_code TEXT NOT NULL,
                        supplier_sku TEXT NOT NULL,
                        currency TEXT NOT NULL,
                        unit_cost TEXT NOT NULL,
                        quote_updated_at TEXT NOT NULL,
                        minimum_total_margin TEXT NOT NULL,
                        maximum_quote_age_seconds INTEGER NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (cookie_id, item_id, card_id)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO supplier_delivery_bindings_v2 (
                        cookie_id, item_id, card_id, supplier_code, supplier_sku,
                        currency, unit_cost, quote_updated_at,
                        minimum_total_margin, maximum_quote_age_seconds, enabled,
                        created_at, updated_at
                    )
                    SELECT
                        cookie_id, item_id, card_id, supplier_code, supplier_sku,
                        currency, unit_cost, quote_updated_at,
                        minimum_total_margin, maximum_quote_age_seconds, enabled,
                        created_at, updated_at
                    FROM supplier_delivery_bindings
                    """
                )
                conn.execute("DROP TABLE supplier_delivery_bindings")
                conn.execute(
                    "ALTER TABLE supplier_delivery_bindings_v2 "
                    "RENAME TO supplier_delivery_bindings"
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        purchase_columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(supplier_delivery_purchases)"
            ).fetchall()
        }
        if "target_indices_json" not in purchase_columns:
            conn.execute(
                "ALTER TABLE supplier_delivery_purchases "
                "ADD COLUMN target_indices_json TEXT NOT NULL DEFAULT '[]'"
            )

    @staticmethod
    def _validated_binding(
        binding: SupplierDeliveryBinding,
    ) -> SupplierDeliveryBinding:
        cookie_id = str(binding.cookie_id or "").strip()
        item_id = str(binding.item_id or "").strip()
        supplier_code = str(binding.supplier_code or "").strip()
        supplier_sku = str(binding.supplier_sku or "").strip()
        currency = str(binding.currency or "").strip().upper()
        if not cookie_id or not item_id:
            raise ValueError("cookie_id and item_id are required")
        if int(binding.card_id) <= 0:
            raise ValueError("card_id must be greater than zero")
        if not supplier_code or not supplier_sku:
            raise ValueError("supplier_code and supplier_sku are required")
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency must be a three-letter code")
        unit_cost = _decimal(binding.unit_cost, "unit_cost")
        minimum_margin = _decimal(
            binding.minimum_total_margin,
            "minimum_total_margin",
        )
        if unit_cost < 0:
            raise ValueError("unit_cost cannot be negative")
        if minimum_margin < 0:
            raise ValueError("minimum_total_margin cannot be negative")
        quote_updated_at = _aware_datetime(
            binding.quote_updated_at,
            "quote_updated_at",
        )
        try:
            quote_age = int(binding.maximum_quote_age_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("maximum_quote_age_seconds must be an integer") from exc
        if quote_age < 1:
            raise ValueError("maximum_quote_age_seconds must be positive")
        return SupplierDeliveryBinding(
            cookie_id=cookie_id,
            item_id=item_id,
            card_id=int(binding.card_id),
            supplier_code=supplier_code,
            supplier_sku=supplier_sku,
            currency=currency,
            unit_cost=unit_cost,
            quote_updated_at=quote_updated_at,
            minimum_total_margin=minimum_margin,
            maximum_quote_age_seconds=quote_age,
            enabled=bool(binding.enabled),
        )

    def upsert_binding(self, binding: SupplierDeliveryBinding) -> Dict[str, Any]:
        binding = self._validated_binding(binding)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO supplier_delivery_bindings (
                    cookie_id, item_id, card_id, supplier_code, supplier_sku,
                    currency, unit_cost, quote_updated_at,
                    minimum_total_margin, maximum_quote_age_seconds, enabled,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(cookie_id, item_id, card_id) DO UPDATE SET
                    supplier_code=excluded.supplier_code,
                    supplier_sku=excluded.supplier_sku,
                    currency=excluded.currency,
                    unit_cost=excluded.unit_cost,
                    quote_updated_at=excluded.quote_updated_at,
                    minimum_total_margin=excluded.minimum_total_margin,
                    maximum_quote_age_seconds=excluded.maximum_quote_age_seconds,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    binding.cookie_id,
                    binding.item_id,
                    binding.card_id,
                    binding.supplier_code,
                    binding.supplier_sku,
                    binding.currency,
                    str(binding.unit_cost),
                    binding.quote_updated_at.isoformat(),
                    str(binding.minimum_total_margin),
                    binding.maximum_quote_age_seconds,
                    1 if binding.enabled else 0,
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        stored = self.get_binding(
            binding.cookie_id,
            binding.item_id,
            binding.card_id,
        )
        if stored is None:
            raise RuntimeError("supplier binding was not persisted")
        return stored

    def get_binding(
        self,
        cookie_id: str,
        item_id: str,
        card_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            if card_id is None:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM supplier_delivery_bindings
                    WHERE cookie_id=? AND item_id=?
                    ORDER BY card_id
                    """,
                    (str(cookie_id), str(item_id)),
                ).fetchall()
                if len(rows) != 1:
                    return None
                row = rows[0]
            else:
                row = conn.execute(
                    """
                    SELECT *
                    FROM supplier_delivery_bindings
                    WHERE cookie_id=? AND item_id=? AND card_id=?
                    """,
                    (str(cookie_id), str(item_id), int(card_id)),
                ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        binding = dict(row)
        binding["enabled"] = bool(binding["enabled"])
        return binding

    def delete_binding(
        self,
        cookie_id: str,
        item_id: str,
        card_id: Optional[int] = None,
    ) -> bool:
        conn = self._connect()
        try:
            if card_id is None:
                cursor = conn.execute(
                    """
                    DELETE FROM supplier_delivery_bindings
                    WHERE cookie_id=? AND item_id=?
                    """,
                    (str(cookie_id), str(item_id)),
                )
            else:
                cursor = conn.execute(
                    """
                    DELETE FROM supplier_delivery_bindings
                    WHERE cookie_id=? AND item_id=? AND card_id=?
                    """,
                    (str(cookie_id), str(item_id), int(card_id)),
                )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def begin_purchase(
        self,
        *,
        order_id: str,
        cookie_id: str,
        item_id: str,
        card_id: int,
        buyer_id: str,
        quantity: int,
        request_id: str,
        idempotency_key: str,
        supplier_code: str,
        supplier_sku: str,
        target_indices: Sequence[int],
    ) -> tuple[bool, Dict[str, Any]]:
        indices = tuple(int(index) for index in target_indices)
        if (
            len(indices) != int(quantity)
            or len(set(indices)) != len(indices)
            or any(index < 0 for index in indices)
        ):
            raise ValueError("target_indices do not match purchase quantity")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT *
                FROM supplier_delivery_purchases
                WHERE order_id=? AND cookie_id=?
                """,
                (str(order_id), str(cookie_id)),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return False, dict(existing)
            conn.execute(
                """
                INSERT INTO supplier_delivery_purchases (
                    order_id, cookie_id, item_id, card_id, buyer_id, quantity,
                    request_id, idempotency_key, supplier_code, supplier_sku,
                    target_indices_json, status, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'in_flight', ?,?)
                """,
                (
                    str(order_id),
                    str(cookie_id),
                    str(item_id),
                    int(card_id),
                    str(buyer_id or ""),
                    int(quantity),
                    request_id,
                    idempotency_key,
                    supplier_code,
                    supplier_sku,
                    json.dumps(
                        list(indices),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        row = self.get_purchase(order_id, cookie_id)
        if row is None:
            raise RuntimeError("supplier purchase intent was not persisted")
        return True, row

    def get_purchase(
        self,
        order_id: str,
        cookie_id: str,
    ) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT *
                FROM supplier_delivery_purchases
                WHERE order_id=? AND cookie_id=?
                """,
                (str(order_id), str(cookie_id)),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row is not None else None

    def conflicting_code_owner(
        self,
        order_id: str,
        cookie_id: str,
        codes: Sequence[str],
    ) -> Optional[Dict[str, str]]:
        wanted = set(str(code) for code in codes)
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT order_id, cookie_id, codes_json
                FROM supplier_delivery_purchases
                WHERE NOT (order_id=? AND cookie_id=?)
                  AND status IN ('purchased', 'codes_received')
                """,
                (str(order_id), str(cookie_id)),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            if wanted.intersection(_decode_codes(row["codes_json"])):
                return {
                    "order_id": str(row["order_id"]),
                    "cookie_id": str(row["cookie_id"]),
                }
        return None

    def update_purchase(
        self,
        order_id: str,
        cookie_id: str,
        *,
        status: str,
        message: str = "",
        supplier_order_id: str = "",
        total_cost: Optional[Decimal] = None,
        margin: Optional[Decimal] = None,
        codes: Optional[Sequence[str]] = None,
    ) -> None:
        fields = [
            "status=?",
            "message=?",
            "updated_at=?",
        ]
        values: list[Any] = [
            str(status),
            str(message or "")[:1000],
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ]
        if supplier_order_id:
            fields.append("supplier_order_id=?")
            values.append(str(supplier_order_id))
        if total_cost is not None:
            fields.append("total_cost=?")
            values.append(str(total_cost))
        if margin is not None:
            fields.append("margin=?")
            values.append(str(margin))
        if codes is not None:
            fields.append("codes_json=?")
            values.append(
                json.dumps(list(codes), ensure_ascii=False, separators=(",", ":"))
            )
        values.extend([str(order_id), str(cookie_id)])
        conn = self._connect()
        try:
            conn.execute(
                f"UPDATE supplier_delivery_purchases SET {', '.join(fields)} "
                "WHERE order_id=? AND cookie_id=?",
                tuple(values),
            )
            conn.commit()
        finally:
            conn.close()


class DeliveryLedgerBridge:
    """Checks code ownership and reserves purchased codes in the delivery ledger."""

    @staticmethod
    def stock_codes(db: Any, card_id: int) -> set[str]:
        card = db.get_card_by_id(int(card_id)) or {}
        return set(split_codes(str(card.get("data_content") or "")))

    @staticmethod
    def conflicting_owner(
        order_id: str,
        cookie_id: str,
        codes: Sequence[str],
    ) -> Optional[Dict[str, str]]:
        import delivery_reliability as dr

        conn = dr._conn()
        try:
            for code in codes:
                row = conn.execute(
                    """
                    SELECT order_id, cookie_id
                    FROM delivery_cards
                    WHERE payload=? AND NOT (order_id=? AND cookie_id=?)
                    LIMIT 1
                    """,
                    (str(code), str(order_id), str(cookie_id)),
                ).fetchone()
                if row is not None:
                    return {
                        "order_id": str(row[0]),
                        "cookie_id": str(row[1]),
                    }
        finally:
            conn.close()
        return None

    @staticmethod
    def reserve(
        *,
        order_id: str,
        cookie_id: str,
        item_id: str,
        chat_id: str,
        buyer_id: str,
        spec: str,
        codes: Sequence[str],
        target_indices: Sequence[int],
    ) -> None:
        if len(codes) != len(target_indices):
            raise CodeReservationError(
                "delivery code count does not match target index count"
            )
        indices = [int(index) for index in target_indices]
        if len(set(indices)) != len(indices) or any(index < 0 for index in indices):
            raise CodeReservationError("delivery target indices are invalid")
        if not indices:
            raise CodeReservationError("delivery target indices are empty")

        contents = [""] * (max(indices) + 1)
        expected: Dict[int, str] = {}
        for index, code in zip(indices, codes):
            contents[index] = code
            expected[index] = code

        import delivery_reliability as dr

        dr.ensure_order(
            order_id,
            cookie_id,
            contents,
            item_id=item_id,
            chat_id=chat_id,
            buyer_id=buyer_id,
            spec=spec,
        )
        stored = {
            int(row["idx"]): str(row.get("payload") or "")
            for row in dr.cards(order_id, cookie_id)
        }
        mismatched = [
            index
            for index, code in expected.items()
            if stored.get(index) != code
        ]
        if mismatched:
            raise CodeReservationError(
                "delivery ledger contains different content at target indices"
            )


_ADAPTER_LOCK = threading.RLock()
_ADAPTERS: Dict[str, BaseSupplierAdapter] = {}


def register_supplier_adapter(adapter: BaseSupplierAdapter) -> None:
    code = str(getattr(adapter, "supplier_code", "") or "").strip()
    if not code:
        raise ValueError("supplier adapter must expose supplier_code")
    with _ADAPTER_LOCK:
        _ADAPTERS[code] = adapter


def unregister_supplier_adapter(supplier_code: str) -> None:
    with _ADAPTER_LOCK:
        _ADAPTERS.pop(str(supplier_code or "").strip(), None)


def get_supplier_adapter(supplier_code: str) -> Optional[BaseSupplierAdapter]:
    with _ADAPTER_LOCK:
        return _ADAPTERS.get(str(supplier_code or "").strip())


def clear_supplier_adapters() -> None:
    with _ADAPTER_LOCK:
        _ADAPTERS.clear()


def _default_notifier(status: str, order_id: str, message: str) -> Dict[str, Any]:
    title = "⚠️ 供应商自动采购已停单"
    content = "\n".join(
        [
            f"订单：{order_id}",
            f"状态：{status}",
            f"原因：{message}",
            "处理：已停止本次自动发货，请到供应商后台核对后再人工放行或退款。",
        ]
    )
    try:
        import notify_service

        return notify_service.send_event("risk", title, content, level="warn")
    except Exception:
        return {"ok": False, "skipped": True, "results": {}}


class DeliverySupplierProcurement:
    RESULT_PERSISTENCE_ATTEMPTS = 3
    RECOVERY_EVIDENCE_VERSION = 1

    def __init__(
        self,
        *,
        store: Optional[SupplierBindingStore] = None,
        db: Any = None,
        adapter_resolver: Callable[[str], Optional[BaseSupplierAdapter]] = get_supplier_adapter,
        ledger: Optional[DeliveryLedgerBridge] = None,
        notifier: Callable[[str, str, str], Mapping[str, Any]] = _default_notifier,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.store = store or SupplierBindingStore()
        if db is None:
            from db_manager import db_manager

            db = db_manager
        self.db = db
        self.adapter_resolver = adapter_resolver
        self.ledger = ledger or DeliveryLedgerBridge()
        self.notifier = notifier
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.recovery_dir = self.store.path.with_name(
            f"{self.store.path.name}.recovery"
        )

    def _decision(
        self,
        *,
        ok: bool,
        status: ProcurementStatus,
        message: str,
        codes: Sequence[str] = (),
        request_id: str = "",
        idempotency_key: str = "",
        supplier_code: str = "",
        supplier_order_id: str = "",
        total_cost: Optional[Decimal] = None,
        margin: Optional[Decimal] = None,
        transport_called: bool = False,
        alert_sent: bool = False,
    ) -> ProcurementDecision:
        return ProcurementDecision(
            ok=ok,
            status=status,
            message=message,
            codes=tuple(codes),
            request_id=request_id,
            idempotency_key=idempotency_key,
            supplier_code=supplier_code,
            supplier_order_id=supplier_order_id,
            total_cost=total_cost,
            margin=margin,
            transport_called=transport_called,
            alert_sent=alert_sent,
        )

    def _notify(
        self,
        *,
        status: ProcurementStatus,
        order_id: str,
        message: str,
    ) -> bool:
        try:
            result = self.notifier(status.value, order_id, message)
        except Exception:
            return False
        return bool(result.get("skipped") is False or result.get("ok") is True)

    def _recovery_path(self, order_id: str, cookie_id: str) -> Path:
        identity = f"{cookie_id}\0{order_id}".encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()
        return self.recovery_dir / f"{digest}.json"

    @staticmethod
    def _load_recovery_evidence(path: Path) -> Dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("recovery evidence must be an object")
        return payload

    def _write_recovery_evidence(self, evidence: Dict[str, Any]) -> Path:
        path = self._recovery_path(
            str(evidence["order_id"]),
            str(evidence["cookie_id"]),
        )
        if path.exists():
            existing = self._load_recovery_evidence(path)
            if existing != evidence:
                raise RuntimeError(
                    "existing recovery evidence does not match this purchase"
                )
            return path

        self.recovery_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.recovery_dir, 0o700)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor: Optional[int] = None
        try:
            descriptor = os.open(str(temporary), flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = None
                json.dump(
                    evidence,
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                directory_fd = os.open(
                    str(self.recovery_dir),
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return path

    @staticmethod
    def _discard_recovery_evidence(path: Optional[Path]) -> None:
        if path is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # A stale receipt is idempotent and identity-scoped. It is safer to
            # retain it than to stop after the database has accepted the result.
            pass

    @staticmethod
    def _validated_recovery_evidence(
        payload: Dict[str, Any],
        *,
        order_id: str,
        cookie_id: str,
        item_id: str,
        card_id: int,
        buyer_id: str,
        chat_id: str,
        spec: str,
        quantity: int,
        target_indices: Sequence[int],
    ) -> Dict[str, Any]:
        if payload.get("version") != DeliverySupplierProcurement.RECOVERY_EVIDENCE_VERSION:
            raise ValueError("unsupported recovery evidence version")

        expected_identity = {
            "order_id": order_id,
            "cookie_id": cookie_id,
            "item_id": item_id,
            "card_id": card_id,
            "buyer_id": buyer_id,
            "chat_id": chat_id,
            "spec": spec,
            "quantity": quantity,
            "target_indices": list(target_indices),
        }
        for field_name, expected in expected_identity.items():
            if payload.get(field_name) != expected:
                raise ValueError(
                    f"recovery evidence {field_name} does not match this purchase"
                )

        codes = payload.get("codes")
        if (
            not isinstance(codes, list)
            or len(codes) != quantity
            or any(not isinstance(code, str) or not code.strip() for code in codes)
            or len(set(codes)) != len(codes)
        ):
            raise ValueError("recovery evidence delivery codes are invalid")

        for field_name in (
            "request_id",
            "idempotency_key",
            "supplier_code",
            "supplier_sku",
            "supplier_order_id",
        ):
            if not isinstance(payload.get(field_name), str):
                raise ValueError(
                    f"recovery evidence {field_name} is invalid"
                )

        for field_name in ("total_cost", "margin"):
            value = payload.get(field_name)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(
                        f"recovery evidence {field_name} is invalid"
                    )
                _decimal(value, field_name)
        return payload

    def _recover_success_evidence(
        self,
        *,
        order_id: str,
        cookie_id: str,
        item_id: str,
        card_id: int,
        buyer_id: str,
        chat_id: str,
        spec: str,
        quantity: int,
        target_indices: Sequence[int],
        code_transform: Optional[Callable[[str], str]],
    ) -> Optional[ProcurementDecision]:
        path = self._recovery_path(order_id, cookie_id)
        if not path.exists():
            return None
        request_id = ""
        idempotency_key = ""
        supplier_code = ""
        supplier_order_id = ""

        try:
            payload = self._validated_recovery_evidence(
                self._load_recovery_evidence(path),
                order_id=order_id,
                cookie_id=cookie_id,
                item_id=item_id,
                card_id=card_id,
                buyer_id=buyer_id,
                chat_id=chat_id,
                spec=spec,
                quantity=quantity,
                target_indices=target_indices,
            )
            request_id = str(payload["request_id"])
            idempotency_key = str(payload["idempotency_key"])
            supplier_code = str(payload["supplier_code"])
            supplier_order_id = str(payload["supplier_order_id"])
            stored = self._persist_codes_received(
                order_id=order_id,
                cookie_id=cookie_id,
                supplier_order_id=supplier_order_id,
                total_cost=(
                    _decimal(payload["total_cost"], "total_cost")
                    if payload.get("total_cost") is not None
                    else None
                ),
                margin=(
                    _decimal(payload["margin"], "margin")
                    if payload.get("margin") is not None
                    else None
                ),
                codes=tuple(payload["codes"]),
            )
        except Exception as exc:
            message = (
                "检测到供应商已返回卡密的恢复证据，但当前无法安全恢复；"
                "本次不会再次调用供应商，"
                f"错误：{type(exc).__name__}: {str(exc)[:300]}"
            )
            alert_sent = self._notify(
                status=ProcurementStatus.PERSISTENCE_FAILED,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=ProcurementStatus.PERSISTENCE_FAILED,
                message=message,
                request_id=request_id,
                idempotency_key=idempotency_key,
                supplier_code=supplier_code,
                supplier_order_id=supplier_order_id,
                alert_sent=alert_sent,
            )

        self._discard_recovery_evidence(path)

        return self._resume_codes_received(
            purchase=stored,
            order_id=order_id,
            cookie_id=cookie_id,
            item_id=item_id,
            card_id=card_id,
            buyer_id=buyer_id,
            chat_id=chat_id,
            spec=spec,
            quantity=quantity,
            target_indices=target_indices,
            code_transform=code_transform,
        )

    def _persist_codes_received(
        self,
        *,
        order_id: str,
        cookie_id: str,
        supplier_order_id: str,
        total_cost: Optional[Decimal],
        margin: Optional[Decimal],
        codes: Sequence[str],
    ) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(self.RESULT_PERSISTENCE_ATTEMPTS):
            try:
                self.store.update_purchase(
                    order_id,
                    cookie_id,
                    status="codes_received",
                    message="供应商已返回有效卡密，正在登记发货账本",
                    supplier_order_id=supplier_order_id,
                    total_cost=total_cost,
                    margin=margin,
                    codes=codes,
                )
                stored = self.store.get_purchase(order_id, cookie_id)
                if (
                    stored
                    and str(stored.get("status") or "") == "codes_received"
                    and _decode_codes(stored.get("codes_json")) == tuple(codes)
                ):
                    return stored
                raise RuntimeError("采购结果写入后回读不一致")
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.RESULT_PERSISTENCE_ATTEMPTS:
                    time.sleep(0.05 * (2 ** attempt))
        raise RuntimeError(
            "供应商采购结果连续持久化失败"
        ) from last_error

    def _validate_codes(
        self,
        *,
        order_id: str,
        cookie_id: str,
        card_id: int,
        codes: Sequence[str],
        quantity: int,
    ) -> tuple[bool, str]:
        if len(codes) != quantity:
            return False, "供应商返回的卡密数量与订单数量不一致"
        if not all(isinstance(code, str) and code.strip() for code in codes):
            return False, "供应商返回了空卡密"
        cleaned = [code.strip() for code in codes]
        if len(set(cleaned)) != len(cleaned):
            return False, "供应商返回的卡密存在重复值"
        stock_conflicts = set(cleaned) & self.ledger.stock_codes(
            self.db,
            card_id,
        )
        if stock_conflicts:
            return False, "供应商卡密与本地待售库存重复"
        owner = self.ledger.conflicting_owner(order_id, cookie_id, cleaned)
        if owner is not None:
            return (
                False,
                f"供应商卡密已登记在其他订单 "
                f"{owner.get('order_id') or '未知订单'}",
            )
        purchase_owner = self.store.conflicting_code_owner(
            order_id,
            cookie_id,
            cleaned,
        )
        if purchase_owner is not None:
            return (
                False,
                f"供应商卡密已由其他采购单取得 "
                f"{purchase_owner.get('order_id') or '未知订单'}",
            )
        return True, ""

    def _resume_codes_received(
        self,
        *,
        purchase: Dict[str, Any],
        order_id: str,
        cookie_id: str,
        item_id: str,
        card_id: int,
        buyer_id: str,
        chat_id: str,
        spec: str,
        quantity: int,
        target_indices: Sequence[int],
        code_transform: Optional[Callable[[str], str]],
    ) -> ProcurementDecision:
        codes = _decode_codes(purchase.get("codes_json"))
        valid, reason = self._validate_codes(
            order_id=order_id,
            cookie_id=cookie_id,
            card_id=card_id,
            codes=codes,
            quantity=quantity,
        )
        if not valid:
            message = f"{reason}，已停止自动发货"
            self.store.update_purchase(
                order_id,
                cookie_id,
                status=ProcurementStatus.CODES_INVALID.value,
                message=message,
            )
            alert_sent = self._notify(
                status=ProcurementStatus.CODES_INVALID,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=ProcurementStatus.CODES_INVALID,
                message=message,
                request_id=str(purchase.get("request_id") or ""),
                idempotency_key=str(purchase.get("idempotency_key") or ""),
                supplier_code=str(purchase.get("supplier_code") or ""),
                supplier_order_id=str(purchase.get("supplier_order_id") or ""),
                alert_sent=alert_sent,
            )
        try:
            reserved_codes = (
                tuple(str(code_transform(code)) for code in codes)
                if code_transform is not None
                else codes
            )
        except Exception as exc:
            message = (
                "供应商卡密已取得，但发货文案处理失败："
                f"{type(exc).__name__}: {str(exc)[:300]}"
            )
            self.store.update_purchase(
                order_id,
                cookie_id,
                status="codes_received",
                message=message,
            )
            alert_sent = self._notify(
                status=ProcurementStatus.RESERVATION_FAILED,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=ProcurementStatus.RESERVATION_FAILED,
                message=message,
                request_id=str(purchase.get("request_id") or ""),
                idempotency_key=str(purchase.get("idempotency_key") or ""),
                supplier_code=str(purchase.get("supplier_code") or ""),
                supplier_order_id=str(purchase.get("supplier_order_id") or ""),
                alert_sent=alert_sent,
            )
        if (
            len(reserved_codes) != quantity
            or any(not code.strip() for code in reserved_codes)
            or len(set(reserved_codes)) != len(reserved_codes)
        ):
            message = "供应商卡密处理后的发货内容无效，已停止自动发货"
            self.store.update_purchase(
                order_id,
                cookie_id,
                status=ProcurementStatus.CODES_INVALID.value,
                message=message,
            )
            alert_sent = self._notify(
                status=ProcurementStatus.CODES_INVALID,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=ProcurementStatus.CODES_INVALID,
                message=message,
                request_id=str(purchase.get("request_id") or ""),
                idempotency_key=str(purchase.get("idempotency_key") or ""),
                supplier_code=str(purchase.get("supplier_code") or ""),
                supplier_order_id=str(purchase.get("supplier_order_id") or ""),
                alert_sent=alert_sent,
            )
        try:
            self.ledger.reserve(
                order_id=order_id,
                cookie_id=cookie_id,
                item_id=item_id,
                chat_id=chat_id,
                buyer_id=buyer_id,
                spec=spec,
                codes=reserved_codes,
                target_indices=target_indices,
            )
        except Exception as exc:
            message = (
                "供应商卡密已取得，但写入发货账本失败："
                f"{type(exc).__name__}: {str(exc)[:300]}"
            )
            self.store.update_purchase(
                order_id,
                cookie_id,
                status="codes_received",
                message=message,
            )
            alert_sent = self._notify(
                status=ProcurementStatus.RESERVATION_FAILED,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=ProcurementStatus.RESERVATION_FAILED,
                message=message,
                request_id=str(purchase.get("request_id") or ""),
                idempotency_key=str(purchase.get("idempotency_key") or ""),
                supplier_code=str(purchase.get("supplier_code") or ""),
                supplier_order_id=str(purchase.get("supplier_order_id") or ""),
                alert_sent=alert_sent,
            )
        self.store.update_purchase(
            order_id,
            cookie_id,
            status=ProcurementStatus.PURCHASED.value,
            message="供应商采购成功，卡密已登记到发货账本",
            supplier_order_id=str(purchase.get("supplier_order_id") or ""),
            codes=codes,
        )
        return self._decision(
            ok=True,
            status=ProcurementStatus.PURCHASED,
            message="供应商采购成功，卡密已登记到发货账本",
            codes=codes,
            request_id=str(purchase.get("request_id") or ""),
            idempotency_key=str(purchase.get("idempotency_key") or ""),
            supplier_code=str(purchase.get("supplier_code") or ""),
            supplier_order_id=str(purchase.get("supplier_order_id") or ""),
            total_cost=(
                _decimal(purchase.get("total_cost"), "total_cost")
                if purchase.get("total_cost")
                else None
            ),
            margin=(
                _decimal(purchase.get("margin"), "margin")
                if purchase.get("margin")
                else None
            ),
        )

    def procure(
        self,
        *,
        cookie_id: str,
        item_id: str,
        card_id: int,
        order_id: str,
        buyer_id: str,
        quantity: int,
        chat_id: str = "",
        spec: str = "",
        target_indices: Optional[Sequence[int]] = None,
        code_transform: Optional[Callable[[str], str]] = None,
    ) -> ProcurementDecision:
        cookie_id = str(cookie_id or "").strip()
        item_id = str(item_id or "").strip()
        order_id = str(order_id or "").strip()
        buyer_id = str(buyer_id or "").strip()
        chat_id = str(chat_id or "")
        spec = str(spec or "")
        try:
            quantity = int(quantity)
            card_id = int(card_id)
        except (TypeError, ValueError):
            return self._decision(
                ok=False,
                status=ProcurementStatus.ORDER_INVALID,
                message="订单数量或卡券分组无效，未调用供应商",
            )
        if quantity < 1 or card_id < 1 or not cookie_id or not item_id or not order_id:
            return self._decision(
                ok=False,
                status=ProcurementStatus.ORDER_INVALID,
                message="账号、商品、订单、数量或卡券分组不完整，未调用供应商",
            )
        raw_indices = list(target_indices or range(quantity))
        try:
            indices = [int(index) for index in raw_indices]
        except (TypeError, ValueError):
            return self._decision(
                ok=False,
                status=ProcurementStatus.ORDER_INVALID,
                message="待采购发货位置格式无效，未调用供应商",
            )
        if (
            len(indices) != quantity
            or len(set(indices)) != len(indices)
            or any(index < 0 for index in indices)
        ):
            return self._decision(
                ok=False,
                status=ProcurementStatus.ORDER_INVALID,
                message="待采购数量与发货位置数量不一致，未调用供应商",
            )

        recovered = self._recover_success_evidence(
            order_id=order_id,
            cookie_id=cookie_id,
            item_id=item_id,
            card_id=card_id,
            buyer_id=buyer_id,
            chat_id=chat_id,
            spec=spec,
            quantity=quantity,
            target_indices=indices,
            code_transform=code_transform,
        )
        if recovered is not None:
            return recovered

        binding = self.store.get_binding(cookie_id, item_id, card_id)
        if binding is None:
            return self._decision(
                ok=False,
                status=ProcurementStatus.NOT_CONFIGURED,
                message="该商品没有启用的供应商 SKU 绑定",
            )
        if not binding.get("enabled"):
            return self._decision(
                ok=False,
                status=ProcurementStatus.NOT_CONFIGURED,
                message="该商品的供应商 SKU 绑定已停用",
            )

        order = self.db.get_order_by_id(order_id)
        if not order:
            return self._decision(
                ok=False,
                status=ProcurementStatus.ORDER_INVALID,
                message="订单不存在，未调用供应商",
            )
        order_account = str(order.get("cookie_id") or "")
        order_item = str(order.get("item_id") or "")
        order_buyer = str(order.get("buyer_id") or "")
        if (
            (order_account and order_account != cookie_id)
            or (order_item and order_item != item_id)
            or (buyer_id and order_buyer and order_buyer != buyer_id)
        ):
            return self._decision(
                ok=False,
                status=ProcurementStatus.ORDER_INVALID,
                message="订单归属与当前账号、商品或买家不一致，未调用供应商",
            )
        try:
            buyer_paid = _parse_order_amount(order.get("amount"))
        except ValueError:
            message = "订单实付金额缺失或格式无效，禁止自动采购"
            alert_sent = self._notify(
                status=ProcurementStatus.ORDER_INVALID,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=ProcurementStatus.ORDER_INVALID,
                message=message,
                alert_sent=alert_sent,
            )

        existing = self.store.get_purchase(order_id, cookie_id)
        if existing is not None:
            existing_status = str(existing.get("status") or "")
            existing_indices = _decode_target_indices(
                existing.get("target_indices_json"),
                quantity,
            )
            if (
                str(existing.get("item_id") or "") != item_id
                or int(existing.get("card_id") or 0) != card_id
                or int(existing.get("quantity") or 0) != quantity
                or (
                    str(existing.get("buyer_id") or "")
                    and str(existing.get("buyer_id") or "") != buyer_id
                )
                or existing_indices != tuple(indices)
            ):
                message = (
                    "该订单已有供应商采购记录，但本次商品、卡密组、数量、"
                    "买家或发货位置与历史记录不一致；已冻结并转人工核对"
                )
                self.store.update_purchase(
                    order_id,
                    cookie_id,
                    status=ProcurementStatus.SUPPLIER_BLOCKED.value,
                    message=message,
                )
                alert_sent = self._notify(
                    status=ProcurementStatus.SUPPLIER_BLOCKED,
                    order_id=order_id,
                    message=message,
                )
                return self._decision(
                    ok=False,
                    status=ProcurementStatus.SUPPLIER_BLOCKED,
                    message=message,
                    request_id=str(existing.get("request_id") or ""),
                    idempotency_key=str(existing.get("idempotency_key") or ""),
                    supplier_code=str(existing.get("supplier_code") or ""),
                    supplier_order_id=str(existing.get("supplier_order_id") or ""),
                    alert_sent=alert_sent,
                )
            if existing_status in {
                ProcurementStatus.PURCHASED.value,
                "codes_received",
            }:
                return self._resume_codes_received(
                    purchase=existing,
                    order_id=order_id,
                    cookie_id=cookie_id,
                    item_id=item_id,
                    card_id=card_id,
                    buyer_id=buyer_id,
                    chat_id=chat_id,
                    spec=spec,
                    quantity=quantity,
                    target_indices=existing_indices,
                    code_transform=code_transform,
                )
            if existing_status == "in_flight":
                message = (
                    "该订单已向供应商发出采购请求，但本地没有记录到明确结果。"
                    "为避免重复扣款，本次不会再次调用供应商。"
                )
                self.store.update_purchase(
                    order_id,
                    cookie_id,
                    status=ProcurementStatus.SUBMISSION_UNKNOWN.value,
                    message=message,
                )
                status = ProcurementStatus.SUBMISSION_UNKNOWN
            elif existing_status == ProcurementStatus.SUBMISSION_UNKNOWN.value:
                status = ProcurementStatus.SUBMISSION_UNKNOWN
                message = str(
                    existing.get("message")
                    or "供应商采购结果未知，禁止自动重复采购"
                )
            else:
                status = ProcurementStatus.SUPPLIER_BLOCKED
                message = str(existing.get("message") or "该订单已有供应商采购冻结记录")
            alert_sent = self._notify(
                status=status,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=status,
                message=message,
                request_id=str(existing.get("request_id") or ""),
                idempotency_key=str(existing.get("idempotency_key") or ""),
                supplier_code=str(existing.get("supplier_code") or ""),
                supplier_order_id=str(existing.get("supplier_order_id") or ""),
                alert_sent=alert_sent,
            )

        adapter = self.adapter_resolver(str(binding.get("supplier_code") or ""))
        if adapter is None:
            message = (
                f"供应商 {binding.get('supplier_code')} 尚未注册已验签适配器，"
                "本次未发起采购"
            )
            alert_sent = self._notify(
                status=ProcurementStatus.ADAPTER_UNAVAILABLE,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=ProcurementStatus.ADAPTER_UNAVAILABLE,
                message=message,
                supplier_code=str(binding.get("supplier_code") or ""),
                alert_sent=alert_sent,
            )

        request_id = f"delivery-{uuid.uuid4().hex}"
        idempotency_key = f"delivery-supplier:{cookie_id}:{order_id}"
        try:
            unit_cost = _decimal(binding.get("unit_cost"), "unit_cost")
            quoted_at = datetime.fromisoformat(
                str(binding.get("quote_updated_at") or "")
            )
            minimum_margin = _decimal(
                binding.get("minimum_total_margin"),
                "minimum_total_margin",
            )
            request = PurchaseRequest(
                request_id=request_id,
                idempotency_key=idempotency_key,
                supplier_sku=str(binding.get("supplier_sku") or ""),
                quantity=quantity,
                buyer_paid=buyer_paid,
                currency=str(binding.get("currency") or ""),
                costs=(
                    CostQuote(
                        key="supplier_total",
                        total_amount=unit_cost * quantity,
                        currency=str(binding.get("currency") or ""),
                        quoted_at=quoted_at,
                    ),
                ),
                required_cost_keys=("supplier_total",),
                minimum_margin=minimum_margin,
                maximum_quote_age=timedelta(
                    seconds=int(binding.get("maximum_quote_age_seconds") or 0)
                ),
            )
        except (ValueError, TypeError):
            message = "供应商绑定中的报价、币种或最低毛利配置无效"
            alert_sent = self._notify(
                status=ProcurementStatus.BINDING_INVALID,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=ProcurementStatus.BINDING_INVALID,
                message=message,
                supplier_code=str(binding.get("supplier_code") or ""),
                alert_sent=alert_sent,
            )

        created, purchase = self.store.begin_purchase(
            order_id=order_id,
            cookie_id=cookie_id,
            item_id=item_id,
            card_id=card_id,
            buyer_id=buyer_id,
            quantity=quantity,
            request_id=request_id,
            idempotency_key=idempotency_key,
            supplier_code=str(binding.get("supplier_code") or ""),
            supplier_sku=str(binding.get("supplier_sku") or ""),
            target_indices=indices,
        )
        if not created:
            message = "并发请求已由另一个发货任务占用，本次未再次调用供应商"
            alert_sent = self._notify(
                status=ProcurementStatus.IN_FLIGHT,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=ProcurementStatus.IN_FLIGHT,
                message=message,
                request_id=str(purchase.get("request_id") or ""),
                idempotency_key=str(purchase.get("idempotency_key") or ""),
                supplier_code=str(purchase.get("supplier_code") or ""),
                alert_sent=alert_sent,
            )

        try:
            result = adapter.purchase(request)
        except Exception as exc:
            message = (
                "供应商采购调用异常，网络提交结果未知；"
                f"已冻结订单等待人工核对：{type(exc).__name__}: {str(exc)[:300]}"
            )
            self.store.update_purchase(
                order_id,
                cookie_id,
                status=ProcurementStatus.SUBMISSION_UNKNOWN.value,
                message=message,
            )
            alert_sent = self._notify(
                status=ProcurementStatus.SUBMISSION_UNKNOWN,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=ProcurementStatus.SUBMISSION_UNKNOWN,
                message=message,
                request_id=request_id,
                idempotency_key=idempotency_key,
                supplier_code=str(binding.get("supplier_code") or ""),
                transport_called=True,
                alert_sent=alert_sent,
            )

        if not isinstance(result, PurchaseResult):
            message = "供应商适配器返回了无法识别的结果，订单已冻结"
            self.store.update_purchase(
                order_id,
                cookie_id,
                status=ProcurementStatus.SUBMISSION_UNKNOWN.value,
                message=message,
            )
            alert_sent = self._notify(
                status=ProcurementStatus.SUBMISSION_UNKNOWN,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=ProcurementStatus.SUBMISSION_UNKNOWN,
                message=message,
                request_id=request_id,
                idempotency_key=idempotency_key,
                supplier_code=str(binding.get("supplier_code") or ""),
                transport_called=True,
                alert_sent=alert_sent,
            )

        succeeded = result.status is PurchaseStatus.SUCCEEDED or (
            result.status is PurchaseStatus.DUPLICATE
            and result.original_status is PurchaseStatus.SUCCEEDED
        )
        if result.status is PurchaseStatus.SUBMISSION_UNKNOWN:
            status = ProcurementStatus.SUBMISSION_UNKNOWN
        elif result.status is PurchaseStatus.MARGIN_BELOW_MINIMUM:
            status = ProcurementStatus.MARGIN_BELOW_MINIMUM
        elif not succeeded:
            status = ProcurementStatus.SUPPLIER_BLOCKED
        else:
            status = ProcurementStatus.PURCHASED

        if not succeeded:
            message = (
                f"供应商采购未成功（{result.status.value}）："
                f"{result.message or '未提供原因'}；已停止自动发货"
            )
            self.store.update_purchase(
                order_id,
                cookie_id,
                status=status.value,
                message=message,
                supplier_order_id=result.supplier_order_id or "",
                total_cost=result.total_cost,
                margin=result.margin,
            )
            alert_sent = self._notify(
                status=status,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=status,
                message=message,
                request_id=result.request_id,
                idempotency_key=result.idempotency_key,
                supplier_code=result.supplier_code,
                supplier_order_id=result.supplier_order_id or "",
                total_cost=result.total_cost,
                margin=result.margin,
                transport_called=result.transport_called,
                alert_sent=alert_sent,
            )

        codes = tuple(result.delivery_codes)
        evidence_path: Optional[Path] = None
        evidence_error: Optional[Exception] = None
        try:
            evidence_path = self._write_recovery_evidence({
                "version": self.RECOVERY_EVIDENCE_VERSION,
                "order_id": order_id,
                "cookie_id": cookie_id,
                "item_id": item_id,
                "card_id": card_id,
                "buyer_id": buyer_id,
                "chat_id": chat_id,
                "spec": spec,
                "quantity": quantity,
                "target_indices": list(indices),
                "request_id": result.request_id,
                "idempotency_key": result.idempotency_key,
                "supplier_code": result.supplier_code,
                "supplier_sku": str(binding.get("supplier_sku") or ""),
                "supplier_order_id": result.supplier_order_id or "",
                "total_cost": (
                    str(result.total_cost)
                    if result.total_cost is not None
                    else None
                ),
                "margin": (
                    str(result.margin)
                    if result.margin is not None
                    else None
                ),
                "codes": list(codes),
                "created_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            })
        except Exception as exc:
            evidence_error = exc

        try:
            valid, reason = self._validate_codes(
                order_id=order_id,
                cookie_id=cookie_id,
                card_id=card_id,
                codes=codes,
                quantity=quantity,
            )
        except Exception as exc:
            message = (
                "供应商已返回卡密，但当前无法完成卡密冲突校验；"
                "已停止自动发货，本次不会再次调用供应商，"
                f"错误：{type(exc).__name__}: {str(exc)[:300]}"
            )
            alert_sent = self._notify(
                status=ProcurementStatus.PERSISTENCE_FAILED,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=ProcurementStatus.PERSISTENCE_FAILED,
                message=message,
                request_id=result.request_id,
                idempotency_key=result.idempotency_key,
                supplier_code=result.supplier_code,
                supplier_order_id=result.supplier_order_id or "",
                total_cost=result.total_cost,
                margin=result.margin,
                transport_called=result.transport_called,
                alert_sent=alert_sent,
            )

        if not valid:
            message = f"{reason}；采购结果已留档，订单停止自动发货"
            status = (
                ProcurementStatus.CODES_CONFLICT
                if (
                    ("重复" in reason and "库存" in reason)
                    or "其他订单" in reason
                    or "其他采购单" in reason
                )
                else ProcurementStatus.CODES_INVALID
            )
            try:
                self.store.update_purchase(
                    order_id,
                    cookie_id,
                    status=status.value,
                    message=message,
                    supplier_order_id=result.supplier_order_id or "",
                    total_cost=result.total_cost,
                    margin=result.margin,
                    codes=codes,
                )
            except Exception as exc:
                message = (
                    "供应商返回的卡密未通过校验，且采购冻结状态无法落库；"
                    "恢复证据已保留，本次不会再次调用供应商，"
                    f"错误：{type(exc).__name__}: {str(exc)[:300]}"
                )
                status = ProcurementStatus.PERSISTENCE_FAILED
            else:
                self._discard_recovery_evidence(evidence_path)
            alert_sent = self._notify(
                status=status,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=status,
                message=message,
                request_id=result.request_id,
                idempotency_key=result.idempotency_key,
                supplier_code=result.supplier_code,
                supplier_order_id=result.supplier_order_id or "",
                total_cost=result.total_cost,
                margin=result.margin,
                transport_called=result.transport_called,
                alert_sent=alert_sent,
            )

        try:
            stored = self._persist_codes_received(
                order_id=order_id,
                cookie_id=cookie_id,
                supplier_order_id=result.supplier_order_id or "",
                total_cost=result.total_cost,
                margin=result.margin,
                codes=codes,
            )
        except Exception as exc:
            evidence_note = (
                "已保留本地恢复证据，重启后可继续恢复；"
                if evidence_path is not None
                else (
                    "本地恢复证据也未能写入；"
                    if evidence_error is not None
                    else ""
                )
            )
            message = (
                "供应商已成功返回卡密，但采购结果连续持久化失败；"
                f"{evidence_note}已停止自动发货并转人工核对，"
                f"错误：{type(exc).__name__}: {str(exc)[:300]}"
            )
            alert_sent = self._notify(
                status=ProcurementStatus.PERSISTENCE_FAILED,
                order_id=order_id,
                message=message,
            )
            return self._decision(
                ok=False,
                status=ProcurementStatus.PERSISTENCE_FAILED,
                message=message,
                request_id=result.request_id,
                idempotency_key=result.idempotency_key,
                supplier_code=result.supplier_code,
                supplier_order_id=result.supplier_order_id or "",
                total_cost=result.total_cost,
                margin=result.margin,
                transport_called=result.transport_called,
                alert_sent=alert_sent,
            )
        self._discard_recovery_evidence(evidence_path)
        return self._resume_codes_received(
            purchase=stored,
            order_id=order_id,
            cookie_id=cookie_id,
            item_id=item_id,
            card_id=card_id,
            buyer_id=buyer_id,
            chat_id=chat_id,
            spec=spec,
            quantity=quantity,
            target_indices=indices,
            code_transform=code_transform,
        )


__all__ = [
    "CodeReservationError",
    "DeliveryLedgerBridge",
    "DeliverySupplierProcurement",
    "ProcurementDecision",
    "ProcurementStatus",
    "SupplierBindingStore",
    "SupplierDeliveryBinding",
    "clear_supplier_adapters",
    "get_supplier_adapter",
    "register_supplier_adapter",
    "unregister_supplier_adapter",
]
