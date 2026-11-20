import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

import order_chaser
from db_manager import DBManager


@pytest.fixture
def database(tmp_path):
    db = DBManager(str(tmp_path / "order-chase.db"))
    yield db
    db.conn.close()


def _order(database, order_id="o-1", created_at="2026-11-20 10:00:00",
           status="processing", amount="99.00", account="shop",
           item="item-1", buyer="buyer-1"):
    with database.conn:
        database.conn.execute(
            "INSERT INTO orders (order_id, item_id, buyer_id, amount, order_status, "
            "cookie_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (order_id, item, buyer, amount, status, account, created_at),
        )


def _buyer_message(database, created_at, account="shop", buyer="buyer-1",
                   item="item-1", chat="chat-1"):
    with database.conn:
        database.conn.execute(
            "INSERT INTO ai_conversations (cookie_id, chat_id, user_id, item_id, "
            "role, content, created_at) VALUES (?, ?, ?, ?, 'user', '在吗', ?)",
            (account, chat, buyer, item, created_at),
        )


def _scheduler(database, *, send=None, release=None):
    from order_chase_scheduler import OrderChaseScheduler

    return OrderChaseScheduler(database, send_message_fn=send, release_stock_fn=release)


def test_chase_table_is_wal_with_required_columns_and_index(database):
    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(unpaid_order_chases)")}
        indexes = {row[1] for row in conn.execute(
            "PRAGMA index_list(unpaid_order_chases)")}
    assert columns == {
        "order_id", "account_id", "item_id", "buyer_id", "order_amount",
        "chase_stage", "chase_state", "last_chased_at", "created_at", "updated_at",
    }
    assert "idx_unpaid_order_chases_state" in indexes


def test_ledger_crud(database):
    created = database.ensure_unpaid_order_chase(
        "o-1", "shop", "item-1", "buyer-1", 99.0, created_at="2026-11-20 02:00:00")
    assert created["chase_stage"] == 0
    assert created["chase_state"] == "active"
    assert database.get_unpaid_order_chase("o-1")["order_amount"] == 99.0

    assert database.update_unpaid_order_chase(
        "o-1", chase_stage=1, last_chased_at="2026-11-20 02:05:00") is True
    refreshed = database.get_unpaid_order_chase("o-1")
    assert refreshed["chase_stage"] == 1
    assert refreshed["last_chased_at"] == "2026-11-20 02:05:00"

    assert [r["order_id"] for r in database.list_unpaid_order_chases(state="active")] == ["o-1"]
    assert database.delete_unpaid_order_chase("o-1") is True
    assert database.get_unpaid_order_chase("o-1") is None


def test_two_stage_chase_advances_and_stops(database):
    _order(database, created_at="2026-11-20 10:00:00")
    sent = []

    def send(account_id, buyer_id, item_id, text):
        sent.append((account_id, buyer_id, item_id, text))
        return True

    scheduler = _scheduler(database, send=send)

    first = scheduler.scan_once(now=datetime(2026, 11, 20, 10, 5, 0))
    assert first["chased"] == 1
    chase = database.get_unpaid_order_chase("o-1")
    assert chase["chase_stage"] == 1
    assert chase["last_chased_at"]
    assert sent[0][:3] == ("shop", "buyer-1", "item-1")
    assert sent[0][3] == order_chaser.get_chase_message(1)

    second = scheduler.scan_once(now=datetime(2026, 11, 20, 10, 12, 0))
    assert second["chased"] == 1
    chase = database.get_unpaid_order_chase("o-1")
    assert chase["chase_stage"] == 2
    assert sent[1][3] == order_chaser.get_chase_message(2)

    third = scheduler.scan_once(now=datetime(2026, 11, 20, 10, 20, 0))
    assert third["chased"] == 0
    assert database.get_unpaid_order_chase("o-1")["chase_stage"] == 2
    assert len(sent) == 2


def test_buyer_message_suspends_chase(database):
    _order(database, created_at="2026-11-20 10:00:00")
    sent = []
    scheduler = _scheduler(
        database, send=lambda *args: sent.append(args) or True)

    scheduler.scan_once(now=datetime(2026, 11, 20, 10, 1, 0))
    chase = database.get_unpaid_order_chase("o-1")
    _buyer_message(database, created_at=chase["created_at"])

    result = scheduler.scan_once(now=datetime(2026, 11, 20, 10, 5, 0))
    assert result["suspended"] == 1
    assert database.get_unpaid_order_chase("o-1")["chase_state"] == "suspended"
    assert sent == []


def test_night_quiet_hours_block_messages(database):
    _order(database, created_at="2026-11-20 23:20:00")
    sent = []
    scheduler = _scheduler(
        database, send=lambda *args: sent.append(args) or True)

    result = scheduler.scan_once(now=datetime(2026, 11, 20, 23, 30, 0))

    assert result["chased"] == 0
    assert sent == []
    chase = database.get_unpaid_order_chase("o-1")
    assert chase["chase_state"] == "active"
    assert chase["chase_stage"] == 0


def test_timeout_expires_and_releases_stock(database):
    _order(database, created_at="2026-11-20 09:00:00")
    released = []
    scheduler = _scheduler(
        database,
        send=lambda *args: True,
        release=lambda order_id, account_id, item_id:
            released.append((order_id, account_id, item_id)) or True,
    )

    result = scheduler.scan_once(now=datetime(2026, 11, 20, 10, 0, 0))

    assert result["expired"] == 1
    assert database.get_unpaid_order_chase("o-1")["chase_state"] == "expired"
    assert released == [("o-1", "shop", "item-1")]

    again = scheduler.scan_once(now=datetime(2026, 11, 20, 10, 5, 0))
    assert again["expired"] == 0
    assert len(released) == 1


def test_paid_order_converts_without_message(database):
    _order(database, created_at="2026-11-20 10:00:00")
    scheduler = _scheduler(database, send=lambda *args: True)
    scheduler.scan_once(now=datetime(2026, 11, 20, 10, 1, 0))

    with database.conn:
        database.conn.execute(
            "UPDATE orders SET order_status='pending_ship' WHERE order_id='o-1'")

    sent = []
    scheduler.send_message_fn = lambda *args: sent.append(args) or True
    result = scheduler.scan_once(now=datetime(2026, 11, 20, 10, 5, 0))

    assert result["converted"] == 1
    assert database.get_unpaid_order_chase("o-1")["chase_state"] == "converted"
    assert sent == []


def test_send_failure_does_not_advance_stage(database):
    _order(database, created_at="2026-11-20 10:00:00")
    scheduler = _scheduler(database, send=lambda *args: False)

    result = scheduler.scan_once(now=datetime(2026, 11, 20, 10, 5, 0))

    assert result["chased"] == 0
    chase = database.get_unpaid_order_chase("o-1")
    assert chase["chase_stage"] == 0
    assert chase["last_chased_at"] is None


def test_missing_send_channel_keeps_state(database):
    _order(database, created_at="2026-11-20 10:00:00")
    scheduler = _scheduler(database, send=None)

    result = scheduler.scan_once(now=datetime(2026, 11, 20, 10, 5, 0))

    assert result["chased"] == 0
    assert database.get_unpaid_order_chase("o-1")["chase_stage"] == 0
