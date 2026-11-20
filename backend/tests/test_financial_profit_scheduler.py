import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

from db_manager import DBManager


@pytest.fixture
def database(tmp_path):
    db = DBManager(str(tmp_path / "financial-profit.db"))
    yield db
    db.conn.close()


def _order(database, order_id="o-1", amount="100.00", status="pending_ship",
           cookie_id="shop", item_id="item-1"):
    with database.conn:
        database.conn.execute(
            "INSERT INTO orders (order_id, item_id, buyer_id, amount, order_status, cookie_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, item_id, "buyer-1", amount, status, cookie_id),
        )


def _dropship_task(database, order_id="o-1", position=1, quantity=2, unit_price="25.50"):
    with database.conn:
        database.conn.execute(
            "INSERT INTO dropship_tasks (order_id, position, spec_hash, upstream_goods_id, "
            "upstream_sku_id, stock_guard, quantity, unit_price) "
            "VALUES (?, ?, 'h', 'g', 's', 1, ?, ?)",
            (order_id, position, quantity, unit_price),
        )


def _voucher_order(database, order_id="o-1"):
    with database.conn:
        database.conn.execute(
            "INSERT INTO voucher_async_reconcile (order_id, account_id, item_id, "
            "charge_account, channels_json) VALUES (?, 'shop', 'item-1', '13800000000', '[]')",
            (order_id,),
        )


def test_profit_table_is_wal_with_required_columns(database):
    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(financial_profit_records)")}
    assert columns == {
        "order_id", "account_id", "item_id", "revenue", "upstream_cost",
        "shipping_cost", "platform_fee", "net_profit", "margin_rate",
        "settlement_status", "created_at", "settled_at",
    }


def test_collects_revenue_upstream_cost_and_margin(database):
    from financial_profit import FinancialProfitLedger

    _order(database)
    _dropship_task(database)
    ledger = FinancialProfitLedger(
        database, platform_fee_rate=0.02, shipping_cost=3.0)

    record = ledger.collect("o-1")

    assert record["revenue"] == 100.0
    assert record["upstream_cost"] == 51.0
    assert record["shipping_cost"] == 3.0
    assert record["platform_fee"] == 2.0
    assert record["net_profit"] == 44.0
    assert record["margin_rate"] == 0.44
    assert record["settlement_status"] == "pending"
    assert record["settled_at"] is None


def test_amount_with_currency_prefix_is_parsed(database):
    from financial_profit import FinancialProfitLedger

    _order(database, amount="¥1,234.50")

    record = FinancialProfitLedger(database).collect("o-1")

    assert record["revenue"] == 1234.5


def test_completed_order_moves_to_settled_once(database):
    from financial_profit import FinancialProfitLedger

    _order(database)
    _dropship_task(database)
    ledger = FinancialProfitLedger(database)
    assert ledger.collect("o-1")["settlement_status"] == "pending"

    with database.conn:
        database.conn.execute(
            "UPDATE orders SET order_status='completed' WHERE order_id='o-1'")
    settled = ledger.collect("o-1")

    assert settled["settlement_status"] == "settled"
    assert settled["settled_at"]

    again = ledger.collect("o-1")
    assert again["settled_at"] == settled["settled_at"]


def test_negative_profit_raises_loss_alert_once(database):
    from financial_profit import FinancialProfitLedger

    _order(database, amount="10.00")
    _dropship_task(database, quantity=1, unit_price="20.00")
    alerts = []

    ledger = FinancialProfitLedger(
        database, alert=lambda event, detail: alerts.append((event, detail)))

    record = ledger.collect("o-1")

    assert record["net_profit"] == -10.0
    assert record["margin_rate"] == -1.0
    assert alerts and alerts[0][0] == "LOSS_ALERT"
    assert "o-1" in alerts[0][1]

    ledger.collect("o-1")
    assert len(alerts) == 1


def test_cancelled_order_releases_revenue_and_flags_loss(database):
    from financial_profit import FinancialProfitLedger

    _order(database, amount="30.00", status="cancelled")
    _dropship_task(database, quantity=1, unit_price="42.00")
    alerts = []

    record = FinancialProfitLedger(
        database, alert=lambda event, detail: alerts.append(event)).collect("o-1")

    assert record["revenue"] == 0.0
    assert record["net_profit"] == -42.0
    assert alerts == ["LOSS_ALERT"]


def test_voucher_order_uses_injected_cost_resolver(database):
    from financial_profit import FinancialProfitLedger

    _order(database, amount="100.00", status="completed")
    _voucher_order(database)
    seen = []

    def voucher_cost(order_id):
        seen.append(order_id)
        return 88.8

    record = FinancialProfitLedger(
        database, voucher_cost=voucher_cost).collect("o-1")

    assert seen == ["o-1"]
    assert record["upstream_cost"] == 88.8
    assert record["net_profit"] == 11.2


def test_scan_once_counts_scanned_and_settled_orders(database):
    from financial_profit import FinancialProfitLedger

    _order(database, order_id="o-1", status="pending_ship")
    _order(database, order_id="o-2", status="completed")

    summary = FinancialProfitLedger(database).scan_once()

    assert summary["scanned"] >= 2
    assert summary["settled"] == 1
    assert database.get_financial_profit_record("o-2")["settlement_status"] == "settled"


def test_edit_order_price_posts_yuan_amounts(monkeypatch):
    import goofish_open

    calls = []

    def call(name, body=None, allow_write=False, timeout=15):
        calls.append((name, body, allow_write))
        return {"code": 0, "data": {"success": True}}

    monkeypatch.setattr(goofish_open, "call", call)

    goofish_open.edit_order_price(
        "3316574319266035695", 19.0, allow_write=True)

    assert calls == [("order.editPrice", {
        "orderId": "3316574319266035695",
        "price": 19.0,
        "postFee": 0.0,
    }, True)]


def test_edit_order_price_translates_business_errors(monkeypatch):
    import goofish_open

    def call(name, body=None, allow_write=False, timeout=15):
        raise goofish_open.OpenPlatformError(
            "接口返回失败（code=500）：ORDER_ALREADY_PAID", kind="business")

    monkeypatch.setattr(goofish_open, "call", call)

    with pytest.raises(goofish_open.OpenPlatformError) as caught:
        goofish_open.edit_order_price("o-1", 10.0, allow_write=True)

    assert "ORDER_ALREADY_PAID" in str(caught.value)
    assert "已付款" in str(caught.value)


def test_bargain_channels_bind_edit_price_only_when_configured(monkeypatch):
    import bargain_closer
    import goofish_open

    monkeypatch.setattr(goofish_open, "get_config",
                        lambda: {"enabled": False, "app_key": "k", "app_secret": "s"})
    monkeypatch.setattr(goofish_open, "is_configured", lambda cfg=None: True)
    assert bargain_closer.build_platform_channels() == {}

    monkeypatch.setattr(goofish_open, "get_config",
                        lambda: {"enabled": True, "app_key": "k", "app_secret": "s"})
    sent = []
    monkeypatch.setattr(goofish_open, "edit_order_price",
                        lambda order_id, price, **kw: sent.append(
                            (order_id, price, kw)) or {"code": 0})

    channels = bargain_closer.build_platform_channels()
    assert channels["apply_concession"]("o-1", "shop", 19.0) is True
    assert sent == [("o-1", 19.0, {"allow_write": True})]
