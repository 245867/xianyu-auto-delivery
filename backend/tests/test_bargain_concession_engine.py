import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from bargain_concession_engine import (
    calculate_concession_price,
    generate_urgency_countdown_message,
)


def test_round_1_polite_rejection():
    # 原价 20，底价 15，第 1 轮无论买家出多少，礼貌婉拒不降价
    price, decision, msg = calculate_concession_price(
        original_price=20.00,
        price_floor=15.00,
        buyer_offer=17.00,
        round_number=1,
    )
    assert price == 20.00
    assert decision == "REJECT_POLITE"
    assert "非常划算" in msg


def test_round_2_step_down():
    # 原价 20，底价 15，第 2 轮同意让步 1 元（降到 19）
    price, decision, msg = calculate_concession_price(
        original_price=20.00,
        price_floor=15.00,
        buyer_offer=18.00,
        round_number=2,
        step_delta=1.0,
    )
    assert price == 19.00
    assert decision == "AGREE_STEP_DOWN"
    assert "减 ¥1.00" in msg


def test_round_2_clamp_to_floor():
    # 原价 15.50，底价 15.00，降 1 元会跌破底价，因此刚性锁定在 15.00
    price, decision, msg = calculate_concession_price(
        original_price=15.50,
        price_floor=15.00,
        round_number=2,
        step_delta=1.0,
    )
    assert price == 15.00
    assert decision == "AGREE_STEP_DOWN"
    assert "减 ¥0.50" in msg


def test_round_3_lock_floor():
    # 第 3 轮，直接锁定在底价 15.00
    price, decision, msg = calculate_concession_price(
        original_price=20.00,
        price_floor=15.00,
        round_number=3,
    )
    assert price == 15.00
    assert decision == "LOCK_FLOOR"
    assert "底价" in msg


def test_reject_extreme_offer():
    # 原价 20，底价 15，买家恶意出价 5 元（< 15 * 0.85），直接触发极端大刀拒绝
    price, decision, msg = calculate_concession_price(
        original_price=20.00,
        price_floor=15.00,
        buyer_offer=5.00,
        round_number=2,
    )
    assert price == 20.00
    assert decision == "REJECT_EXTREME"
    assert "大刀真的砍不动" in msg


def test_generate_urgency_countdown_message():
    msg = generate_urgency_countdown_message(
        concession_price=18.00,
        original_price=20.00,
        expiry_minutes=15,
    )
    assert "优惠 ¥2.00" in msg
    assert "¥18.00" in msg
    assert "锁定 15 分钟" in msg
    assert "恢复原价 ¥20.00" in msg
