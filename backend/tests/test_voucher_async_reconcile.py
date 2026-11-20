import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

import delivery_reliability
import goofish_open
import idempotency
import rate_limiter
from db_manager import DBManager


@pytest.fixture
def database(tmp_path, monkeypatch):
    db = DBManager(str(tmp_path / "voucher.db"))
    monkeypatch.setattr(delivery_reliability, "DB_PATH", tmp_path / "delivery.db")
    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "idempotency.db")
    monkeypatch.setattr(rate_limiter, "CIRCUIT_FILE", tmp_path / "circuit.json")
    assert db.save_cookie("shop", "offline")
    assert db.insert_or_update_order(
        "order", cookie_id="shop", item_id="item", buyer_id="buyer", order_status="pending_ship",
    )
    yield db
    db.conn.close()


@pytest.fixture
def route(database):
    from voucher_async_reconcile import VoucherAsyncReconcile

    primary, backup = Mock(), Mock()
    alert = Mock()
    now = datetime(2026, 11, 20, 7, tzinfo=timezone.utc)
    engine = VoucherAsyncReconcile(
        database, {"channel_a": primary, "channel_b": backup}, timeout_seconds=600, alert=alert,
    )
    assert engine.enroll("order", "shop", "item", "offline-charge-account", ("channel_a", "channel_b"), now=now)
    return engine, primary, backup, alert, now


def test_out_of_stock_fails_over_and_callback_settles_once(route, database):
    from voucher_async_reconcile import TopupResult

    engine, primary, backup, alert, now = route
    primary.submit.return_value = TopupResult("OUT_OF_STOCK")
    backup.submit.return_value = TopupResult("topping_up", "backup-1")
    assert engine.dispatch("order", now=now) == "topping_up"
    first = primary.submit.call_args.args[0]
    second = backup.submit.call_args.args[0]
    assert first.request_id != second.request_id
    assert second.charge_account == "offline-charge-account"
    assert not engine.record_callback("channel_a", second.request_id, TopupResult("success", "backup-1"), now=now)
    assert not engine.record_callback("channel_b", second.request_id, TopupResult("success", "wrong"), now=now)
    assert engine.record_callback("channel_b", second.request_id, TopupResult("success", "backup-1"), now=now)
    assert engine.record_callback("channel_b", second.request_id, TopupResult("success", "backup-1"), now=now)
    assert not engine.record_callback("channel_b", second.request_id, TopupResult("failed", "backup-1"), now=now)
    assert engine.get("order", "shop")["topup_status"] == "success"
    assert engine.get("order", "shop")["attempts"] == 2
    assert database.conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    alert.assert_not_called()


def test_network_timeout_requires_confirmed_non_submission_before_failover(route):
    from voucher_async_reconcile import TopupResult

    engine, primary, backup, alert, now = route
    primary.submit.side_effect = TimeoutError
    assert engine.dispatch("order", now=now) == "submission_unknown"
    assert engine.dispatch("order", now=now) == "submission_unknown"
    primary.submit.assert_called_once()
    backup.submit.assert_not_called()
    primary.lookup.return_value = TopupResult("not_submitted")
    backup.submit.return_value = TopupResult("success", "backup-2")
    assert engine.dispatch("order", now=now + timedelta(minutes=10)) == "success"
    assert primary.lookup.call_args.args[0].request_id == primary.submit.call_args.args[0].request_id
    assert primary.lookup.call_args.args[1] is None
    backup.submit.assert_called_once()
    alert.assert_not_called()


def test_exhaustion_alert_is_once_and_terminal_failure_releases_freeze(route, database):
    from voucher_async_reconcile import REFUND_FREEZE_BLOCKED, TopupResult

    engine, primary, backup, alert, now = route
    primary.submit.return_value = TopupResult("OUT_OF_STOCK")
    backup.submit.return_value = TopupResult("topping_up", "backup-3")
    assert engine.dispatch("order", now=now) == "topping_up"
    assert database.insert_or_update_order("order", order_status="refunding")
    assert engine.refund_decision("order", "shop") == REFUND_FREEZE_BLOCKED
    assert engine.record_callback(
        "channel_b", backup.submit.call_args.args[0].request_id, TopupResult("failed", "backup-3"), now=now,
    )
    assert engine.refund_decision("order", "shop") is None
    assert engine.dispatch("order", now=now) == "failed"
    alert.assert_called_once_with("shop", "order")


def test_refund_reuses_official_reject_and_confirms_readback(route, database, monkeypatch):
    from voucher_async_reconcile import REFUND_EXPLANATION, TopupResult

    engine, primary, _, _, now = route
    primary.submit.return_value = TopupResult("topping_up", "primary-1")
    assert engine.dispatch("order", now=now) == "topping_up"
    assert database.insert_or_update_order("order", order_status="refunding")
    detail = {"order_no": "order", "refund_no": "refund-1", "seller_eid": "seller",
              "refund_status": 1, "refund_type": 1}

    def call(action, body, allow_write=False):
        if action == "aftersale.detail":
            return {"code": 0, "data": detail.copy()}
        if action == "aftersale.reject_reasons":
            return {"code": 0, "data": {"refuse_reason": ["verified reason"]}}
        assert action == "aftersale.reject" and allow_write
        assert body == {"order_no": "order", "refuse_reason": "verified reason",
                        "refuse_explain": REFUND_EXPLANATION}
        detail["refund_status"] = 6
        return {"code": 0}

    transport = Mock(side_effect=call)
    monkeypatch.setattr(goofish_open, "call", transport)
    engine.handle_refund("order", "shop", "seller", "verified reason", now=now)
    engine.handle_refund("order", "shop", "seller", "verified reason", now=now)
    assert sum(entry.args[0] == "aftersale.reject" for entry in transport.call_args_list) == 1
    assert engine.get("order", "shop")["refund_state"] == "rejected"


def test_persisted_claim_prevents_parallel_submit_and_allows_early_callback(route, database):
    from voucher_async_reconcile import TopupResult, VoucherAsyncReconcile

    engine, primary, backup, _, now = route
    other_engine = VoucherAsyncReconcile(
        database, {"channel_a": primary, "channel_b": backup}, timeout_seconds=600,
    )
    started, complete = Event(), Event()

    def submit(request):
        assert other_engine.record_callback(
            "channel_a", request.request_id, TopupResult("success", "primary-fast"), now=now,
        )
        started.set()
        assert complete.wait(5)
        return TopupResult("topping_up", "primary-fast")

    primary.submit.side_effect = submit
    with ThreadPoolExecutor() as pool:
        result = pool.submit(engine.dispatch, "order", now=now)
        try:
            assert started.wait(5)
            assert other_engine.dispatch("order", now=now) == "success"
        finally:
            complete.set()
        assert result.result() == "success"
    primary.submit.assert_called_once()
    backup.submit.assert_not_called()


def test_confirmed_topup_keeps_refund_frozen_when_lookup_times_out(route, database):
    from voucher_async_reconcile import REFUND_FREEZE_BLOCKED, TopupResult

    engine, primary, backup, alert, now = route
    primary.submit.return_value = TopupResult("topping_up", "primary-4")
    primary.lookup.side_effect = TimeoutError
    assert engine.dispatch("order", now=now) == "topping_up"
    assert database.insert_or_update_order("order", order_status="refunding")
    expired = now + timedelta(minutes=10)
    assert engine.dispatch("order", now=expired) == "topping_up"
    assert engine.refund_decision("order", "shop") == REFUND_FREEZE_BLOCKED
    backup.submit.assert_not_called()
    alert.assert_not_called()


def test_terminal_callback_during_refund_precheck_prevents_rejection(route, database, monkeypatch):
    from voucher_async_reconcile import TopupResult

    engine, primary, backup, _, now = route
    primary.submit.return_value = TopupResult("topping_up", "primary-5")
    assert engine.dispatch("order", now=now) == "topping_up"
    assert database.insert_or_update_order("order", order_status="refunding")
    request_id = primary.submit.call_args.args[0].request_id
    detail = {"order_no": "order", "refund_no": "refund-2", "seller_eid": "seller",
              "refund_status": 1, "refund_type": 1}

    def call(action, body, allow_write=False):
        if action == "aftersale.detail":
            return {"code": 0, "data": detail.copy()}
        assert action == "aftersale.reject_reasons"
        assert engine.record_callback("channel_a", request_id, TopupResult("failed", "primary-5"), now=now)
        return {"code": 0, "data": {"refuse_reason": ["verified reason"]}}

    monkeypatch.setattr(goofish_open, "call", call)
    engine.handle_refund("order", "shop", "seller", "verified reason", now=now)
    assert engine.get("order", "shop")["topup_status"] == "failed"
    assert engine.get("order", "shop")["refund_state"] == "released"
    backup.submit.assert_not_called()


def test_configured_route_reserves_only_eligible_unfulfilled_order(database):
    from voucher_async_reconcile import VoucherAsyncReconcile

    engine = VoucherAsyncReconcile(database, {"channel_a": Mock()}, timeout_seconds=600,
                                   item_channels={"shop": {"item": ["channel_a"]}})
    delivery_reliability.ensure_order("order", "shop", ["existing voucher"], item_id="item", buyer_id="buyer")
    assert not engine.enroll("order", "shop", "item", "offline-charge-account")
    assert database.insert_or_update_order(
        "fresh", cookie_id="shop", item_id="item", buyer_id="buyer", order_status="pending_ship",
    )
    assert engine.enroll("fresh", "shop", "item", "offline-charge-account")
    assert database.is_voucher_async_order("fresh", "shop")
    assert not database.is_voucher_async_order("fresh", "other")
    assert "charge_account" not in engine.get("fresh", "shop")
    assert not engine.enroll("fresh", "shop", "item", "different-charge-account")


def test_lookup_confirms_terminal_failure_and_releases_refund(route, database):
    from voucher_async_reconcile import REFUND_FREEZE_BLOCKED, TopupResult

    engine, primary, backup, _, now = route
    primary.submit.return_value = TopupResult("topping_up", "primary-failed")
    primary.lookup.return_value = TopupResult("failed", "primary-failed")
    assert engine.dispatch("order", now=now) == "topping_up"
    assert database.insert_or_update_order("order", order_status="refunding")
    assert engine.handle_refund("order", "shop", None, None, now=now) == REFUND_FREEZE_BLOCKED
    expired = now + timedelta(minutes=10)
    assert engine.dispatch("order", now=expired) == "failed"
    assert engine.get("order", "shop")["refund_state"] == "released"
    assert engine.refund_decision("order", "shop") is None
    backup.submit.assert_not_called()


def test_existing_card_at_dispatch_prevents_topup(route):
    engine, primary, backup, _, now = route
    delivery_reliability.ensure_order("order", "shop", ["existing voucher"], item_id="item", buyer_id="buyer")
    assert engine.dispatch("order", now=now) == "failed"
    primary.submit.assert_not_called()
    backup.submit.assert_not_called()


def test_cancelled_before_submit_does_not_leave_queued_record(route, database):
    engine, primary, _, _, now = route
    assert database.insert_or_update_order("order", order_status="cancelled")
    assert engine.dispatch("order", now=now) == "failed"
    assert engine.get("order", "shop")["topup_status"] == "failed"
    primary.submit.assert_not_called()


def test_unknown_submission_is_not_failover_or_refund_rejected(route, database):
    engine, primary, backup, _, now = route
    primary.submit.side_effect = TimeoutError
    assert engine.dispatch("order", now=now) == "submission_unknown"
    assert database.insert_or_update_order("order", order_status="refunding")
    assert engine.handle_refund("order", "shop", None, None) == "SUBMISSION_PENDING_LOOKUP"
    assert engine.get("order", "shop")["topup_status"] == "submission_unknown"
    backup.submit.assert_not_called()
