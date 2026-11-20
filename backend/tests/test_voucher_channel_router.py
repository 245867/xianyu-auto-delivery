import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from voucher_channel_router import (
    extract_charge_account,
    VoucherChannelRouter,
)


def test_extract_charge_account_phone():
    assert extract_charge_account("充值手机号 13800138000 谢谢") == ("13800138000", "phone")
    assert extract_charge_account("+86 139-1234-5678 请快点") == ("13912345678", "phone")
    assert extract_charge_account("86-159 0000 1111") == ("15900001111", "phone")


def test_extract_charge_account_email_and_qq():
    assert extract_charge_account("我的邮箱是 user.test+vip@gmail.com") == ("user.test+vip@gmail.com", "email")
    assert extract_charge_account("QQ充值: 123456789") == ("123456789", "qq")
    assert extract_charge_account("Q号 88888888") == ("88888888", "qq")


def test_extract_charge_account_empty():
    assert extract_charge_account("") == (None, None)
    assert extract_charge_account(None) == (None, None)
    assert extract_charge_account("老板发货") == (None, None)


def test_voucher_channel_router_failover():
    router = VoucherChannelRouter(failure_threshold=2, cooldown_seconds=60)
    candidates = ["chan_a", "chan_b", "chan_c"]

    # 首次选择主渠道
    assert router.select_next_channel(candidates, []) == "chan_a"

    # chan_a 失败顺延 chan_b
    assert router.select_next_channel(candidates, ["chan_a"]) == "chan_b"

    # chan_a, chan_b 失败顺延 chan_c
    assert router.select_next_channel(candidates, ["chan_a", "chan_b"]) == "chan_c"

    # 全渠道耗尽返回 None
    assert router.select_next_channel(candidates, ["chan_a", "chan_b", "chan_c"]) is None


def test_voucher_channel_router_cooldown():
    router = VoucherChannelRouter(failure_threshold=2, cooldown_seconds=100)
    candidates = ["chan_fast", "chan_slow"]

    assert router.is_channel_available("chan_fast") is True

    # 记录 1 次失败不熔断
    router.record_channel_failure("chan_fast", "TIMEOUT")
    assert router.is_channel_available("chan_fast") is True

    # 达到 2 次失败触发熔断冷却
    router.record_channel_failure("chan_fast", "TIMEOUT")
    assert router.is_channel_available("chan_fast") is False

    # 熔断后优先选择健康备选通道 chan_slow
    selected = router.select_next_channel(candidates, [])
    assert selected == "chan_slow"

    # 恢复成功重置熔断
    router.record_channel_success("chan_fast")
    assert router.is_channel_available("chan_fast") is True
