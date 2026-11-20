import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import delivery_reliability as ledger
from pickup_service import PickupError, PickupService, configured_service, create_pickup_router, delivery_message


@pytest.fixture
def service(tmp_path, monkeypatch):
    orders = tmp_path / "orders.db"
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "delivery.db")
    with closing(sqlite3.connect(orders)) as conn, conn:
        conn.executescript("""
            CREATE TABLE orders (order_id TEXT PRIMARY KEY, order_status TEXT,
                buyer_id TEXT, cookie_id TEXT, item_id TEXT);
            CREATE TABLE item_info (item_id TEXT, cookie_id TEXT, item_title TEXT);
            INSERT INTO orders VALUES ('100', 'pending_ship', 'buyer', 'shop', '200');
            INSERT INTO item_info VALUES ('200', 'shop', '网盘年卡测试商品');
        """)
    ledger.ensure_order("100", "shop", ["SECRET-VOUCHER"], item_id="200", buyer_id="buyer")
    return PickupService(orders, "https://pickup.example/pickup", b"a" * 32)


def enroll(service):
    link = service.issue_link("100", "shop", "buyer")
    return parse_qs(urlsplit(link).fragment)["pickup_token"][0]


def test_authorization_refund_and_idempotent_collection(service):
    token = enroll(service)
    with pytest.raises(PickupError) as error:
        service.access("100", "", "192.0.2.1")
    assert error.value.status == 404
    assert service.access("100", token, "192.0.2.1")["status"] == "pending"
    assert ledger.mark_sent("100", "shop", 0)
    data = service.access("100", token, "192.0.2.1")
    assert data["title"] == "网盘***"
    assert data["status"] == "ready" and data["shipped_at"]
    assert "cards" not in data
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda ip: service.access("100", token, ip, collect=True),
                                ("192.0.2.1", "192.0.2.2")))
    assert results[0]["cards"] == results[1]["cards"] == [{"kind": "text", "payload": "SECRET-VOUCHER"}]
    assert ledger.cards("100", "shop")[0]["status"] == "collected"
    assert ledger.summary("100", "shop")["all_sent"]
    assert not ledger.claim_card("100", "shop", 0)["claimed"]
    assert not ledger.force_retry("100", "shop", 0)
    with closing(sqlite3.connect(ledger.DB_PATH)) as conn:
        receipt = conn.execute("SELECT collected_at, collected_ip FROM delivery_pickups").fetchone()
        assert receipt[0] and receipt[1] in ("192.0.2.1", "192.0.2.2")
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    with closing(sqlite3.connect(service.orders_path)) as conn, conn:
        conn.execute("UPDATE orders SET order_status='refunding'")
    with pytest.raises(PickupError) as error:
        service.access("100", token, "192.0.2.1", collect=True)
    assert error.value.status == 404


def test_ip_rate_and_failed_credential_ban(service):
    token = enroll(service)
    for _ in range(3):
        with pytest.raises(PickupError) as error:
            service.access("999", "", "192.0.2.1", now=1000)
        assert error.value.status == 404
    with pytest.raises(PickupError) as error:
        service.access("100", token, "192.0.2.1", now=1899)
    assert error.value.retry_after == 1
    for attempt in range(10):
        service.access("100", token, "192.0.2.1", now=1900 + attempt)
    with pytest.raises(PickupError) as error:
        service.access("100", token, "192.0.2.1", now=1910, collect=True)
    assert error.value.status == 429
    assert service.access("100", token, "192.0.2.1", now=2200)["status"] == "pending"


def test_http_credentials_are_order_bound_and_skip_chat_resend(service, monkeypatch):
    monkeypatch.setenv("PICKUP_PUBLIC_URL", "https://pickup.example/pickup")
    monkeypatch.setenv("PICKUP_SIGNING_KEY", "a" * 32)
    message = delivery_message(service.orders_path, "100", "shop", "buyer", "SECRET-VOUCHER")
    link = message.splitlines()[-1]
    token = parse_qs(urlsplit(link).fragment)["pickup_token"][0]
    assert "SECRET-VOUCHER" not in message and not urlsplit(link).query
    assert ledger.mark_sent("100", "shop", 0)
    assert ledger.latest_sent_delivery("shop", "buyer") is None
    with closing(sqlite3.connect(service.orders_path)) as conn, conn:
        conn.execute("INSERT INTO orders VALUES ('101', 'pending_ship', 'buyer', 'shop', '200')")
    ledger.ensure_order("101", "shop", ["OTHER-VOUCHER"], item_id="200", buyer_id="buyer")
    service.issue_link("101", "shop", "buyer")
    app = FastAPI()
    app.include_router(create_pickup_router(service.orders_path))
    with TestClient(app) as client:
        response = client.post("/api/pickup/collect", json={"order_id": "101", "pickup_token": token})
        assert response.status_code == 404
        response = client.post("/api/pickup/query", json={"order_id": "100", "pickup_token": token})
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        assert "SECRET-VOUCHER" not in response.text
        response = client.post("/api/pickup/collect", json={"order_id": "100", "pickup_token": token})
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        assert response.json()["data"]["cards"] == [{"kind": "text", "payload": "SECRET-VOUCHER"}]
    monkeypatch.delenv("PICKUP_PUBLIC_URL")
    monkeypatch.delenv("PICKUP_SIGNING_KEY")
    with pytest.raises(PickupError):
        delivery_message(service.orders_path, "100", "shop", "buyer", "SECRET-VOUCHER")


def test_disabled_config_does_not_enable_pickup_with_saved_credentials(service, monkeypatch):
    from config import config

    settings = {
        "PICKUP_PORTAL.enabled": False,
        "PICKUP_PORTAL.public_url": "https://pickup.example/pickup",
        "PICKUP_PORTAL.signing_key": "a" * 32,
    }
    monkeypatch.delenv("PICKUP_PUBLIC_URL", raising=False)
    monkeypatch.delenv("PICKUP_SIGNING_KEY", raising=False)
    monkeypatch.setattr(config, "get", settings.get)
    assert configured_service(service.orders_path) is None
    settings["PICKUP_PORTAL.enabled"] = True
    assert configured_service(service.orders_path).issue_link("100", "shop", "buyer").startswith(
        "https://pickup.example/pickup#",
    )
