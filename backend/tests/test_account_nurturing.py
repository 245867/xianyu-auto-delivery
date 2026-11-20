import asyncio
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from account_nurturing import AccountNurturing, PlatformChallenge


def test_daily_schedule_quota_and_deduplication(tmp_path):
    path = tmp_path / "nurturing.db"
    engine = AccountNurturing(path, rng=random.Random(4))
    first = engine.schedule("shop", "2026-11-20")
    assert 1 <= len(first) <= 2
    assert all(10 * 3600 <= slot < 21 * 3600 for slot in first)
    assert AccountNurturing(path).schedule("shop", "2026-11-20") == first
    assert [engine.reserve_action("shop", "2026-11-20") for _ in range(6)] == [True] * 5 + [False]
    assert engine.reserve_action("shop", "2026-11-21")


def test_challenge_stops_remaining_actions_and_daily_slots(tmp_path):
    engine = AccountNurturing(tmp_path / "nurturing.db", rng=random.Random(5))
    calls = []
    delays = []

    class Transport:
        async def recommendations(self, account):
            calls.append("feed")
            return ["100", "200"]

        async def detail(self, account, item):
            calls.append("detail")
            raise PlatformChallenge("RGV587")

        async def interact(self, account, item):
            calls.append("interaction")

    async def sleep(delay):
        delays.append(delay)

    async def run():
        when = datetime.fromisoformat("2026-11-20T20:59:59+08:00")
        assert await engine.run_due("shop", Transport(), when=when, sleep=sleep) == "challenge"
        assert await engine.run_due("shop", Transport(), when=when, sleep=sleep) == "idle"

    asyncio.run(run())
    assert calls == ["feed", "detail"]
    assert delays and all(3 <= delay <= 10 for delay in delays)
