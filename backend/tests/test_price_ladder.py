import json
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import goofish_open
from price_ladder import PriceLadder, PricePolicy, config, create_price_router


@pytest.fixture
def ladder(tmp_path):
    path = tmp_path / "shop.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript("""
            CREATE TABLE item_info (item_id TEXT, item_price TEXT, is_multi_spec INTEGER);
            INSERT INTO item_info VALUES ('22', '11.00', 0);
        """)
    return PriceLadder(path)


def test_idle_floor_success_and_local_audit(ladder):
    policy = PricePolicy(price_floor="10.50", drop_step="2")
    now = 200_000
    listing = {"product_id": 10, "item_id": "22", "price": 1100, "online_time": 100,
               "update_time": 200, "product_status": 22}
    sent = []
    update = lambda product, price: sent.append((product, price))
    assert ladder.apply({**listing, "update_time": now - 100}, policy, update, now=now) == "idle"
    assert ladder.apply(listing, policy, update, now=now) == "changed"
    assert sent == [(10, 1050)]
    assert ladder.apply(listing, policy, update, now=now) == "idle"
    with closing(sqlite3.connect(ladder.path)) as conn:
        assert conn.execute("SELECT item_price FROM item_info").fetchone() == ("10.50",)
        assert conn.execute("SELECT state FROM price_ladder_log").fetchone() == ("changed",)


def test_error_cooldown_and_price_validation(ladder):
    policy = PricePolicy(price_floor="10.50", drop_step="1")
    listing = {"product_id": 10, "item_id": "22", "price": 1100, "online_time": 100,
               "update_time": 100, "product_status": 22}
    attempts = []

    def fail(product, price):
        attempts.append(product)
        raise RuntimeError("HTTP 429")

    assert ladder.apply(listing, policy, fail, now=200_000) == "failed"
    assert PriceLadder(ladder.path).apply(listing, policy, fail, now=286_399) == "cooldown"
    assert attempts == [10]
    with pytest.raises(ValueError):
        PricePolicy(price_floor="0.001", drop_step="1")


def test_scan_requires_signed_success_before_local_price_changes(ladder, monkeypatch):
    now = int(time.time())
    listing = {
        "product_id": 10, "price": 1100, "online_time": 100, "update_time": 200,
        "product_status": 22, "spec_type": 1,
    }
    requests = []
    settings = {"enabled": True, "dry_run": False, "app_key": "app", "app_secret": "secret"}
    monkeypatch.setattr(goofish_open, "get_config", lambda: settings)
    original_get = config.get
    monkeypatch.setattr(config, "get", lambda key, default=None:
                        True if key == "PRICE_LADDER.enabled" else original_get(key, default))

    def call(name, body, allow_write=False):
        requests.append((name, body, allow_write))
        if name == "product.list":
            return {"data": {"list": [listing], "count": 1}}
        if name == "product.detail":
            return {"data": {**listing, "publish_shop": [{"item_id": 22}]}}
        return {"code": 0, "data": {}}

    monkeypatch.setattr(goofish_open, "call", call)
    ladder.scan("https://shop.example", PricePolicy("10.50", "2"), {}, threading.Event())
    assert requests[0][1]["product_status"] == 22
    _, request, allowed = requests[-1]
    assert allowed and request["product_id"] == 10 and request["price"] == 1050
    callback = urlsplit(request["notify_url"])
    operation_id = parse_qs(callback.query)["operation_id"][0]
    with closing(sqlite3.connect(ladder.path)) as conn:
        assert conn.execute("SELECT item_price FROM item_info").fetchone() == ("11.00",)
        assert conn.execute("SELECT state FROM price_ladder_log").fetchone() == ("pending",)
    app = FastAPI()
    app.include_router(create_price_router(ladder.path))
    body = json.dumps({"product_id": 10, "item_id": 22, "task_type": 13,
                       "task_time": now + 5, "task_result": 1})
    params = {"operation_id": operation_id, "appid": "app", "timestamp": now}
    with TestClient(app) as client:
        assert client.post(callback.path, params={**params, "sign": "bad"}, content=body).status_code == 403
        params["sign"] = goofish_open.sign("app", body, str(now), "secret")
        for _ in range(2):
            assert client.post(callback.path, params=params, content=body).json()["result"] == "success"
    with closing(sqlite3.connect(ladder.path)) as conn:
        assert conn.execute("SELECT item_price FROM item_info").fetchone() == ("10.50",)
        assert conn.execute("SELECT state FROM price_ladder_log").fetchone() == ("changed",)
