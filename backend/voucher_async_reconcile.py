"""Persisted slow-topup dispatch, reconciliation, and refund checks."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

from loguru import logger

import delivery_reliability
import rate_limiter
from order_status_rules import cross_shop_block_reason

REFUND_FREEZE_BLOCKED = "REFUND_FREEZE_BLOCKED"
REFUND_EXPLANATION = "第三方运营商正在充值处理中，无法中途撤单，请等待充值完成"
ACTIVE = ("submitting", "submission_unknown", "topping_up")
CHANNEL_TERMINAL = ("success", "failed")
SAFE_TO_SWITCH = ("OUT_OF_STOCK", "not_submitted")


@dataclass(frozen=True)
class TopupRequest:
    request_id: str
    order_id: str
    account_id: str
    item_id: str
    charge_account: str = field(repr=False)
    timeout_at: str
    quantity: str | None
    paid_amount: str | None


@dataclass(frozen=True)
class TopupResult:
    status: str
    channel_order_id: str | None = None


class TopupAdapter(Protocol):
    def submit(self, request: TopupRequest) -> TopupResult:
        """Submit with the persisted request ID as the provider idempotency key."""
        ...

    def lookup(self, request: TopupRequest, channel_order_id: str | None) -> TopupResult:
        """Report not_submitted only when the request can no longer be accepted."""
        ...


def _utc(now):
    if now is None:
        return datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError("充值对账时间须包含时区")
    return now.astimezone(timezone.utc)


def _stamp(now):
    return now.isoformat(timespec="microseconds")


class VoucherAsyncReconcile:
    def __init__(self, database, adapters: dict[str, TopupAdapter], *, timeout_seconds: int,
                 item_channels=None, alert=None):
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise ValueError("充值超时须为正整数秒")
        self.database = database
        self.adapters = adapters
        self.item_channels = item_channels if item_channels is not None else {}
        self.timeout = timedelta(seconds=timeout_seconds)
        self.alert = alert if alert is not None else self._log_exhausted

    @contextmanager
    def _cursor(self, *, write=False):
        with self.database.lock, self.database.conn as conn, closing(conn.cursor()) as cursor:
            cursor.row_factory = sqlite3.Row
            if write:
                cursor.execute("BEGIN IMMEDIATE")
            yield cursor

    def enroll(self, order_id, account_id, item_id, charge_account, channels=None, *, now=None):
        """Enroll a paid order using the caller's approved channel priority."""
        if channels is None:
            channels = self.item_channels.get(account_id, {}).get(item_id)
        if (not isinstance(channels, (list, tuple)) or not channels
                or any(not isinstance(code, str) or not code or code not in self.adapters for code in channels)
                or len(set(channels)) != len(channels)):
            raise ValueError("充值渠道须为已配置且互异的有序列表")
        if not isinstance(charge_account, str) or not charge_account or charge_account != charge_account.strip():
            raise ValueError("充值账号缺失或格式无效")
        timestamp = _stamp(_utc(now))
        with self._cursor(write=True) as cursor:
            if delivery_reliability.cards(order_id, account_id):
                return False
            return bool(cursor.execute("""
                INSERT INTO voucher_async_reconcile (order_id, account_id, item_id, charge_account,
                    channels_json, topup_status, attempts, created_at, updated_at)
                SELECT order_id, cookie_id, item_id, ?, ?, 'queued', 0, ?, ?
                FROM orders WHERE order_id=? AND cookie_id=? AND item_id=? AND order_status='pending_ship'
                    AND COALESCE(dropship_status, '')='' AND COALESCE(system_shipped, 0)=0
                ON CONFLICT (order_id) DO NOTHING
            """, (charge_account, json.dumps(channels), timestamp, timestamp, order_id, account_id, item_id)).rowcount)

    def get(self, order_id, account_id):
        with self._cursor() as cursor:
            row = cursor.execute("""
                SELECT order_id, account_id, item_id, channel_code, channel_order_id,
                       topup_status, timeout_at, attempts, refund_state, created_at, updated_at
                FROM voucher_async_reconcile WHERE order_id=? AND account_id=?
            """, (order_id, account_id)).fetchone()
            return dict(row) if row else None

    def _claim(self, order_id, now):
        with self._cursor(write=True) as cursor:
            row = cursor.execute("""
                SELECT ledger.*, orders.order_status, orders.buyer_id, orders.system_shipped,
                       orders.quantity, orders.amount,
                       attempt.request_id
                FROM voucher_async_reconcile AS ledger
                JOIN orders ON orders.order_id=ledger.order_id AND orders.cookie_id=ledger.account_id
                    AND orders.item_id=ledger.item_id
                LEFT JOIN voucher_async_attempts AS attempt ON attempt.order_id=ledger.order_id
                    AND attempt.channel_code=ledger.channel_code
                WHERE ledger.order_id=?
            """, (order_id,)).fetchone()
            if row is None:
                return None, None, None, None
            status = row["topup_status"]
            if status in ACTIVE and row["timeout_at"] <= _stamp(now):
                request = TopupRequest(row["request_id"], order_id, row["account_id"], row["item_id"],
                                       row["charge_account"], row["timeout_at"], row["quantity"], row["amount"])
                return status, row["channel_code"], request, row["channel_order_id"]
            if status == "queued" and (
                row["order_status"] in ("cancelled", "shipped", "completed")
                or row["system_shipped"] or delivery_reliability.cards(order_id, row["account_id"])
            ):
                cursor.execute("""
                    UPDATE voucher_async_reconcile SET topup_status='failed', updated_at=? WHERE order_id=?
                """, (_stamp(now), order_id))
                return "failed", None, None, None
            if (status != "queued" or row["order_status"] != "pending_ship"
                    or not self.database.get_cookie_status(row["account_id"])
                    or self.database.is_account_automation_silent(row["account_id"])
                    or rate_limiter.circuit_blocked(row["account_id"])[0]
                    or cross_shop_block_reason(self.database, row["buyer_id"])):
                if status == "queued" and row["order_status"] in ("cancelled", "refunding"):
                    cursor.execute("""
                        UPDATE voucher_async_reconcile SET topup_status='failed', updated_at=? WHERE order_id=?
                    """, (_stamp(now), order_id))
                    return "failed", None, None, None
                return status, None, None, None

            channel = json.loads(row["channels_json"])[row["attempts"]]
            if channel not in self.adapters:
                return status, None, None, None
            timeout_at = _stamp(now + self.timeout)
            request = TopupRequest(uuid4().hex, order_id, row["account_id"], row["item_id"],
                                   row["charge_account"], timeout_at, row["quantity"], row["amount"])
            cursor.execute("""
                INSERT INTO voucher_async_attempts (request_id, order_id, channel_code, status,
                    timeout_at, created_at, updated_at) VALUES (?, ?, ?, 'submitting', ?, ?, ?)
            """, (request.request_id, order_id, channel, timeout_at, _stamp(now), _stamp(now)))
            cursor.execute("""
                UPDATE voucher_async_reconcile SET topup_status='submitting',
                    channel_code=?, channel_order_id=NULL, timeout_at=?, attempts=attempts+1, updated_at=?
                WHERE order_id=?
            """, (channel, timeout_at, _stamp(now), order_id))
            return "queued", channel, request, None

    def _record(self, channel, request_id, result, now):
        if not isinstance(result, TopupResult):
            return False
        status, upstream_id = result.status, result.channel_order_id
        if status in ("topping_up", *CHANNEL_TERMINAL):
            if not isinstance(upstream_id, str) or not upstream_id or upstream_id != upstream_id.strip():
                return False
        elif status not in (*SAFE_TO_SWITCH, "submission_unknown") or upstream_id is not None:
            return False

        with self._cursor(write=True) as cursor:
            row = cursor.execute("""
                SELECT attempt.*, ledger.channel_code AS current_channel, ledger.channels_json, ledger.attempts
                FROM voucher_async_attempts AS attempt JOIN voucher_async_reconcile AS ledger
                    ON ledger.order_id=attempt.order_id
                WHERE attempt.request_id=? AND attempt.channel_code=?
            """, (request_id, channel)).fetchone()
            if row is None:
                return False
            if (row["status"], row["channel_order_id"]) == (status, upstream_id):
                return True
            if (row["status"] not in ACTIVE or row["current_channel"] != channel
                    or (row["channel_order_id"] is not None and row["channel_order_id"] != upstream_id)):
                return False
            if status == "submission_unknown" and row["status"] != "submitting":
                return False
            if upstream_id is not None and cursor.execute("""
                SELECT 1 FROM voucher_async_attempts
                WHERE channel_code=? AND channel_order_id=? AND request_id<>?
            """, (channel, upstream_id, request_id)).fetchone():
                return False
            next_status = status
            if status in SAFE_TO_SWITCH:
                next_status = "queued" if row["attempts"] < len(json.loads(row["channels_json"])) else "failed"
            cursor.execute("""
                UPDATE voucher_async_attempts SET status=?, channel_order_id=?, updated_at=? WHERE request_id=?
            """, (status, upstream_id, _stamp(now), request_id))
            cursor.execute("""
                UPDATE voucher_async_reconcile SET topup_status=?, channel_order_id=?, updated_at=?,
                    refund_state=CASE WHEN ?='failed' AND refund_state IS NOT NULL
                                      THEN 'released' ELSE refund_state END
                WHERE order_id=?
            """, (next_status, upstream_id, _stamp(now), next_status, row["order_id"]))
            return True

    def record_callback(self, channel_code, request_id, result: TopupResult, *, now=None):
        """Accept a terminal event from a supplier adapter after signature verification."""
        return (isinstance(result, TopupResult) and result.status in CHANNEL_TERMINAL
                and self._record(channel_code, request_id, result, _utc(now)))

    def dispatch(self, order_id, *, now=None):
        current_time = _utc(now)
        while True:
            status, channel, request, upstream_id = self._claim(order_id, current_time)
            if request is None:
                if status == "failed":
                    self._alert_exhausted(order_id)
                return status
            adapter = self.adapters.get(channel)
            if adapter is None:
                return status
            try:
                result = adapter.submit(request) if status == "queued" else adapter.lookup(request, upstream_id)
            except Exception as exc:
                logger.warning("订单 {} 充值渠道 {} 请求未确认（{}）", order_id, channel, type(exc).__name__)
                result = TopupResult("submission_unknown")
            recorded_at = _utc(now)
            if not self._record(channel, request.request_id, result, recorded_at) and status == "queued":
                self._record(channel, request.request_id, TopupResult("submission_unknown"), recorded_at)
            latest = self.get(order_id, request.account_id)["topup_status"]
            if latest != "queued":
                if latest == "failed":
                    self._alert_exhausted(order_id)
                return latest
            current_time = recorded_at

    @staticmethod
    def _log_exhausted(account_id, order_id):
        logger.error("账号 {} 订单 {} 充值渠道已耗尽，需人工处理", account_id, order_id)

    def _alert_exhausted(self, order_id):
        with self._cursor(write=True) as cursor:
            row = cursor.execute("""
                UPDATE voucher_async_reconcile SET failure_alerted=1
                WHERE order_id=? AND topup_status='failed' AND failure_alerted=0
                    AND attempts=json_array_length(channels_json) RETURNING account_id
            """, (order_id,)).fetchone()
        if row:
            try:
                self.alert(row["account_id"], order_id)
            except Exception:
                self._log_exhausted(row["account_id"], order_id)

    def refund_decision(self, order_id, account_id):
        with self._cursor() as cursor:
            row = cursor.execute("""
                SELECT ledger.topup_status FROM voucher_async_reconcile AS ledger JOIN orders
                    ON orders.order_id=ledger.order_id AND orders.cookie_id=ledger.account_id
                    AND orders.item_id=ledger.item_id
                WHERE ledger.order_id=? AND ledger.account_id=?
                    AND ledger.topup_status IN ('submission_unknown', 'topping_up')
                    AND ledger.channel_order_id IS NOT NULL
                    AND orders.order_status='refunding'
            """, (order_id, account_id)).fetchone()
            if row:
                return REFUND_FREEZE_BLOCKED
            unknown = cursor.execute("""
                SELECT 1 FROM voucher_async_reconcile AS ledger JOIN orders
                    ON orders.order_id=ledger.order_id AND orders.cookie_id=ledger.account_id
                    AND orders.item_id=ledger.item_id
                WHERE ledger.order_id=? AND ledger.account_id=?
                    AND ledger.topup_status='submission_unknown'
                    AND ledger.channel_order_id IS NULL
                    AND orders.order_status='refunding'
            """, (order_id, account_id)).fetchone()
            return "SUBMISSION_PENDING_LOOKUP" if unknown else None

    def handle_refund(self, order_id, account_id, seller_eid, reason, *, now=None):
        from dropshipping import REFUND_ONLY, REFUND_WAITING_FOR_SELLER, reject_platform_refund

        def still_eligible():
            return self.refund_decision(order_id, account_id) == REFUND_FREEZE_BLOCKED

        decision = self.refund_decision(order_id, account_id)
        if not decision:
            return None
        if decision != REFUND_FREEZE_BLOCKED:
            logger.warning("订单 {} 充值提交结果未知，待供应商查单确认后再处理退款", order_id)
            return decision

        if not still_eligible():
            return None
        with self._cursor() as cursor:
            cursor.execute("""
                UPDATE voucher_async_reconcile SET refund_state=?, updated_at=?
                WHERE order_id=? AND account_id=? AND topup_status='topping_up' AND refund_state IS NULL
            """, (REFUND_FREEZE_BLOCKED, _stamp(_utc(now)), order_id, account_id))
        if not seller_eid or not reason or rate_limiter.circuit_blocked(account_id)[0]:
            return REFUND_FREEZE_BLOCKED
        outcome = reject_platform_refund(
            self.database, order_id, account_id, seller_eid, reason, action="voucher_refund",
            explanation=REFUND_EXPLANATION, still_eligible=still_eligible,
            can_refuse=lambda detail: (detail.get("refund_status") == REFUND_WAITING_FOR_SELLER
                                       and detail.get("refund_type") == REFUND_ONLY),
        )
        if outcome:
            with self._cursor() as cursor:
                cursor.execute("""
                    UPDATE voucher_async_reconcile SET refund_state=?, updated_at=?
                    WHERE order_id=? AND account_id=? AND topup_status='topping_up'
                """, (outcome, _stamp(_utc(now)), order_id, account_id))
        return REFUND_FREEZE_BLOCKED

    def poll(self, stop, seller_eids, refund_reason):
        with self._cursor() as cursor:
            work = cursor.execute("""
                SELECT ledger.order_id, ledger.account_id, orders.order_status
                FROM voucher_async_reconcile AS ledger LEFT JOIN orders ON ledger.order_id=orders.order_id
                WHERE ledger.topup_status IN ('queued', 'submitting', 'submission_unknown', 'topping_up')
                    OR (ledger.topup_status='failed' AND ledger.failure_alerted=0
                        AND ledger.attempts=json_array_length(ledger.channels_json))
                ORDER BY ledger.created_at
            """).fetchall()
        for row in work:
            if stop.is_set():
                return
            order_id, account_id = row["order_id"], row["account_id"]
            try:
                self.dispatch(order_id)
                if row["order_status"] == "refunding":
                    self.handle_refund(order_id, account_id, seller_eids.get(account_id), refund_reason)
            except Exception as exc:
                logger.warning("订单 {} 充值对账未完成（{}）", order_id, type(exc).__name__)

    async def run(self, stop, seller_eids, refund_reason):
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.poll, stop, seller_eids, refund_reason)
            except Exception as exc:
                logger.warning("充值对账巡检失败（{}）", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                continue
