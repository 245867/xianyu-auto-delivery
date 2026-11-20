import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import delivery_reliability as ledger
from pickup_service import (
    PickupService,
    configured_service,
    create_pickup_router,
    delivery_message,
)


@pytest.fixture
def first_sku_env(tmp_path, monkeypatch):
    """设置第一 SKU (职场汇报模板精选版) 完整演练环境。"""
    db_file = tmp_path / "shop_first_sku.db"
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "delivery_test.db")
    signing_key = "a" * 32
    public_url = "https://pickup.example.com/pickup"

    monkeypatch.setenv("PICKUP_PUBLIC_URL", public_url)
    monkeypatch.setenv("PICKUP_SIGNING_KEY", signing_key)

    # 按照实际 xianyu_data.db 结构建立表与首个 SKU 数据
    with closing(sqlite3.connect(db_file)) as conn, conn:
        conn.executescript("""
            CREATE TABLE orders (
                order_id TEXT PRIMARY KEY,
                order_status TEXT,
                buyer_id TEXT,
                cookie_id TEXT,
                item_id TEXT,
                item_title TEXT,
                order_price TEXT
            );
            CREATE TABLE item_info (
                item_id TEXT,
                cookie_id TEXT,
                item_title TEXT,
                item_price TEXT,
                auto_delivery_enabled INTEGER,
                delivery_card_id INTEGER
            );
            CREATE TABLE cards (
                id INTEGER PRIMARY KEY,
                name TEXT,
                type TEXT,
                content TEXT
            );

            -- 插入实际的第一 SKU 商品与卡密素材
            INSERT INTO item_info VALUES (
                '1100023370210', '2219399967630', '职场汇报模板精选版｜3套可编辑Word文件', '5.9', 1, 7
            );
            INSERT INTO cards VALUES (
                7, '职场汇报模板精选版-3套', 'text',
                '感谢购买！职场汇报模板精选版（通用周报、个人月报、一页纸进展汇报）下载链接：https://pan.baidu.com/s/1aZz9QTNvD5wI06GQo68VAg。'
            );
        """)

    return {
        "db_file": db_file,
        "item_id": "1100023370210",
        "cookie_id": "2219399967630",
        "card_content": (
            "感谢购买！职场汇报模板精选版（通用周报、个人月报、一页纸进展汇报）"
            "下载链接：https://pan.baidu.com/s/1aZz9QTNvD5wI06GQo68VAg。"
        ),
        "public_url": public_url,
    }


def test_first_sku_pickup_flow_end_to_end(first_sku_env):
    """第一 SKU 完整自提履约链路验收：
    1. 买家下单
    2. 系统生成带有 HMAC 的自提防封专属链接 (私聊无网盘直链)
    3. 买家访问自提中心脱敏查询 (防枚举)
    4. 买家确认提卡，获得完整精选版网盘内容并核销
    """
    db_file = first_sku_env["db_file"]
    cookie_id = first_sku_env["cookie_id"]
    order_id = "ORDER_20261120_001"
    buyer_id = "BUYER_TEST_168"
    raw_content = first_sku_env["card_content"]

    # 1. 模拟平台买家下单付款进库
    with closing(sqlite3.connect(db_file)) as conn, conn:
        conn.execute(
            "INSERT INTO orders VALUES (?, 'pending_ship', ?, ?, '1100023370210', '职场汇报模板精选版', '5.9')",
            (order_id, buyer_id, cookie_id),
        )

    # 2. 账本登记该订单要发的内容 (此时在本地账本中就绪)
    ledger.ensure_order(order_id, cookie_id, [raw_content], item_id="1100023370210", buyer_id=buyer_id)

    # 3. XianyuAutoAsync 发送准备：调用 delivery_message 获取发送文本
    msg = delivery_message(db_file, order_id, cookie_id, buyer_id, raw_content)

    # 核心安全断言：聊天框文本中绝对不得包含网盘原始链接或提取码
    assert "pan.baidu.com" not in msg
    assert "提取码" not in msg
    assert "专属链接提货" in msg
    assert msg.startswith("您的订单已准备，打开专属链接提货。请勿转发。\n")

    # 提取生成的提货外链与 token
    pickup_link = msg.splitlines()[-1]
    parsed_url = urlsplit(pickup_link)
    assert parsed_url.scheme == "https"
    assert parsed_url.netloc == "pickup.example.com"
    params = parse_qs(parsed_url.fragment)
    assert params["order_id"] == [order_id]
    pickup_token = params["pickup_token"][0]
    assert len(pickup_token) == 64  # SHA256 HMAC 长度

    # 模拟标记平台已通知
    ledger.mark_sent(order_id, cookie_id, 0)

    # 4. 构建自提 API 服务并由买家端发起请求
    app = FastAPI()
    app.include_router(create_pickup_router(db_file))
    client = TestClient(app)

    # 4.1 买家在 H5 页面发起脱敏查询
    resp_query = client.post("/api/pickup/query", json={"order_id": order_id, "pickup_token": pickup_token})
    assert resp_query.status_code == 200
    query_data = resp_query.json()
    assert query_data["ok"] is True
    # 标题自动脱敏展示为 "职场***"
    assert query_data["data"]["title"].startswith("职场")
    assert query_data["data"]["status"] == "ready"
    # 查询阶段绝对不返回卡密明文
    assert "cards" not in query_data["data"]

    # 4.2 买家点击一键提取卡密内容
    resp_collect = client.post("/api/pickup/collect", json={"order_id": order_id, "pickup_token": pickup_token})
    assert resp_collect.status_code == 200
    collect_data = resp_collect.json()
    assert collect_data["ok"] is True
    assert collect_data["data"]["status"] == "collected"
    cards = collect_data["data"]["cards"]
    assert len(cards) == 1
    assert cards[0]["kind"] == "text"
    assert cards[0]["payload"] == raw_content  # 100% 完整交付原始网盘下载文本

    # 4.3 重复核销幂等性测试
    resp_collect2 = client.post("/api/pickup/collect", json={"order_id": order_id, "pickup_token": pickup_token})
    assert resp_collect2.status_code == 200
    assert resp_collect2.json()["data"]["status"] == "collected"
    assert resp_collect2.json()["data"]["cards"][0]["payload"] == raw_content
