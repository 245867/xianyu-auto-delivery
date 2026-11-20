import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from price_rank_guard import calculate_target_price


def test_under_cut_competitor():
    # 竞品最低价 15.00，我当前 16.00，下调 0.1 卡位为 14.90
    target, should_revise, reason = calculate_target_price(
        current_price=16.00,
        competitor_prices=[15.00, 18.00, 15.50],
        price_floor=10.00,
        price_ceiling=20.00,
        under_cut_delta=0.1,
    )
    assert target == 14.90
    assert should_revise is True
    assert reason == "UNDER_CUT"


def test_floor_guard_blocking():
    # 竞品恶意压价到 8.00，但我底价为 10.00，卡位算得 7.90 < 10.00，刚性锁死在 10.00
    target, should_revise, reason = calculate_target_price(
        current_price=14.00,
        competitor_prices=[8.00, 8.50],
        price_floor=10.00,
        price_ceiling=20.00,
        under_cut_delta=0.1,
    )
    assert target == 10.00
    assert should_revise is True
    assert reason == "FLOOR_GUARD"

    # 如果当前价格已经是底价 10.00，不再重复改价
    t2, should_revise2, r2 = calculate_target_price(
        current_price=10.00,
        competitor_prices=[8.00],
        price_floor=10.00,
        price_ceiling=20.00,
    )
    assert t2 == 10.00
    assert should_revise2 is False
    assert r2 == "NO_CHANGE"


def test_ceiling_rebound_when_competitors_gone():
    # 同行断货或下架（无有效竞品），售价自动回弹到顶价 20.00 收割毛利
    target, should_revise, reason = calculate_target_price(
        current_price=14.00,
        competitor_prices=[],
        price_floor=10.00,
        price_ceiling=20.00,
    )
    assert target == 20.00
    assert should_revise is True
    assert reason == "CEILING_REBOUND"


def test_rate_limit_tripped():
    # 今日改价已达 5 次上限，直接熔断拒绝改价
    target, should_revise, reason = calculate_target_price(
        current_price=16.00,
        competitor_prices=[15.00],
        price_floor=10.00,
        price_ceiling=20.00,
        daily_revision_count=5,
        max_daily_revisions=5,
    )
    assert target == 16.00
    assert should_revise is False
    assert reason == "RATE_LIMIT_TRIPPED"


def test_no_change_when_already_optimal():
    # 竞品 15.00，目标价 14.90，当前已经是 14.90，微调差值 < 0.05 不改价
    target, should_revise, reason = calculate_target_price(
        current_price=14.90,
        competitor_prices=[15.00],
        price_floor=10.00,
        price_ceiling=20.00,
        under_cut_delta=0.1,
    )
    assert target == 14.90
    assert should_revise is False
    assert reason == "NO_CHANGE"
