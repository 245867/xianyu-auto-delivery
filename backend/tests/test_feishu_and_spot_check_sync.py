import hmac
import hashlib
import base64
import time
import pytest
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

from notify_service import (
    sign_feishu_payload,
    is_incident_duplicate,
    send_event,
    _INCIDENT_LEDGER,
)
from db_manager import DBManager


def test_sign_feishu_payload_correctness():
    # 测试 HMAC-SHA256 签名逻辑
    payload = {"msg_type": "text", "content": {"text": "hello"}}
    secret = "my_secret_token"
    fixed_ts = 1700000000

    signed = sign_feishu_payload(payload, secret, timestamp=fixed_ts)
    assert signed["timestamp"] == str(fixed_ts)
    assert "sign" in signed

    # 手动验签
    key = f"{fixed_ts}\n{secret}".encode("utf-8")
    expected_sig = base64.b64encode(hmac.digest(key, b"", "sha256")).decode("utf-8")
    assert signed["sign"] == expected_sig


def test_sign_feishu_payload_empty_secret():
    payload = {"msg_type": "text", "content": {"text": "hello"}}
    signed = sign_feishu_payload(payload, "")
    assert "sign" not in signed
    assert "timestamp" not in signed


def test_incident_deduplication():
    incident_id = "incident-loss-alert-item-999"
    _INCIDENT_LEDGER.clear()

    # 首次触发，不重复
    assert is_incident_duplicate(incident_id, ttl=60) is False

    # 立即再次触发，处于 60s 冷却期内，判定为重复
    assert is_incident_duplicate(incident_id, ttl=60) is True

    # 另一个 incident_id 不受影响
    assert is_incident_duplicate("other-incident", ttl=60) is False


def test_send_event_incident_dedup_suppression():
    incident_id = "test-alert-suppress"
    _INCIDENT_LEDGER.clear()

    # 模拟发送事件
    res1 = send_event("risk", "首次报警", "详情", force=True, incident_id=incident_id, incident_ttl=60)
    # 重复发送
    res2 = send_event("risk", "重复报警", "详情", force=True, incident_id=incident_id, incident_ttl=60)

    assert res2.get("skipped") is True
    assert res2.get("deduped") is True
    assert "已抑制" in str(res2.get("results"))


@pytest.fixture
def test_db(tmp_path):
    db = DBManager(str(tmp_path / "spot_check.db"))
    yield db
    db.conn.close()


def test_order_spot_check_candidates(test_db):
    # 构造订单：
    # item-1 有 2 笔订单 (符合 1..3 单抽检标准)
    # item-2 有 5 笔订单 (已起量爆款，不属于新品抽检)
    # item-3 有 1 笔订单 (符合 1..3 单抽检标准)
    with test_db.conn:
        test_db.conn.execute("INSERT INTO orders (order_id, item_id, amount) VALUES ('o-1', 'item-1', '10.0')")
        test_db.conn.execute("INSERT INTO orders (order_id, item_id, amount) VALUES ('o-2', 'item-1', '10.0')")

        for i in range(1, 6):
            test_db.conn.execute(f"INSERT INTO orders (order_id, item_id, amount) VALUES ('o-2-{i}', 'item-2', '20.0')")

        test_db.conn.execute("INSERT INTO orders (order_id, item_id, amount) VALUES ('o-3', 'item-3', '30.0')")

    res = test_db.get_order_spot_check_candidates(page=1, page_size=10, max_orders=3)
    assert res["page"] == 1
    assert res["has_next"] is False

    item_ids = {it["item_id"] for it in res["items"]}
    assert "item-1" in item_ids
    assert "item-3" in item_ids
    assert "item-2" not in item_ids  # item-2 超过 3 单被排除

    # 校验每个抽检项包含订单详情
    item1_entry = next(it for it in res["items"] if it["item_id"] == "item-1")
    assert item1_entry["order_count"] == 2
    assert len(item1_entry["orders"]) == 2
