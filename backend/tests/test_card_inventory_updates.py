from __future__ import annotations

import ast
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

from fastapi import Body, Depends, HTTPException

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

with patch.dict(os.environ, {"DB_PATH": ":memory:", "SQL_LOG_ENABLED": "false"}):
    import db_manager

from sqlite_runtime import BUSY_TIMEOUT_MS, connect_sqlite


class CardInventoryUpdatesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database_path = str(Path(temporary.name) / "cards.sqlite")
        with patch.dict(os.environ, {"SQL_LOG_ENABLED": "false"}):
            self.database = db_manager.DBManager(self.database_path)
        self.addCleanup(self.database.close)
        self.assertEqual(
            self.database.conn.execute("PRAGMA journal_mode").fetchone()[0].lower(),
            "wal",
        )
        self.assertEqual(
            self.database.conn.execute("PRAGMA busy_timeout").fetchone()[0],
            BUSY_TIMEOUT_MS,
        )
        self.assertEqual(
            self.database.conn.execute("PRAGMA synchronous").fetchone()[0],
            1,
        )
        mocked_db = patch.object(db_manager, "db_manager", self.database)
        mocked_db.start()
        self.addCleanup(mocked_db.stop)
        self.user = {"user_id": 1}
        routes = ast.parse((BACKEND / "reply_server.py").read_text(encoding="utf-8"))
        self.routes = {
            "Any": Any,
            "Body": Body,
            "Depends": Depends,
            "Dict": Dict,
            "HTTPException": HTTPException,
            "get_current_user": lambda: self.user,
        }
        for node in routes.body:
            if isinstance(node, ast.FunctionDef) and node.name in {"update_card", "append_card_inventory"}:
                node.decorator_list = []
                exec(compile(ast.Module(body=[node], type_ignores=[]), "reply_server.py", "exec"), self.routes)

    def group(self, duration, content, enabled=True):
        return self.database.create_card(
            name="会员", card_type="data", data_content=content, enabled=enabled,
            is_multi_spec=True, spec_name="时长", spec_value=duration, user_id=1,
        )

    def test_partial_card_update_preserves_disabled_state(self):
        monthly = self.group("月卡", "MONTH-1", enabled=False)
        self.routes["update_card"](monthly, {"description": "更新说明"}, self.user)
        card = self.database.get_card_by_id(monthly, 1)
        self.assertFalse(card["enabled"])
        self.assertEqual(card["data_content"], "MONTH-1")

    def test_reconnect_reapplies_sqlite_concurrency_pragmas(self):
        self.database.close()
        conn = self.database.get_connection()

        self.assertEqual(
            conn.execute("PRAGMA journal_mode").fetchone()[0].lower(),
            "wal",
        )
        self.assertEqual(
            conn.execute("PRAGMA busy_timeout").fetchone()[0],
            BUSY_TIMEOUT_MS,
        )
        self.assertEqual(
            conn.execute("PRAGMA synchronous").fetchone()[0],
            1,
        )

    def test_append_uses_selected_id_and_current_stock(self):
        daily = self.group("日卡", "DAY-1")
        monthly = self.group("月卡", "MONTH-1\nMONTH-2")
        old_page = self.database.get_card_by_id(monthly, 1)
        self.assertEqual(self.database.consume_batch_data(monthly), "MONTH-1")
        self.routes["update_card"](old_page["id"], {"enabled": False}, self.user)
        self.routes["append_card_inventory"](old_page["id"], "MONTH-3\nMONTH-3", self.user)
        card = self.database.get_card_by_id(monthly, 1)
        self.assertEqual(card["data_content"], "MONTH-2\nMONTH-3")
        self.assertFalse(card["enabled"])
        self.assertEqual(self.database.get_card_by_id(daily, 1)["data_content"], "DAY-1")

    def test_consume_deduplicates_before_sale_and_logs_count(self):
        monthly = self.group("月卡", "MONTH-1\nMONTH-1\nMONTH-2\nMONTH-1")
        with patch.object(db_manager.logger, "warning") as warning:
            codes, available = self.database.consume_batch_codes(monthly, 1)

        self.assertEqual(codes, ["MONTH-1"])
        self.assertEqual(available, 2)
        self.assertEqual(
            self.database.get_card_by_id(monthly, 1)["data_content"],
            "MONTH-2",
        )
        logged = " ".join(str(call.args[0]) for call in warning.call_args_list)
        self.assertIn("重复卡密", logged)
        self.assertIn("重复=2条", logged)

    def test_append_checks_ownership(self):
        monthly = self.group("月卡", "MONTH-1")
        with self.assertRaises(HTTPException) as raised:
            self.routes["append_card_inventory"](monthly, "MONTH-2", {"user_id": 2})
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.database.get_card_by_id(monthly, 1)["data_content"], "MONTH-1")

    def test_append_and_consume_on_two_connections_keep_both_updates(self):
        monthly = self.group("月卡", "MONTH-1\nMONTH-2")
        with patch.dict(os.environ, {"SQL_LOG_ENABLED": "false"}):
            second = db_manager.DBManager(self.database_path)
        self.addCleanup(second.close)
        barrier = threading.Barrier(2)

        def consume():
            barrier.wait(timeout=5)
            return self.database.consume_batch_data(monthly)

        def append():
            barrier.wait(timeout=5)
            return second.append_card_inventory(monthly, 1, "MONTH-3")

        with ThreadPoolExecutor(max_workers=2) as tasks:
            consumed = tasks.submit(consume)
            appended = tasks.submit(append)
            self.assertEqual(consumed.result(timeout=10), "MONTH-1")
            self.assertTrue(appended.result(timeout=10))
        self.assertEqual(self.database.get_card_by_id(monthly, 1)["data_content"], "MONTH-2\nMONTH-3")


class SqliteRuntimeTests(unittest.TestCase):
    def test_file_connection_enables_required_pragmas(self):
        with tempfile.TemporaryDirectory() as temporary:
            database_path = str(Path(temporary) / "runtime.sqlite")
            conn = connect_sqlite(database_path)
            try:
                self.assertEqual(
                    conn.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                    "wal",
                )
                self.assertEqual(
                    conn.execute("PRAGMA busy_timeout").fetchone()[0],
                    BUSY_TIMEOUT_MS,
                )
                self.assertEqual(
                    conn.execute("PRAGMA synchronous").fetchone()[0],
                    1,
                )
            finally:
                conn.close()

    def test_writer_waits_for_lock_and_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            database_path = str(Path(temporary) / "contention.sqlite")
            first = connect_sqlite(database_path, check_same_thread=False)
            second = connect_sqlite(database_path)
            try:
                first.execute("CREATE TABLE lock_probe (value TEXT NOT NULL)")
                first.commit()

                first.execute("BEGIN IMMEDIATE")
                first.execute("INSERT INTO lock_probe (value) VALUES ('holder')")
                holder_errors = []

                def release_lock():
                    try:
                        time.sleep(0.3)
                        first.commit()
                    except Exception as error:
                        holder_errors.append(error)

                release_thread = threading.Thread(target=release_lock)
                release_thread.start()
                started = time.monotonic()
                second.execute("INSERT INTO lock_probe (value) VALUES ('waiter')")
                second.commit()
                waited_seconds = time.monotonic() - started
                release_thread.join(timeout=5)

                self.assertFalse(release_thread.is_alive())
                self.assertEqual(holder_errors, [])
                self.assertGreaterEqual(waited_seconds, 0.2)
                self.assertLess(waited_seconds, 5)
                values = {
                    row[0]
                    for row in second.execute("SELECT value FROM lock_probe").fetchall()
                }
                self.assertEqual(values, {"holder", "waiter"})
            finally:
                second.close()
                first.close()

    def test_memory_database_skips_wal_without_breaking_usage(self):
        conn = connect_sqlite(":memory:")
        self.addCleanup(conn.close)

        self.assertEqual(
            conn.execute("PRAGMA journal_mode").fetchone()[0].lower(),
            "memory",
        )
        self.assertEqual(
            conn.execute("PRAGMA busy_timeout").fetchone()[0],
            BUSY_TIMEOUT_MS,
        )
        conn.execute("CREATE TABLE memory_probe (value INTEGER NOT NULL)")
        conn.execute("INSERT INTO memory_probe (value) VALUES (7)")
        self.assertEqual(
            conn.execute("SELECT value FROM memory_probe").fetchone()[0],
            7,
        )


if __name__ == "__main__":
    unittest.main()
