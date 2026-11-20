"""
Profit Calculator - 全要素边际利润核算与资金状态模型
《拆解 56：商业闭源「全天候多店铺财务收支流水核算与真实 ROI 利润看板」》

口径统一标准（对齐 backend/financial_profit.py 与数据库表 financial_profit_records）：
1. 逐单全要素边际利润：net_profit = revenue - upstream_cost - shipping_cost - platform_fee
2. 平台费率与运费：默认费率 0.0，运费 0.0，由配置驱动，不做隐式假设；
3. 结算状态：两态模型 (pending 在途 / settled 已结算，以 orders.order_status == 'completed' 为终态)；
4. 负毛利刚性预警 (Loss Alert Guard)：net_profit < 0 触发严重倒挂报警；
5. 多单/多店铺聚合看板汇总 (calculate_batch_summary)。
"""

from typing import Dict, Any, Tuple, Optional, List


def calculate_order_profit(
    revenue: float,
    upstream_cost: float,
    shipping_cost: float = 0.0,
    platform_fee_rate: float = 0.0,
    platform_fee: Optional[float] = None,
    refund_loss: float = 0.0,
) -> Dict[str, Any]:
    """
    核算单笔订单全要素边际净利与毛利率。

    公式：
      platform_fee = platform_fee or round(revenue * platform_fee_rate, 2)
      total_cost   = upstream_cost + shipping_cost + platform_fee + refund_loss
      net_profit   = revenue - total_cost
      margin_rate  = net_profit / revenue (当 revenue > 0，否则 0.0)
      roi          = net_profit / total_cost (当 total_cost > 0，否则 0.0)
    """
    revenue = max(0.0, round(float(revenue), 2))
    upstream_cost = max(0.0, round(float(upstream_cost), 2))
    shipping_cost = max(0.0, round(float(shipping_cost), 2))
    refund_loss = max(0.0, round(float(refund_loss), 2))

    if platform_fee is not None:
        fee = max(0.0, round(float(platform_fee), 2))
    else:
        fee = round(revenue * float(platform_fee_rate), 2)

    total_cost = round(upstream_cost + shipping_cost + fee + refund_loss, 2)
    net_profit = round(revenue - total_cost, 2)

    margin_rate = round(net_profit / revenue, 4) if revenue > 0 else 0.0
    roi = round(net_profit / total_cost, 4) if total_cost > 0 else 0.0

    return {
        "revenue": revenue,
        "upstream_cost": upstream_cost,
        "shipping_cost": shipping_cost,
        "platform_fee": fee,
        "refund_loss": refund_loss,
        "total_cost": total_cost,
        "net_profit": net_profit,
        "margin_rate": margin_rate,
        "roi": roi,
    }


def is_loss_alert(
    net_profit: float,
    margin_rate: float,
    min_margin_rate: float = 0.08,
) -> Tuple[bool, str]:
    """
    负毛利倒挂刚性预警判据。

    规则：
    1. net_profit < 0: 严重倒挂 (P0)，卖价低于总成本，触发倒挂报警；
    2. margin_rate < min_margin_rate (默认 8%): 薄利高危品，预警提示；
    3. 正常盈利: 返回 False。
    """
    net_profit = round(float(net_profit), 2)
    margin_rate = float(margin_rate)
    min_margin_rate = float(min_margin_rate)

    if net_profit < 0:
        return True, f"LOSS_INVERTED: 销售价格倒挂，单单净亏损 {net_profit:.2f} 元"
    if margin_rate < min_margin_rate:
        return True, f"LOW_MARGIN: 毛利率 {margin_rate * 100:.2f}% 低于安全阈值 {min_margin_rate * 100:.2f}%"
    return False, "NORMAL: 利润正常"


def categorize_fund_state(
    order_status: str,
    refund_status: Optional[str] = None,
) -> str:
    """
    资金结算状态分类（对齐 financial_profit_records.settlement_status 两态口径）：
    - settled: 已完成/已结算 (orders.order_status == 'completed' 或 trade_finished/success)
    - pending: 在途资金 (待发货/已付款/运输中/待确认收货)
    """
    norm_order = (order_status or "").strip().lower().replace("-", "_")
    norm_refund = (refund_status or "").strip().lower().replace("-", "_")

    # 终态结算判定
    settled_keywords = {
        "completed",
        "settled",
        "trade_finished",
        "trade_success",
        "success",
        "confirmed",
        "rate_finished",
    }
    if norm_order in settled_keywords:
        return "settled"

    return "pending"


def calculate_batch_summary(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    聚合多店铺/批次订单的财务指标看板数据。
    支持 settlement_status 或 fund_state 字段。
    """
    total_revenue = round(sum(float(r.get("revenue", 0.0)) for r in records), 2)
    total_upstream_cost = round(sum(float(r.get("upstream_cost", 0.0)) for r in records), 2)
    total_shipping_cost = round(sum(float(r.get("shipping_cost", 0.0)) for r in records), 2)
    total_platform_fee = round(sum(float(r.get("platform_fee", 0.0)) for r in records), 2)
    total_refund_loss = round(sum(float(r.get("refund_loss", 0.0)) for r in records), 2)

    total_cost = round(
        total_upstream_cost + total_shipping_cost + total_platform_fee + total_refund_loss,
        2,
    )
    total_net_profit = round(total_revenue - total_cost, 2)

    overall_margin_rate = round(total_net_profit / total_revenue, 4) if total_revenue > 0 else 0.0
    overall_roi = round(total_net_profit / total_cost, 4) if total_cost > 0 else 0.0

    def is_settled(r):
        return (r.get("settlement_status") or r.get("fund_state")) == "settled"

    def is_pending(r):
        return (r.get("settlement_status") or r.get("fund_state")) == "pending"

    pending_funds = round(
        sum(float(r.get("revenue", 0.0)) for r in records if is_pending(r)),
        2,
    )
    settled_funds = round(
        sum(float(r.get("revenue", 0.0)) for r in records if is_settled(r)),
        2,
    )
    loss_alert_count = sum(
        1 for r in records if float(r.get("net_profit", 0.0)) < 0 or r.get("loss_alert", False)
    )

    return {
        "order_count": len(records),
        "total_revenue": total_revenue,
        "total_upstream_cost": total_upstream_cost,
        "total_shipping_cost": total_shipping_cost,
        "total_platform_fee": total_platform_fee,
        "total_refund_loss": total_refund_loss,
        "total_cost": total_cost,
        "total_net_profit": total_net_profit,
        "overall_margin_rate": overall_margin_rate,
        "overall_roi": overall_roi,
        "pending_funds": pending_funds,
        "settled_funds": settled_funds,
        "loss_alert_count": loss_alert_count,
    }
