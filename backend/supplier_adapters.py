"""Contract-driven supplier procurement adapters.

Supplier endpoints, request fields, response fields, success values, headers,
and signing behavior must be supplied explicitly by a verified contract.
Adapters never contain supplier payload defaults or create network transports.
"""

from __future__ import annotations

import hashlib
import json
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol
from urllib.parse import urlsplit


CARD91_CLOUD_WAREHOUSE_CODE = "card91_cloud_warehouse"


class ContractStatus(str, Enum):
    MISSING = "missing"
    UNVERIFIED = "unverified"
    INCOMPLETE = "incomplete"
    VERIFIED = "verified"


class CapabilityStatus(str, Enum):
    AVAILABLE = "available"
    BLOCKED = "blocked"


class RequestFieldSource(str, Enum):
    REQUEST_ID = "request_id"
    IDEMPOTENCY_KEY = "idempotency_key"
    SUPPLIER_SKU = "supplier_sku"
    QUANTITY = "quantity"


class MarginStatus(str, Enum):
    OK = "ok"
    INVALID_REQUEST = "invalid_request"
    COST_MISSING = "cost_missing"
    COST_DUPLICATE = "cost_duplicate"
    QUOTE_INVALID = "quote_invalid"
    QUOTE_STALE = "quote_stale"
    CURRENCY_MISMATCH = "currency_mismatch"
    MARGIN_BELOW_MINIMUM = "margin_below_minimum"


class PurchaseStatus(str, Enum):
    SUCCEEDED = "succeeded"
    DUPLICATE = "duplicate"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    CONTRACT_MISSING = "contract_missing"
    CONTRACT_UNVERIFIED = "contract_unverified"
    CONTRACT_INCOMPLETE = "contract_incomplete"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"
    INVALID_REQUEST = "invalid_request"
    COST_MISSING = "cost_missing"
    COST_DUPLICATE = "cost_duplicate"
    QUOTE_INVALID = "quote_invalid"
    QUOTE_STALE = "quote_stale"
    CURRENCY_MISMATCH = "currency_mismatch"
    MARGIN_BELOW_MINIMUM = "margin_below_minimum"
    REQUEST_BUILD_FAILED = "request_build_failed"
    SIGNING_ERROR = "signing_error"
    TRANSPORT_ERROR = "transport_error"
    SUBMISSION_UNKNOWN = "submission_unknown"
    INVALID_RESPONSE = "invalid_response"
    SUPPLIER_REJECTED = "supplier_rejected"


@dataclass(frozen=True)
class CostQuote:
    """A total cost quote already applicable to the normalized request quantity."""

    key: str
    total_amount: Decimal
    currency: str
    quoted_at: datetime


@dataclass(frozen=True)
class PurchaseRequest:
    request_id: str
    idempotency_key: str
    supplier_sku: str
    quantity: int
    buyer_paid: Decimal
    currency: str
    costs: tuple[CostQuote, ...]
    required_cost_keys: tuple[str, ...]
    minimum_margin: Decimal
    maximum_quote_age: timedelta

    def value_for(self, source: RequestFieldSource) -> Any:
        values = {
            RequestFieldSource.REQUEST_ID: self.request_id,
            RequestFieldSource.IDEMPOTENCY_KEY: self.idempotency_key,
            RequestFieldSource.SUPPLIER_SKU: self.supplier_sku,
            RequestFieldSource.QUANTITY: self.quantity,
        }
        return values[source]


@dataclass(frozen=True)
class MarginDecision:
    status: MarginStatus
    message: str
    total_cost: Optional[Decimal] = None
    margin: Optional[Decimal] = None


@dataclass(frozen=True)
class RequestFieldMapping:
    source: RequestFieldSource
    target_path: tuple[str, ...]


@dataclass(frozen=True)
class FixedRequestValue:
    target_path: tuple[str, ...]
    value: Any


@dataclass(frozen=True)
class RequestMapping:
    fields: tuple[RequestFieldMapping, ...]
    fixed_values: tuple[FixedRequestValue, ...] = ()


@dataclass(frozen=True)
class ResponseMapping:
    success_http_statuses: tuple[int, ...]
    status_path: tuple[str | int, ...]
    success_status_values: tuple[Any, ...]
    order_id_path: tuple[str | int, ...]
    message_path: Optional[tuple[str | int, ...]] = None
    purchase_cost_path: Optional[tuple[str | int, ...]] = None
    delivery_codes_path: Optional[tuple[str | int, ...]] = None


@dataclass(frozen=True)
class SignatureInput:
    method: str
    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True)
class SignatureContract:
    contract_id: str
    apply: Callable[[SignatureInput], Mapping[str, str]]


@dataclass(frozen=True)
class SupplierContract:
    supplier_code: str
    endpoint_url: str
    http_method: str
    request_mapping: RequestMapping
    response_mapping: ResponseMapping
    verified: bool
    verification_reference: str
    headers: tuple[tuple[str, str], ...] = ()
    signature_required: bool = False
    signature_contract: Optional[SignatureContract] = None


@dataclass(frozen=True)
class SupplierCapability:
    supplier_code: str
    supplier_label: str
    capability_status: CapabilityStatus
    contract_status: ContractStatus
    reason: str
    endpoint_url: Optional[str]
    http_method: Optional[str]


@dataclass(frozen=True)
class SupplierHttpRequest:
    method: str
    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True)
class SupplierHttpResponse:
    status_code: int
    body: bytes


class SupplierTransport(Protocol):
    def send(self, request: SupplierHttpRequest) -> SupplierHttpResponse: ...


@dataclass(frozen=True)
class PurchaseResult:
    request_id: str
    idempotency_key: str
    supplier_code: str
    status: PurchaseStatus
    message: str
    contract_status: ContractStatus
    transport_called: bool
    total_cost: Optional[Decimal] = None
    margin: Optional[Decimal] = None
    supplier_order_id: Optional[str] = None
    original_status: Optional[PurchaseStatus] = None
    delivery_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ContractCheck:
    status: ContractStatus
    reason: str


@dataclass(frozen=True)
class _IdempotencyRecord:
    fingerprint: str
    result: PurchaseResult


def _is_decimal(value: Any) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _is_timezone_aware(value: Any) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _decision(
    status: MarginStatus,
    message: str,
    *,
    total_cost: Optional[Decimal] = None,
    margin: Optional[Decimal] = None,
) -> MarginDecision:
    return MarginDecision(
        status=status,
        message=message,
        total_cost=total_cost,
        margin=margin,
    )


def evaluate_purchase_margin(
    request: PurchaseRequest,
    *,
    evaluated_at: Optional[datetime] = None,
) -> MarginDecision:
    """Evaluate all costs without network access or fallback estimates."""

    if not request.idempotency_key.strip():
        return _decision(MarginStatus.INVALID_REQUEST, "idempotency_key is empty")
    if not request.supplier_sku.strip():
        return _decision(MarginStatus.INVALID_REQUEST, "supplier_sku is empty")
    if isinstance(request.quantity, bool) or request.quantity <= 0:
        return _decision(MarginStatus.INVALID_REQUEST, "quantity must be positive")
    if not _is_decimal(request.buyer_paid):
        return _decision(MarginStatus.INVALID_REQUEST, "buyer_paid is not finite")
    if request.buyer_paid < 0:
        return _decision(MarginStatus.INVALID_REQUEST, "buyer_paid is negative")
    if not request.currency.strip():
        return _decision(MarginStatus.INVALID_REQUEST, "currency is empty")
    if not _is_decimal(request.minimum_margin):
        return _decision(MarginStatus.INVALID_REQUEST, "minimum_margin is not finite")
    if request.minimum_margin < 0:
        return _decision(MarginStatus.INVALID_REQUEST, "minimum_margin is negative")
    if not isinstance(request.maximum_quote_age, timedelta):
        return _decision(MarginStatus.INVALID_REQUEST, "maximum_quote_age is invalid")
    if request.maximum_quote_age < timedelta(0):
        return _decision(MarginStatus.INVALID_REQUEST, "maximum_quote_age is negative")

    required_keys = request.required_cost_keys
    if not required_keys or any(
        not isinstance(key, str) or not key.strip() for key in required_keys
    ):
        return _decision(
            MarginStatus.INVALID_REQUEST,
            "required_cost_keys must contain non-empty keys",
        )
    if len(set(required_keys)) != len(required_keys):
        return _decision(
            MarginStatus.INVALID_REQUEST,
            "required_cost_keys contains duplicates",
        )

    check_time = evaluated_at or datetime.now(timezone.utc)
    if not _is_timezone_aware(check_time):
        return _decision(
            MarginStatus.INVALID_REQUEST,
            "evaluated_at must be timezone-aware",
        )

    cost_by_key: dict[str, CostQuote] = {}
    for quote in request.costs:
        if not isinstance(quote.key, str) or not quote.key.strip():
            return _decision(MarginStatus.QUOTE_INVALID, "cost key is empty")
        if quote.key in cost_by_key:
            return _decision(
                MarginStatus.COST_DUPLICATE,
                f"cost quote is duplicated: {quote.key}",
            )
        cost_by_key[quote.key] = quote

    missing_keys = [key for key in required_keys if key not in cost_by_key]
    if missing_keys:
        return _decision(
            MarginStatus.COST_MISSING,
            "required cost missing: " + ",".join(missing_keys),
        )

    total_cost = Decimal("0")
    for quote in request.costs:
        if not _is_decimal(quote.total_amount) or quote.total_amount < 0:
            return _decision(
                MarginStatus.QUOTE_INVALID,
                f"cost quote is invalid: {quote.key}",
            )
        if quote.currency != request.currency:
            return _decision(
                MarginStatus.CURRENCY_MISMATCH,
                f"cost currency mismatch: {quote.key}",
            )
        if not _is_timezone_aware(quote.quoted_at):
            return _decision(
                MarginStatus.QUOTE_INVALID,
                f"cost quote timestamp is invalid: {quote.key}",
            )
        quote_age = check_time - quote.quoted_at
        if quote_age < timedelta(0):
            return _decision(
                MarginStatus.QUOTE_INVALID,
                f"cost quote timestamp is in the future: {quote.key}",
            )
        if quote_age > request.maximum_quote_age:
            return _decision(
                MarginStatus.QUOTE_STALE,
                f"cost quote is stale: {quote.key}",
            )
        total_cost += quote.total_amount

    margin = request.buyer_paid - total_cost
    if margin < request.minimum_margin:
        return _decision(
            MarginStatus.MARGIN_BELOW_MINIMUM,
            "margin is below the configured minimum",
            total_cost=total_cost,
            margin=margin,
        )
    return _decision(
        MarginStatus.OK,
        "margin passed",
        total_cost=total_cost,
        margin=margin,
    )


def _path_is_valid(path: tuple[Any, ...], *, allow_index: bool) -> bool:
    if not path:
        return False
    for segment in path:
        if isinstance(segment, str) and segment:
            continue
        if allow_index and isinstance(segment, int) and not isinstance(segment, bool):
            continue
        return False
    return True


def _paths_overlap(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    if first == second:
        return True
    shorter, longer = (
        (first, second) if len(first) < len(second) else (second, first)
    )
    return longer[: len(shorter)] == shorter


def _invalid_contract_reason(contract: SupplierContract) -> str:
    if not contract.supplier_code:
        return "supplier_code is empty"
    if not contract.endpoint_url:
        return "endpoint_url is empty"
    try:
        parsed_url = urlsplit(contract.endpoint_url)
    except ValueError:
        return "endpoint_url is invalid"
    if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
        return "endpoint_url must contain an http(s) scheme and hostname"
    if not contract.http_method:
        return "http_method is empty"

    request_paths: list[tuple[str, ...]] = []
    for field in contract.request_mapping.fields:
        if not isinstance(field.source, RequestFieldSource):
            return "request field source is invalid"
        if not _path_is_valid(field.target_path, allow_index=False):
            return "request field target_path is invalid"
        request_paths.append(field.target_path)
    for fixed_value in contract.request_mapping.fixed_values:
        if not _path_is_valid(fixed_value.target_path, allow_index=False):
            return "fixed request target_path is invalid"
        try:
            json.dumps(
                fixed_value.value,
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError):
            return "fixed request value is not valid JSON"
        request_paths.append(fixed_value.target_path)
    if not request_paths:
        return "request_mapping has no fields"
    for index, path in enumerate(request_paths):
        for other_path in request_paths[index + 1 :]:
            if _paths_overlap(path, other_path):
                return "request_mapping target paths overlap"

    response_mapping = contract.response_mapping
    if not response_mapping.success_http_statuses or any(
        isinstance(status, bool) or not isinstance(status, int)
        for status in response_mapping.success_http_statuses
    ):
        return "success_http_statuses is invalid"
    if not _path_is_valid(response_mapping.status_path, allow_index=True):
        return "status_path is invalid"
    if not response_mapping.success_status_values:
        return "success_status_values is empty"
    if not _path_is_valid(response_mapping.order_id_path, allow_index=True):
        return "order_id_path is invalid"
    if response_mapping.message_path is not None and not _path_is_valid(
        response_mapping.message_path,
        allow_index=True,
    ):
        return "message_path is invalid"
    if response_mapping.purchase_cost_path is not None and not _path_is_valid(
        response_mapping.purchase_cost_path,
        allow_index=True,
    ):
        return "purchase_cost_path is invalid"
    if not _path_is_valid(response_mapping.delivery_codes_path, allow_index=True):
        return "delivery_codes_path is required and must be valid"

    header_names: list[str] = []
    for name, value in contract.headers:
        if not isinstance(name, str) or not name or not isinstance(value, str):
            return "header tuple is invalid"
        if any(character in name for character in "\r\n"):
            return "header name is invalid"
        if name in header_names:
            return "header name is duplicated"
        header_names.append(name)

    if contract.signature_required and contract.signature_contract is None:
        return "signature_contract is required"
    if contract.signature_contract is not None:
        if not contract.signature_contract.contract_id.strip():
            return "signature contract_id is empty"
        if not callable(contract.signature_contract.apply):
            return "signature contract apply is not callable"
    return ""


def _contract_check(
    contract: Optional[SupplierContract],
    *,
    supplier_code: str,
    required_http_method: Optional[str],
    required_endpoint_path: Optional[str],
) -> _ContractCheck:
    if contract is None:
        return _ContractCheck(ContractStatus.MISSING, "supplier contract is missing")
    if (
        contract.verified is not True
        or not isinstance(contract.verification_reference, str)
        or not contract.verification_reference.strip()
    ):
        return _ContractCheck(
            ContractStatus.UNVERIFIED,
            "supplier contract has not been verified",
        )
    if contract.supplier_code != supplier_code:
        return _ContractCheck(
            ContractStatus.INCOMPLETE,
            "contract supplier_code does not match adapter",
        )
    if (
        required_http_method is not None
        and contract.http_method != required_http_method
    ):
        return _ContractCheck(
            ContractStatus.INCOMPLETE,
            "contract http_method does not match adapter",
        )
    if required_endpoint_path is not None:
        parsed_url = urlsplit(contract.endpoint_url)
        if parsed_url.path != required_endpoint_path:
            return _ContractCheck(
                ContractStatus.INCOMPLETE,
                "contract endpoint path does not match adapter",
            )
    invalid_reason = _invalid_contract_reason(contract)
    if invalid_reason:
        return _ContractCheck(ContractStatus.INCOMPLETE, invalid_reason)
    return _ContractCheck(ContractStatus.VERIFIED, "")


def _set_path(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = target
    for segment in path[:-1]:
        child = current.get(segment)
        if child is None:
            child = {}
            current[segment] = child
        if not isinstance(child, dict):
            raise ValueError("request mapping path collision")
        current = child
    current[path[-1]] = value


def _read_path(value: Any, path: tuple[str | int, ...]) -> Any:
    current = value
    for segment in path:
        if isinstance(segment, str):
            if not isinstance(current, dict) or segment not in current:
                raise KeyError(segment)
            current = current[segment]
            continue
        if not isinstance(current, list) or segment < 0 or segment >= len(current):
            raise KeyError(segment)
        current = current[segment]
    return current


def _exact_equal(first: Any, second: Any) -> bool:
    return type(first) is type(second) and first == second


def _response_cost(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            return None
    else:
        return None
    if not result.is_finite() or result < 0:
        return None
    return result


def _request_fingerprint(request: PurchaseRequest) -> str:
    canonical = {
        "request_id": request.request_id,
        "idempotency_key": request.idempotency_key,
        "supplier_sku": request.supplier_sku,
        "quantity": request.quantity,
        "buyer_paid": str(request.buyer_paid),
        "currency": request.currency,
        "costs": [
            {
                "key": quote.key,
                "total_amount": str(quote.total_amount),
                "currency": quote.currency,
                "quoted_at": quote.quoted_at.isoformat(),
            }
            for quote in sorted(request.costs, key=lambda item: str(item.key))
        ],
        "required_cost_keys": sorted(request.required_cost_keys),
        "minimum_margin": str(request.minimum_margin),
        "maximum_quote_age_seconds": request.maximum_quote_age.total_seconds(),
    }
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class BaseSupplierAdapter(ABC):
    """Fail-closed adapter whose only network path is an injected transport."""

    def __init__(
        self,
        *,
        contract: Optional[SupplierContract] = None,
        transport: Optional[SupplierTransport] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.contract = contract
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._idempotency_lock = threading.RLock()
        self._idempotency_records: dict[str, _IdempotencyRecord] = {}

    @property
    @abstractmethod
    def supplier_code(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def supplier_label(self) -> str:
        raise NotImplementedError

    @property
    def required_http_method(self) -> Optional[str]:
        return None

    @property
    def required_endpoint_path(self) -> Optional[str]:
        return None

    def _check_contract(self) -> _ContractCheck:
        return _contract_check(
            self.contract,
            supplier_code=self.supplier_code,
            required_http_method=self.required_http_method,
            required_endpoint_path=self.required_endpoint_path,
        )

    def capability(self) -> SupplierCapability:
        check = self._check_contract()
        available = check.status is ContractStatus.VERIFIED
        return SupplierCapability(
            supplier_code=self.supplier_code,
            supplier_label=self.supplier_label,
            capability_status=(
                CapabilityStatus.AVAILABLE
                if available
                else CapabilityStatus.BLOCKED
            ),
            contract_status=check.status,
            reason=check.reason,
            endpoint_url=self.contract.endpoint_url if self.contract else None,
            http_method=self.contract.http_method if self.contract else None,
        )

    def _result(
        self,
        request: PurchaseRequest,
        status: PurchaseStatus,
        message: str,
        contract_status: ContractStatus,
        *,
        transport_called: bool = False,
        total_cost: Optional[Decimal] = None,
        margin: Optional[Decimal] = None,
        supplier_order_id: Optional[str] = None,
        original_status: Optional[PurchaseStatus] = None,
        delivery_codes: tuple[str, ...] = (),
    ) -> PurchaseResult:
        return PurchaseResult(
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            supplier_code=self.supplier_code,
            status=status,
            message=message,
            contract_status=contract_status,
            transport_called=transport_called,
            total_cost=total_cost,
            margin=margin,
            supplier_order_id=supplier_order_id,
            original_status=original_status,
            delivery_codes=delivery_codes,
        )

    def _build_supplier_request(
        self,
        request: PurchaseRequest,
        contract: SupplierContract,
    ) -> SupplierHttpRequest:
        payload: dict[str, Any] = {}
        for field in contract.request_mapping.fields:
            _set_path(payload, field.target_path, request.value_for(field.source))
        for fixed_value in contract.request_mapping.fixed_values:
            _set_path(payload, fixed_value.target_path, fixed_value.value)
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return SupplierHttpRequest(
            method=contract.http_method,
            url=contract.endpoint_url,
            headers=tuple(contract.headers),
            body=body,
        )

    @staticmethod
    def _apply_signature(
        outbound: SupplierHttpRequest,
        contract: SupplierContract,
    ) -> SupplierHttpRequest:
        signature_contract = contract.signature_contract
        if signature_contract is None:
            return outbound
        signature_headers = signature_contract.apply(
            SignatureInput(
                method=outbound.method,
                url=outbound.url,
                headers=outbound.headers,
                body=outbound.body,
            )
        )
        if not isinstance(signature_headers, Mapping):
            raise ValueError("signature headers are not a mapping")
        headers = outbound.headers
        existing_names = {name for name, _ in headers}
        for name, value in signature_headers.items():
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(value, str)
                or any(character in name for character in "\r\n")
                or any(character in value for character in "\r\n")
            ):
                raise ValueError("signature header is invalid")
            if name in existing_names:
                raise ValueError("signature header collides with contract header")
            headers += ((name, value),)
            existing_names.add(name)
        return replace(outbound, headers=headers)

    def _normalize_response(
        self,
        request: PurchaseRequest,
        response: SupplierHttpResponse,
        decision: MarginDecision,
        contract: SupplierContract,
    ) -> PurchaseResult:
        mapping = contract.response_mapping
        try:
            body = json.loads(
                response.body.decode("utf-8"),
                parse_float=Decimal,
            )
            raw_status = _read_path(body, mapping.status_path)
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            return self._result(
                request,
                PurchaseStatus.INVALID_RESPONSE,
                "supplier response does not match the contract",
                ContractStatus.VERIFIED,
                transport_called=True,
                total_cost=decision.total_cost,
                margin=decision.margin,
            )

        if response.status_code not in mapping.success_http_statuses or not any(
            _exact_equal(raw_status, success_value)
            for success_value in mapping.success_status_values
        ):
            message = "supplier rejected the purchase"
            if mapping.message_path is not None:
                try:
                    raw_message = _read_path(body, mapping.message_path)
                except (KeyError, TypeError):
                    raw_message = None
                if isinstance(raw_message, str) and raw_message.strip():
                    message = raw_message.strip()
            return self._result(
                request,
                PurchaseStatus.SUPPLIER_REJECTED,
                message,
                ContractStatus.VERIFIED,
                transport_called=True,
                total_cost=decision.total_cost,
                margin=decision.margin,
            )

        try:
            raw_order_id = _read_path(body, mapping.order_id_path)
        except (KeyError, TypeError):
            raw_order_id = None
        supplier_order_id = (
            str(raw_order_id).strip()
            if isinstance(raw_order_id, (str, int))
            and not isinstance(raw_order_id, bool)
            else ""
        )
        if not supplier_order_id:
            return self._result(
                request,
                PurchaseStatus.INVALID_RESPONSE,
                "supplier response is missing the contracted order id",
                ContractStatus.VERIFIED,
                transport_called=True,
                total_cost=decision.total_cost,
                margin=decision.margin,
            )

        response_cost: Optional[Decimal] = None
        if mapping.purchase_cost_path is not None:
            try:
                raw_cost = _read_path(body, mapping.purchase_cost_path)
            except (KeyError, TypeError):
                return self._result(
                    request,
                    PurchaseStatus.INVALID_RESPONSE,
                    "supplier response is missing the contracted purchase cost",
                    ContractStatus.VERIFIED,
                    transport_called=True,
                    supplier_order_id=supplier_order_id,
                )
            response_cost = _response_cost(raw_cost)
            if response_cost is None:
                return self._result(
                    request,
                    PurchaseStatus.INVALID_RESPONSE,
                    "supplier purchase cost does not match the contract",
                    ContractStatus.VERIFIED,
                    transport_called=True,
                    supplier_order_id=supplier_order_id,
                )

        total_cost = (
            response_cost
            if response_cost is not None
            else decision.total_cost
        )
        margin = (
            request.buyer_paid - total_cost
            if total_cost is not None
            else decision.margin
        )
        if margin is not None and margin < request.minimum_margin:
            return self._result(
                request,
                PurchaseStatus.MARGIN_BELOW_MINIMUM,
                "supplier response cost makes the order margin too low",
                ContractStatus.VERIFIED,
                transport_called=True,
                total_cost=total_cost,
                margin=margin,
                supplier_order_id=supplier_order_id,
            )
        delivery_codes: tuple[str, ...] = ()
        try:
            raw_codes = _read_path(body, mapping.delivery_codes_path)
            if not isinstance(raw_codes, list):
                raise ValueError("delivery codes are not a list")
            cleaned_codes = []
            for raw_code in raw_codes:
                if not isinstance(raw_code, str) or not raw_code.strip():
                    raise ValueError("delivery code is empty")
                cleaned_codes.append(raw_code.strip())
            if len(cleaned_codes) != request.quantity:
                raise ValueError("delivery code count does not match quantity")
            if len(set(cleaned_codes)) != len(cleaned_codes):
                raise ValueError("delivery codes contain duplicates")
            delivery_codes = tuple(cleaned_codes)
        except (KeyError, TypeError, ValueError):
            return self._result(
                request,
                PurchaseStatus.INVALID_RESPONSE,
                "supplier delivery codes do not match the contract",
                ContractStatus.VERIFIED,
                transport_called=True,
                total_cost=total_cost,
                margin=margin,
                supplier_order_id=supplier_order_id,
            )
        return self._result(
            request,
            PurchaseStatus.SUCCEEDED,
            "supplier purchase succeeded",
            ContractStatus.VERIFIED,
            transport_called=True,
            total_cost=total_cost,
            margin=margin,
            supplier_order_id=supplier_order_id,
            delivery_codes=delivery_codes,
        )

    def purchase(self, request: PurchaseRequest) -> PurchaseResult:
        check = self._check_contract()
        fingerprint = _request_fingerprint(request)
        with self._idempotency_lock:
            previous = self._idempotency_records.get(request.idempotency_key)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    return self._result(
                        request,
                        PurchaseStatus.IDEMPOTENCY_CONFLICT,
                        "idempotency_key belongs to a different request",
                        check.status,
                        total_cost=previous.result.total_cost,
                        margin=previous.result.margin,
                    )
                return self._result(
                    request,
                    PurchaseStatus.DUPLICATE,
                    "purchase request was already dispatched",
                    previous.result.contract_status,
                    total_cost=previous.result.total_cost,
                    margin=previous.result.margin,
                    supplier_order_id=previous.result.supplier_order_id,
                    original_status=previous.result.status,
                    delivery_codes=previous.result.delivery_codes,
                )

            contract_status_to_purchase_status = {
                ContractStatus.MISSING: PurchaseStatus.CONTRACT_MISSING,
                ContractStatus.UNVERIFIED: PurchaseStatus.CONTRACT_UNVERIFIED,
                ContractStatus.INCOMPLETE: PurchaseStatus.CONTRACT_INCOMPLETE,
            }
            if check.status is not ContractStatus.VERIFIED:
                return self._result(
                    request,
                    contract_status_to_purchase_status[check.status],
                    check.reason,
                    check.status,
                )
            if self.transport is None:
                return self._result(
                    request,
                    PurchaseStatus.TRANSPORT_UNAVAILABLE,
                    "no supplier transport was injected",
                    check.status,
                )

            evaluated_at = self.clock()
            decision = evaluate_purchase_margin(
                request,
                evaluated_at=evaluated_at,
            )
            if decision.status is not MarginStatus.OK:
                return self._result(
                    request,
                    PurchaseStatus(decision.status.value),
                    decision.message,
                    check.status,
                    total_cost=decision.total_cost,
                    margin=decision.margin,
                )

            contract = self.contract
            if contract is None:
                return self._result(
                    request,
                    PurchaseStatus.CONTRACT_MISSING,
                    "supplier contract is missing",
                    ContractStatus.MISSING,
                    total_cost=decision.total_cost,
                    margin=decision.margin,
                )
            try:
                outbound = self._build_supplier_request(request, contract)
            except Exception as exc:
                return self._result(
                    request,
                    PurchaseStatus.REQUEST_BUILD_FAILED,
                    f"supplier request build failed: {type(exc).__name__}",
                    check.status,
                    total_cost=decision.total_cost,
                    margin=decision.margin,
                )
            try:
                outbound = self._apply_signature(outbound, contract)
            except Exception as exc:
                return self._result(
                    request,
                    PurchaseStatus.SIGNING_ERROR,
                    f"supplier signing failed: {type(exc).__name__}",
                    check.status,
                    total_cost=decision.total_cost,
                    margin=decision.margin,
                )

            in_flight = self._result(
                request,
                PurchaseStatus.SUBMISSION_UNKNOWN,
                "supplier request is in flight",
                check.status,
                transport_called=True,
                total_cost=decision.total_cost,
                margin=decision.margin,
            )
            self._idempotency_records[request.idempotency_key] = _IdempotencyRecord(
                fingerprint=fingerprint,
                result=in_flight,
            )

            try:
                response = self.transport.send(outbound)
            except Exception as exc:
                result = replace(
                    in_flight,
                    message=(
                        "supplier transport failed after dispatch; "
                        f"status is unknown: {type(exc).__name__}"
                    ),
                )
                self._idempotency_records[request.idempotency_key] = _IdempotencyRecord(
                    fingerprint=fingerprint,
                    result=result,
                )
                return result

            if not isinstance(response, SupplierHttpResponse):
                result = replace(
                    in_flight,
                    status=PurchaseStatus.INVALID_RESPONSE,
                    message="supplier transport returned an invalid response type",
                )
            else:
                result = self._normalize_response(
                    request,
                    response,
                    decision,
                    contract,
                )
            self._idempotency_records[request.idempotency_key] = _IdempotencyRecord(
                fingerprint=fingerprint,
                result=result,
            )
            return result


class BaiZhouSupplierAdapter(BaseSupplierAdapter):
    """白舟 adapter placeholder with no assumed endpoint or payload schema."""

    @property
    def supplier_code(self) -> str:
        return "baizhou"

    @property
    def supplier_label(self) -> str:
        return "白舟"


class FuLuSupplierAdapter(BaseSupplierAdapter):
    """福禄 adapter placeholder with no assumed endpoint or payload schema."""

    @property
    def supplier_code(self) -> str:
        return "fulu"

    @property
    def supplier_label(self) -> str:
        return "福禄"


class Card91CloudWarehouseAdapter(BaseSupplierAdapter):
    """91卡券云仓 placeholder for the supplied get-all-goods endpoint."""

    @property
    def supplier_code(self) -> str:
        return CARD91_CLOUD_WAREHOUSE_CODE

    @property
    def supplier_label(self) -> str:
        return "91卡券云仓"

    @property
    def required_http_method(self) -> Optional[str]:
        return "POST"

    @property
    def required_endpoint_path(self) -> Optional[str]:
        return "/dockapi/v3/getallgoods"


__all__ = [
    "BaiZhouSupplierAdapter",
    "BaseSupplierAdapter",
    "CapabilityStatus",
    "Card91CloudWarehouseAdapter",
    "CARD91_CLOUD_WAREHOUSE_CODE",
    "ContractStatus",
    "CostQuote",
    "FixedRequestValue",
    "FuLuSupplierAdapter",
    "MarginDecision",
    "MarginStatus",
    "PurchaseRequest",
    "PurchaseResult",
    "PurchaseStatus",
    "RequestFieldMapping",
    "RequestFieldSource",
    "RequestMapping",
    "ResponseMapping",
    "SignatureContract",
    "SignatureInput",
    "SupplierCapability",
    "SupplierContract",
    "SupplierHttpRequest",
    "SupplierHttpResponse",
    "SupplierTransport",
    "evaluate_purchase_margin",
]
