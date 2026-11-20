import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

from db_manager import DBManager


@pytest.fixture
def database(tmp_path):
    db = DBManager(str(tmp_path / "refund-dispute.db"))
    yield db
    db.conn.close()


def _order(
    database,
    order_id="order-1",
    account_id="shop",
    item_id="item-1",
    buyer_id="buyer-1",
    amount="99.00",
    status="refunding",
    express_company="",
    express_no="",
    dropship_status="",
):
    with database.conn:
        database.conn.execute(
            """INSERT INTO orders (
                   order_id, item_id, buyer_id, amount, order_status, cookie_id,
                   express_company, express_no, dropship_status
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_id, item_id, buyer_id, amount, status, account_id,
             express_company, express_no, dropship_status),
        )
    return database.get_order_by_id(order_id)


def _conversation(database, text="收到了，密码对的", account_id="shop",
                  buyer_id="buyer-1", item_id="item-1",
                  created_at="2026-11-20 10:05:00"):
    with database.conn:
        database.conn.execute(
            """INSERT INTO ai_conversations (
                   cookie_id, chat_id, user_id, item_id, role, content, created_at
               ) VALUES (?, 'chat-1', ?, ?, 'user', ?, ?)""",
            (account_id, buyer_id, item_id, text, created_at),
        )


def test_dispute_table_is_wal_with_required_columns_and_index(database):
    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(refund_dispute_cases)")}
        indexes = {row[1] for row in conn.execute(
            "PRAGMA index_list(refund_dispute_cases)")}
    assert columns == {
        "order_id", "account_id", "item_id", "buyer_id", "refund_amount",
        "refund_reason", "dispute_state", "evidence_json", "statement_text",
        "created_at", "updated_at",
    }
    assert "idx_refund_dispute_state" in indexes


def test_dispute_case_crud_and_active_listing(database):
    evidence = {"delivery": {"sent_count": 1}}
    created = database.upsert_dispute_case(
        "order-1", "shop", "item-1", "buyer-1", 99.0, "仅退款",
        "evidence_collected", evidence, "申诉文本",
        created_at="2026-11-20 10:00:00",
    )

    assert created["dispute_state"] == "evidence_collected"
    assert json.loads(created["evidence_json"]) == evidence
    assert database.get_dispute_case("order-1")["statement_text"] == "申诉文本"
    assert [row["order_id"] for row in database.list_active_disputes()] == ["order-1"]

    updated = database.upsert_dispute_case(
        "order-1", "shop", "item-1", "buyer-1", 99.0, "仅退款",
        "refused", evidence, "申诉文本",
    )
    assert updated["dispute_state"] == "refused"
    assert updated["created_at"] == "2026-11-20 10:00:00"

    database.upsert_dispute_case(
        "order-2", "shop", "item-2", "buyer-2", 10.0, "仅退款",
        "won", {}, "结案",
    )
    assert [row["order_id"] for row in database.list_active_disputes()] == ["order-1"]


def test_evidence_pack_aggregates_logistics_delivery_and_chat(database):
    from dispute_evidence_pack import dispute_evidence_pack

    order = _order(
        database, express_company="YTO", express_no="YT1234567890",
        dropship_status="shipped",
    )
    _conversation(database)
    cards = [{
        "idx": 0, "kind": "text", "payload": "SECRET-VOUCHER",
        "status": "sent", "sent_at": "2026-11-20 10:04:00",
    }]

    pack = dispute_evidence_pack(
        database, order,
        cards_reader=lambda order_id, account_id: cards,
        now=datetime(2026, 11, 20, 10, 10, 0),
    )

    assert pack["refund_amount"] == 99.0
    assert pack["evidence"]["order"]["order_id"] == "order-1"
    assert pack["evidence"]["logistics"]["express_no"] == "YT1234567890"
    assert pack["evidence"]["delivery"]["sent_count"] == 1
    assert pack["evidence"]["chat"]["matched"] is True
    assert "收到了" in pack["evidence"]["chat"]["quotes"][0]
    assert "SECRET-VOUCHER" not in json.dumps(pack["evidence"], ensure_ascii=False)
    assert "【买家沟通确保证明】" in pack["statement_text"]
    assert pack["auto_refuse"] is True


def test_scheduler_collects_evidence_and_refuses_once(database):
    from refund_dispute_scheduler import RefundDisputeScheduler

    _order(database)
    pack = {
        "evidence": {"delivery": {"sent_count": 1}},
        "statement_text": "标准化申诉公文",
        "refund_amount": 99.0,
        "refund_reason": "仅退款",
        "auto_refuse": True,
        "risk_flags": [],
    }
    refuse = Mock(return_value={"ok": True})
    alert = Mock()
    scheduler = RefundDisputeScheduler(
        database,
        evidence_pack=lambda order: pack,
        refuse_refund=refuse,
        auto_refuse=True,
        alert=alert,
        refuse_reason="TEST_REFUSE_REASON",
    )

    first = scheduler.scan_once(now=datetime(2026, 11, 20, 10, 10, 0))

    assert first == {"scanned": 1, "collected": 1, "refused": 1, "won": 0, "lost": 0}
    case = database.get_dispute_case("order-1")
    assert case["dispute_state"] == "refused"
    assert case["statement_text"] == "标准化申诉公文"
    refuse.assert_called_once()
    assert refuse.call_args.kwargs["allow_write"] is True

    second = scheduler.scan_once(now=datetime(2026, 11, 20, 10, 11, 0))

    assert second["refused"] == 0
    assert database.get_dispute_case("order-1")["dispute_state"] == "arbitrating"
    refuse.assert_called_once()
    alert.assert_not_called()


def test_insufficient_evidence_stays_collected_and_alerts(database):
    from refund_dispute_scheduler import RefundDisputeScheduler

    _order(database)
    pack = {
        "evidence": {"delivery": {"sent_count": 0}},
        "statement_text": "证据不足",
        "refund_amount": 99.0,
        "refund_reason": "仅退款",
        "auto_refuse": False,
        "risk_flags": ["missing_delivery_evidence"],
    }
    refuse = Mock()
    alert = Mock()
    scheduler = RefundDisputeScheduler(
        database,
        evidence_pack=lambda order: pack,
        refuse_refund=refuse,
        auto_refuse=True,
        alert=alert,
    )

    result = scheduler.scan_once()

    assert result == {"scanned": 1, "collected": 1, "refused": 0, "won": 0, "lost": 0}
    assert database.get_dispute_case("order-1")["dispute_state"] == "evidence_collected"
    refuse.assert_not_called()
    alert.assert_called_once()


def test_refusal_failure_keeps_case_active_for_retry(database):
    from refund_dispute_scheduler import RefundDisputeScheduler

    _order(database)
    pack = {
        "evidence": {"delivery": {"sent_count": 1}},
        "statement_text": "申诉",
        "refund_amount": 99.0,
        "refund_reason": "仅退款",
        "auto_refuse": True,
        "risk_flags": [],
    }
    alert = Mock()
    scheduler = RefundDisputeScheduler(
        database,
        evidence_pack=lambda order: pack,
        refuse_refund=Mock(return_value={"ok": False, "kind": "disabled"}),
        auto_refuse=True,
        alert=alert,
        refuse_reason="TEST_REFUSE_REASON",
    )

    result = scheduler.scan_once()

    assert result["refused"] == 0
    assert database.get_dispute_case("order-1")["dispute_state"] == "evidence_collected"
    alert.assert_called_once()


def test_missing_refuse_reason_blocks_auto_refusal(database):
    from refund_dispute_scheduler import RefundDisputeScheduler

    _order(database)
    pack = {
        "evidence": {"delivery": {"sent_count": 1}},
        "statement_text": "申诉",
        "refund_amount": 99.0,
        "refund_reason": "仅退款",
        "auto_refuse": True,
        "risk_flags": [],
    }
    refuse = Mock()
    alert = Mock()
    scheduler = RefundDisputeScheduler(
        database,
        evidence_pack=lambda order: pack,
        refuse_refund=refuse,
        auto_refuse=True,
        alert=alert,
        refuse_reason="",
    )

    result = scheduler.scan_once()

    assert result["refused"] == 0
    assert database.get_dispute_case("order-1")["dispute_state"] == "evidence_collected"
    refuse.assert_not_called()
    alert.assert_called_once()


def test_refusal_accepts_open_platform_success_response(database):
    from refund_dispute_scheduler import RefundDisputeScheduler

    _order(database)
    pack = {
        "evidence": {"delivery": {"sent_count": 1}},
        "statement_text": "申诉",
        "refund_amount": 99.0,
        "refund_reason": "仅退款",
        "auto_refuse": True,
        "risk_flags": [],
    }
    scheduler = RefundDisputeScheduler(
        database,
        evidence_pack=lambda order: pack,
        refuse_refund=Mock(return_value={"code": 0, "data": {"success": True}}),
        auto_refuse=True,
        alert=Mock(),
        refuse_reason="TEST_REFUSE_REASON",
    )

    result = scheduler.scan_once()

    assert result["refused"] == 1
    assert database.get_dispute_case("order-1")["dispute_state"] == "refused"


def test_terminal_order_states_close_won_or_lost(database):
    from refund_dispute_scheduler import RefundDisputeScheduler

    _order(database, order_id="won", status="refunding")
    _order(database, order_id="lost", status="refunding")
    pack = {
        "evidence": {"delivery": {"sent_count": 1}},
        "statement_text": "申诉",
        "refund_amount": 99.0,
        "refund_reason": "仅退款",
        "auto_refuse": True,
        "risk_flags": [],
    }
    scheduler = RefundDisputeScheduler(
        database,
        evidence_pack=lambda order: pack,
        refuse_refund=Mock(return_value={"ok": True}),
        auto_refuse=True,
        alert=Mock(),
        refuse_reason="TEST_REFUSE_REASON",
    )
    scheduler.scan_once()
    assert scheduler.scan_once()["refused"] == 0

    with database.conn:
        database.conn.execute("UPDATE orders SET order_status='pending_ship' WHERE order_id='won'")
        database.conn.execute("UPDATE orders SET order_status='cancelled' WHERE order_id='lost'")

    result = scheduler.scan_once()

    assert result["won"] == 1
    assert result["lost"] == 1
    assert database.get_dispute_case("won")["dispute_state"] == "won"
    assert database.get_dispute_case("lost")["dispute_state"] == "lost"


def test_order_refuse_refund_uses_verified_reject_contract(monkeypatch):
    import goofish_open

    assert goofish_open.ALL_APIS["order.refuseRefund"] == goofish_open.ALL_APIS["aftersale.reject"]
    calls = []

    def call(name, body=None, allow_write=False, timeout=15):
        calls.append((name, body, allow_write))
        return {"code": 0, "data": {"success": True}}

    monkeypatch.setattr(goofish_open, "call", call)
    result = goofish_open.refuse_order_refund(
        "order-1", "未沟通协商一致", "商品已实际发货", allow_write=True,
    )

    assert result["code"] == 0
    assert calls == [("order.refuseRefund", {
        "order_no": "order-1",
        "refuse_reason": "未沟通协商一致",
        "refuse_explain": "商品已实际发货",
    }, True)]


def test_reply_server_and_example_config_are_wired():
    reply_source = (BACKEND / "reply_server.py").read_text(encoding="utf-8")
    example = (BACKEND.parent / "global_config.example.yml").read_text(encoding="utf-8")

    assert "REFUND_DISPUTE.enabled" in reply_source
    assert "refund_dispute_scheduler" in reply_source
    assert "REFUND_DISPUTE:" in example
