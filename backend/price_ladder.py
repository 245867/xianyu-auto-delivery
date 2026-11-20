"""Opt-in price reductions with a platform-confirmed SQLite audit trail."""

import asyncio
import hmac
import json
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from loguru import logger

import goofish_open
from config import config
from sqlite_runtime import connect_sqlite

ON_SALE = 22
COOLDOWN = 24 * 3600
CALLBACK_TIMEOUT = 10 * 60
CALLBACK_PATH = "/api/price-ladder/callback"


def _cents(value):
    try:
        amount = Decimal(str(value))
        cents = amount * 100
        if not amount.is_finite() or not 1 <= cents <= 2**63 - 1 or cents % 1:
            raise ValueError("价格必须为正数，且最多保留两位小数")
        return int(cents)
    except InvalidOperation as exc:
        raise ValueError("价格格式无效") from exc


@dataclass(frozen=True)
class PricePolicy:
    price_floor: str
    drop_step: str
    idle_hours: int = 48
    floor_cents: int = field(init=False)
    step_cents: int = field(init=False)

    def __post_init__(self):
        if type(self.idle_hours) is not int or self.idle_hours < 1:
            raise ValueError("idle_hours 必须为正整数")
        object.__setattr__(self, "floor_cents", _cents(self.price_floor))
        object.__setattr__(self, "step_cents", _cents(self.drop_step))


class PriceLadder:
    def __init__(self, path):
        self.path = path
        with closing(connect_sqlite(path)) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS price_ladder_log (
                    id TEXT PRIMARY KEY, product_id INTEGER NOT NULL, item_id TEXT NOT NULL,
                    old_price INTEGER NOT NULL, new_price INTEGER NOT NULL, state TEXT NOT NULL,
                    created_at INTEGER NOT NULL, finished_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_price_ladder_product
                    ON price_ladder_log(product_id, created_at DESC);
            """)

    def apply(self, listing, policy, edit, *, now=None, operation_id=None):
        now = int(time.time() if now is None else now)
        if listing.get("product_status") != ON_SALE:
            return "offline"
        product_id, source_item_id = listing["product_id"], listing["item_id"]
        price = listing["price"]
        online_time = listing["online_time"]
        update_time = listing["update_time"]
        if (type(product_id) is not int or product_id <= 0 or type(price) is not int or price <= 0
                or type(online_time) is not int or online_time <= 0
                or type(update_time) is not int or update_time < 0 or not source_item_id):
            return "invalid"
        item_id = str(source_item_id)
        new_price = max(price - policy.step_cents, policy.floor_cents)
        if new_price >= price:
            return "floor"
        claim_id = operation_id or uuid.uuid4().hex
        with closing(connect_sqlite(self.path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT is_multi_spec FROM item_info WHERE item_id=? LIMIT 2", (item_id,),
            ).fetchall()
            if rows != [(0,)]:
                return "unsupported"
            latest = conn.execute(
                "SELECT id,state,created_at,finished_at FROM price_ladder_log "
                "WHERE product_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (product_id,),
            ).fetchone()
            last_changed = 0
            if latest:
                prior_id, state, created, finished = latest
                if state == "pending":
                    if created + CALLBACK_TIMEOUT <= now:
                        conn.execute(
                            "UPDATE price_ladder_log SET state='failed',finished_at=? WHERE id=?",
                            (created + CALLBACK_TIMEOUT, prior_id),
                        )
                    return "cooldown"
                if state == "failed" and finished + COOLDOWN > now:
                    return "cooldown"
                if state == "changed":
                    last_changed = finished
            if now - max(online_time, update_time, last_changed) < policy.idle_hours * 3600:
                return "idle"
            conn.execute(
                "INSERT INTO price_ladder_log VALUES(?,?,?,?,?,'pending',?,NULL)",
                (claim_id, product_id, item_id, price, new_price, now),
            )
        try:
            edit(product_id, new_price)
        except Exception as exc:
            logger.warning("商品 {} 改价调用失败（{}），暂停改价 24 小时", product_id, type(exc).__name__)
            return self._finish(claim_id, False, int(time.time()) if operation_id else now)
        if operation_id:
            return "pending"
        return self._finish(claim_id, True, now)

    def apply_exact(self, product_id, item_id, old_price, new_price, edit, *,
                    now=None, operation_id=None):
        """Submit an exact target price while keeping the pending callback audit."""
        now = int(time.time() if now is None else now)
        item_id = str(item_id or "")
        if (type(product_id) is not int or product_id <= 0
                or type(old_price) is not int or old_price <= 0
                or type(new_price) is not int or new_price <= 0 or not item_id):
            return "invalid"
        claim_id = operation_id or uuid.uuid4().hex
        with closing(connect_sqlite(self.path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT is_multi_spec FROM item_info WHERE item_id=? LIMIT 2", (item_id,),
            ).fetchall()
            if rows != [(0,)]:
                return "unsupported"
            latest = conn.execute(
                "SELECT id,state,created_at FROM price_ladder_log "
                "WHERE product_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (product_id,),
            ).fetchone()
            if latest:
                prior_id, state, created = latest
                if state == "pending":
                    if created + CALLBACK_TIMEOUT > now:
                        return "cooldown"
                    conn.execute(
                        "UPDATE price_ladder_log SET state='failed',finished_at=? WHERE id=?",
                        (created + CALLBACK_TIMEOUT, prior_id),
                    )
            conn.execute(
                "INSERT INTO price_ladder_log VALUES(?,?,?,?,?,'pending',?,NULL)",
                (claim_id, product_id, item_id, old_price, new_price, now),
            )
        try:
            edit(product_id, new_price)
        except Exception as exc:
            logger.warning("商品 {} 改价调用失败（{}），暂停改价 24 小时", product_id, type(exc).__name__)
            return self._finish(claim_id, False, int(time.time()) if operation_id else now)
        if operation_id:
            return "pending"
        return self._finish(claim_id, True, now)

    def _finish(self, operation_id, success, now, *, confirmation=None):
        with closing(connect_sqlite(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM price_ladder_log WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                return None
            if confirmation and (
                confirmation.get("task_type") != 13
                or confirmation.get("product_id") != row["product_id"]
                or (confirmation.get("item_id") is not None and str(confirmation["item_id"]) != row["item_id"])
                or type(confirmation.get("task_time")) is not int
                or confirmation["task_time"] < row["created_at"]
            ):
                return None
            if row["state"] != "pending":
                return row["state"]
            state = "changed" if success else "failed"
            if success:
                conn.execute(
                    "UPDATE item_info SET item_price=? WHERE item_id=? AND is_multi_spec=0",
                    (format(Decimal(row["new_price"]) / 100, ".2f"), row["item_id"]),
                )
            conn.execute("UPDATE price_ladder_log SET state=?,finished_at=? WHERE id=?",
                         (state, now, operation_id))
            return state

    def scan(self, callback_base, global_policy, product_policies, stop):
        settings = goofish_open.get_config()
        if not settings["enabled"] or settings["dry_run"] or not goofish_open.is_configured(settings):
            return
        page = 1
        while not stop.is_set():
            data = goofish_open.call(
                "product.list", {"product_status": ON_SALE, "page_no": page, "page_size": 50},
            )["data"]
            for listing in data["list"]:
                if stop.is_set():
                    return
                policy = product_policies.get(str(listing["product_id"]), global_policy)
                if policy is None or listing.get("spec_type") != 1:
                    continue
                now = int(time.time())
                if (listing["price"] <= policy.floor_cents or
                        now - max(listing["update_time"], listing["online_time"]) < policy.idle_hours * 3600):
                    continue
                detail = goofish_open.call("product.detail", {"product_id": listing["product_id"]})["data"]
                shops = detail.get("publish_shop") or []
                if (detail.get("spec_type") != 1 or len(shops) != 1
                        or not shops[0].get("item_id") or detail.get("sku_items")):
                    continue
                operation_id = uuid.uuid4().hex
                notify_url = f"{callback_base}{CALLBACK_PATH}?{urlencode({'operation_id': operation_id})}"

                def edit(product_id, new_price):
                    reply = goofish_open.call("product.edit", {
                        "product_id": product_id, "price": new_price, "notify_url": notify_url,
                    }, allow_write=True)
                    if reply.get("code") != 0:
                        raise ValueError("商品改价响应缺少成功状态")

                self.apply({**detail, "item_id": shops[0]["item_id"]},
                           policy, edit, operation_id=operation_id)
            if not data["list"] or page * 50 >= data["count"]:
                return
            page += 1


async def run_scheduler(orders_path, stop):
    callback_base = config.get("PRICE_LADDER.callback_base")
    if not isinstance(callback_base, str):
        logger.error("阶梯降价需要配置公开 HTTPS 回调域名")
        return
    url = urlsplit(callback_base)
    if (url.scheme != "https" or not url.hostname or url.path not in ("", "/")
            or url.query or url.fragment or url.username or url.password):
        logger.error("阶梯降价需要配置公开 HTTPS 回调域名")
        return
    try:
        global_settings = config.get("PRICE_LADDER.global_policy")
        global_policy = PricePolicy(**global_settings) if global_settings else None
        product_policies = {str(product): PricePolicy(**policy) for product, policy in
                            config.get("PRICE_LADDER.products", {}).items()}
    except (TypeError, ValueError):
        logger.error("阶梯降价策略配置无效")
        return
    if global_policy is None and not product_policies:
        logger.error("阶梯降价需要至少一条价格策略")
        return
    ladder = await asyncio.to_thread(PriceLadder, orders_path)
    while not stop.is_set():
        try:
            await asyncio.to_thread(ladder.scan, callback_base.rstrip("/"), global_policy, product_policies, stop)
        except Exception as exc:
            logger.warning("阶梯降价扫描中断（{}）", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), 300)
        except asyncio.TimeoutError:
            continue


def create_price_router(orders_path):
    router = APIRouter()

    @router.post(CALLBACK_PATH, include_in_schema=False)
    async def callback(request: Request, operation_id: str, appid: str, timestamp: int, sign: str):
        if not config.get("PRICE_LADDER.enabled", False):
            raise HTTPException(404, "阶梯降价未启用")
        settings = goofish_open.get_config()
        now = int(time.time())
        if not settings["app_secret"] or appid != settings["app_key"] or abs(now - timestamp) > 300:
            raise HTTPException(403, "回调签名无效")
        body = await request.body()
        try:
            text = body.decode("utf-8")
        except UnicodeError as exc:
            raise HTTPException(400, "回调编码无效") from exc
        expected = goofish_open.sign(appid, text, str(timestamp), settings["app_secret"])
        if not hmac.compare_digest(expected.encode(), sign.encode()):
            raise HTTPException(403, "回调签名无效")
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise HTTPException(400, "回调内容无效") from exc
        if not isinstance(payload, dict) or payload.get("task_result") not in (1, 2):
            raise HTTPException(400, "回调内容无效")
        ladder = await asyncio.to_thread(PriceLadder, orders_path)
        state = await asyncio.to_thread(
            ladder._finish, operation_id, payload["task_result"] == 1, now, confirmation=payload,
        )
        if not state:
            raise HTTPException(404, "改价任务不匹配")
        if state == "failed":
            logger.warning("商品 {} 平台改价未确认成功", payload["product_id"])
        return {"result": "success", "msg": "接收成功"}

    return router
