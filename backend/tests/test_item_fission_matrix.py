import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BACKEND), str(BACKEND.parent)]

import goofish_open
import seller_api
from account_nurturing import AccountNurtureScheduler
from db_manager import DBManager


@pytest.fixture
def database(tmp_path):
    manager = DBManager(str(tmp_path / "fission.db"))
    yield manager
    manager.conn.close()


@pytest.fixture
def variants():
    return [
        {"variant_title": f"香薰款式 {index}", "cover_image_url": f"https://example.com/{index}.jpg"}
        for index in range(5)
    ]


def test_signed_exposure_request_and_deduplicated_health(database, monkeypatch):
    account = "2219399967630"
    cookie = f"unb={account}; {seller_api.TOKEN_COOKIE}=offline_123"
    rows = [
        {"date": "2026-11-18", "exposure": 1250, "pv": 380, "uv": 210, "inquiry": 35},
        {"date": "2026-11-19", "exposure": 420, "pv": 110, "uv": 65, "inquiry": 8},
    ]
    result = {
        "api": "mtop.taobao.idle.seller.stat.get", "v": "1.0", "ret": ["SUCCESS::调用成功"],
        "data": {"accountId": account, "dailyStats": rows},
    }
    response = Mock(status_code=200, headers={})
    response.json.return_value = result
    post = Mock(return_value=response)
    monkeypatch.setattr(seller_api.requests, "post", post)
    monkeypatch.setattr(seller_api, "_app_key", lambda: "offline-app")
    assert seller_api.fetch_seller_exposure_stats(cookie) == rows
    request = post.call_args
    assert request.args == ("https://h5api.m.goofish.com/h5/mtop.taobao.idle.seller.stat.get/1.0/",)
    assert json.loads(request.kwargs["data"]["data"]) == {
        "days": 2, "statType": "EXPOSURE_PV_UV", "pageNumber": 1, "pageSize": 20,
    }
    headers = request.kwargs["headers"]
    assert headers["origin"] == "https://www.goofish.com"
    assert headers["referer"] == "https://www.goofish.com/"
    assert headers["content-type"] == "application/x-www-form-urlencoded"
    assert headers["cookie"] == cookie
    params = request.kwargs["params"]
    assert params["sign"] == seller_api._sign("offline", params["t"], request.kwargs["data"]["data"], "offline-app")

    assert database.save_cookie("shop", cookie)
    assert database.save_cookie("other", f"unb=123; {seller_api.TOKEN_COOKIE}=offline_123")
    assert database.save_cookie("disabled", cookie)
    database.save_cookie_status("disabled", False)
    scheduler = AccountNurtureScheduler(database)
    scheduler.poll_seller_stats()
    scheduler.poll_seller_stats()
    assert database.get_account_nurture_state("shop")["health_score"] == 65
    assert database.get_account_nurture_state("other") is None
    assert database.get_account_nurture_state("disabled") is None
    assert post.call_count == 5


def test_parent_stop_retries_published_children_and_blocks_drafts(database, variants):
    from item_fission_matrix import ItemFissionMatrix

    assert database.save_cookie("shop", "offline")
    assert database.save_item_basic_info("shop", "100", item_title="香薰")
    matrix = ItemFissionMatrix(database)
    tasks = matrix.create("shop", "100", variants)
    assert len({task["fission_id"] for task in tasks}) == 5
    child = tasks[0]["fission_id"]
    submit = Mock(return_value={"success": True, "itemId": "200"})
    assert matrix.publish("shop", child, {"desc": "香薰"}, submit) == "published"
    assert submit.call_args.args[0]["imageUrls"] == [tasks[0]["cover_image_url"]]
    assert matrix.publish("shop", child, {"desc": "香薰"}, submit) == "published"
    submit.assert_called_once()

    retire = Mock(side_effect=[OSError("offline"), True])
    assert matrix.intercept_parent("shop", "100")
    assert matrix.publish("shop", tasks[1]["fission_id"], {}, submit) == "blocked"
    matrix.retire_pending(retire)
    assert matrix.list("shop", "100")[0]["status"] == "retirement_pending"
    matrix.retire_pending(retire)
    matrix.retire_pending(retire)
    assert retire.call_count == 2
    assert retire.call_args.args == ("shop", "200")
    assert [task["status"] for task in matrix.list("shop", "100")] == ["retired"] + ["blocked"] * 4
    with pytest.raises(ValueError, match="熔断"):
        matrix.create("shop", "100", variants)
    assert database.conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_publish_result_returned_after_parent_stop_is_queued_for_retirement(database, variants):
    from item_fission_matrix import ItemFissionMatrix

    assert database.save_cookie("shop", "offline")
    assert database.save_item_basic_info("shop", "100", item_title="香薰")
    matrix = ItemFissionMatrix(database)
    task = matrix.create("shop", "100", variants)[0]

    def publish(_payload):
        assert matrix.intercept_parent("shop", "100")
        return {"success": True, "itemId": "200"}

    assert matrix.publish("shop", task["fission_id"], {"desc": "香薰"}, publish) == "retirement_pending"
    assert matrix.list("shop", "100")[0]["child_item_id"] == "200"


def test_authorized_retirement_uses_verified_product_identity(monkeypatch):
    from item_fission_matrix import retire_platform_item

    call = Mock(side_effect=[
        {"code": 0, "data": {"product_id": 80, "publish_shop": [{"item_id": 200}], "spec_type": 1}},
        {"code": 0}, {"code": 0},
    ])
    monkeypatch.setattr(goofish_open, "call", call)
    assert retire_platform_item("shop", "200")
    assert [entry.args for entry in call.call_args_list] == [
        ("product.detail", {"item_id": "200"}),
        ("product.editStock", {"product_id": 80, "stock": 0}),
        ("product.downShelf", {"product_id": 80}),
    ]
    assert all(entry.kwargs["allow_write"] is True for entry in call.call_args_list[1:])


def test_parent_poll_distinguishes_stockout_down_shelf_and_republication(database, variants):
    from item_fission_matrix import ItemFissionMatrix

    assert database.save_cookie("shop", "offline")
    matrix = ItemFissionMatrix(database)
    for item_id in ("100", "101", "102"):
        assert database.save_item_basic_info("shop", item_id, item_title="香薰")
        matrix.create("shop", item_id, variants)
    responses = {
        "100": {"stock": 0, "offline_time": 1, "online_time": 2},
        "101": {"stock": 5, "offline_time": 3, "online_time": 2},
        "102": {"stock": 5, "offline_time": 1, "online_time": 2},
    }
    matrix.check_parents(responses.get)
    assert [row["status"] for row in matrix.list("shop", "100")] == ["blocked"] * 5
    assert [row["status"] for row in matrix.list("shop", "101")] == ["blocked"] * 5
    assert [row["status"] for row in matrix.list("shop", "102")] == ["draft"] * 5


def test_admin_publication_checks_parent_and_persists_result(database, monkeypatch, variants):
    import item_fission_api
    from item_fission_matrix import ItemFissionMatrix

    assert database.save_cookie("shop", "offline")
    assert database.save_item_basic_info("shop", "100", item_title="香薰")
    settings = {"enabled": True, "dry_run": False, "app_key": "app", "app_secret": "offline"}
    monkeypatch.setattr(goofish_open, "get_config", lambda: settings)
    monkeypatch.setattr(goofish_open, "get_product", lambda item_id: {
        "code": 0, "data": {"stock": 2, "publish_shop": [{"item_id": int(item_id)}]},
    })
    monkeypatch.setattr(item_fission_api.config, "get", lambda key, default=None: {
        "ITEM_FISSION.enabled": True, "APP_CONFIG.app_key": "app",
    }.get(key, default))
    monkeypatch.setattr(item_fission_api, "_night_ban", lambda: (True, ""))
    monkeypatch.setattr(item_fission_api.rate_limiter, "daily_count", lambda *_: 0)
    monkeypatch.setattr(item_fission_api.rate_limiter, "acquire", lambda *args, **kwargs: (True, 0))
    submit = Mock(return_value={"success": True, "itemId": "200"})
    monkeypatch.setattr(item_fission_api, "publish_item", submit)
    for index, variant in enumerate(variants):
        variant["cover_image_url"] = f"https://img.alicdn.com/{index}.jpg"
    matrix = ItemFissionMatrix(database)
    app = FastAPI()
    admin = Mock()

    def require_admin():
        admin()

    app.include_router(item_fission_api.create_fission_router(matrix, require_admin))
    with TestClient(app) as client:
        parent = "/api/item-fission/parents/shop/100"
        created = client.post(parent, json={"variants": variants})
        assert created.status_code == 201
        task = created.json()["tasks"][0]
        endpoint = f"/api/item-fission/tasks/shop/{task['fission_id']}/publish"
        assert client.post(endpoint, json={
            "desc": "香薰", "price": 19.9, "quantity": 1, "delivery": "包邮", "width": 800, "height": 800,
        }).json() == {"status": "published"}
        assert client.get(parent).json()["tasks"][0]["child_item_id"] == "200"
    assert admin.call_count == 3
    assert submit.call_args.args[1]["imageUrls"] == [
        {"url": task["cover_image_url"], "width": 800, "height": 800},
    ]
