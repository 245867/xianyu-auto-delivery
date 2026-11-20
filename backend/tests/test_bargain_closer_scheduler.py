import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

from db_manager import DBManager


@pytest.fixture
def database(tmp_path):
    db = DBManager(str(tmp_path / "bargain-closer.db"))
    with db.conn:
        db.conn.execute(
            "INSERT INTO item_info (cookie_id, item_id, item_price, is_multi_spec) "
            "VALUES ('shop', 'item', '100.00', 0)"
        )
    yield db
    db.conn.close()


def _stamp(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _order(database, order_id="o-1", created_at=None, status="processing",
           buyer_id="buyer-1", item_id="item", cookie_id="shop"):
    with database.conn:
        database.conn.execute(
            "INSERT INTO orders (order_id, item_id, buyer_id, order_status, cookie_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, item_id, buyer_id, status, cookie_id,
             _stamp(created_at or datetime(2026, 11, 20, 10, 5, tzinfo=timezone.utc))),
        )


def _closer(database, *, apply_ok=True, alert=None):
    from bargain_closer import BargainCloser

    apply_concession = Mock(return_value=apply_ok)
    restore_price = Mock(return_value=True)
    release_stock = Mock(return_value=True)
    closer = BargainCloser(
        database,
        apply_concession=apply_concession,
        restore_price=restore_price,
        release_stock=release_stock,
        alert=alert or Mock(),
    )
    return closer, apply_concession, restore_price, release_stock


def test_buyer_rate_verdict_matches_platform_contract():
    import care_message

    assert care_message.buyer_rate_verdict({"buyerRateStatus": "4", "rateResult": "1"}) == "good"
    assert care_message.buyer_rate_verdict({"buyerRateStatus": "4", "rateResult": "0"}) == "bad"
    assert care_message.buyer_rate_verdict({"buyerRateStatus": "4", "rateResult": "-1"}) == "bad"
    assert care_message.buyer_rate_verdict({"buyerRateStatus": "5"}) == "pending"
    assert care_message.buyer_rate_verdict({}) == "unknown"
    assert care_message.buyer_rate_verdict(None) == "unknown"


def test_create_session_lasts_fifteen_minutes(database):
    closer, _apply, _restore, _release = _closer(database)
    now = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)

    session = closer.create_session(
        "shop", "buyer-1", "item", 100.0, 90.0, now=now,
    )

    assert session["status"] == "active"
    assert session["expires_at"] == _stamp(now + timedelta(minutes=15))
    assert session["original_price"] == 100.0
    assert session["concession_price"] == 90.0


def test_order_inside_window_consumes_session_and_applies_concession(database):
    closer, apply_concession, _restore, _release = _closer(database)
    closer.create_session(
        "shop", "buyer-1", "item", 100.0, 90.0,
        now=datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc),
    )
    _order(database, created_at=datetime(2026, 11, 20, 10, 5, tzinfo=timezone.utc))

    result = closer.scan_once(now=datetime(2026, 11, 20, 10, 6, tzinfo=timezone.utc))

    assert result == {"expired": 0, "consumed": 1}
    apply_concession.assert_called_once_with("o-1", "shop", 90.0)
    assert database.list_bargain_sessions("consumed")[0]["buyer_id"] == "buyer-1"
    assert database.list_bargain_sessions("consumed")[0]["item_id"] == "item"


def test_expired_session_restores_price_and_releases_stock(database):
    closer, _apply, restore_price, release_stock = _closer(database)
    closer.create_session(
        "shop", "buyer-1", "item", 100.0, 90.0,
        now=datetime(2026, 11, 20, 9, 40, tzinfo=timezone.utc),
    )

    result = closer.scan_once(now=datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc))

    assert result == {"expired": 1, "consumed": 0}
    restore_price.assert_called_once_with("shop", "item", 100.0)
    release_stock.assert_called_once_with("shop", "item")
    assert database.list_bargain_sessions("expired")[0]["session_id"]


def test_order_after_expiry_is_not_consumed(database):
    closer, apply_concession, _restore, _release = _closer(database)
    closer.create_session(
        "shop", "buyer-1", "item", 100.0, 90.0,
        now=datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc),
    )
    _order(database, created_at=datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc))

    result = closer.scan_once(now=datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc))

    assert result == {"expired": 1, "consumed": 0}
    apply_concession.assert_not_called()


def test_scan_is_idempotent(database):
    closer, apply_concession, restore_price, release_stock = _closer(database)
    now = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    closer.create_session("shop", "buyer-1", "item", 100.0, 90.0, now=now)
    _order(database, created_at=now + timedelta(minutes=5))
    closer.create_session(
        "shop", "buyer-2", "item", 100.0, 80.0,
        now=now - timedelta(minutes=30),
    )

    closer.scan_once(now=now + timedelta(minutes=6))
    closer.scan_once(now=now + timedelta(minutes=7))

    assert apply_concession.call_count == 1
    assert restore_price.call_count == 1
    assert release_stock.call_count == 1


def test_failed_concession_marks_session_failed_and_alerts(database):
    alert = Mock()
    closer, apply_concession, _restore, _release = _closer(
        database, apply_ok=False, alert=alert,
    )
    now = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    closer.create_session("shop", "buyer-1", "item", 100.0, 90.0, now=now)
    _order(database, created_at=now + timedelta(minutes=1))

    result = closer.scan_once(now=now + timedelta(minutes=2))

    assert result == {"expired": 0, "consumed": 0}
    apply_concession.assert_called_once()
    assert database.list_bargain_sessions("failed")[0]["session_id"]
    alert.assert_called_once()


def test_active_session_without_order_stays_active(database):
    closer, apply_concession, _restore, _release = _closer(database)
    now = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    closer.create_session("shop", "buyer-1", "item", 100.0, 90.0, now=now)

    assert closer.scan_once(now=now + timedelta(minutes=5)) == {"expired": 0, "consumed": 0}
    apply_concession.assert_not_called()
    assert len(database.list_bargain_sessions("active")) == 1


def test_duplicate_accept_reuses_active_session(database):
    closer, _apply, _restore, _release = _closer(database)
    now = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)

    first = closer.create_session("shop", "buyer-1", "item", 100.0, 90.0, now=now)
    second = closer.create_session(
        "shop", "buyer-1", "item", 100.0, 85.0, now=now + timedelta(minutes=1),
    )

    assert second["session_id"] == first["session_id"]
    assert len(database.list_bargain_sessions()) == 1


def test_bargain_tables_stay_wal(database):
    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
