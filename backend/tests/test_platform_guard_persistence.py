"""平台风控持久化守卫的回归测试。

覆盖四件事：
[1] 四状态迁移与 wall-clock 绝对秒罚期；
[2] 进程重启后罚期不重置（crash-loop 防护）；
[3] 原子写、无临时文件残留、损坏状态文件保守拦截；
[4] 小时/日预算，以及现有请求入口已经接线。
"""

import json
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

import platform_guard as pg


def test_state_and_budget_paths_follow_cookie_id(tmp_path):
    guard = pg.PlatformGuard("acc1", state_dir=tmp_path)

    assert guard.state_path == tmp_path / "platform_guard.acc1.json"
    assert guard.budget_path == tmp_path / "platform_guard.budget.acc1.json"

    snapshot = guard.snapshot(now=1000.0)
    assert snapshot["state"] == "normal"
    assert snapshot["retry_seconds"] == 0


def test_transitions_escalate_from_throttled_to_cooling_to_blocked(tmp_path):
    guard = pg.PlatformGuard(
        "acc1",
        state_dir=tmp_path,
        throttle_cooldown_seconds=300,
        cooling_cooldown_seconds=900,
        block_cooldown_seconds=3600,
    )
    now = 1_700_000_000.0

    guard.record_throttle("HTTP 429", now=now)
    first = json.loads(guard.state_path.read_text(encoding="utf-8"))
    assert first["state"] == "throttled"
    assert first["cooldown_until"] == now + 300
    assert first["updated_at"] == now

    guard.record_throttle("still limited", now=now + 10)
    second = json.loads(guard.state_path.read_text(encoding="utf-8"))
    assert second["state"] == "cooling"
    assert second["cooldown_until"] == now + 10 + 900

    guard.record_risk("RGV587", now=now + 20)
    third = json.loads(guard.state_path.read_text(encoding="utf-8"))
    assert third["state"] == "blocked"
    assert third["cooldown_until"] == now + 20 + 3600


def test_restart_reads_absolute_penalty_from_disk_without_reset(tmp_path):
    now = 1_700_000_000.0
    first = pg.PlatformGuard("acc1", state_dir=tmp_path, cooling_cooldown_seconds=600)
    first.record_risk("PUNISH", now=now)

    second = pg.PlatformGuard("acc1", state_dir=tmp_path, cooling_cooldown_seconds=600)
    allowed, retry = second.acquire("read", now=now + 10, jitter=False)

    assert allowed is False
    assert retry == 590
    assert second.snapshot(now=now + 10)["state"] == "cooling"


def test_success_before_penalty_expiry_cannot_clear_penalty(tmp_path):
    now = 1_700_000_000.0
    guard = pg.PlatformGuard("acc1", state_dir=tmp_path, block_cooldown_seconds=3600)
    guard.record_block("USER_VALIDATE", now=now)

    guard.record_success(now=now + 30)

    assert guard.snapshot(now=now + 30)["state"] == "blocked"
    allowed, retry = guard.acquire("read", now=now + 30, jitter=False)
    assert allowed is False
    assert retry == 3570


def test_success_after_penalty_expiry_returns_to_normal(tmp_path):
    now = 1_700_000_000.0
    guard = pg.PlatformGuard("acc1", state_dir=tmp_path, cooling_cooldown_seconds=60)
    guard.record_risk("RGV587", now=now)

    guard.record_success(now=now + 61)

    assert guard.snapshot(now=now + 61)["state"] == "normal"
    assert guard.acquire("read", now=now + 61, jitter=False)[0] is True


def test_corrupt_state_file_blocks_conservatively_and_is_not_reset(tmp_path):
    state_path = tmp_path / "platform_guard.acc1.json"
    state_path.write_text("{not-json", encoding="utf-8")

    guard = pg.PlatformGuard("acc1", state_dir=tmp_path, corrupt_hold_seconds=900)
    allowed, retry = guard.acquire("read", now=5000.0, jitter=False)
    snapshot = guard.snapshot(now=5000.0)

    assert allowed is False
    assert 0 < retry <= 900
    assert snapshot["state"] == "blocked"
    assert snapshot["corrupt"] is True
    assert state_path.read_text(encoding="utf-8") == "{not-json"


def test_atomic_write_leaves_no_temporary_file(tmp_path):
    guard = pg.PlatformGuard("acc1", state_dir=tmp_path)
    for tick in range(5):
        guard.record_throttle("HTTP 429", now=1000.0 + tick)

    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    json.loads(guard.state_path.read_text(encoding="utf-8"))


def test_hourly_and_daily_budgets_persist_across_instances(tmp_path):
    now = 1_700_000_000.0
    guard = pg.PlatformGuard(
        "acc1", state_dir=tmp_path, hourly_budget=2, daily_budget=3
    )

    assert guard.acquire("read", now=now, jitter=False)[0] is True
    assert guard.acquire("read", now=now + 1, jitter=False)[0] is True
    allowed, retry = guard.acquire("read", now=now + 2, jitter=False)
    assert allowed is False
    assert retry > 0

    restarted = pg.PlatformGuard(
        "acc1", state_dir=tmp_path, hourly_budget=2, daily_budget=3
    )
    assert restarted.snapshot(now=now + 3)["hour_used"] == 2

    next_hour = now + 3600 + 1
    assert restarted.acquire("read", now=next_hour, jitter=False)[0] is True
    allowed, retry = restarted.acquire("read", now=next_hour + 1, jitter=False)
    assert allowed is False
    assert retry > 0

    next_day = now + 86400 + 1
    assert restarted.acquire("read", now=next_day, jitter=False)[0] is True


def test_jitter_seconds_stays_inside_configured_range(tmp_path):
    guard = pg.PlatformGuard(
        "acc1",
        state_dir=tmp_path,
        jitter_min_seconds=0.2,
        jitter_max_seconds=0.4,
    )

    for _ in range(20):
        delay = guard.jitter_seconds()
        assert 0.2 <= delay <= 0.4


def test_existing_request_entrypoints_are_wired_to_platform_guard():
    for name in (
        "goofish_open.py",
        "opportunity_publisher.py",
        "care_message.py",
        "sold_out_relist.py",
        "seller_api.py",
        "polish_service.py",
    ):
        source = (BACKEND / name).read_text(encoding="utf-8")
        assert "platform_guard" in source, f"{name} 尚未接入 platform_guard"
