import sys
from datetime import datetime
from pathlib import Path

# 确保 backend 位于 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from order_chaser import (
    is_night_quiet_hours,
    decide_chase_action,
    get_chase_message,
    STAGE_1_TEXT,
    STAGE_2_TEXT,
)


def test_is_night_quiet_hours():
    # 夜间 23:00 ~ 08:00
    assert is_night_quiet_hours(datetime(2026, 11, 20, 23, 15)) is True
    assert is_night_quiet_hours(datetime(2026, 11, 20, 2, 30)) is True
    assert is_night_quiet_hours(datetime(2026, 11, 20, 7, 59)) is True

    # 白天非静默期
    assert is_night_quiet_hours(datetime(2026, 11, 20, 8, 0)) is False
    assert is_night_quiet_hours(datetime(2026, 11, 20, 14, 30)) is False
    assert is_night_quiet_hours(datetime(2026, 11, 20, 22, 59)) is False


def test_decide_chase_action_terminal_status():
    action, reason = decide_chase_action(
        order_age_minutes=5.0,
        chase_count=0,
        is_terminal_status=True,
    )
    assert action == "SKIP_TERMINATED"
    assert "已付款或已终结" in reason


def test_decide_chase_action_buyer_talking():
    # 买家已发言沟通，必须立刻熔断挂起催付，防惹恼买家
    action, reason = decide_chase_action(
        order_age_minutes=5.0,
        chase_count=0,
        buyer_has_replied=True,
    )
    assert action == "SKIP_BUYER_TALKING"
    assert "已有最新回复" in reason


def test_decide_chase_action_timeout_expire():
    # 待付款超过 30 分钟，触发超时自愈与库存预占释放
    action, reason = decide_chase_action(
        order_age_minutes=35.0,
        chase_count=1,
    )
    assert action == "EXPIRE_RELEASE"
    assert "释放预占库存" in reason


def test_decide_chase_action_max_attempts():
    # 已催付 2 次，触碰单单催付上限
    action, reason = decide_chase_action(
        order_age_minutes=15.0,
        chase_count=2,
    )
    assert action == "SKIP_MAX_ATTEMPTS"
    assert "最大催付次数" in reason


def test_decide_chase_action_night_silent():
    # 夜间静默免打扰
    action, reason = decide_chase_action(
        order_age_minutes=5.0,
        chase_count=0,
        is_night_time=True,
    )
    assert action == "SKIP_NIGHT_SILENT"
    assert "夜间免打扰" in reason


def test_decide_chase_action_stage_1():
    # 待付 4 分钟，未催付过 -> 触发第 1 阶柔和排单关怀
    action, reason = decide_chase_action(
        order_age_minutes=4.0,
        chase_count=0,
    )
    assert action == "CHASE_STAGE_1"
    assert "第 1 阶" in reason


def test_decide_chase_action_stage_2():
    # 待付 12 分钟，已催付 1 次 -> 触发第 2 阶紧迫感逼单
    action, reason = decide_chase_action(
        order_age_minutes=12.0,
        chase_count=1,
    )
    assert action == "CHASE_STAGE_2"
    assert "第 2 阶" in reason


def test_decide_chase_action_wait_window():
    # 刚拍下 1 分钟 -> 处于正常等待期
    action, reason = decide_chase_action(
        order_age_minutes=1.0,
        chase_count=0,
    )
    assert action == "WAIT_NEXT_WINDOW"


def test_get_chase_message():
    assert get_chase_message(1) == STAGE_1_TEXT
    assert get_chase_message(2) == STAGE_2_TEXT
    assert get_chase_message(1, custom_text="自定义话术") == "自定义话术"
