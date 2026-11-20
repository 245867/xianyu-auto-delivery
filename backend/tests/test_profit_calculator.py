import sys
from pathlib import Path

# 确保 backend 位于 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from profit_calculator import (
    calculate_order_profit,
    is_loss_alert,
    categorize_fund_state,
    calculate_batch_summary,
)


def test_calculate_order_profit_standard():
    # 售价 20.00 元，上游成本 12.00 元，默认平台技术服务费 0.0，运费 0.0
    res = calculate_order_profit(revenue=20.00, upstream_cost=12.00)
    assert res["revenue"] == 20.00
    assert res["upstream_cost"] == 12.00
    assert res["platform_fee"] == 0.00
    assert res["shipping_cost"] == 0.00
    assert res["refund_loss"] == 0.00
    assert res["total_cost"] == 12.00
    assert res["net_profit"] == 8.00
    assert res["margin_rate"] == 0.4000
    assert res["roi"] == round(8.00 / 12.00, 4)


def test_calculate_order_profit_with_shipping_and_explicit_fee():
    # 补充运费 3.50 元，指定平台费 0.30 元
    res = calculate_order_profit(
        revenue=30.00,
        upstream_cost=15.00,
        shipping_cost=3.50,
        platform_fee=0.30,
    )
    assert res["shipping_cost"] == 3.50
    assert res["platform_fee"] == 0.30
    assert res["total_cost"] == 18.80
    assert res["net_profit"] == 11.20
    assert res["margin_rate"] == round(11.20 / 30.00, 4)


def test_calculate_order_profit_with_fee_rate_and_refund_loss():
    # 配置平台费率 0.006，退款折损 5.00 元
    res = calculate_order_profit(
        revenue=19.90,
        upstream_cost=10.00,
        platform_fee_rate=0.006,
        refund_loss=5.00,
    )
    assert res["refund_loss"] == 5.00
    assert res["platform_fee"] == round(19.90 * 0.006, 2)  # 0.12
    assert res["total_cost"] == 15.12
    assert res["net_profit"] == 4.78


def test_calculate_order_profit_zero_revenue():
    res = calculate_order_profit(revenue=0.0, upstream_cost=5.0)
    assert res["revenue"] == 0.0
    assert res["net_profit"] == -5.0
    assert res["margin_rate"] == 0.0
    assert res["roi"] == -1.0


def test_is_loss_alert_inverted_price():
    # 卖价倒挂：成本高于售价，净亏损
    profit = calculate_order_profit(revenue=10.0, upstream_cost=12.0)
    alert, msg = is_loss_alert(profit["net_profit"], profit["margin_rate"])
    assert alert is True
    assert "LOSS_INVERTED" in msg


def test_is_loss_alert_low_margin():
    # 毛利率低于 8% (0.08)
    profit = calculate_order_profit(revenue=100.0, upstream_cost=94.0)
    alert, msg = is_loss_alert(profit["net_profit"], profit["margin_rate"], min_margin_rate=0.08)
    assert alert is True
    assert "LOW_MARGIN" in msg


def test_is_loss_alert_healthy():
    # 正常健康毛利率
    profit = calculate_order_profit(revenue=100.0, upstream_cost=60.0)
    alert, msg = is_loss_alert(profit["net_profit"], profit["margin_rate"])
    assert alert is False
    assert "NORMAL" in msg


def test_categorize_fund_state_pending():
    assert categorize_fund_state("paid") == "pending"
    assert categorize_fund_state("shipped") == "pending"
    assert categorize_fund_state("processing") == "pending"
    assert categorize_fund_state("wait_buyer_confirm") == "pending"


def test_categorize_fund_state_settled():
    assert categorize_fund_state("completed") == "settled"
    assert categorize_fund_state("trade_finished") == "settled"
    assert categorize_fund_state("settled") == "settled"


def test_calculate_batch_summary():
    # 构造 3 笔对齐 financial_profit_records 的订单流水
    records = [
        {
            "revenue": 20.0,
            "upstream_cost": 10.0,
            "shipping_cost": 0.0,
            "platform_fee": 0.12,
            "refund_loss": 0.0,
            "total_cost": 10.12,
            "net_profit": 9.88,
            "settlement_status": "settled",
        },
        {
            "revenue": 50.0,
            "upstream_cost": 30.0,
            "shipping_cost": 2.0,
            "platform_fee": 0.30,
            "refund_loss": 0.0,
            "total_cost": 32.30,
            "net_profit": 17.70,
            "settlement_status": "pending",
        },
        {
            "revenue": 10.0,
            "upstream_cost": 15.0,
            "shipping_cost": 0.0,
            "platform_fee": 0.06,
            "refund_loss": 0.0,
            "total_cost": 15.06,
            "net_profit": -5.06,
            "settlement_status": "pending",
        },
    ]

    summary = calculate_batch_summary(records)
    assert summary["order_count"] == 3
    assert summary["total_revenue"] == 80.0
    assert summary["total_upstream_cost"] == 55.0
    assert summary["total_shipping_cost"] == 2.0
    assert summary["total_platform_fee"] == 0.48
    assert summary["total_cost"] == 57.48
    assert summary["total_net_profit"] == 22.52
    assert summary["settled_funds"] == 20.0
    assert summary["pending_funds"] == 60.0
    assert summary["loss_alert_count"] == 1
    assert summary["overall_margin_rate"] == round(22.52 / 80.0, 4)
    assert summary["overall_roi"] == round(22.52 / 57.48, 4)
