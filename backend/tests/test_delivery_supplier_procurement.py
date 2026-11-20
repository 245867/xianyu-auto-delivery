from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from delivery_supplier_procurement import (  # noqa: E402
    DeliverySupplierProcurement,
    ProcurementStatus,
    SupplierBindingStore,
    SupplierDeliveryBinding,
)
from supplier_adapters import (  # noqa: E402
    BaiZhouSupplierAdapter,
    ContractStatus,
    PurchaseResult,
    PurchaseStatus,
    RequestFieldMapping,
    RequestFieldSource,
    RequestMapping,
    ResponseMapping,
    SupplierContract,
    SupplierHttpResponse,
)


NOW = datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)


class FakeDatabase:
    def __init__(self, amount: str = "20.00"):
        self.order = {
            "order_id": "order-1",
            "cookie_id": "shop-a",
            "item_id": "item-1",
            "buyer_id": "buyer-1",
            "amount": amount,
        }
        self.card = {
            "id": 11,
            "type": "data",
            "data_content": "",
        }

    def get_order_by_id(self, order_id):
        return dict(self.order) if order_id == self.order["order_id"] else None

    def get_card_by_id(self, card_id, user_id=None):
        return dict(self.card) if int(card_id) == self.card["id"] else None


class FakeLedger:
    def __init__(self):
        self.stock: set[str] = set()
        self.conflict = None
        self.reserve_calls = []
        self.reserve_error = None

    def stock_codes(self, _db, _card_id):
        return set(self.stock)

    def conflicting_owner(self, _order_id, _cookie_id, _codes):
        return self.conflict

    def reserve(self, **kwargs):
        if self.reserve_error is not None:
            raise self.reserve_error
        self.reserve_calls.append(kwargs)


class RecordingTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def supplier_response(
    *,
    codes=None,
    charged: str = "10.00",
    status: str = "ok",
) -> SupplierHttpResponse:
    body = {
        "result": {
            "status": status,
            "order_id": "supplier-order-1",
            "message": "accepted",
            "charged": charged,
            "codes": codes if codes is not None else ["code-a", "code-b"],
        }
    }
    return SupplierHttpResponse(
        status_code=200,
        body=json.dumps(body).encode("utf-8"),
    )


def verified_contract() -> SupplierContract:
    return SupplierContract(
        supplier_code="baizhou",
        endpoint_url="https://supplier.example/purchase",
        http_method="POST",
        request_mapping=RequestMapping(
            fields=(
                RequestFieldMapping(
                    source=RequestFieldSource.SUPPLIER_SKU,
                    target_path=("sku",),
                ),
                RequestFieldMapping(
                    source=RequestFieldSource.QUANTITY,
                    target_path=("quantity",),
                ),
                RequestFieldMapping(
                    source=RequestFieldSource.IDEMPOTENCY_KEY,
                    target_path=("idempotency_key",),
                ),
            )
        ),
        response_mapping=ResponseMapping(
            success_http_statuses=(200,),
            status_path=("result", "status"),
            success_status_values=("ok",),
            order_id_path=("result", "order_id"),
            message_path=("result", "message"),
            purchase_cost_path=("result", "charged"),
            delivery_codes_path=("result", "codes"),
        ),
        verified=True,
        verification_reference="fixture-contract-v1",
    )


def binding(store: SupplierBindingStore, **overrides):
    values = {
        "cookie_id": "shop-a",
        "item_id": "item-1",
        "card_id": 11,
        "supplier_code": "baizhou",
        "supplier_sku": "sku-1",
        "currency": "CNY",
        "unit_cost": Decimal("5.00"),
        "quote_updated_at": NOW,
        "minimum_total_margin": Decimal("9.00"),
        "maximum_quote_age_seconds": 300,
        "enabled": True,
    }
    values.update(overrides)
    return store.upsert_binding(SupplierDeliveryBinding(**values))


class DeliverySupplierProcurementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = SupplierBindingStore(Path(temporary.name) / "supplier.db")
        self.db = FakeDatabase()
        self.ledger = FakeLedger()
        self.alerts = []

    def service(self, adapter):
        return DeliverySupplierProcurement(
            store=self.store,
            db=self.db,
            adapter_resolver=lambda _code: adapter,
            ledger=self.ledger,
            notifier=lambda status, order_id, message: (
                self.alerts.append((status, order_id, message))
                or {"ok": True}
            ),
            clock=lambda: NOW,
        )

    def procure(self, service, **overrides):
        values = {
            "cookie_id": "shop-a",
            "item_id": "item-1",
            "card_id": 11,
            "order_id": "order-1",
            "buyer_id": "buyer-1",
            "quantity": 2,
            "chat_id": "chat-1",
            "spec": "月卡",
            "target_indices": (0, 1),
        }
        values.update(overrides)
        return service.procure(**values)

    def test_successful_purchase_reserves_codes_and_is_not_repeated(self):
        binding(self.store)
        transport = RecordingTransport(response=supplier_response())
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )
        service = self.service(adapter)

        first = self.procure(service)
        second = self.procure(service)

        self.assertTrue(first.ok)
        self.assertEqual(first.status, ProcurementStatus.PURCHASED)
        self.assertEqual(first.codes, ("code-a", "code-b"))
        self.assertEqual(first.total_cost, Decimal("10.00"))
        self.assertEqual(first.margin, Decimal("10.00"))
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(len(self.ledger.reserve_calls), 2)
        self.assertEqual(
            self.store.get_purchase("order-1", "shop-a")["status"],
            "purchased",
        )
        self.assertTrue(second.ok)
        self.assertEqual(self.alerts, [])

    def test_transport_timeout_is_frozen_and_never_retried(self):
        binding(self.store)
        transport = RecordingTransport(error=TimeoutError("fixture timeout"))
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )
        service = self.service(adapter)

        first = self.procure(service)
        second = self.procure(service)

        self.assertFalse(first.ok)
        self.assertEqual(first.status, ProcurementStatus.SUBMISSION_UNKNOWN)
        self.assertEqual(second.status, ProcurementStatus.SUBMISSION_UNKNOWN)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(self.ledger.reserve_calls, [])
        self.assertEqual(
            self.store.get_purchase("order-1", "shop-a")["status"],
            "submission_unknown",
        )
        self.assertEqual(len(self.alerts), 2)

    def test_actual_cost_below_minimum_margin_stops_before_ledger(self):
        binding(self.store)
        transport = RecordingTransport(
            response=supplier_response(charged="13.00")
        )
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )
        service = self.service(adapter)

        decision = self.procure(service)

        self.assertFalse(decision.ok)
        self.assertEqual(
            decision.status,
            ProcurementStatus.MARGIN_BELOW_MINIMUM,
        )
        self.assertTrue(decision.transport_called)
        self.assertEqual(decision.margin, Decimal("7.00"))
        self.assertEqual(self.ledger.reserve_calls, [])

    def test_invalid_order_amount_blocks_before_transport(self):
        binding(self.store)
        self.db.order["amount"] = "未知金额"
        transport = RecordingTransport(response=supplier_response())
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )
        service = self.service(adapter)

        decision = self.procure(service)

        self.assertFalse(decision.ok)
        self.assertEqual(decision.status, ProcurementStatus.ORDER_INVALID)
        self.assertEqual(transport.requests, [])
        self.assertEqual(self.store.get_purchase("order-1", "shop-a"), None)

    def test_duplicate_codes_are_not_reserved(self):
        binding(self.store)

        class DuplicateCodeAdapter:
            supplier_code = "baizhou"

            def purchase(self, request):
                return PurchaseResult(
                    request_id=request.request_id,
                    idempotency_key=request.idempotency_key,
                    supplier_code="baizhou",
                    status=PurchaseStatus.SUCCEEDED,
                    message="fixture response",
                    contract_status=ContractStatus.VERIFIED,
                    transport_called=True,
                    total_cost=Decimal("10.00"),
                    margin=Decimal("10.00"),
                    supplier_order_id="supplier-order-1",
                    delivery_codes=("same-code", "same-code"),
                )

        decision = self.procure(self.service(DuplicateCodeAdapter()))

        self.assertFalse(decision.ok)
        self.assertEqual(decision.status, ProcurementStatus.CODES_INVALID)
        self.assertEqual(self.ledger.reserve_calls, [])
        stored = self.store.get_purchase("order-1", "shop-a")
        self.assertEqual(stored["status"], "codes_invalid")

    def test_ledger_failure_keeps_codes_for_retry_without_new_purchase(self):
        binding(self.store)
        transport = RecordingTransport(response=supplier_response())
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )
        service = self.service(adapter)
        self.ledger.reserve_error = RuntimeError("fixture ledger unavailable")

        first = self.procure(service)
        self.assertFalse(first.ok)
        self.assertEqual(
            first.status,
            ProcurementStatus.RESERVATION_FAILED,
        )
        self.assertEqual(
            self.store.get_purchase("order-1", "shop-a")["status"],
            "codes_received",
        )

        self.ledger.reserve_error = None
        second = self.procure(service)

        self.assertTrue(second.ok)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(len(self.ledger.reserve_calls), 1)

    def test_transient_result_persistence_failure_retries_without_new_purchase(self):
        binding(self.store)
        transport = RecordingTransport(response=supplier_response())
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )
        service = self.service(adapter)
        original_update = self.store.update_purchase
        persistence_attempts = []

        def fail_first_result_persistence(*args, **kwargs):
            if kwargs.get("status") == "codes_received":
                if not persistence_attempts:
                    persistence_attempts.append("failed")
                    raise sqlite3.OperationalError("database is locked")
                persistence_attempts.append("succeeded")
            return original_update(*args, **kwargs)

        with patch.object(
            self.store,
            "update_purchase",
            side_effect=fail_first_result_persistence,
        ):
            decision = self.procure(service)

        stored = self.store.get_purchase("order-1", "shop-a")
        self.assertTrue(decision.ok)
        self.assertEqual(decision.status, ProcurementStatus.PURCHASED)
        self.assertEqual(decision.codes, ("code-a", "code-b"))
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(len(self.ledger.reserve_calls), 1)
        self.assertEqual(
            self.ledger.reserve_calls[0]["codes"],
            ("code-a", "code-b"),
        )
        self.assertEqual(stored["status"], "purchased")
        self.assertEqual(json.loads(stored["codes_json"]), ["code-a", "code-b"])
        self.assertEqual(persistence_attempts, ["failed", "succeeded"])

    def test_successful_result_recovers_after_persistent_database_lock(self):
        binding(self.store)
        transport = RecordingTransport(response=supplier_response())
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )
        service = self.service(adapter)
        original_update = self.store.update_purchase

        def reject_codes_received(*args, **kwargs):
            if kwargs.get("status") == "codes_received":
                raise sqlite3.OperationalError("database is locked")
            return original_update(*args, **kwargs)

        with patch.object(
            self.store,
            "update_purchase",
            side_effect=reject_codes_received,
        ):
            first = self.procure(service)

        self.assertFalse(first.ok)
        self.assertEqual(first.status, ProcurementStatus.PERSISTENCE_FAILED)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(self.ledger.reserve_calls, [])
        self.assertEqual(
            self.store.get_purchase("order-1", "shop-a")["status"],
            "in_flight",
        )
        recovery_files = [
            path
            for path in service.recovery_dir.glob("*.json")
            if path.is_file()
        ]
        self.assertEqual(len(recovery_files), 1)
        recovery_text = recovery_files[0].read_text(encoding="utf-8")
        self.assertNotIn("code-a", first.message)
        self.assertNotIn("code-b", first.message)
        self.assertIn("code-a", recovery_text)
        self.assertIn("code-b", recovery_text)

        with patch.object(
            self.store,
            "update_purchase",
            side_effect=reject_codes_received,
        ):
            still_locked = self.procure(service)
        self.assertFalse(still_locked.ok)
        self.assertEqual(
            still_locked.status,
            ProcurementStatus.PERSISTENCE_FAILED,
        )
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(len(list(service.recovery_dir.glob("*.json"))), 1)

        restarted = self.service(adapter)
        second = self.procure(restarted)

        self.assertTrue(second.ok)
        self.assertEqual(second.status, ProcurementStatus.PURCHASED)
        self.assertEqual(second.codes, ("code-a", "code-b"))
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(len(self.ledger.reserve_calls), 1)
        stored = self.store.get_purchase("order-1", "shop-a")
        self.assertEqual(stored["status"], "purchased")
        self.assertEqual(
            json.loads(stored["codes_json"]),
            ["code-a", "code-b"],
        )
        self.assertEqual(list(restarted.recovery_dir.glob("*.json")), [])

    def test_same_item_can_bind_multiple_card_groups_without_overwrite(self):
        binding(
            self.store,
            card_id=11,
            supplier_sku="sku-month",
        )
        binding(
            self.store,
            card_id=12,
            supplier_sku="sku-year",
        )

        first = self.store.get_binding("shop-a", "item-1", 11)
        second = self.store.get_binding("shop-a", "item-1", 12)

        self.assertEqual(first["supplier_sku"], "sku-month")
        self.assertEqual(second["supplier_sku"], "sku-year")
        self.assertIsNone(self.store.get_binding("shop-a", "item-1"))

    def test_changed_target_indices_freeze_existing_purchase(self):
        binding(self.store)
        self.store.begin_purchase(
            order_id="order-1",
            cookie_id="shop-a",
            item_id="item-1",
            card_id=11,
            buyer_id="buyer-1",
            quantity=2,
            request_id="existing-request",
            idempotency_key="existing-idempotency",
            supplier_code="baizhou",
            supplier_sku="sku-1",
            target_indices=(0, 1),
        )
        transport = RecordingTransport(response=supplier_response())
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )

        decision = self.procure(
            self.service(adapter),
            target_indices=(0, 2),
        )

        self.assertFalse(decision.ok)
        self.assertEqual(decision.status, ProcurementStatus.SUPPLIER_BLOCKED)
        self.assertEqual(transport.requests, [])
        stored = self.store.get_purchase("order-1", "shop-a")
        self.assertEqual(stored["status"], "supplier_blocked")
        self.assertEqual(stored["target_indices_json"], "[0,1]")

    def test_only_missing_position_is_purchased_and_transformed_before_reserve(self):
        binding(self.store)
        transport = RecordingTransport(
            response=supplier_response(codes=["raw-code"])
        )
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )

        decision = self.procure(
            self.service(adapter),
            quantity=1,
            target_indices=(2,),
            code_transform=lambda code: f"月卡：{code}",
        )

        self.assertTrue(decision.ok)
        self.assertEqual(decision.codes, ("raw-code",))
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(len(self.ledger.reserve_calls), 1)
        self.assertEqual(
            self.ledger.reserve_calls[0]["target_indices"],
            [2],
        )
        self.assertEqual(
            self.ledger.reserve_calls[0]["codes"],
            ("月卡：raw-code",),
        )

    def test_codes_from_another_purchase_are_rejected(self):
        binding(self.store)
        self.store.begin_purchase(
            order_id="order-old",
            cookie_id="shop-a",
            item_id="item-1",
            card_id=11,
            buyer_id="buyer-old",
            quantity=1,
            request_id="old-request",
            idempotency_key="old-idempotency",
            supplier_code="baizhou",
            supplier_sku="sku-1",
            target_indices=(0,),
        )
        self.store.update_purchase(
            "order-old",
            "shop-a",
            status="codes_received",
            codes=("same-code",),
        )
        transport = RecordingTransport(
            response=supplier_response(codes=["same-code", "new-code"])
        )
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )

        decision = self.procure(self.service(adapter))

        self.assertFalse(decision.ok)
        self.assertEqual(decision.status, ProcurementStatus.CODES_CONFLICT)
        self.assertIn("order-old", decision.message)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(self.ledger.reserve_calls, [])

    def test_legacy_schema_is_migrated_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-supplier.db"
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE supplier_delivery_bindings (
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
                        PRIMARY KEY (cookie_id, item_id)
                    );
                    CREATE TABLE supplier_delivery_purchases (
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
                    """
                )
                conn.execute(
                    """
                    INSERT INTO supplier_delivery_bindings (
                        cookie_id, item_id, card_id, supplier_code, supplier_sku,
                        currency, unit_cost, quote_updated_at,
                        minimum_total_margin, maximum_quote_age_seconds, enabled,
                        created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "shop-a",
                        "item-1",
                        11,
                        "baizhou",
                        "sku-legacy",
                        "CNY",
                        "5.00",
                        NOW.isoformat(),
                        "9.00",
                        300,
                        1,
                        NOW.isoformat(),
                        NOW.isoformat(),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO supplier_delivery_purchases (
                        order_id, cookie_id, item_id, card_id, buyer_id, quantity,
                        request_id, idempotency_key, supplier_code, supplier_sku,
                        status, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "order-old",
                        "shop-a",
                        "item-1",
                        11,
                        "buyer-old",
                        2,
                        "request-old",
                        "idempotency-old",
                        "baizhou",
                        "sku-legacy",
                        "submission_unknown",
                        NOW.isoformat(),
                        NOW.isoformat(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            migrated = SupplierBindingStore(path)
            stored_binding = migrated.get_binding("shop-a", "item-1", 11)
            stored_purchase = migrated.get_purchase("order-old", "shop-a")
            conn = sqlite3.connect(path)
            try:
                binding_pk = [
                    row[1]
                    for row in sorted(
                        (
                            (int(row[5]), row[1])
                            for row in conn.execute(
                                "PRAGMA table_info(supplier_delivery_bindings)"
                            )
                            if int(row[5]) > 0
                        ),
                        key=lambda row: row[0],
                    )
                ]
                purchase_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(supplier_delivery_purchases)"
                    )
                }
            finally:
                conn.close()

        self.assertEqual(
            binding_pk,
            ["cookie_id", "item_id", "card_id"],
        )
        self.assertIn("target_indices_json", purchase_columns)
        self.assertEqual(stored_binding["supplier_sku"], "sku-legacy")
        self.assertEqual(stored_purchase["status"], "submission_unknown")
        self.assertEqual(stored_purchase["target_indices_json"], "[]")


if __name__ == "__main__":
    unittest.main()
