from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from opportunity_automation import FulfillmentOrchestrator, FulfillmentStore  # noqa: E402
from supplier_adapters import (  # noqa: E402
    BaiZhouSupplierAdapter,
    CostQuote,
    PurchaseRequest,
    RequestFieldMapping,
    RequestFieldSource,
    RequestMapping,
    ResponseMapping,
    SupplierContract,
    SupplierHttpRequest,
    SupplierHttpResponse,
)


NOW = datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)


class FakeDatabase:
    def __init__(self):
        self.cookies_by_user = {7: {"shop-a": {"enabled": True}}}
        self.cards_by_user = {
            7: {
                11: {
                    "id": 11,
                    "name": "视频会员日卡",
                    "type": "data",
                    "data_content": "A\nB",
                    "text_content": "",
                    "image_url": "",
                    "api_config": "",
                    "enabled": True,
                }
            }
        }
        self.items = {}
        self.settings = {}

    def get_all_cookies(self, user_id):
        return self.cookies_by_user.get(int(user_id), {})

    def get_card_by_id(self, card_id, user_id):
        return self.cards_by_user.get(int(user_id), {}).get(int(card_id))

    def save_item_basic_info(
        self,
        cookie_id,
        item_id,
        item_title=None,
        item_description=None,
        item_category=None,
        item_price=None,
        item_detail=None,
        item_image=None,
    ):
        key = (cookie_id, str(item_id))
        self.items[key] = {
            "cookie_id": cookie_id,
            "item_id": str(item_id),
            "item_title": item_title or "",
            "item_description": item_description or "",
            "item_category": item_category or "",
            "item_price": item_price or "",
            "item_detail": item_detail or "",
            "item_image": item_image or "",
        }
        return True

    def get_item_info(self, cookie_id, item_id):
        return self.items.get((cookie_id, str(item_id)))

    def update_item_delivery_settings(
        self,
        cookie_id,
        item_id,
        ai_reply_enabled=None,
        auto_delivery_enabled=True,
        delivery_card_id=None,
    ):
        key = (cookie_id, str(item_id))
        if key not in self.items:
            return False
        self.settings[key] = {
            "ai_reply_enabled": ai_reply_enabled,
            "auto_delivery_enabled": bool(auto_delivery_enabled),
            "delivery_card_id": delivery_card_id,
        }
        return True

    def get_item_delivery_settings(self, cookie_id, item_id):
        return self.settings.get(
            (cookie_id, str(item_id)),
            {
                "ai_reply_enabled": None,
                "auto_delivery_enabled": True,
                "delivery_card_id": None,
            },
        )


class FakeIdempotency:
    def __init__(self):
        self.records = {}

    def make_key(self, action, *parts):
        return f"{action}:{'|'.join(str(part) for part in parts)}"

    def precheck(self, key):
        record = self.records.get(key)
        return {"exists": bool(record), "record": record}

    def claim(self, key, action="", note=""):
        if key in self.records:
            return {"claimed": False, "record": self.records[key], "blocked": False}
        record = {"status": "pending", "note": note}
        self.records[key] = record
        return {"claimed": True, "record": record, "blocked": False}

    def finish(self, key, status="done", note=""):
        self.records[key] = {"status": status, "note": note}
        return True

    def release(self, key):
        self.records.pop(key, None)
        return True


class FakeRateLimiter:
    def acquire(self, action, key, limit=1, window=60):
        return True, 0


class DenyOnceRateLimiter:
    def __init__(self):
        self.calls = 0

    def acquire(self, action, key, limit=1, window=60):
        self.calls += 1
        if self.calls == 1:
            return False, 60
        return True, 0


class PassingGuard:
    @staticmethod
    def check_publish(*args, **kwargs):
        return {"ok": True, "warnings": [], "block": []}


class RecordingTransport:
    def __init__(self, response=None, error=None, timeline=None):
        self.response = response
        self.error = error
        self.timeline = timeline
        self.requests: list[SupplierHttpRequest] = []

    def send(self, request):
        self.requests.append(request)
        if self.timeline is not None:
            self.timeline.append("supplier_transport")
        if self.error is not None:
            raise self.error
        return self.response


def supplier_response() -> SupplierHttpResponse:
    return SupplierHttpResponse(
        status_code=200,
        body=json.dumps(
            {
                "result": {
                    "status": "ok",
                    "order_id": "supplier-order-9001",
                    "message": "accepted",
                    "delivery_codes": ["fixture-code-a", "fixture-code-b"],
                }
            }
        ).encode("utf-8"),
    )


def verified_contract() -> SupplierContract:
    return SupplierContract(
        supplier_code="baizhou",
        endpoint_url="https://supplier.example/purchase",
        http_method="POST",
        request_mapping=RequestMapping(
            fields=(
                RequestFieldMapping(
                    source=RequestFieldSource.REQUEST_ID,
                    target_path=("order", "request_id"),
                ),
                RequestFieldMapping(
                    source=RequestFieldSource.IDEMPOTENCY_KEY,
                    target_path=("order", "idempotency_key"),
                ),
                RequestFieldMapping(
                    source=RequestFieldSource.SUPPLIER_SKU,
                    target_path=("order", "supplier_sku"),
                ),
                RequestFieldMapping(
                    source=RequestFieldSource.QUANTITY,
                    target_path=("order", "quantity"),
                ),
            )
        ),
        response_mapping=ResponseMapping(
            success_http_statuses=(200,),
            status_path=("result", "status"),
            success_status_values=("ok",),
            order_id_path=("result", "order_id"),
            message_path=("result", "message"),
            delivery_codes_path=("result", "delivery_codes"),
        ),
        verified=True,
        verification_reference="fixture-contract-v1",
    )


def purchase_request(task):
    return PurchaseRequest(
        request_id=f"request-{task['id']}",
        idempotency_key=f"purchase:{task['id']}",
        supplier_sku="fixture-sku-001",
        quantity=task["payload"]["quantity"],
        buyer_paid=Decimal(str(task["payload"]["price"])),
        currency="CNY",
        costs=(
            CostQuote(
                key="supplier_total",
                total_amount=Decimal("8.00"),
                currency="CNY",
                quoted_at=NOW,
            ),
        ),
        required_cost_keys=("supplier_total",),
        minimum_margin=Decimal("1.00"),
        maximum_quote_age=timedelta(minutes=5),
    )


def make_payload():
    return {
        "cookie_id": "shop-a",
        "card_id": 11,
        "title": "视频会员日卡自动发货",
        "desc": "下单后自动发送，按页面说明使用。",
        "imageUrls": ["https://img.example/item.jpg"],
        "price": 9.9,
        "quantity": 2,
        "delivery": "无需邮寄",
        "postPrice": 0,
        "tags": [],
        "source": {
            "title": "视频会员日卡",
            "price": 4.5,
            "supplier": "测试上游",
            "url": "https://supplier.example/item",
            "source": "fixture",
            "source_label": "测试报价",
            "source_url": "https://supplier.example",
            "updated_at": "2026-11-17T10:00:00+08:00",
        },
    }


class FulfillmentSupplierIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FulfillmentStore(Path(self.temp.name) / "fulfillment.db")
        self.db = FakeDatabase()
        self.publisher_calls = []
        self.readback_calls = []
        self.timeline = []

    def publisher(self, cookie_str, item_data, app_key, **kwargs):
        self.publisher_calls.append((cookie_str, item_data, app_key, kwargs))
        self.timeline.append("publisher")
        return {"success": True, "itemId": "9001"}

    def readback(self, cookie_str, app_key, cookie_id="", max_pages=2):
        self.readback_calls.append((cookie_str, app_key, cookie_id, max_pages))
        return {
            "ok": True,
            "items": [
                {
                    "item_id": "9001",
                    "title": "视频会员日卡自动发货",
                    "price": 9.9,
                    "status": "在售",
                    "category": "virtual",
                    "image_url": "https://img.example/item.jpg",
                }
            ],
        }

    def make_service(
        self,
        *,
        supplier_adapter=None,
        supplier_request_factory=None,
        rate_limiter=None,
        publisher=None,
    ):
        return FulfillmentOrchestrator(
            store=self.store,
            db=self.db,
            publisher=publisher or self.publisher,
            readback=self.readback,
            resolve_cookie=lambda cookie_id: "unb=1",
            app_key="test-key",
            idempotency_api=FakeIdempotency(),
            rate_limiter_api=rate_limiter or FakeRateLimiter(),
            publish_guard_api=PassingGuard(),
            night_ban_ok=lambda: (True, ""),
            sleep_fn=lambda _seconds: None,
            readback_attempts=1,
            supplier_adapter=supplier_adapter,
            supplier_request_factory=supplier_request_factory,
        )

    def test_blocked_supplier_contract_creates_failed_task_without_execution(self):
        transport = RecordingTransport(response=supplier_response())
        adapter = BaiZhouSupplierAdapter(transport=transport, clock=lambda: NOW)
        service = self.make_service(
            supplier_adapter=adapter,
            supplier_request_factory=purchase_request,
        )

        task = service.create_task(7, make_payload())

        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["stage"], "supplier_preflight")
        self.assertTrue(task["error"])
        procurement = task["result"]["supplier_procurement"]
        self.assertIs(procurement["configured"], True)
        self.assertEqual(procurement["mode"], "supplier_adapter_blocked")
        self.assertEqual(procurement["message"], "supplier contract is missing")
        self.assertEqual(procurement["supplier_code"], "baizhou")
        self.assertEqual(procurement["supplier_label"], "白舟")
        self.assertEqual(procurement["capability_status"], "blocked")
        self.assertEqual(procurement["contract_status"], "missing")
        self.assertEqual(procurement["reason"], "supplier contract is missing")

        self.assertEqual(service.execute_task(task["id"])["status"], "failed")
        self.assertEqual(transport.requests, [])
        self.assertEqual(self.publisher_calls, [])

    def test_available_contract_without_request_factory_fails_closed(self):
        transport = RecordingTransport(response=supplier_response())
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )
        service = self.make_service(supplier_adapter=adapter)

        task = service.create_task(7, make_payload())

        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["stage"], "supplier_preflight")
        procurement = task["result"]["supplier_procurement"]
        self.assertIs(procurement["configured"], True)
        self.assertEqual(procurement["mode"], "supplier_request_factory_required")
        self.assertEqual(
            procurement["message"],
            "供应商采购请求构造器未注入，未进入自动发布",
        )
        self.assertEqual(procurement["capability_status"], "available")
        self.assertEqual(procurement["contract_status"], "verified")
        self.assertEqual(transport.requests, [])
        self.assertEqual(self.publisher_calls, [])

    def test_stale_quote_blocks_transport_and_publish(self):
        transport = RecordingTransport(response=supplier_response())
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )

        def stale_request(task):
            return replace(
                purchase_request(task),
                costs=(
                    CostQuote(
                        key="supplier_total",
                        total_amount=Decimal("8.00"),
                        currency="CNY",
                        quoted_at=NOW - timedelta(minutes=6),
                    ),
                ),
            )

        service = self.make_service(
            supplier_adapter=adapter,
            supplier_request_factory=stale_request,
        )
        task = service.create_task(7, make_payload())
        self.assertEqual(task["status"], "ready_to_publish")

        task = service.execute_task(task["id"])

        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["stage"], "supplier_purchase")
        purchase = task["result"]["supplier_purchase"]
        self.assertEqual(purchase["status"], "quote_stale")
        self.assertIs(purchase["transport_called"], False)
        self.assertTrue(task["error"])
        self.assertEqual(transport.requests, [])
        self.assertEqual(self.publisher_calls, [])
        self.assertEqual(self.db.settings, {})

    def test_margin_below_minimum_blocks_transport_and_publish(self):
        transport = RecordingTransport(response=supplier_response())
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )

        def low_margin_request(task):
            return replace(
                purchase_request(task),
                costs=(
                    CostQuote(
                        key="supplier_total",
                        total_amount=Decimal("8.91"),
                        currency="CNY",
                        quoted_at=NOW,
                    ),
                ),
            )

        service = self.make_service(
            supplier_adapter=adapter,
            supplier_request_factory=low_margin_request,
        )
        task = service.create_task(7, make_payload())

        task = service.execute_task(task["id"])

        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["stage"], "supplier_purchase")
        purchase = task["result"]["supplier_purchase"]
        self.assertEqual(purchase["status"], "margin_below_minimum")
        self.assertEqual(purchase["total_cost"], "8.91")
        self.assertEqual(purchase["margin"], "0.99")
        self.assertIs(purchase["transport_called"], False)
        self.assertEqual(transport.requests, [])
        self.assertEqual(self.publisher_calls, [])

    def test_transport_timeout_becomes_unknown_and_blocks_publish(self):
        transport = RecordingTransport(error=TimeoutError("fixture timeout"))
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )
        service = self.make_service(
            supplier_adapter=adapter,
            supplier_request_factory=purchase_request,
        )
        task = service.create_task(7, make_payload())

        task = service.execute_task(task["id"])

        self.assertEqual(task["status"], "submission_unknown_review_required")
        self.assertEqual(task["stage"], "supplier_purchase")
        purchase = task["result"]["supplier_purchase"]
        self.assertEqual(purchase["status"], "submission_unknown")
        self.assertIs(purchase["transport_called"], True)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(self.publisher_calls, [])
        self.assertEqual(self.db.settings, {})

    def test_persisted_in_flight_purchase_blocks_restart_without_network(self):
        first_transport = RecordingTransport(response=supplier_response())
        first_adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=first_transport,
            clock=lambda: NOW,
        )
        first_service = self.make_service(
            supplier_adapter=first_adapter,
            supplier_request_factory=purchase_request,
        )
        task = first_service.create_task(7, make_payload())
        result = dict(task["result"])
        result["supplier_purchase"] = {
            "request_id": f"request-{task['id']}",
            "idempotency_key": f"purchase:{task['id']}",
            "supplier_code": "baizhou",
            "status": "in_flight",
            "message": "simulated process stop after dispatch",
            "transport_called": False,
            "started_at": NOW.isoformat(),
        }
        self.store.update_task(
            task["id"],
            status="ready_to_publish",
            stage="supplier_purchase",
            result=result,
        )

        second_transport = RecordingTransport(response=supplier_response())
        second_adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=second_transport,
            clock=lambda: NOW,
        )
        second_service = self.make_service(
            supplier_adapter=second_adapter,
            supplier_request_factory=purchase_request,
        )

        task = second_service.execute_task(task["id"])

        self.assertEqual(task["status"], "submission_unknown_review_required")
        self.assertEqual(task["stage"], "supplier_purchase")
        self.assertEqual(
            task["result"]["supplier_purchase"]["status"],
            "submission_unknown",
        )
        self.assertEqual(second_transport.requests, [])
        self.assertEqual(self.publisher_calls, [])

    def test_purchase_is_persisted_before_publish_and_reaches_delivery_ready(self):
        transport = RecordingTransport(
            response=supplier_response(),
            timeline=self.timeline,
        )
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )
        task_id_holder = {}
        purchase_at_publish = []

        def publisher(cookie_str, item_data, app_key, **kwargs):
            current = self.store.get_task(str(task_id_holder["task_id"]))
            purchase_at_publish.append(
                ((current or {}).get("result") or {}).get("supplier_purchase")
            )
            return self.publisher(cookie_str, item_data, app_key, **kwargs)

        service = self.make_service(
            supplier_adapter=adapter,
            supplier_request_factory=purchase_request,
            publisher=publisher,
        )
        task = service.create_task(7, make_payload())
        task_id_holder["task_id"] = task["id"]

        procurement = task["result"]["supplier_procurement"]
        self.assertEqual(procurement["mode"], "supplier_adapter_available")
        self.assertEqual(
            procurement["message"],
            "供应商适配器与采购请求构造器已就绪",
        )
        self.assertEqual(procurement["capability_status"], "available")
        self.assertEqual(procurement["contract_status"], "verified")

        task = service.execute_task(task["id"])

        self.assertEqual(task["status"], "delivery_ready")
        self.assertEqual(task["item_id"], "9001")
        purchase = task["result"]["supplier_purchase"]
        self.assertEqual(purchase["status"], "succeeded")
        self.assertEqual(purchase["supplier_order_id"], "supplier-order-9001")
        self.assertEqual(purchase["total_cost"], "8.00")
        self.assertEqual(purchase["margin"], "1.90")
        self.assertIs(purchase["transport_called"], True)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(len(self.publisher_calls), 1)
        self.assertEqual(self.timeline, ["supplier_transport", "publisher"])
        self.assertEqual(len(purchase_at_publish), 1)
        self.assertEqual(purchase_at_publish[0]["status"], "succeeded")
        self.assertEqual(
            purchase_at_publish[0]["supplier_order_id"],
            "supplier-order-9001",
        )

    def test_persisted_successful_purchase_is_not_repeated_after_restart(self):
        first_transport = RecordingTransport(response=supplier_response())
        first_adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=first_transport,
            clock=lambda: NOW,
        )
        first_service = self.make_service(
            supplier_adapter=first_adapter,
            supplier_request_factory=purchase_request,
            rate_limiter=DenyOnceRateLimiter(),
        )
        task = first_service.create_task(7, make_payload())
        task = first_service.execute_task(task["id"])

        self.assertEqual(task["status"], "publish_throttled")
        self.assertEqual(len(first_transport.requests), 1)
        self.assertEqual(
            task["result"]["supplier_purchase"]["supplier_order_id"],
            "supplier-order-9001",
        )

        second_transport = RecordingTransport(response=supplier_response())
        second_adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=second_transport,
            clock=lambda: NOW,
        )
        second_service = self.make_service(
            supplier_adapter=second_adapter,
            supplier_request_factory=purchase_request,
        )

        task = second_service.execute_task(task["id"])

        self.assertEqual(task["status"], "delivery_ready")
        self.assertEqual(second_transport.requests, [])
        self.assertEqual(len(first_transport.requests), 1)
        self.assertEqual(len(self.publisher_calls), 1)

    def test_duplicate_successful_purchase_can_continue_to_publish(self):
        transport = RecordingTransport(response=supplier_response())
        adapter = BaiZhouSupplierAdapter(
            contract=verified_contract(),
            transport=transport,
            clock=lambda: NOW,
        )
        service = self.make_service(
            supplier_adapter=adapter,
            supplier_request_factory=purchase_request,
        )
        task = service.create_task(7, make_payload())
        primed = adapter.purchase(purchase_request(task))
        self.assertEqual(primed.status.value, "succeeded")
        self.assertEqual(len(transport.requests), 1)

        task = service.execute_task(task["id"])

        self.assertEqual(task["status"], "delivery_ready")
        purchase = task["result"]["supplier_purchase"]
        self.assertEqual(purchase["status"], "duplicate")
        self.assertEqual(purchase["original_status"], "succeeded")
        self.assertEqual(purchase["supplier_order_id"], "supplier-order-9001")
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(len(self.publisher_calls), 1)

    def test_without_supplier_adapter_api_card_still_reaches_delivery_ready(self):
        self.db.cards_by_user[7][11].update(
            {
                "type": "api",
                "data_content": "",
                "api_config": {"url": "https://api.example/card"},
            }
        )
        service = self.make_service()

        task = service.create_task(7, make_payload())

        self.assertEqual(
            task["result"]["supplier_procurement"],
            {
                "configured": False,
                "mode": "supplier_adapter_required",
                "message": (
                    "尚未取得供应商下单接口契约；"
                    "当前使用已绑定的本地或 API 卡券履约"
                ),
            },
        )
        self.assertEqual(task["status"], "ready_to_publish")

        task = service.execute_task(task["id"])

        self.assertEqual(task["status"], "delivery_ready")
        self.assertEqual(len(self.publisher_calls), 1)


if __name__ == "__main__":
    unittest.main()
