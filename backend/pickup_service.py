"""Buyer-authorized pickup backed by the order database and delivery ledger."""

import asyncio
import hashlib
import hmac
import json
import os
import time
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import delivery_reliability as ledger
from sqlite_runtime import connect_sqlite


class PickupError(Exception):
    def __init__(self, status, message="订单或提货凭证无效", retry_after=0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class PickupService:
    allowed_statuses = {"pending_ship", "shipped"}

    def __init__(self, orders_path, public_url, secret):
        url = urlsplit(public_url)
        is_local = url.hostname in ("127.0.0.1", "localhost")
        if ((url.scheme != "https" and not is_local) or not url.hostname or url.username or url.password
                or url.query or url.fragment or len(secret) < 32):
            raise ValueError("自提需要 HTTPS 页面地址和至少 32 字节的签名密钥")
        self.orders_path = orders_path
        self.url = url
        self.secret = secret

    def _token(self, order_id, cookie_id, buyer_id):
        body = json.dumps([order_id, cookie_id, buyer_id], separators=(",", ":")).encode()
        return hmac.new(self.secret, body, hashlib.sha256).hexdigest()

    def issue_link(self, order_id, cookie_id, buyer_id):
        with closing(connect_sqlite(self.orders_path)) as orders_conn:
            order = orders_conn.execute(
                "SELECT order_status FROM orders WHERE order_id=? AND cookie_id=? AND buyer_id=?",
                (order_id, cookie_id, buyer_id),
            ).fetchone()
            if not buyer_id or not order or order[0] not in self.allowed_statuses:
                raise PickupError(404)
        with closing(ledger._conn()) as conn, conn:
            if not conn.execute(
                "SELECT 1 FROM delivery_cards WHERE order_id=? AND cookie_id=? AND buyer_id=?",
                (order_id, cookie_id, buyer_id),
            ).fetchone():
                raise PickupError(409, "订单发货账本未就绪")
            conn.execute(
                "INSERT OR IGNORE INTO delivery_pickups(order_id, cookie_id, buyer_id) VALUES(?,?,?)",
                (order_id, cookie_id, buyer_id),
            )
        fragment = urlencode({"order_id": order_id, "pickup_token": self._token(order_id, cookie_id, buyer_id)})
        return urlunsplit(self.url._replace(fragment=fragment))

    def access(self, order_id, token, ip, *, collect=False, now=None):
        now = int(time.time() if now is None else now)
        with closing(ledger._conn()) as conn, conn:
            conn.execute("ATTACH DATABASE ? AS shop", (str(self.orders_path),))
            conn.execute("BEGIN IMMEDIATE")
            self._check_limit(conn, ip, now)
            try:
                return self._access_order(conn, order_id, token, ip, now, collect)
            except PickupError:
                conn.commit()
                raise

    @staticmethod
    def _check_limit(conn, ip, now):
        conn.execute("DELETE FROM pickup_requests WHERE requested_at <= ?", (now - 300,))
        conn.execute("DELETE FROM pickup_ip_state WHERE updated_at <= ? AND banned_until <= ?",
                     (now - 900, now))
        row = conn.execute("SELECT banned_until FROM pickup_ip_state WHERE ip=?", (ip,)).fetchone()
        if row and row[0] > now:
            raise PickupError(429, "查询暂时受限，请稍后再试", row[0] - now)
        count, oldest = conn.execute(
            "SELECT count(*), min(requested_at) FROM pickup_requests WHERE ip=?", (ip,),
        ).fetchone()
        if count >= 10:
            raise PickupError(429, "查询次数已达上限，请稍后再试", oldest + 300 - now)
        conn.execute("INSERT INTO pickup_requests VALUES(?,?)", (ip, now))

    def _access_order(self, conn, order_id, token, ip, now, collect):
        order = conn.execute(
            "SELECT o.order_status, p.cookie_id, p.buyer_id, i.item_title "
            "FROM shop.orders o JOIN delivery_pickups p USING(order_id, cookie_id, buyer_id) "
            "LEFT JOIN shop.item_info i ON i.item_id=o.item_id AND i.cookie_id=o.cookie_id "
            "WHERE o.order_id=?", (order_id,),
        ).fetchone()
        expected = self._token(order_id, order["cookie_id"], order["buyer_id"]) if order else "0" * 64
        if not hmac.compare_digest(expected.encode(), token.encode()) or not order:
            conn.execute(
                "INSERT INTO pickup_ip_state VALUES(?,1,0,?) ON CONFLICT(ip) DO UPDATE SET "
                "failures=CASE WHEN banned_until>0 THEN 1 ELSE failures+1 END, "
                "banned_until=CASE WHEN banned_until=0 AND failures>=2 THEN ? ELSE 0 END, "
                "updated_at=excluded.updated_at", (ip, now, now + 900),
            )
            raise PickupError(404)
        conn.execute("DELETE FROM pickup_ip_state WHERE ip=?", (ip,))
        if order["order_status"] not in self.allowed_statuses:
            raise PickupError(404)

        key = (order_id, order["cookie_id"], order["buyer_id"])
        cards = conn.execute(
            "SELECT kind, payload, status, sent_at FROM delivery_cards "
            "WHERE order_id=? AND cookie_id=? AND buyer_id=? ORDER BY idx", key,
        ).fetchall()
        ready = bool(cards) and all(
            card["status"] in {"sent", "collected"} and card["payload"] for card in cards
        )
        already_collected = ready and all(card["status"] == "collected" for card in cards)
        title = (order["item_title"] or "").strip()
        result = {
            "title": f"{title[:2]}***" if len(title) > 2 else "***",
            "status": "collected" if already_collected else "ready" if ready else "pending",
            "shipped_at": max((card["sent_at"] or "" for card in cards), default="") or None,
        }
        if not collect:
            return result
        if not ready:
            raise PickupError(409, "发货内容尚未就绪")
        if not already_collected and conn.execute(
            "SELECT 1 FROM shop.sqlite_schema WHERE type='table' AND name='global_cross_shop_blacklist'",
        ).fetchone() and conn.execute(
            "SELECT 1 FROM shop.global_cross_shop_blacklist WHERE buyer_id=?", (order["buyer_id"],),
        ).fetchone():
            raise PickupError(409, "订单提货已暂停，请联系卖家")
        collected_at = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE delivery_cards SET status='collected', updated_at=? "
            "WHERE order_id=? AND cookie_id=? AND buyer_id=? AND status='sent'",
            (ledger._now(), *key),
        )
        conn.execute(
            "UPDATE delivery_pickups SET collected_at=?, collected_ip=? "
            "WHERE order_id=? AND cookie_id=? AND buyer_id=? AND collected_at IS NULL",
            (collected_at, ip, *key),
        )
        return {**result, "status": "collected",
                "cards": [{"kind": row["kind"], "payload": row["payload"]} for row in cards]}


def configured_service(orders_path):
    from config import config

    public_url = os.getenv("PICKUP_PUBLIC_URL", "")
    secret = os.getenv("PICKUP_SIGNING_KEY", "")
    enabled = config.get("PICKUP_PORTAL.enabled", False) is True
    if not (public_url or secret or enabled):
        return None
    if enabled:
        public_url = public_url or config.get("PICKUP_PORTAL.public_url", "")
        secret = secret or config.get("PICKUP_PORTAL.signing_key", "")
    if not isinstance(public_url, str) or not isinstance(secret, str):
        raise ValueError("自提地址与签名密钥需要填写为字符串")
    return PickupService(orders_path, public_url, secret.encode())


def delivery_message(orders_path, order_id, cookie_id, buyer_id, content):
    service = configured_service(orders_path)
    if service is None:
        if ledger.uses_pickup(order_id, cookie_id):
            raise PickupError(503, "该单的网页自提配置不可用")
        return content
    link = service.issue_link(order_id, cookie_id, buyer_id)
    return f"您的订单已准备，打开专属链接提货。请勿转发。\n{link}"


async def prepare_delivery_message(order_id, cookie_id, buyer_id, content):
    from db_manager import db_manager

    try:
        return await asyncio.to_thread(
            delivery_message, db_manager.db_path, order_id, cookie_id, buyer_id, content,
        )
    except Exception as exc:
        raise ledger.DeliverySubmissionNotAttempted("订单发货内容准备失败，请检查自提配置与订单账本") from exc


class PickupRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=128)
    pickup_token: str = Field(default="", max_length=128)


def create_pickup_router(orders_path):
    router = APIRouter(prefix="/api/pickup", tags=["pickup"])

    def respond(payload, request, *, collect):
        headers = {"Cache-Control": "no-store"}
        try:
            try:
                service = configured_service(orders_path)
            except ValueError as exc:
                raise PickupError(503, "网页自提配置无效") from exc
            if service is None:
                raise PickupError(503, "网页自提未启用")
            ip = request.client.host if request.client else "unknown"
            data = service.access(payload.order_id, payload.pickup_token, ip, collect=collect)
            return JSONResponse({"ok": True, "data": data}, headers=headers)
        except PickupError as exc:
            if exc.retry_after:
                headers["Retry-After"] = str(exc.retry_after)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status, headers=headers)

    @router.post("/query")
    def query(payload: PickupRequest, request: Request):
        return respond(payload, request, collect=False)

    @router.post("/collect")
    def collect(payload: PickupRequest, request: Request):
        return respond(payload, request, collect=True)

    return router
