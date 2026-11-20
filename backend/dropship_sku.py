"""Verified SKU lines and procurement guards for physical orders."""

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from loguru import logger

import rate_limiter

MINIMUM_MARGIN = Decimal("2.00")
LOW_STOCK = 3


@dataclass(frozen=True)
class OrderLine:
    spec_hash: str
    quantity: int
    unit_price: Decimal

    def __post_init__(self):
        if not isinstance(self.spec_hash, str) or not self.spec_hash or self.spec_hash != self.spec_hash.strip():
            raise ValueError("订单行需要精确的规格哈希")
        if type(self.quantity) is not int or self.quantity < 1:
            raise ValueError("订单行数量必须为正整数")
        if not isinstance(self.unit_price, Decimal) or not self.unit_price.is_finite() or self.unit_price < 0:
            raise ValueError("订单行需要核实的实收单价")


@dataclass(frozen=True)
class StockQuote:
    stock: int
    unit_cost: Decimal
    available: bool = True

    def __post_init__(self):
        if type(self.stock) is not int or self.stock < 0 or type(self.available) is not bool:
            raise ValueError("上游报价需要明确的库存及在售状态")
        if not isinstance(self.unit_cost, Decimal) or not self.unit_cost.is_finite() or self.unit_cost < 0:
            raise ValueError("上游报价需要核实的采购单价")


def upstream_sku(mapping):
    return mapping["upstream_goods_id"], mapping["upstream_sku_id"]


def make_zero_stock_simulator(database, send):
    """Build the stock guard hook with an injected, credential-free request simulator."""
    def set_stock(_cookie_id, item_id, spec_hash, quantity):
        mapping = database.get_spec_mapping(item_id, spec_hash)
        if type(quantity) is not int or quantity != 0 or not mapping or not mapping["platform_sku_id"]:
            return False
        return send(
            "mtop.taobao.idle.item.stock.update",
            {"itemId": item_id, "skuStocks": [{"skuId": mapping["platform_sku_id"], "quantity": 0}]},
            version="1.0",
        ) is True

    return set_stock


class ZeroStockGuard:
    def __init__(self, database, adapter, set_stock=None, *, minimum_margin=MINIMUM_MARGIN):
        self.database = database
        self.adapter = adapter
        self.set_stock = set_stock
        if not isinstance(minimum_margin, Decimal) or not minimum_margin.is_finite() or minimum_margin < 0:
            raise ValueError("最低毛利必须是非负 Decimal")
        self.minimum_margin = minimum_margin

    def _quote(self, sku):
        quote = self.adapter.quote_sku(*sku)
        if not isinstance(quote, StockQuote):
            raise ValueError("上游未返回有效的 SKU 报价")
        return quote

    def _alarm(self, cookie_id, item_id, mapping, reason, *, zero=False):
        spec_hash = mapping["spec_hash"]
        live = self.database.get_spec_mapping(item_id, spec_hash)
        if (
            not live or upstream_sku(live) != upstream_sku(mapping)
            or live["stock_guard"] != mapping["stock_guard"]
            or live["platform_sku_id"] != mapping["platform_sku_id"]
        ):
            logger.warning("【{}】商品 {} 规格 {} 的旧映射采购已拦截（{}）", cookie_id, item_id, spec_hash, reason)
            return
        key = cookie_id, item_id, spec_hash, reason
        created, zeroed = self.database.record_spec_stock_alert(*key)
        if created:
            logger.warning("【{}】商品 {} 规格 {} 代拍已拦截（{}）", cookie_id, item_id, spec_hash, reason)
        if not zero or zeroed or self.set_stock is None:
            return
        if (
            self.database.get_cookie_status(cookie_id) and rate_limiter.gate_write(cookie_id)[0]
            and self.set_stock(cookie_id, item_id, spec_hash, 0) is True
        ):
            self.database.mark_spec_stock_zeroed(*key)

    def check_purchase(self, cookie_id, item_id, tasks):
        demand = Counter()
        for task in tasks:
            demand[upstream_sku(task)] += task["quantity"]
        quotes = {sku: self._quote(sku) for sku in demand}
        allowed = True
        for task in tasks:
            sku = upstream_sku(task)
            quote = quotes[sku]
            if not quote.available:
                self._alarm(cookie_id, item_id, task, "sku_unavailable", zero=bool(task["stock_guard"]))
                allowed = False
            elif task["stock_guard"] and quote.stock <= LOW_STOCK:
                self._alarm(cookie_id, item_id, task, "low_stock", zero=True)
                allowed = False
            elif quote.stock < demand[sku]:
                self._alarm(cookie_id, item_id, task, "insufficient_stock")
                allowed = False
            elif Decimal(task["unit_price"]) - quote.unit_cost < self.minimum_margin:
                self._alarm(cookie_id, item_id, task, "margin_below_minimum")
                allowed = False
        return allowed

    def poll(self):
        quotes = {}
        for mapping in self.database.get_stock_guard_mappings():
            cookie_id, item_id = mapping["cookie_id"], mapping["item_id"]
            if not self.database.get_cookie_status(cookie_id) or rate_limiter.circuit_blocked(cookie_id)[0]:
                continue
            try:
                sku = upstream_sku(mapping)
                if sku not in quotes:
                    quotes[sku] = self._quote(sku)
                quote = quotes[sku]
                if not quote.available:
                    self._alarm(cookie_id, item_id, mapping, "sku_unavailable", zero=True)
                elif quote.stock <= LOW_STOCK:
                    self._alarm(cookie_id, item_id, mapping, "low_stock", zero=True)
                else:
                    self.database.resolve_spec_stock_alerts(
                        cookie_id, item_id, mapping["spec_hash"], "sku_unavailable", "low_stock",
                    )
            except Exception as exc:
                logger.warning("【{}】商品 {} 库存探测失败（{}）", cookie_id, item_id, type(exc).__name__)
