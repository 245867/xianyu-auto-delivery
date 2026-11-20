import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.order_detail_fetcher import read_order_trade_fields


class OrderPage:
    def __init__(self, rows):
        self.rows = rows

    def get_by_role(self, role):
        if role != "listitem":
            raise AssertionError(role)
        return self

    async def all_text_contents(self):
        return self.rows


class OrderTradeFieldsTests(unittest.TestCase):
    def test_reads_actual_trade_amount_and_order_time(self):
        page = OrderPage([
            "商品单价 ￥9.90",
            "成交价（在支付宝担保账户中）\n¥ 5.90",
            "下单时间2026-11-17 14:27:52",
            "付款时间2026-11-17 14:28:01",
        ])
        self.assertEqual(asyncio.run(read_order_trade_fields(page)), {
            "amount": "5.90", "order_time": "2026-11-17 14:27:52",
        })

    def test_never_uses_unrelated_prices_or_payment_time(self):
        page = OrderPage(["原价 ¥19.90", "付款时间2026-11-17 14:28:01"])
        self.assertEqual(asyncio.run(read_order_trade_fields(page)), {})

    def test_reads_trade_amount_without_escrow_caption_and_rejects_duplicates(self):
        page = OrderPage(["成交价 ￥1,234.50", "下单时间2026-11-17 14:27:52",
                          "下单时间2026-11-17 15:27:52"])
        self.assertEqual(asyncio.run(read_order_trade_fields(page)), {"amount": "1234.50"})


if __name__ == "__main__":
    unittest.main()
