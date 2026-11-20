from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from opportunity_automation import (  # noqa: E402
    FulfillmentOrchestrator,
    FulfillmentOwnershipError,
    FulfillmentStore,
    card_inventory,
)


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
        self.saved_items = []

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
        self.saved_items.append(key)
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


class PassingGuard:
    @staticmethod
    def check_publish(*args, **kwargs):
        return {"ok": True, "warnings": [], "block": []}


class UnknownSubmissionError(Exception):
    submission_status = "submission_unknown"
    stage = "submit"


def make_payload(**overrides):
    payload = {
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
    payload.update(overrides)
    return payload


class FulfillmentInventoryTests(unittest.TestCase):
    def test_all_confirmed_card_types_can_be_ready(self):
        cases = [
            (
                {"type": "data", "enabled": True, "data_content": "A\nB"},
                {"ready": True, "kind": "data", "available": 2},
            ),
            (
                {"type": "text", "enabled": True, "text_content": "使用说明"},
                {"ready": True, "kind": "text"},
            ),
            (
                {"type": "image", "enabled": True, "image_url": "https://img.example/card.png"},
                {"ready": True, "kind": "image"},
            ),
            (
                {
                    "type": "api",
                    "enabled": True,
                    "api_config": {"url": "https://api.example/card"},
                },
                {"ready": True, "kind": "api"},
            ),
        ]
        for card, expected in cases:
            with self.subTest(card_type=card["type"]):
                result = card_inventory(card)
                for key, value in expected.items():
                    self.assertEqual(result[key], value)

    def test_empty_disabled_and_unknown_cards_are_not_ready(self):
        cases = [
            {"type": "data", "enabled": False, "data_content": "A"},
            {"type": "data", "enabled": True, "data_content": "\n"},
            {"type": "text", "enabled": True, "text_content": " "},
            {"type": "image", "enabled": True, "image_url": ""},
            {"type": "api", "enabled": True, "api_config": {}},
            {"type": "unknown", "enabled": True},
        ]
        for card in cases:
            with self.subTest(card=card):
                self.assertFalse(card_inventory(card)["ready"])


class FulfillmentOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FulfillmentStore(Path(self.temp.name) / "fulfillment.db")
        self.db = FakeDatabase()
        self.idem = FakeIdempotency()
        self.publisher_calls = []
        self.readback_calls = []

    def publisher(self, cookie_str, item_data, app_key, **kwargs):
        self.publisher_calls.append((cookie_str, item_data, app_key, kwargs))
        return {
            "success": True,
            "itemId": "9001",
            "status": "submitted",
            "requires_readback": True,
        }

    def readback(self, cookie_str, app_key, cookie_id="", max_pages=2):
        self.readback_calls.append((cookie_str, app_key, cookie_id, max_pages))
        return {
            "ok": True,
            "items": [
                {
                    "item_id": "9999",
                    "title": "其他商品",
                    "price": 1.0,
                    "status": "在售",
                    "category": "other",
                    "image_url": "https://img.example/other.jpg",
                },
                {
                    "item_id": "9001",
                    "title": "视频会员日卡自动发货",
                    "price": 9.9,
                    "status": "在售",
                    "category": "virtual",
                    "image_url": "https://img.example/item.jpg",
                },
            ],
        }

    def make_service(self, publisher=None, readback=None, readback_attempts=1):
        return FulfillmentOrchestrator(
            store=self.store,
            db=self.db,
            publisher=publisher or self.publisher,
            readback=readback or self.readback,
            resolve_cookie=lambda cookie_id: "unb=1; _m_h5_tk=token_1",
            app_key="test-key",
            idempotency_api=self.idem,
            rate_limiter_api=FakeRateLimiter(),
            publish_guard_api=PassingGuard(),
            night_ban_ok=lambda: (True, ""),
            sleep_fn=lambda _seconds: None,
            readback_attempts=readback_attempts,
        )

    def test_create_rejects_unowned_account(self):
        service = self.make_service()
        payload = make_payload(cookie_id="another-user-shop")
        with self.assertRaises(FulfillmentOwnershipError):
            service.create_task(7, payload)
        self.assertEqual(self.store.list_tasks(7), [])

    def test_create_rejects_unowned_or_disabled_card(self):
        service = self.make_service()
        with self.assertRaises(FulfillmentOwnershipError):
            service.create_task(7, make_payload(card_id=999))

        self.db.cards_by_user[7][11]["enabled"] = False
        with self.assertRaises(FulfillmentOwnershipError):
            service.create_task(7, make_payload())
        self.assertEqual(self.store.list_tasks(7), [])

    def test_readback_without_exact_item_id_never_binds_card(self):
        def empty_readback(cookie_str, app_key, cookie_id="", max_pages=2):
            self.readback_calls.append((cookie_str, app_key, cookie_id, max_pages))
            return {"ok": True, "items": []}

        service = self.make_service(readback=empty_readback)
        task = service.create_task(7, make_payload())
        task = service.execute_task(task["id"])

        self.assertEqual(task["status"], "readback_not_visible")
        self.assertEqual(self.publisher_calls.__len__(), 1)
        self.assertEqual(self.readback_calls.__len__(), 1)
        self.assertEqual(self.db.settings, {})

    def test_unknown_submission_never_republishes_or_binds(self):
        def unknown_publisher(*args, **kwargs):
            self.publisher_calls.append((args, kwargs))
            raise UnknownSubmissionError("提交已发出，结果未知")

        service = self.make_service(publisher=unknown_publisher)
        task = service.create_task(7, make_payload())
        task = service.execute_task(task["id"])
        self.assertEqual(task["status"], "submission_unknown_review_required")

        task = service.recover_task(task["id"])
        self.assertEqual(task["status"], "submission_unknown_review_required")
        self.assertEqual(len(self.publisher_calls), 1)
        self.assertEqual(self.db.settings, {})

    def test_success_binds_only_the_exact_item_id_after_readback(self):
        service = self.make_service()
        task = service.create_task(7, make_payload())
        task = service.execute_task(task["id"])

        self.assertEqual(task["status"], "delivery_ready")
        self.assertEqual(task["item_id"], "9001")
        self.assertEqual(self.db.saved_items, [("shop-a", "9001")])
        self.assertEqual(
            self.db.settings[("shop-a", "9001")],
            {
                "ai_reply_enabled": None,
                "auto_delivery_enabled": True,
                "delivery_card_id": 11,
            },
        )
        record = self.store.get_task(task["id"], user_id=7)
        detail = record["item_detail"]
        self.assertEqual(detail["item_id"], "9001")

    def test_inventory_becoming_empty_before_binding_blocks_delivery_ready(self):
        service = self.make_service()
        task = service.create_task(7, make_payload())
        self.db.cards_by_user[7][11]["data_content"] = ""

        task = service.execute_task(task["id"])
        self.assertEqual(task["status"], "inventory_not_ready")
        self.assertEqual(self.db.settings, {})

        self.db.cards_by_user[7][11]["data_content"] = "C\nD"
        task = service.recover_task(task["id"])
        self.assertEqual(task["status"], "inventory_not_ready")
        task = service.execute_task(task["id"])
        self.assertEqual(task["status"], "delivery_ready")
        self.assertEqual(
            self.db.settings[("shop-a", "9001")]["delivery_card_id"],
            11,
        )

    def test_one_code_cannot_publish_five_units(self):
        self.db.cards_by_user[7][11]["data_content"] = "A\nA"
        service = self.make_service()
        task = service.create_task(7, make_payload(quantity=5))
        self.assertEqual(task["status"], "inventory_not_ready")
        self.assertEqual(task["result"]["inventory"]["available"], 1)
        self.assertEqual(task["error"], "")
        self.assertEqual(service.execute_task(task["id"])["status"], "inventory_not_ready")
        self.assertEqual(self.publisher_calls, [])

    def test_stock_decreasing_before_execution_blocks_publish(self):
        service = self.make_service()
        task = service.create_task(7, make_payload())
        self.db.cards_by_user[7][11]["data_content"] = "A"
        task = service.execute_task(task["id"])
        self.assertEqual(task["status"], "inventory_not_ready")
        self.assertEqual(self.publisher_calls, [])

    def test_stock_decreasing_during_readback_blocks_binding(self):
        def readback(*args, **kwargs):
            if not self.readback_calls:
                self.db.cards_by_user[7][11]["data_content"] = "B"
            return self.readback(*args, **kwargs)

        service = self.make_service(readback=readback)
        task = service.create_task(7, make_payload())
        task = service.execute_task(task["id"])
        self.assertEqual(task["status"], "inventory_not_ready")
        self.assertEqual(task["item_id"], "9001")
        self.assertEqual(self.db.settings, {})
        self.db.cards_by_user[7][11]["data_content"] = "B\nC"
        task = service.execute_task(task["id"])
        self.assertEqual(task["status"], "delivery_ready")
        self.assertEqual(len(self.publisher_calls), 1)

    def test_stale_publishing_without_item_id_requires_manual_review(self):
        service = self.make_service()
        task = service.create_task(7, make_payload())
        self.store.update_task(
            task["id"],
            status="publishing",
            stage="submit",
            message="进程在提交阶段停止",
        )

        recovered = service.recover_task(task["id"])
        self.assertEqual(
            recovered["status"],
            "submission_unknown_review_required",
        )
        self.assertEqual(self.publisher_calls, [])


if __name__ == "__main__":
    unittest.main()
