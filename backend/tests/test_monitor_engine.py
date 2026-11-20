import asyncio
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

import monitor_engine
import notify_service
from monitor_api import create_monitor_router, monitor_lifespan
from monitor_engine import MonitorEngine, fetch_detail, search_listings
from monitor_storage import MonitorStore, MonitorTask
from sqlite_runtime import connect_sqlite


@pytest.fixture
def store(tmp_path):
    return MonitorStore(tmp_path / "monitor_engine.db")


@pytest.fixture
def listing():
    return {
        "item_id": "1001",
        "title": "相机",
        "price": 200,
        "category_id": "12",
        "published_at": 1744198800000,
        "main_image": "https://img.alicdn.com/cover.jpg",
    }


def make_task(store, **changes):
    return store.create_task(MonitorTask(keyword="相机", max_price=300, **changes))


def make_engine(store, *, search, detail=None, **options):
    return MonitorEngine(
        store,
        resolve_account=lambda: "test-session",
        search=search,
        detail=detail or (lambda cookie, listing: {}),
        acquire=lambda scope, key: (True, 0),
        **options,
    )


def test_task_crud_and_wal(store):
    task = make_task(store, min_price=100, category_id="12", exclude_words=["配件"])
    assert task["interval_seconds"] == 120
    assert store.list_tasks() == [task]
    assert store.get_task(task["id"]) == task
    with connect_sqlite(store.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    updated = store.update_task(task["id"], MonitorTask(
        keyword="镜头", max_price=500, interval_seconds=180, is_enabled=False
    ))
    assert updated["keyword"] == "镜头"
    assert updated["created_at"] == task["created_at"]
    assert store.list_tasks(enabled_only=True) == []
    assert store.delete_task(task["id"])
    assert not store.delete_task(task["id"])
    assert store.get_task(task["id"]) is None

    with pytest.raises(ValueError):
        MonitorTask(keyword="相机", min_price=301, max_price=300)


def test_atomic_claim_survives_task_deletion(store, listing):
    tasks = [make_task(store), make_task(store)]
    with ThreadPoolExecutor(max_workers=2) as workers:
        claimed = list(workers.map(lambda task: store.claim(task["id"], listing), tasks))
    assert sorted(claimed) == [False, True]
    assert store.delete_task(tasks[claimed.index(True)]["id"])
    assert not MonitorStore(store.path).claim(make_task(store)["id"], listing)
    assert len(store.list_records()) == 1


def test_filters_and_two_levels_of_deduplication(store, listing, monkeypatch):
    task = make_task(store, min_price=100, category_id="12", exclude_words=["配件"])
    search_items = [
        {**listing, "item_id": "2", "price": 350},
        {**listing, "item_id": "3", "price": 10},
        {**listing, "item_id": "4", "title": "相机配件"},
        {**listing, "item_id": "5", "category_id": None},
        {**listing, "item_id": "6", "category_id": "13"},
        listing,
        listing,
    ]
    calls = []
    original_claim = store.claim

    def claim(task_id, item):
        calls.append("claim")
        return original_claim(task_id, item)

    def detail(cookie, item):
        calls.append("detail")
        return {"description": "盒装", "seller_id": "8001", "seller_credit": "5"}

    def evaluate(item):
        calls.append("evaluate")
        assert item["description"] == "盒装"
        assert item["published_at"] == listing["published_at"]
        return {"profit_rate": 0.25, "verdict": "buy", "risk_level": "low"}

    def push(item):
        calls.append("push")
        return {"ok": True, "skipped": False}

    monkeypatch.setattr(store, "claim", claim)
    dependencies = dict(search=lambda cookie, task: search_items, detail=detail,
                        evaluator=evaluate, notify=push)

    async def run():
        engine = make_engine(store, **dependencies)
        await engine.poll_task(task["id"])
        await engine.poll_task(task["id"])
        await make_engine(store, **dependencies).poll_task(task["id"])

    asyncio.run(run())
    assert calls == ["claim", "detail", "evaluate", "push", "claim"]
    record, = store.list_records()
    assert record["status"] == "pushed"
    assert record["seller_id"] == "8001"
    assert record["ai_evaluation"]["profit_rate"] == 0.25


@pytest.mark.parametrize("error", ["RGV587_ERROR", "FAIL_SYS_USER_VALIDATE",
                                  "HTTP 429", "FAIL_SYS_TRAFFIC_LIMIT"])
def test_platform_backoff_persists_and_stops_other_tasks(store, error):
    task = make_task(store)
    other = make_task(store)
    now = 1_000.0
    calls = []

    def blocked_search(cookie, task):
        calls.append(task["id"])
        raise RuntimeError(error)

    async def run():
        nonlocal now
        for count, delay in enumerate((300, 900, 1800, 1800), start=1):
            engine = make_engine(MonitorStore(store.path), search=blocked_search, clock=lambda: now)
            assert await engine.poll_task(task["id"]) == delay
            assert store.get_task(task["id"])["cooldown_until"] == now + delay
            assert await engine.poll_task(task["id"]) == delay
            assert await engine.poll_task(other["id"]) == delay
            assert calls == [task["id"]] * count
            now += delay

        recovery = make_engine(store, search=lambda cookie, task: [], clock=lambda: now)
        assert await recovery.poll_task(task["id"]) == 120
        assert store.get_task(task["id"])["risk_count"] == 0

    asyncio.run(run())


def test_detail_challenge_ends_round_and_keeps_claim(store, listing):
    task = make_task(store)
    items = [listing, {**listing, "item_id": "1002"}]
    calls = []

    def detail(cookie, item):
        calls.append(item["item_id"])
        raise RuntimeError("RGV587")

    engine = make_engine(store, search=lambda cookie, task: items, detail=detail)
    assert asyncio.run(engine.poll_task(task["id"])) == 300
    assert calls == ["1001"]
    record, = store.list_records()
    assert record["status"] == "discovered"
    assert "RGV587" in record["error"]


def test_detail_price_filter_and_local_quota(store, listing):
    task = make_task(store)
    calls = []
    engine = make_engine(store, search=lambda cookie, task: [listing],
                         detail=lambda cookie, listing: {"price": 350},
                         evaluator=lambda item: calls.append(item))

    async def run():
        engine.acquire = lambda scope, key: (scope == "search", 60)
        assert await engine.poll_task(task["id"]) == 60
        assert store.list_records() == []
        engine.acquire = lambda scope, key: (True, 0)
        await engine.poll_task(task["id"])

    asyncio.run(run())
    assert not calls
    record, = store.list_records()
    assert (record["price"], record["status"]) == (350, "ignored")


def test_test_mode_push_does_not_claim_skipped_notifications(store, listing):
    task = make_task(store, test_mode=True)
    sent = []

    def notify(item):
        sent.append(item)
        return {"ok": True, "skipped": True}

    engine = make_engine(store, search=lambda cookie, task: [listing], notify=notify)
    asyncio.run(engine.poll_task(task["id"]))
    assert sent[0]["test_mode"] is True
    assert sent[0]["ai_evaluation"] == {"profit_rate": None, "verdict": None, "risk_level": None}
    assert store.list_records()[0]["status"] == "discovered"


def test_scheduler_runs_another_task_during_evaluation(store, listing, monkeypatch):
    tasks = [make_task(store), make_task(store)]
    first_evaluating = threading.Event()
    second_evaluated = threading.Event()

    def evaluate(item):
        if item["item_id"] == "1001":
            first_evaluating.set()
            assert second_evaluated.wait(timeout=5)
        else:
            assert first_evaluating.wait(timeout=5)
            second_evaluated.set()
        return {"verdict": "neutral"}

    monkeypatch.setattr(monitor_engine.random, "uniform", lambda *_: 0)
    engine = make_engine(store, search=lambda cookie, task: [
        {**listing, "item_id": "1001" if task["id"] == tasks[0]["id"] else "1002"},
    ], evaluator=evaluate)

    async def run():
        worker = asyncio.create_task(engine.run())
        try:
            assert await asyncio.to_thread(second_evaluated.wait, 5)
        finally:
            engine.stop()
            await worker

    asyncio.run(run())
    assert len(store.list_records()) == 2
    assert {row["status"] for row in store.list_records()} == {"ignored"}


def test_admin_routes_and_lifespan(tmp_path, monkeypatch):
    monkeypatch.setenv("MONITOR_DB_PATH", str(tmp_path / "api.db"))

    def require_admin(authorization: str | None = Header(default=None)):
        if authorization != "test-admin":
            raise HTTPException(403)

    app = FastAPI(lifespan=monitor_lifespan)
    app.include_router(create_monitor_router(require_admin))
    with TestClient(app) as client:
        path = "/api/monitor/tasks"
        assert client.get(path).status_code == 403
        headers = {"Authorization": "test-admin"}
        body = {"keyword": "相机", "max_price": 200, "is_enabled": False}
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == 201
        task = response.json()["task"]
        item_path = f"{path}/{task['id']}"
        assert client.get(path, headers=headers).json()["tasks"] == [task]
        assert client.get(item_path, headers=headers).json()["task"] == task
        assert client.put(item_path, headers=headers, json={**body, "max_price": 300}).status_code == 200
        assert client.get("/api/monitor/records", headers=headers).json()["records"] == []
        assert client.delete(item_path, headers=headers).status_code == 200
        assert client.get(item_path, headers=headers).status_code == 404


def test_search_and_detail_adapters(monkeypatch):
    calls = []
    card = {"data": {"item": {"main": {
        "clickParam": {"args": {"item_id": "1001", "publishTime": "1744198800000",
                                "cCatId": "12", "displayPrice": "200"}},
        "exContent": {"title": "相机"},
    }}}}

    def search_request(cookie, api, payload, *args, **kwargs):
        calls.append(payload)
        return {"ret": ["SUCCESS::调用成功"], "data": {"resultList": [card]}}, None

    def detail_request(cookie, api, payload, *args, **kwargs):
        assert payload == {"itemId": "1001"}
        return {"ret": ["SUCCESS::调用成功"], "data": {
            "itemDO": {"itemId": "1001", "title": "相机", "desc": "盒装",
                       "soldPrice": "220.00", "imageInfos": [{"url": "//img.alicdn.com/1.jpg"}]},
            "sellerDO": {"sellerId": 9001, "idleFishCreditTag": {
                "trackParams": {"sellerLevel": "5"},
            }},
        }}, None

    monkeypatch.setattr("market_listings._mtop_request", search_request)
    monkeypatch.setattr(monitor_engine, "_mtop_request", detail_request)
    item, = search_listings(" _m_h5_tk=fake_123", {"keyword": "相机"})
    assert calls[0]["sortField"] == "pub_time"
    assert calls[0]["sortValue"] == "DESC"
    assert item["category_id"] == "12"
    assert item["published_at"] == 1744198800000
    detail = fetch_detail("session", item)
    assert detail["image_urls"] == ["https://img.alicdn.com/1.jpg"]
    assert detail["price"] == 220
    assert detail["description"] == "盒装"
    assert detail["seller_id"] == "9001"
    assert detail["seller_credit"] == "5"


def test_radar_webhook_formats_and_rejection(monkeypatch, listing):
    payloads = {}

    class Response:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    def post(url, *, json, timeout):
        payloads[url] = json
        return Response({"code": 0} if url == "feishu" else
                        {"errcode": 40001 if url == "wecom" else 0})

    monkeypatch.setattr(notify_service, "get_config", lambda: {
        **notify_service.DEFAULTS, "wecom": "wecom", "dingtalk": "dingtalk", "feishu": "feishu",
    })
    monkeypatch.setattr(notify_service.requests, "post", post)
    failures = []
    monkeypatch.setattr(notify_service, "_record_failure", lambda *args: failures.append(args))
    result = notify_service.notify_monitor_item({
        **listing, "item_url": "https://www.goofish.com/item?id=1001",
        "image_urls": [listing["main_image"]],
        "ai_evaluation": {"profit_rate": 0.25, "estimated_profit": 50},
    })
    assert result["ok"]
    assert failures[0][:3] == ("monitor", "wecom", 1)
    article, = payloads["wecom"]["news"]["articles"]
    assert "25.0%" in article["description"]
    assert article["picurl"] == listing["main_image"]
    assert "https://img.alicdn.com/cover.jpg" in payloads["dingtalk"]["actionCard"]["text"]
    assert payloads["feishu"]["msg_type"] == "post"
    paragraphs = payloads["feishu"]["content"]["post"]["zh_cn"]["content"]
    assert paragraphs[-1][0]["href"] == listing["main_image"]
