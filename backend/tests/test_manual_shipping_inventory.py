from __future__ import annotations

import ast
import asyncio
import os
import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, Mock, patch

from fastapi import Body, Depends, HTTPException
from loguru import logger

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

with patch.dict(os.environ, {"DB_PATH": ":memory:", "SQL_LOG_ENABLED": "false"}):
    import db_manager

import delivery_reliability as ledger
import rate_limiter
import ship_verify
import spec_gate


def delivered(*codes):
    return [
        f"{code}\n\n{ledger.SELF_SERVICE_RESEND_HINT}"
        for code in codes
    ]


def load_functions(file_name, names, globals_dict, class_name=None):
    tree = ast.parse((BACKEND / file_name).read_text(encoding="utf-8"))
    nodes = tree.body
    if class_name:
        nodes = next(node.body for node in nodes if isinstance(node, ast.ClassDef) and node.name == class_name)
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            exec(compile(ast.Module(body=[node], type_ignores=[]), file_name, "exec"), globals_dict)


class ManualShippingInventoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for name, filename in (("DB_PATH", "ledger.sqlite"), ("CONFIG_PATH", "ledger.json")):
            mock = patch.object(ledger, name, Path(temporary.name) / filename)
            mock.start()
            self.addCleanup(mock.stop)
        with patch.dict(os.environ, {"SQL_LOG_ENABLED": "false"}):
            self.database = db_manager.DBManager(":memory:")
        self.addCleanup(self.database.close)
        for module, id_name, value in (
            (db_manager, "db_manager", self.database),
            (ship_verify, "verify", Mock(return_value={"ok": True})),
            (rate_limiter, "gate_write", Mock(return_value=(True, ""))),
        ):
            mock = patch.object(module, id_name, value)
            mock.start()
            self.addCleanup(mock.stop)

        self.account, self.item, self.order, self.buyer = "shop", "item", "order", "buyer"
        self.user = {"user_id": 1}
        self.database.save_cookie(self.account, "fixture-cookie", 1)
        self.database.save_item_basic_info(self.account, self.item, "月卡", item_detail="月卡内容")
        self.card_id = self.database.create_card("月卡", "data", user_id=1, data_content="A\nB\nC\nD")
        self.database.update_item_delivery_settings(self.account, self.item, True, True, self.card_id)
        self.database.update_item_multi_quantity_delivery_status(self.account, self.item, True)
        self.database.insert_or_update_order(
            self.order, item_id=self.item, buyer_id=self.buyer, cookie_id=self.account,
            quantity="3", order_status="pending_ship", chat_id="chat",
        )

        globals_dict = {**vars(spec_gate), "asyncio": asyncio, "db_manager": self.database, "logger": logger, "os": os}
        names = {
            "_auto_delivery", "_process_delivery_content_with_description", "_send_delivery_card",
            "_prepare_delivery_contents", "deliver_order_manually", "_safe_str",
        }
        load_functions("XianyuAutoAsync.py", names, globals_dict, "XianyuLive")
        live_class = type("Live", (), {name: globals_dict[name] for name in names if name in globals_dict})
        self.live = live_class()
        self.live.cookie_id = self.account
        self.live.cookies_str = "fixture-cookie"
        self.live.ws = SimpleNamespace(closed=False)
        self.live._order_locks = defaultdict(asyncio.Lock)
        self.live.order_status_handler = None
        self.live.save_item_info_to_db = AsyncMock()
        self.live.fetch_order_detail_info = AsyncMock(return_value={"quantity": "3"})
        self.live.send_msg = AsyncMock()
        self.live._supplier_delivery_procurement = SimpleNamespace(
            store=SimpleNamespace(get_binding=Mock(return_value=None)),
            procure=Mock(),
        )
        self.live.mark_delivery_sent = Mock()
        self.live._confirm_platform_shipped = AsyncMock(return_value=True)
        self.modules = patch.dict(sys.modules, {
            "XianyuAutoAsync": SimpleNamespace(XianyuLive=SimpleNamespace(get_instance=lambda _: self.live)),
            "cookie_manager": SimpleNamespace(manager=None),
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.routes = {
            "Any": Any, "Body": Body, "Depends": Depends, "Dict": Dict, "HTTPException": HTTPException,
            "List": List, "Optional": Optional, "get_current_user": lambda: self.user,
            "log_with_user": Mock(), "cookie_manager": sys.modules["cookie_manager"],
        }
        load_functions("reply_server.py", {"manual_ship_orders"}, self.routes)

    async def ship(self):
        response = await self.routes["manual_ship_orders"]([self.order], "full_delivery", None, self.user)
        return response["results"][0]

    def inventory(self):
        return self.database.get_card_by_id(self.card_id, 1)["data_content"]

    def sent_messages(self):
        return [call.args[-1] for call in self.live.send_msg.await_args_list]

    async def test_stock_shortage_sends_nothing_and_keeps_stock(self):
        self.database.update_card(self.card_id, data_content="A")
        result = await self.ship()
        self.assertFalse(result["success"], result)
        self.assertEqual(result["reason_code"], "stock_shortage")
        self.assertEqual(self.sent_messages(), [])
        self.assertEqual(self.inventory(), "A")
        self.assertFalse(self.database.is_order_system_shipped(self.order))
        self.live._confirm_platform_shipped.assert_not_awaited()
        self.assertTrue(self.database.append_card_inventory(self.card_id, 1, "B\nC"))
        self.assertTrue((await self.ship())["success"])
        self.assertEqual(self.sent_messages(), delivered("A", "B", "C"))
        self.assertEqual(self.inventory(), "")

    async def test_string_quantity_delivers_three_and_confirms_once(self):
        result = await self.ship()
        self.assertTrue(result["success"], result)
        self.assertEqual(self.sent_messages(), delivered("A", "B", "C"))
        self.assertEqual(self.inventory(), "D")
        self.assertTrue(self.database.is_order_system_shipped(self.order))
        self.assertFalse((await self.ship())["success"])
        self.assertEqual(self.sent_messages(), delivered("A", "B", "C"))
        self.live._confirm_platform_shipped.assert_awaited_once()

    async def test_partial_ledger_only_purchases_missing_position_and_rereads_codes(self):
        ledger.ensure_order(
            self.order,
            self.account,
            ["LOCAL-1", "LOCAL-2", ""],
            item_id=self.item,
            chat_id="chat",
            buyer_id=self.buyer,
            spec="月卡",
        )
        self.database.update_card(self.card_id, data_content="")
        self.live._supplier_delivery_procurement = SimpleNamespace(
            store=SimpleNamespace(get_binding=Mock(return_value={"enabled": True})),
            procure=Mock(),
        )

        def procure(**kwargs):
            self.assertEqual(kwargs["quantity"], 1)
            self.assertEqual(kwargs["target_indices"], (2,))
            transformed = kwargs["code_transform"]("SUPPLIER-3")
            ledger.ensure_order(
                self.order,
                self.account,
                ["", "", transformed],
                item_id=self.item,
                chat_id="chat",
                buyer_id=self.buyer,
                spec="月卡",
            )
            return SimpleNamespace(
                ok=True,
                public_payload=lambda: {"status": "purchased"},
            )

        self.live._supplier_delivery_procurement.procure.side_effect = procure
        contents = await self.live._prepare_delivery_contents(
            self.item,
            "",
            self.order,
            self.buyer,
            3,
            {
                "order_spec": "月卡",
                "rule": {
                    "card_id": self.card_id,
                    "card_type": "data",
                    "card_description": "",
                },
            },
            chat_id="chat",
        )

        self.assertEqual(
            contents,
            [
                "LOCAL-1",
                "LOCAL-2",
                f"SUPPLIER-3\n\n{ledger.SELF_SERVICE_RESEND_HINT}",
            ],
        )
        self.live._supplier_delivery_procurement.procure.assert_called_once()
        self.assertEqual(self.inventory(), "")

    async def test_failed_second_card_only_retries_the_same_card(self):
        self.live.send_msg.side_effect = [None, RuntimeError("模拟发送失败"), None, None]
        result = await self.ship()
        self.assertFalse(result["success"], result)
        self.assertTrue(result["partial"])
        self.assertEqual(self.sent_messages(), delivered("A", "B", "C"))
        self.assertEqual(self.inventory(), "D")
        self.assertFalse(self.database.is_order_system_shipped(self.order))
        self.assertEqual(self.database.get_order_by_id(self.order)["order_status"], "pending_ship")
        self.assertEqual([row["status"] for row in ledger.cards(self.order, self.account)], ["sent", "failed", "sent"])
        self.live._confirm_platform_shipped.assert_not_awaited()
        already_allocated = await self.live._prepare_delivery_contents(self.item, "", self.order, self.buyer, 3, {})
        self.assertEqual(already_allocated, delivered("A", "B", "C"))
        self.assertEqual(self.inventory(), "D")

        result = await self.ship()
        self.assertTrue(result["success"], result)
        self.assertEqual(self.sent_messages(), delivered("A", "B", "C", "B"))
        self.assertEqual(self.inventory(), "D")
        self.assertTrue(self.database.is_order_system_shipped(self.order))
        self.live._confirm_platform_shipped.assert_awaited_once()

    async def test_parallel_requests_do_not_allocate_or_confirm_twice(self):
        results = await asyncio.gather(self.ship(), self.ship())
        self.assertEqual(sum(result["success"] for result in results), 1)
        self.assertEqual(self.sent_messages(), delivered("A", "B", "C"))
        self.assertEqual(self.inventory(), "D")
        self.live._confirm_platform_shipped.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
