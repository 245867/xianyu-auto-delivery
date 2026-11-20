import asyncio
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from loguru import logger

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

import db_manager
import product_template
import rate_limiter
from account_nurturing import AccountNurtureScheduler, AccountTraffic48h


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "accounts.db"
    manager = db_manager.DBManager(str(path))
    monkeypatch.setattr(db_manager, "db_manager", manager)
    monkeypatch.setattr(rate_limiter, "CIRCUIT_FILE", tmp_path / "circuit.json")
    yield manager
    manager.conn.close()


def test_state_is_account_scoped_and_persists_in_wal(database):
    database.update_account_nurture_state("north", base_lat=39.9, base_lon=116.4,
                                         current_city="北京")
    database.update_account_nurture_state("south", base_lat=23.1, base_lon=113.3,
                                         current_city="广州")
    database.update_account_nurture_state("south", health_score=62.5, is_silent_mode=True)

    reopened = db_manager.DBManager(database.db_path)
    try:
        assert reopened.conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        first = reopened.get_account_nurture_state("north")
        second = reopened.get_account_nurture_state("south")
        assert (first["current_city"], first["health_score"], first["is_silent_mode"]) == ("北京", 100, False)
        assert (second["base_lat"], second["base_lon"], second["current_city"]) == (23.1, 113.3, "广州")
        assert (second["health_score"], second["is_silent_mode"]) == (62.5, True)
        assert reopened.get_account_nurture_state("missing") is None
    finally:
        reopened.conn.close()


def test_offline_sessions_audit_and_roll_over_without_recovering_health(database):
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)), level="INFO")
    database.update_account_nurture_state("shop", base_lat=31.2, base_lon=121.5,
                                         current_city="上海", is_silent_mode=True)
    scheduler = AccountNurtureScheduler(database, rng=random.Random(2))
    before_midnight = datetime.fromisoformat("2026-11-20T23:59:00+08:00")
    after_midnight = datetime.fromisoformat("2026-11-21T00:01:00+08:00")
    try:
        events = scheduler.simulate_session("shop", "二手书", when=before_midnight)
        assert [event["action"] for event in events] == ["home_feed", "local_trending", "category_search"]
        assert 5 <= events[1]["dwell_seconds"] <= 15
        assert events[1]["city"] == "上海"
        assert events[2]["keyword"] == "二手书"
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(
                lambda _: scheduler.simulate_session("shop", "二手书", when=before_midnight),
                range(4),
            ))
        assert database.get_account_nurture_state("shop", when=before_midnight)["daily_actions"] == 5
        assert database.get_account_nurture_state("shop", when=after_midnight)["daily_actions"] == 0
        scheduler.simulate_session("shop", "二手书", when=after_midnight)
        state = database.get_account_nurture_state("shop", when=after_midnight)
        assert state["daily_actions"] == 1
        assert state["last_warmed_at"]
        assert state["is_silent_mode"] is True
        assert sum("离线养号会话已记录" in message for message in logs) == 6
    finally:
        logger.remove(sink)


def test_exposure_cutoff_and_explicit_recovery(database):
    scheduler = AccountNurtureScheduler(database)
    baseline = AccountTraffic48h(exposures=500, views=100)

    def reconcile(recent, previous, score):
        scheduler.reconcile_traffic(
            "shop", recent=recent, previous=previous, min_exposure_ratio=0.5,
            health_score=score, recovery_health_score=80,
        )
        return database.get_account_nurture_state("shop")

    assert reconcile(AccountTraffic48h(510, 0), baseline, 90)["is_silent_mode"] is False
    assert reconcile(AccountTraffic48h(40, 10), baseline, 30)["is_silent_mode"] is True
    assert reconcile(AccountTraffic48h(0, 0), AccountTraffic48h(0, 0), 100)["is_silent_mode"] is True
    assert reconcile(baseline, baseline, 79)["is_silent_mode"] is True
    recovered = reconcile(baseline, baseline, 80)
    assert recovered["is_silent_mode"] is False
    assert recovered["health_score"] == 80


def test_repeated_platform_blocks_latch_only_the_affected_account(database):
    rate_limiter.trip_circuit("shop", reason="测试平台安全拦截")
    state = database.get_account_nurture_state("shop")
    assert state["health_score"] == 75
    assert state["is_silent_mode"] is False
    rate_limiter.trip_circuit("shop", reason="测试平台安全拦截")
    state = database.get_account_nurture_state("shop")
    assert state["health_score"] == 50
    assert state["is_silent_mode"] is True
    assert database.get_account_nurture_state("other") is None

    scheduler = AccountNurtureScheduler(database)
    for reset in (False, True):
        if reset:
            rate_limiter.reset_circuit("shop")
        scheduler.reconcile_traffic(
            "shop", recent=AccountTraffic48h(100, 50), previous=AccountTraffic48h(100, 50),
            min_exposure_ratio=0.5, health_score=100, recovery_health_score=80,
        )
        assert database.get_account_nurture_state("shop")["is_silent_mode"] is (not reset)


def test_seller_daily_statistics_apply_each_drop_once(database):
    scheduler = AccountNurtureScheduler(database)
    response = {
        "api": "mtop.taobao.idle.seller.stat.get", "v": "1.0", "ret": ["SUCCESS::调用成功"],
        "data": {"accountId": "2219399967630", "dailyStats": [
            {"date": "2026-11-18", "exposure": 1250, "pv": 380, "uv": 210, "inquiry": 35},
            {"date": "2026-11-19", "exposure": 420, "pv": 110, "uv": 65, "inquiry": 8},
        ]},
    }
    with pytest.raises(ValueError):
        scheduler.reconcile_seller_stats("shop", "different-account", response)
    assert database.get_account_nurture_state("shop") is None

    assert scheduler.reconcile_seller_stats("shop", "2219399967630", response)
    assert not scheduler.reconcile_seller_stats("shop", "2219399967630", response)
    state = database.get_account_nurture_state("shop")
    assert state["health_score"] == 65
    assert state["is_silent_mode"] is False

    rows = response["data"]["dailyStats"]
    rows[0].update(date="2026-11-20", exposure=50)
    rows[1].update(date="2026-11-21", exposure=0)
    assert not scheduler.reconcile_seller_stats("shop", "2219399967630", response)
    rows[0]["exposure"], rows[1]["exposure"] = 100, 40
    assert scheduler.reconcile_seller_stats("shop", "2219399967630", response)
    state = database.get_account_nurture_state("shop")
    assert state["health_score"] == 30
    assert state["is_silent_mode"] is True


def test_quiet_daily_recovery_is_once_only_and_late_block_revokes_credit(database, monkeypatch):
    database.update_account_nurture_state("shop", base_lat=31.2, base_lon=121.5, current_city="上海",
                                         health_score=50, is_silent_mode=True)
    scheduler = AccountNurtureScheduler(database, rng=random.Random(2))
    first = datetime.fromisoformat("2026-11-20T12:00:00+08:00")
    second = datetime.fromisoformat("2026-11-21T12:00:00+08:00")
    for when, expected_score in ((first, 65), (second, 80)):
        for _ in range(4):
            scheduler.simulate_session("shop", "二手书", when=when)
        state = database.get_account_nurture_state("shop", when=when)
        assert state["health_score"] == expected_score
        assert state["is_silent_mode"] is (expected_score < 80)

    monkeypatch.setattr(rate_limiter.time, "time", lambda: second.timestamp())
    rate_limiter.trip_circuit("shop", reason="测试同一天的延迟拦截")
    for _ in range(3):
        scheduler.simulate_session("shop", "二手书", when=second)
    state = database.get_account_nurture_state("shop", when=second)
    assert state["health_score"] == 40
    assert state["is_silent_mode"] is True


def test_silent_mode_blocks_automatic_delivery_before_processing(database, monkeypatch):
    from XianyuAutoAsync import XianyuLive

    database.update_account_nurture_state("shop", is_silent_mode=True)
    monkeypatch.setattr(database, "get_item_info", Mock())
    monkeypatch.setattr("delivery_reliability.get_config", Mock())
    monkeypatch.setattr("delivery_reliability.pending_platform_shipments", Mock())
    account = SimpleNamespace(cookie_id="shop", _is_auto_delivery_trigger=lambda *_: True,
                              _send_delivery_card=AsyncMock())

    asyncio.run(XianyuLive._handle_auto_delivery(account, None, {}, "", "", "item", "", ""))
    assert asyncio.run(XianyuLive._resend_undelivered(account, None)) == 0
    assert asyncio.run(XianyuLive._retry_pending_confirms(account)) == 0
    database.get_item_info.assert_not_called()
    account._send_delivery_card.assert_not_awaited()

    import delivery_reliability

    delivery_reliability.get_config.assert_not_called()
    delivery_reliability.pending_platform_shipments.assert_not_called()


def test_silent_mode_keeps_physical_order_pending(database, monkeypatch):
    import dropshipping

    database.update_account_nurture_state("shop", is_silent_mode=True)
    order = {"order_status": "pending_ship", "dropship_status": "shipped"}
    monkeypatch.setattr(database, "dropship_orders_to_sync", Mock(return_value=["order"]))
    monkeypatch.setattr(database, "get_dropship_order", Mock(return_value=order))
    ship = Mock()
    monkeypatch.setattr(dropshipping, "_ship", ship)

    dropshipping.sync_account(database, "offline", "shop")
    ship.assert_not_called()
    database.update_account_nurture_state("shop", is_silent_mode=False)
    dropshipping.sync_account(database, "offline", "shop")
    ship.assert_called_once()


@pytest.mark.parametrize("silent_while_waiting", [False, True])
def test_batch_pauses_without_consuming_pending_row(database, tmp_path, monkeypatch, silent_while_waiting):
    import idempotency

    monkeypatch.setattr(product_template, "DB_PATH", tmp_path / "templates.db")
    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "idempotency.db")
    monkeypatch.setattr(product_template, "_night_ban", lambda: (True, ""))
    publish = Mock(return_value={"itemId": "123"})
    monkeypatch.setattr(product_template, "_publish_one", publish)
    account = "batch-shop"
    database.update_account_nurture_state(account, is_silent_mode=not silent_while_waiting)

    def wait(*args, **kwargs):
        database.update_account_nurture_state(account, is_silent_mode=True)
        return True

    quota = Mock(side_effect=wait)
    monkeypatch.setattr(rate_limiter, "wait", quota)
    template = product_template.create_template({
        "name": "二手", "title_tpl": "{{name}}", "desc_tpl": "支持验货", "price": 10,
    })
    job_id = product_template.create_job(
        template["id"], [{"name": "二手书", "price": 10}], account, dry_run=False,
    )["job_id"]
    job = product_template.run_job(job_id)["job"]
    assert job["status"] == "paused"
    assert job["items"][0]["status"] == "pending"
    assert quota.call_count == int(silent_while_waiting)
    publish.assert_not_called()

    database.update_account_nurture_state(account, is_silent_mode=False)
    quota.side_effect = None
    quota.return_value = True
    assert product_template.run_job(job_id)["job"]["items"][0]["status"] == "done"
