import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TEMP = tempfile.TemporaryDirectory()
os.environ["DB_PATH"] = str(Path(_TEMP.name) / "orders.db")

from db_manager import db_manager


class OrderStatusPersistenceTests(unittest.TestCase):
    def test_detail_can_update_fields_without_erasing_known_status(self):
        key = "detail"
        self.assertTrue(db_manager.insert_or_update_order(key, order_status="pending_ship"))
        self.assertTrue(db_manager.insert_or_update_order(key, amount="5.90", order_status="unknown"))
        order = db_manager.get_order_by_id(key)
        self.assertEqual(order["order_status"], "pending_ship")
        self.assertEqual(order["amount"], "5.90")

    def test_late_payment_cannot_regress_a_shipped_order(self):
        key = "shipped"
        self.assertTrue(db_manager.insert_or_update_order(key, order_status="shipped", system_shipped=True))
        for status in ("processing", "pending_ship", "unknown"):
            self.assertTrue(db_manager.insert_or_update_order(key, order_status=status))
            self.assertEqual(db_manager.get_order_by_id(key)["order_status"], "shipped")
        for status in ("completed", "refunding", "cancelled"):
            self.assertTrue(db_manager.insert_or_update_order(key, order_status=status))
            self.assertEqual(db_manager.get_order_by_id(key)["order_status"], status)

    def test_first_known_status_can_replace_unknown(self):
        key = "new"
        self.assertTrue(db_manager.insert_or_update_order(key, order_status="unknown"))
        self.assertTrue(db_manager.insert_or_update_order(key, order_status="pending_ship"))
        self.assertEqual(db_manager.get_order_by_id(key)["order_status"], "pending_ship")


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        db_manager.conn.close()
        _TEMP.cleanup()
