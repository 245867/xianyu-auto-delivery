from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from supplier_adapters import (  # noqa: E402
    BaiZhouSupplierAdapter,
    CARD91_CLOUD_WAREHOUSE_CODE,
    CapabilityStatus,
    Card91CloudWarehouseAdapter,
    ContractStatus,
    CostQuote,
    FixedRequestValue,
    MarginStatus,
    PurchaseRequest,
    PurchaseStatus,
    RequestFieldMapping,
    RequestFieldSource,
    RequestMapping,
    ResponseMapping,
    SignatureContract,
    SupplierContract,
    SupplierHttpRequest,
    SupplierHttpResponse,
    evaluate_purchase_margin,
)


NOW = datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, response: SupplierHttpResponse):
        self.response = response
        self.requests: list[SupplierHttpRequest] = []

    def send(self, request: SupplierHttpRequest) -> SupplierHttpResponse:
        self.requests.append(request)
        return self.response


class FailingTransport:
    def __init__(self):
        self.requests: list[SupplierHttpRequest] = []

    def send(self, request: SupplierHttpRequest) -> SupplierHttpResponse:
        self.requests.append(request)
        raise TimeoutError("contract transport timeout")


def cost_quote(
    *,
    key: str = "supplier_total",
    amount: str = "90.00",
    quoted_at: datetime = NOW,
) -> CostQuote:
    return CostQuote(
        key=key,
        total_amount=Decimal(amount),
        currency="CNY",
        quoted_at=quoted_at,
    )


def purchase_request(
    *,
    idempotency_key: str = "idem-contract-001",
    supplier_sku: str = "contract-sku-001",
    buyer_paid: str = "100.00",
    minimum_margin: str = "10.00",
    costs: tuple[CostQuote, ...] | None = None,
    required_cost_keys: tuple[str, ...] = ("supplier_total",),
    maximum_quote_age: timedelta = timedelta(minutes=5),
) -> PurchaseRequest:
    return PurchaseRequest(
        request_id="request-contract-001",
        idempotency_key=idempotency_key,
        supplier_sku=supplier_sku,
        quantity=1,
        buyer_paid=Decimal(buyer_paid),
        currency="CNY",
        costs=costs or (cost_quote(),),
        required_cost_keys=required_cost_keys,
        minimum_margin=Decimal(minimum_margin),
        maximum_quote_age=maximum_quote_age,
    )


def response_mapping() -> ResponseMapping:
    return ResponseMapping(
        success_http_statuses=(200,),
        status_path=("contract_response", "state"),
        success_status_values=("accepted",),
        order_id_path=("contract_response", "supplier_order"),
        message_path=("contract_response", "detail"),
        purchase_cost_path=("contract_response", "charged"),
        delivery_codes_path=("contract_response", "delivery_codes"),
    )


def supplier_contract(
    *,
    supplier_code: str = "baizhou",
    endpoint_url: str = "https://contract.invalid/purchase",
    http_method: str = "POST",
    verified: bool = True,
    signature_required: bool = False,
    signature_contract: SignatureContract | None = None,
) -> SupplierContract:
    return SupplierContract(
        supplier_code=supplier_code,
        endpoint_url=endpoint_url,
        http_method=http_method,
        request_mapping=RequestMapping(
            fields=(
                RequestFieldMapping(
                    source=RequestFieldSource.SUPPLIER_SKU,
                    target_path=("contract_request", "item"),
                ),
                RequestFieldMapping(
                    source=RequestFieldSource.QUANTITY,
                    target_path=("contract_request", "count"),
                ),
                RequestFieldMapping(
                    source=RequestFieldSource.IDEMPOTENCY_KEY,
                    target_path=("contract_request", "reference"),
                ),
            ),
            fixed_values=(
                FixedRequestValue(
                    target_path=("contract_request", "action"),
                    value="contract-fixed-value",
                ),
            ),
        ),
        response_mapping=response_mapping(),
        verified=verified,
        verification_reference="verified-fixture-contract",
        headers=(("X-Contract-Header", "contract-header-value"),),
        signature_required=signature_required,
        signature_contract=signature_contract,
    )


_MISSING = object()


def success_response(
    *,
    charged: str = "90.00",
    delivery_codes: object = _MISSING,
) -> SupplierHttpResponse:
    contract_response = {
        "state": "accepted",
        "supplier_order": "supplier-order-001",
        "detail": "accepted by contract",
        "charged": charged,
    }
    if delivery_codes is _MISSING:
        contract_response["delivery_codes"] = ["contract-code-001"]
    elif delivery_codes is not None:
        contract_response["delivery_codes"] = delivery_codes
    body = json.dumps(
        {
            "contract_response": contract_response
        }
    ).encode("utf-8")
    return SupplierHttpResponse(status_code=200, body=body)


def test_missing_contract_blocks_before_transport() -> None:
    transport = FakeTransport(success_response())
    adapter = BaiZhouSupplierAdapter(transport=transport, clock=lambda: NOW)

    capability = adapter.capability()
    result = adapter.purchase(purchase_request())

    assert capability.capability_status is CapabilityStatus.BLOCKED
    assert capability.contract_status is ContractStatus.MISSING
    assert result.status is PurchaseStatus.CONTRACT_MISSING
    assert result.transport_called is False
    assert transport.requests == []


def test_unverified_contract_blocks_before_transport() -> None:
    transport = FakeTransport(success_response())
    adapter = BaiZhouSupplierAdapter(
        contract=supplier_contract(verified=False),
        transport=transport,
        clock=lambda: NOW,
    )

    result = adapter.purchase(purchase_request())

    assert result.status is PurchaseStatus.CONTRACT_UNVERIFIED
    assert result.contract_status is ContractStatus.UNVERIFIED
    assert result.transport_called is False
    assert transport.requests == []


def test_contract_without_delivery_codes_path_blocks_before_transport() -> None:
    transport = FakeTransport(success_response())
    contract = supplier_contract()
    contract = replace(
        contract,
        response_mapping=replace(
            contract.response_mapping,
            delivery_codes_path=None,
        ),
    )
    adapter = BaiZhouSupplierAdapter(
        contract=contract,
        transport=transport,
        clock=lambda: NOW,
    )

    result = adapter.purchase(purchase_request())

    assert result.status is PurchaseStatus.CONTRACT_INCOMPLETE
    assert result.contract_status is ContractStatus.INCOMPLETE
    assert result.transport_called is False
    assert transport.requests == []


def test_margin_below_minimum_is_rejected_before_transport() -> None:
    transport = FakeTransport(success_response())
    adapter = BaiZhouSupplierAdapter(
        contract=supplier_contract(),
        transport=transport,
        clock=lambda: NOW,
    )

    result = adapter.purchase(
        purchase_request(costs=(cost_quote(amount="91.00"),))
    )

    assert result.status is PurchaseStatus.MARGIN_BELOW_MINIMUM
    assert result.total_cost == Decimal("91.00")
    assert result.margin == Decimal("9.00")
    assert result.transport_called is False
    assert transport.requests == []


def test_missing_required_cost_is_rejected_before_transport() -> None:
    transport = FakeTransport(success_response())
    adapter = BaiZhouSupplierAdapter(
        contract=supplier_contract(),
        transport=transport,
        clock=lambda: NOW,
    )

    result = adapter.purchase(
        purchase_request(required_cost_keys=("supplier_total", "gateway_fee"))
    )

    assert result.status is PurchaseStatus.COST_MISSING
    assert result.transport_called is False
    assert transport.requests == []


def test_stale_quote_is_rejected_before_transport() -> None:
    transport = FakeTransport(success_response())
    adapter = BaiZhouSupplierAdapter(
        contract=supplier_contract(),
        transport=transport,
        clock=lambda: NOW,
    )
    stale_at = NOW - timedelta(minutes=5, seconds=1)

    result = adapter.purchase(
        purchase_request(costs=(cost_quote(quoted_at=stale_at),))
    )

    assert result.status is PurchaseStatus.QUOTE_STALE
    assert result.transport_called is False
    assert transport.requests == []


def test_exact_minimum_margin_passes_and_uses_exact_contract_mapping() -> None:
    transport = FakeTransport(success_response())
    adapter = BaiZhouSupplierAdapter(
        contract=supplier_contract(),
        transport=transport,
        clock=lambda: NOW,
    )

    result = adapter.purchase(purchase_request())

    assert result.status is PurchaseStatus.SUCCEEDED
    assert result.transport_called is True
    assert result.supplier_order_id == "supplier-order-001"
    assert result.total_cost == Decimal("90.00")
    assert result.margin == Decimal("10.00")
    assert result.delivery_codes == ("contract-code-001",)
    assert len(transport.requests) == 1
    outbound = transport.requests[0]
    assert outbound.method == "POST"
    assert outbound.url == "https://contract.invalid/purchase"
    assert outbound.headers == (("X-Contract-Header", "contract-header-value"),)
    assert json.loads(outbound.body) == {
        "contract_request": {
            "item": "contract-sku-001",
            "count": 1,
            "reference": "idem-contract-001",
            "action": "contract-fixed-value",
        }
    }


def test_exact_threshold_evaluation_is_deterministic() -> None:
    decision = evaluate_purchase_margin(
        purchase_request(),
        evaluated_at=NOW,
    )

    assert decision.status is MarginStatus.OK
    assert decision.total_cost == Decimal("90.00")
    assert decision.margin == Decimal("10.00")


def test_duplicate_request_is_not_dispatched_twice() -> None:
    transport = FakeTransport(success_response())
    adapter = BaiZhouSupplierAdapter(
        contract=supplier_contract(),
        transport=transport,
        clock=lambda: NOW,
    )
    request = purchase_request()

    first = adapter.purchase(request)
    duplicate = adapter.purchase(request)

    assert first.status is PurchaseStatus.SUCCEEDED
    assert duplicate.status is PurchaseStatus.DUPLICATE
    assert duplicate.original_status is PurchaseStatus.SUCCEEDED
    assert duplicate.transport_called is False
    assert duplicate.supplier_order_id == "supplier-order-001"
    assert duplicate.delivery_codes == ("contract-code-001",)
    assert len(transport.requests) == 1


def test_idempotency_key_reuse_with_different_request_is_blocked() -> None:
    transport = FakeTransport(success_response())
    adapter = BaiZhouSupplierAdapter(
        contract=supplier_contract(),
        transport=transport,
        clock=lambda: NOW,
    )

    first = adapter.purchase(purchase_request())
    conflict = adapter.purchase(
        purchase_request(supplier_sku="contract-sku-different")
    )

    assert first.status is PurchaseStatus.SUCCEEDED
    assert conflict.status is PurchaseStatus.IDEMPOTENCY_CONFLICT
    assert conflict.transport_called is False
    assert len(transport.requests) == 1


def test_signature_contract_is_required_only_when_contract_requires_it() -> None:
    without_signature = Card91CloudWarehouseAdapter(
        contract=supplier_contract(
            supplier_code=CARD91_CLOUD_WAREHOUSE_CODE,
            endpoint_url="https://contract.invalid/dockapi/v3/getallgoods",
            signature_required=True,
        ),
        transport=FakeTransport(success_response()),
        clock=lambda: NOW,
    )

    capability = without_signature.capability()
    result = without_signature.purchase(purchase_request())

    assert capability.contract_status is ContractStatus.INCOMPLETE
    assert capability.reason == "signature_contract is required"
    assert result.status is PurchaseStatus.CONTRACT_INCOMPLETE
    assert result.transport_called is False


def test_supplied_signature_contract_sets_only_exact_returned_headers() -> None:
    transport = FakeTransport(success_response())
    signature_inputs = []

    def apply_signature(signature_input):
        signature_inputs.append(signature_input)
        return {"X-Contract-Signature": "contract-signature-value"}

    adapter = Card91CloudWarehouseAdapter(
        contract=supplier_contract(
            supplier_code=CARD91_CLOUD_WAREHOUSE_CODE,
            endpoint_url="https://contract.invalid/dockapi/v3/getallgoods",
            signature_required=True,
            signature_contract=SignatureContract(
                contract_id="contract-signature-version-001",
                apply=apply_signature,
            ),
        ),
        transport=transport,
        clock=lambda: NOW,
    )

    result = adapter.purchase(purchase_request())

    assert result.status is PurchaseStatus.SUCCEEDED
    assert len(signature_inputs) == 1
    assert signature_inputs[0].method == "POST"
    assert signature_inputs[0].url == (
        "https://contract.invalid/dockapi/v3/getallgoods"
    )
    assert transport.requests[0].headers == (
        ("X-Contract-Header", "contract-header-value"),
        ("X-Contract-Signature", "contract-signature-value"),
    )


def test_signature_failure_blocks_transport_before_dispatch() -> None:
    transport = FakeTransport(success_response())

    def fail_signature(_signature_input):
        raise ValueError("contract signer rejected input")

    adapter = Card91CloudWarehouseAdapter(
        contract=supplier_contract(
            supplier_code=CARD91_CLOUD_WAREHOUSE_CODE,
            endpoint_url="https://contract.invalid/dockapi/v3/getallgoods",
            signature_required=True,
            signature_contract=SignatureContract(
                contract_id="contract-signature-version-001",
                apply=fail_signature,
            ),
        ),
        transport=transport,
        clock=lambda: NOW,
    )

    result = adapter.purchase(purchase_request())

    assert result.status is PurchaseStatus.SIGNING_ERROR
    assert result.transport_called is False
    assert transport.requests == []


def test_transport_timeout_becomes_unknown_and_is_not_retried() -> None:
    transport = FailingTransport()
    adapter = BaiZhouSupplierAdapter(
        contract=supplier_contract(),
        transport=transport,
        clock=lambda: NOW,
    )
    request = purchase_request()

    first = adapter.purchase(request)
    duplicate = adapter.purchase(request)

    assert first.status is PurchaseStatus.SUBMISSION_UNKNOWN
    assert duplicate.status is PurchaseStatus.DUPLICATE
    assert duplicate.original_status is PurchaseStatus.SUBMISSION_UNKNOWN
    assert len(transport.requests) == 1


def test_zero_supplier_response_cost_remains_zero() -> None:
    transport = FakeTransport(success_response(charged="0.00"))
    adapter = BaiZhouSupplierAdapter(
        contract=supplier_contract(),
        transport=transport,
        clock=lambda: NOW,
    )

    result = adapter.purchase(purchase_request())

    assert result.status is PurchaseStatus.SUCCEEDED
    assert result.total_cost == Decimal("0.00")
    assert result.margin == Decimal("100.00")


def test_missing_contracted_response_cost_is_rejected() -> None:
    body = json.dumps(
        {
            "contract_response": {
                "state": "accepted",
                "supplier_order": "supplier-order-001",
                "detail": "accepted by contract",
                "delivery_codes": ["contract-code-001"],
            }
        }
    ).encode("utf-8")
    transport = FakeTransport(
        SupplierHttpResponse(status_code=200, body=body)
    )
    adapter = BaiZhouSupplierAdapter(
        contract=supplier_contract(),
        transport=transport,
        clock=lambda: NOW,
    )

    result = adapter.purchase(purchase_request())

    assert result.status is PurchaseStatus.INVALID_RESPONSE
    assert result.transport_called is True
    assert result.total_cost is None
    assert result.margin is None
    assert result.supplier_order_id == "supplier-order-001"
    assert result.delivery_codes == ()
    assert len(transport.requests) == 1


def test_actual_response_cost_cannot_understate_minimum_margin() -> None:
    transport = FakeTransport(success_response(charged="95.00"))
    adapter = BaiZhouSupplierAdapter(
        contract=supplier_contract(),
        transport=transport,
        clock=lambda: NOW,
    )

    result = adapter.purchase(purchase_request())

    assert result.status is PurchaseStatus.MARGIN_BELOW_MINIMUM
    assert result.transport_called is True
    assert result.total_cost == Decimal("95.00")
    assert result.margin == Decimal("5.00")
    assert result.delivery_codes == ()


def test_delivery_code_count_must_match_purchase_quantity() -> None:
    transport = FakeTransport(success_response(delivery_codes=["code-1", "code-2"]))
    adapter = BaiZhouSupplierAdapter(
        contract=supplier_contract(),
        transport=transport,
        clock=lambda: NOW,
    )

    result = adapter.purchase(purchase_request())

    assert result.status is PurchaseStatus.INVALID_RESPONSE
    assert result.transport_called is True
    assert result.delivery_codes == ()
    assert result.supplier_order_id == "supplier-order-001"


def test_duplicate_delivery_codes_are_rejected() -> None:
    request = purchase_request()
    request = PurchaseRequest(
        request_id=request.request_id,
        idempotency_key=request.idempotency_key,
        supplier_sku=request.supplier_sku,
        quantity=2,
        buyer_paid=Decimal("200.00"),
        currency=request.currency,
        costs=(cost_quote(amount="180.00"),),
        required_cost_keys=request.required_cost_keys,
        minimum_margin=request.minimum_margin,
        maximum_quote_age=request.maximum_quote_age,
    )
    transport = FakeTransport(
        success_response(delivery_codes=["same-code", "same-code"])
    )
    adapter = BaiZhouSupplierAdapter(
        contract=supplier_contract(),
        transport=transport,
        clock=lambda: NOW,
    )

    result = adapter.purchase(request)

    assert result.status is PurchaseStatus.INVALID_RESPONSE
    assert result.delivery_codes == ()


def test_missing_or_empty_delivery_code_is_rejected() -> None:
    for response in (
        success_response(delivery_codes=None),
        success_response(delivery_codes=[""]),
        success_response(delivery_codes=[123]),
    ):
        adapter = BaiZhouSupplierAdapter(
            contract=supplier_contract(),
            transport=FakeTransport(response),
            clock=lambda: NOW,
        )

        result = adapter.purchase(purchase_request())

        assert result.status is PurchaseStatus.INVALID_RESPONSE
        assert result.delivery_codes == ()


def test_card91_rejects_contract_for_a_different_endpoint_path() -> None:
    transport = FakeTransport(success_response())
    adapter = Card91CloudWarehouseAdapter(
        contract=supplier_contract(supplier_code=CARD91_CLOUD_WAREHOUSE_CODE),
        transport=transport,
        clock=lambda: NOW,
    )

    capability = adapter.capability()
    result = adapter.purchase(purchase_request())

    assert capability.contract_status is ContractStatus.INCOMPLETE
    assert capability.reason == "contract endpoint path does not match adapter"
    assert result.status is PurchaseStatus.CONTRACT_INCOMPLETE
    assert transport.requests == []
