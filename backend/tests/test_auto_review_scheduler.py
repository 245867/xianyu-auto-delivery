import sqlite3
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

from db_manager import DBManager


@pytest.fixture
def database(tmp_path):
    db = DBManager(str(tmp_path / "auto-review.db"))
    with db.conn:
        db.conn.execute(
            "INSERT INTO item_info (cookie_id, item_id, item_price, is_multi_spec, review_gift_card) "
            "VALUES ('shop', 'item', '9.90', 0, '')"
        )
    yield db
    db.conn.close()


def _order(database, order_id="o-1", status="completed", cookie_id="shop",
           buyer_id="buyer-1", item_id="item"):
    with database.conn:
        database.conn.execute(
            "INSERT INTO orders (order_id, item_id, buyer_id, order_status, cookie_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (order_id, item_id, buyer_id, status, cookie_id),
        )
    return order_id


def _scheduler(database, *, rating="good", found=True, trade_id="trade-1",
               submit_ok=True, gift="", gift_ok=True, has_refund=False, alert=None):
    from auto_review import AutoReviewScheduler

    find_pending = Mock(return_value={"ok": True, "found": found, "trade_id": trade_id})
    submit_rate = Mock(return_value={"ok": submit_ok, "kind": "ok" if submit_ok else "failed"})
    gift_card = Mock(return_value=gift)
    send_gift = Mock(return_value=gift_ok)
    scheduler = AutoReviewScheduler(
        database,
        buyer_rating=lambda order: {"status": rating},
        find_pending=find_pending,
        submit_rate=submit_rate,
        gift_card=gift_card,
        send_gift=send_gift,
        feedback="感谢支持，非常爽快的好买家！",
        has_refund=lambda order_id: has_refund,
        alert=alert or Mock(),
    )
    return scheduler, find_pending, submit_rate, send_gift


def test_good_buyer_review_triggers_auto_rate(database):
    _order(database)
    scheduler, find_pending, submit_rate, _send_gift = _scheduler(database)

    result = scheduler.scan_once()

    assert result == {"scanned": 1, "rated": 1, "suspended": 0, "gifted": 0}
    find_pending.assert_called_once_with("shop", "o-1")
    submit_rate.assert_called_once_with("shop", "trade-1", "感谢支持，非常爽快的好买家！")
    record = database.get_auto_review_record("o-1")
    assert record["buyer_rate_status"] == "good"
    assert record["seller_rate_status"] == "rated"


def test_buyer_bad_review_suspends_and_never_rates(database):
    _order(database)
    alert = Mock()
    scheduler, _find_pending, submit_rate, send_gift = _scheduler(
        database, rating="bad", gift="赠品", alert=alert,
    )

    result = scheduler.scan_once()

    assert result == {"scanned": 1, "rated": 0, "suspended": 1, "gifted": 0}
    submit_rate.assert_not_called()
    send_gift.assert_not_called()
    alert.assert_called_once()
    record = database.get_auto_review_record("o-1")
    assert record["buyer_rate_status"] == "bad"
    assert record["seller_rate_status"] == "RATE_SUSPENDED"
    assert record["gift_sent"] is False


def test_refund_record_suspends_even_when_buyer_rated_good(database):
    _order(database)
    scheduler, _find_pending, submit_rate, _send_gift = _scheduler(
        database, has_refund=True,
    )

    result = scheduler.scan_once()

    assert result == {"scanned": 1, "rated": 0, "suspended": 1, "gifted": 0}
    submit_rate.assert_not_called()
    assert database.get_auto_review_record("o-1")["seller_rate_status"] == "RATE_SUSPENDED"


def test_buyer_pending_keeps_unrated_without_suspending(database):
    _order(database)
    scheduler, _find_pending, submit_rate, _send_gift = _scheduler(database, rating="pending")

    result = scheduler.scan_once()

    assert result == {"scanned": 1, "rated": 0, "suspended": 0, "gifted": 0}
    submit_rate.assert_not_called()
    assert database.get_auto_review_record("o-1")["seller_rate_status"] == "unrated"


def test_unknown_buyer_rating_is_fail_closed(database):
    _order(database)
    scheduler, _find_pending, submit_rate, _send_gift = _scheduler(database, rating="unknown")

    result = scheduler.scan_once()

    assert result == {"scanned": 1, "rated": 0, "suspended": 0, "gifted": 0}
    submit_rate.assert_not_called()
    assert database.get_auto_review_record("o-1")["seller_rate_status"] == "unrated"


def test_gift_is_sent_once_after_good_review(database):
    _order(database)
    scheduler, _find_pending, _submit_rate, send_gift = _scheduler(
        database, gift="百度网盘下载凭证已发送",
    )

    first = scheduler.scan_once()
    second = scheduler.scan_once()

    assert first["gifted"] == 1
    assert second["gifted"] == 0
    send_gift.assert_called_once_with("shop", "buyer-1", "item", "百度网盘下载凭证已发送")
    assert database.get_auto_review_record("o-1")["gift_sent"] is True


def test_gift_failure_releases_claim_for_next_scan(database):
    _order(database)
    scheduler, _find_pending, _submit_rate, send_gift = _scheduler(
        database, gift="券码：XYZ", gift_ok=False,
    )

    first = scheduler.scan_once()
    assert first["gifted"] == 0
    assert database.get_auto_review_record("o-1")["gift_sent"] is False

    send_gift.return_value = True
    second = scheduler.scan_once()
    assert second["gifted"] == 1
    assert database.get_auto_review_record("o-1")["gift_sent"] is True


def test_repeated_scan_does_not_submit_rate_twice(database):
    _order(database)
    scheduler, _find_pending, submit_rate, _send_gift = _scheduler(database, submit_ok=True)

    scheduler.scan_once()
    scheduler.scan_once()

    submit_rate.assert_called_once()


def test_platform_not_pending_marks_skipped(database):
    _order(database)
    scheduler, _find_pending, submit_rate, _send_gift = _scheduler(database, found=False)

    result = scheduler.scan_once()

    assert result == {"scanned": 1, "rated": 0, "suspended": 0, "gifted": 0}
    submit_rate.assert_not_called()
    assert database.get_auto_review_record("o-1")["seller_rate_status"] == "skipped"


def test_auto_review_tables_stay_wal(database):
    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
