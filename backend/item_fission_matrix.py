"""SQLite publication tasks and parent inventory circuit breaker."""

import asyncio
import sqlite3
from contextlib import closing
from urllib.parse import urlsplit
from uuid import uuid4

import goofish_open
from loguru import logger

from sqlite_runtime import connect_sqlite


class ItemFissionMatrix:
    def __init__(self, database):
        self.database = database

    def _connection(self):
        conn = connect_sqlite(self.database.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create(self, cookie_id, parent_item_id, variants):
        if not self.database.get_item_info(cookie_id, parent_item_id):
            raise ValueError("母商品不存在或不属于当前账号")
        if not 5 <= len(variants) <= 10:
            raise ValueError("每批需要 5 至 10 组标题和主图")
        tasks = []
        for variant in variants:
            title, url = variant["variant_title"].strip(), variant["cover_image_url"].strip()
            parsed = urlsplit(url)
            if (not title or not 1 <= len(title) <= 30 or parsed.scheme not in ("http", "https")
                    or not parsed.hostname or parsed.username is not None or parsed.password is not None):
                raise ValueError("派生标题须为 1 至 30 字，主图须为 HTTP 或 HTTPS 地址")
            tasks.append((parent_item_id, uuid4().hex, title, url, cookie_id))
        if len({task[2] for task in tasks}) != len(tasks) or len({task[3] for task in tasks}) != len(tasks):
            raise ValueError("每组派生任务须使用独立标题和主图")
        try:
            with closing(self._connection()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                if self._is_stopped(conn, cookie_id, parent_item_id):
                    raise ValueError("母商品已熔断")
                conn.executemany(
                    "INSERT INTO item_fission_matrix "
                    "(parent_item_id, fission_id, variant_title, cover_image_url, cookie_id) VALUES (?, ?, ?, ?, ?)",
                    tasks,
                )
                return self._list(conn, cookie_id, parent_item_id)[-len(tasks):]
        except sqlite3.IntegrityError as exc:
            raise ValueError("母商品已有相同派生标题") from exc

    @staticmethod
    def _list(conn, cookie_id, parent_item_id):
        return [dict(row) for row in conn.execute(
            "SELECT * FROM item_fission_matrix WHERE cookie_id=? AND parent_item_id=? ORDER BY rowid",
            (cookie_id, parent_item_id),
        )]

    def list(self, cookie_id, parent_item_id):
        with closing(self._connection()) as conn:
            return self._list(conn, cookie_id, parent_item_id)

    @staticmethod
    def _is_stopped(conn, cookie_id, parent_item_id):
        return conn.execute(
            "SELECT 1 FROM item_fission_stops WHERE cookie_id=? AND parent_item_id=?",
            (cookie_id, parent_item_id),
        ).fetchone() is not None

    def intercept_parent(self, cookie_id, parent_item_id):
        with closing(self._connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(
                "SELECT 1 FROM item_fission_matrix WHERE cookie_id=? AND parent_item_id=? LIMIT 1",
                (cookie_id, parent_item_id),
            ).fetchone():
                return False
            inserted = conn.execute(
                "INSERT OR IGNORE INTO item_fission_stops (cookie_id, parent_item_id) VALUES (?, ?)",
                (cookie_id, parent_item_id),
            ).rowcount
            conn.execute("""
                UPDATE item_fission_matrix
                SET status=CASE status WHEN 'draft' THEN 'blocked' ELSE 'retirement_pending' END
                WHERE cookie_id=? AND parent_item_id=? AND status IN ('draft', 'published')
            """, (cookie_id, parent_item_id))
            return bool(inserted)

    def publish(self, cookie_id, fission_id, item_data, submit, *, ready=None):
        from publish_guard import check_publish

        with closing(self._connection()) as conn:
            task = conn.execute(
                "SELECT * FROM item_fission_matrix WHERE cookie_id=? AND fission_id=?",
                (cookie_id, fission_id),
            ).fetchone()
            if task is None:
                raise ValueError("派生任务不存在")
            if task["status"] != "draft":
                return task["status"]
            payload = {**item_data, "title": task["variant_title"], "imageUrls": [task["cover_image_url"]]}
            desc = payload.get("desc")
            if not isinstance(desc, str) or not desc.strip() or not check_publish(payload["title"], desc, images=1)["ok"]:
                raise ValueError("商品描述为空或内容未通过发布检查")
            if self.database.is_account_automation_silent(cookie_id):
                return "paused"
            if ready is not None and not ready(task["parent_item_id"], task["cover_image_url"]):
                row = conn.execute(
                    "SELECT status FROM item_fission_matrix WHERE fission_id=?", (fission_id,),
                ).fetchone()
                return "paused" if row["status"] == "draft" else row["status"]
            with conn:
                claimed = conn.execute("""
                    UPDATE item_fission_matrix SET status='publishing'
                    WHERE cookie_id=? AND fission_id=? AND status='draft'
                """, (cookie_id, fission_id)).rowcount
            if not claimed:
                return conn.execute("SELECT status FROM item_fission_matrix WHERE fission_id=?",
                                    (fission_id,)).fetchone()["status"]

        try:
            result = submit(payload)
            child_item_id = result.get("itemId")
            if (result.get("success") is not True or not isinstance(child_item_id, str)
                    or not child_item_id.isascii() or not child_item_id.isdecimal()
                    or child_item_id == task["parent_item_id"]):
                raise ValueError("发布结果缺少有效子商品 ID")
        except Exception as exc:
            logger.warning("派生商品发布任务 {} 未确认（{}）", fission_id, type(exc).__name__)
            return self._finish_publication(task, "submission_unknown", None)
        try:
            return self._finish_publication(task, "published", child_item_id)
        except sqlite3.IntegrityError:
            logger.warning("派生商品发布任务 {} 返回的商品 ID 已绑定", fission_id)
            return self._finish_publication(task, "submission_unknown", None)

    def _finish_publication(self, task, status, child_item_id):
        with closing(self._connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if status == "published" and self._is_stopped(conn, task["cookie_id"], task["parent_item_id"]):
                status = "retirement_pending"
            conn.execute(
                "UPDATE item_fission_matrix SET status=?, child_item_id=? WHERE fission_id=? AND status='publishing'",
                (status, child_item_id, task["fission_id"]),
            )
            return status

    def retire_pending(self, retire, stop=None):
        with closing(self._connection()) as conn:
            tasks = conn.execute("""
                SELECT fission_id, cookie_id, child_item_id FROM item_fission_matrix
                WHERE status='retirement_pending' ORDER BY rowid
            """).fetchall()
            for task in tasks:
                if stop is not None and stop.is_set():
                    return
                try:
                    if retire(task["cookie_id"], task["child_item_id"]) is not True:
                        continue
                except Exception as exc:
                    logger.warning("派生商品 {} 下架未确认（{}）", task["child_item_id"], type(exc).__name__)
                    continue
                with conn:
                    conn.execute(
                        "UPDATE item_fission_matrix SET status='retired' WHERE fission_id=? AND status='retirement_pending'",
                        (task["fission_id"],),
                    )

    def check_parents(self, fetch, stop=None):
        with closing(self._connection()) as conn:
            parents = conn.execute("""
                SELECT DISTINCT matrix.cookie_id, matrix.parent_item_id FROM item_fission_matrix AS matrix
                WHERE matrix.status IN ('draft', 'published', 'publishing', 'submission_unknown')
                AND NOT EXISTS (SELECT 1 FROM item_fission_stops AS stops
                                WHERE stops.cookie_id=matrix.cookie_id AND stops.parent_item_id=matrix.parent_item_id)
            """).fetchall()
        for parent in parents:
            if stop is not None and stop.is_set():
                return
            try:
                detail = fetch(parent["parent_item_id"])
                if detail and _is_unavailable(detail):
                    self.intercept_parent(parent["cookie_id"], parent["parent_item_id"])
            except Exception as exc:
                logger.warning("母商品 {} 库存状态读取失败（{}）", parent["parent_item_id"], type(exc).__name__)


def _platform_detail(item_id):
    result = goofish_open.get_product(item_id=item_id)
    detail = result.get("data")
    if result.get("code") != 0 or not isinstance(detail, dict):
        return None
    shops = detail.get("publish_shop")
    if (not isinstance(shops, list) or len(shops) != 1 or not isinstance(shops[0], dict)
            or str(shops[0].get("item_id")) != item_id):
        return None
    return detail


def _is_off_shelf(detail):
    offline, online = detail.get("offline_time"), detail.get("online_time")
    return type(offline) is int and type(online) is int and offline > 0 and offline >= online >= 0


def _is_unavailable(detail):
    stock = detail.get("stock")
    return (type(stock) is int and stock == 0) or _is_off_shelf(detail)


def parent_ready(matrix, cookie_id, item_id):
    detail = _platform_detail(item_id)
    if not detail:
        return False
    if _is_unavailable(detail):
        matrix.intercept_parent(cookie_id, item_id)
        return False
    return type(detail.get("stock")) is int and detail["stock"] > 0


def retire_platform_item(_cookie_id, item_id):
    detail = _platform_detail(item_id)
    if not detail or detail.get("spec_type") != 1:
        return False
    product_id = detail.get("product_id")
    if type(product_id) is not int or product_id <= 0:
        return False
    if (detail.get("stock") != 0
            and goofish_open.call("product.editStock", {"product_id": product_id, "stock": 0},
                                  allow_write=True).get("code") != 0):
        return False
    return _is_off_shelf(detail) or goofish_open.call(
        "product.downShelf", {"product_id": product_id}, allow_write=True,
    ).get("code") == 0


async def run_scheduler(matrix, stop):
    def scan():
        settings = goofish_open.get_config()
        if settings["enabled"] and goofish_open.is_configured(settings):
            matrix.check_parents(_platform_detail, stop)
            if not settings["dry_run"]:
                matrix.retire_pending(retire_platform_item, stop)

    while not stop.is_set():
        try:
            await asyncio.to_thread(scan)
        except Exception as exc:
            logger.warning("裂变商品巡检失败（{}）", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), 300)
        except asyncio.TimeoutError:
            continue
