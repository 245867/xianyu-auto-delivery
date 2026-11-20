import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

import idempotency
import rate_limiter
from db_manager import DBManager


@pytest.fixture
def database(tmp_path, monkeypatch):
    db = DBManager(str(tmp_path / "rank-guard.db"))
    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "idempotency.db")
    monkeypatch.setattr(rate_limiter, "CIRCUIT_FILE", tmp_path / "circuit.json")
    with db.conn:
        db.conn.execute(
            "INSERT INTO item_info (cookie_id, item_id, item_price, is_multi_spec) "
            "VALUES ('shop', 'item', '20.00', 0)"
        )
    yield db
    db.conn.close()


def _target(database, **overrides):
    target = {
        "item_id": "item",
        "search_keyword": "车载香薰",
        "price_floor": 10.0,
        "price_ceiling": 30.0,
        "under_cut_delta": 0.1,
        "is_active": True,
    }
    target.update(overrides)
    return database.save_competitor_monitor_target(**target)


def _detail(price=2000):
    return {
        "code": 0,
        "data": {
            "product_id": 77,
            "price": price,
            "spec_type": 1,
            "sku_items": [],
            "publish_shop": [{"item_id": "item"}],
        },
    }


def _search(prices):
    return {
        "ok": True,
        "error": "",
        "items": [
            {"item_id": f"competitor-{index}", "price": price, "title": f"竞品 {index}"}
            for index, price in enumerate(prices, 1)
        ],
    }


def _guard(database, detail, search, edit=None, *, alert=None, max_daily=5):
    from price_rank_guard import PriceRankGuard

    return PriceRankGuard(
        database,
        detail=detail,
        search=search,
        edit=edit or Mock(return_value=None),
        resolve_account=lambda: "cookie-value",
        app_key="app-key",
        max_daily_revisions=max_daily,
        alert=alert or Mock(),
    )


def test_undercut_uses_first_three_valid_competitors_and_audits_pending(database):
    from price_ladder import PriceLadder

    _target(database, under_cut_delta=1.0)
    edit = Mock(return_value=None)
    guard = _guard(
        database,
        _detail(2000),
        lambda *_args, **_kwargs: _search([12, 13, 12.5, 5]),
        edit,
    )
    result = guard.scan_once(callback_base="https://shop.example", now=datetime(2026, 11, 20, 6, tzinfo=timezone.utc))

    assert result == {"scanned": 1, "changed": 1}
    assert edit.call_count == 1
    assert edit.call_args.args[:2] == (77, 1100)
    target = database.get_competitor_monitor_target("item")
    assert target["daily_revision_count"] == 1
    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute("SELECT state FROM price_ladder_log").fetchone() == ("pending",)
    with sqlite3.connect(PriceLadder(database.db_path).path) as conn:
        assert conn.execute("SELECT item_price FROM item_info WHERE item_id='item'").fetchone() == ("20.00",)


def test_competitor_rise_returns_to_ceiling_not_above_it(database):
    _target(database, price_floor=8.0, price_ceiling=12.0)
    edit = Mock(return_value=None)
    guard = _guard(database, _detail(1000), lambda *_a, **_k: _search([15, 16, 17]), edit)

    assert guard.scan_once(callback_base="https://shop.example", now=datetime(2026, 11, 20, 6, tzinfo=timezone.utc))["changed"] == 1
    assert edit.call_args.args[:2] == (77, 1200)


def test_floor_is_rigid_and_emits_warning(database):
    _target(database, price_floor=10.0, price_ceiling=30.0)
    alert = Mock()
    edit = Mock(return_value=None)
    guard = _guard(database, _detail(1200), lambda *_a, **_k: _search([7, 7.2, 7.5]), edit, alert=alert)

    assert guard.scan_once(callback_base="https://shop.example", now=datetime(2026, 11, 20, 6, tzinfo=timezone.utc))["changed"] == 1
    assert edit.call_args.args[:2] == (77, 1000)
    alert.assert_called_once()


def test_no_competitors_restores_ceiling(database):
    _target(database, price_floor=10.0, price_ceiling=30.0)
    edit = Mock(return_value=None)
    guard = _guard(database, _detail(1500), lambda *_a, **_k: _search([]), edit)

    assert guard.scan_once(callback_base="https://shop.example", now=datetime(2026, 11, 20, 6, tzinfo=timezone.utc))["changed"] == 1
    assert edit.call_args.args[:2] == (77, 3000)


def test_daily_cap_blocks_sixth_revision_until_next_day(database):
    _target(database)
    with database.conn:
        database.conn.execute(
            "UPDATE competitor_monitor_targets SET daily_revision_count=5, "
            "last_revised_at='2026-11-20T06:00:00+00:00' WHERE item_id='item'"
        )
    edit = Mock(return_value=None)
    guard = _guard(database, _detail(2000), lambda *_a, **_k: _search([12, 13, 14]), edit)

    assert guard.scan_once(
        callback_base="https://shop.example", now=datetime(2026, 11, 20, 8, tzinfo=timezone.utc),
    ) == {"scanned": 1, "changed": 0}
    edit.assert_not_called()
    with database.conn:
        database.conn.execute(
            "UPDATE competitor_monitor_targets SET daily_revision_count=5, "
            "last_revised_at='2026-11-19T23:00:00+00:00' WHERE item_id='item'"
        )
    assert guard.scan_once(
        callback_base="https://shop.example", now=datetime(2026, 11, 20, 8, tzinfo=timezone.utc),
    )["changed"] == 1
    assert edit.call_count == 1


def test_management_api_creates_lists_updates_and_deletes(database):
    from price_rank_guard_api import create_price_rank_guard_router

    admin = Mock()
    app = FastAPI()
    app.include_router(create_price_rank_guard_router(database, admin))
    payload = {
        "item_id": "item",
        "search_keyword": "车载香薰",
        "price_floor": 10,
        "price_ceiling": 30,
        "under_cut_delta": 0.1,
        "is_active": True,
    }
    with TestClient(app) as client:
        assert client.post("/api/price-rank-guard/targets", json=payload).status_code == 201
        assert client.get("/api/price-rank-guard/targets").json()["targets"][0]["search_keyword"] == "车载香薰"
        assert client.put("/api/price-rank-guard/targets/item", json={
            **payload, "price_ceiling": 28, "is_active": False,
        }).json()["target"]["is_active"] is False
        assert client.delete("/api/price-rank-guard/targets/item").status_code == 200
        assert client.get("/api/price-rank-guard/targets").json()["targets"] == []
    assert admin.call_count == 5
    with sqlite3.connect(database.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
