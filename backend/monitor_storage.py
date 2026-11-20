"""SQLite storage for monitor tasks, cooldowns and globally unique discoveries."""

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sqlite_runtime import connect_sqlite


DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "monitor_engine.db"
BACKOFF_SECONDS = (300, 900, 1800)


class MonitorTask(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    keyword: str = Field(min_length=1, max_length=200)
    max_price: float = Field(ge=0)
    min_price: float = Field(default=0, ge=0)
    exclude_words: list[str] = Field(default_factory=list, max_length=100)
    category_id: str | None = Field(default=None, min_length=1, max_length=100)
    interval_seconds: int = Field(default=120, ge=60)
    is_enabled: bool = True
    test_mode: bool = False

    @model_validator(mode="after")
    def validate_price_range(self):
        if self.min_price > self.max_price:
            raise ValueError("最低过滤价不能高于最高限价")
        return self


class MonitorStore:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS monitor_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL,
                    max_price REAL NOT NULL,
                    min_price REAL NOT NULL CHECK (min_price >= 0 AND min_price <= max_price),
                    exclude_words TEXT NOT NULL DEFAULT '[]',
                    category_id TEXT,
                    interval_seconds INTEGER NOT NULL DEFAULT 120 CHECK (interval_seconds >= 60),
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    test_mode INTEGER NOT NULL DEFAULT 0,
                    risk_count INTEGER NOT NULL DEFAULT 0,
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS monitor_records (
                    item_id TEXT PRIMARY KEY NOT NULL,
                    task_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    price REAL NOT NULL,
                    image_urls TEXT NOT NULL DEFAULT '[]',
                    seller_id TEXT,
                    seller_credit TEXT,
                    description TEXT NOT NULL DEFAULT '',
                    published_at INTEGER,
                    item_url TEXT NOT NULL,
                    ai_evaluation TEXT,
                    status TEXT NOT NULL DEFAULT 'discovered'
                        CHECK (status IN ('discovered', 'pushed', 'ignored', 'relisted')),
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_monitor_records_task_time
                    ON monitor_records(task_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS monitor_platform_cooldown (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cooldown_until REAL NOT NULL DEFAULT 0
                );
            """)

    @contextmanager
    def _connection(self):
        with closing(connect_sqlite(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            yield conn

    @staticmethod
    def _task(row):
        if row is None:
            return None
        data = dict(row)
        data["exclude_words"] = json.loads(data["exclude_words"])
        data["is_enabled"] = bool(data["is_enabled"])
        data["test_mode"] = bool(data["test_mode"])
        return data

    @staticmethod
    def _values(task):
        return {**task.model_dump(), "exclude_words": json.dumps(
            [word for word in task.exclude_words if word], ensure_ascii=False
        )}

    def create_task(self, task: MonitorTask):
        with self._connection() as conn:
            row = conn.execute("""
                INSERT INTO monitor_tasks (keyword, max_price, min_price, exclude_words,
                    category_id, interval_seconds, is_enabled, test_mode)
                VALUES (:keyword, :max_price, :min_price, :exclude_words,
                    :category_id, :interval_seconds, :is_enabled, :test_mode)
                RETURNING *
            """, self._values(task)).fetchone()
        return self._task(row)

    def get_task(self, task_id: int):
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM monitor_tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task(row)

    def list_tasks(self, *, enabled_only=False):
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT * FROM monitor_tasks WHERE (NOT ? OR is_enabled = 1) ORDER BY id
            """, (enabled_only,)).fetchall()
        return [self._task(row) for row in rows]

    def update_task(self, task_id: int, task: MonitorTask):
        with self._connection() as conn:
            row = conn.execute("""
                UPDATE monitor_tasks SET keyword = :keyword, max_price = :max_price,
                    min_price = :min_price, exclude_words = :exclude_words,
                    category_id = :category_id, interval_seconds = :interval_seconds,
                    is_enabled = :is_enabled, test_mode = :test_mode, updated_at = CURRENT_TIMESTAMP
                WHERE id = :id RETURNING *
            """, {**self._values(task), "id": task_id}).fetchone()
        return self._task(row)

    def delete_task(self, task_id: int) -> bool:
        with self._connection() as conn:
            return bool(conn.execute("DELETE FROM monitor_tasks WHERE id = ?", (task_id,)).rowcount)

    def claim(self, task_id, item):
        with self._connection() as conn:
            inserted = conn.execute("""
                INSERT INTO monitor_records
                    (item_id, task_id, title, price, image_urls, published_at, item_url)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO NOTHING
            """, (item["item_id"], task_id, item["title"], item["price"],
                  json.dumps([item["main_image"]] if item.get("main_image") else []),
                  item.get("published_at"), f"https://www.goofish.com/item?id={item['item_id']}"))
            return inserted.rowcount == 1

    def release_unfetched(self, item_id):
        with self._connection() as conn:
            conn.execute("DELETE FROM monitor_records WHERE item_id = ?", (item_id,))

    def save_details(self, item, evaluation, status="discovered"):
        with self._connection() as conn:
            conn.execute("""
                UPDATE monitor_records SET title = ?, price = ?, image_urls = ?,
                    seller_id = ?, seller_credit = ?, description = ?, published_at = ?,
                    ai_evaluation = ?, status = ?
                WHERE item_id = ?
            """, (item["title"], item["price"], json.dumps(item["image_urls"], ensure_ascii=False),
                  item.get("seller_id"), item.get("seller_credit"), item.get("description", ""),
                  item.get("published_at"),
                  json.dumps(evaluation, ensure_ascii=False, allow_nan=False) if evaluation else None,
                  status, item["item_id"]))

    def mark_pushed(self, item_id):
        with self._connection() as conn:
            conn.execute("UPDATE monitor_records SET status = 'pushed' WHERE item_id = ?", (item_id,))

    def record_error(self, item_id, error):
        with self._connection() as conn:
            conn.execute("UPDATE monitor_records SET error = ? WHERE item_id = ?", (error, item_id))

    def list_records(self, task_id=None, *, limit=100, offset=0):
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT * FROM monitor_records WHERE (? IS NULL OR task_id = ?)
                ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?
            """, (task_id, task_id, limit, offset)).fetchall()
        records = [dict(row) for row in rows]
        for record in records:
            record["image_urls"] = json.loads(record["image_urls"])
            if record["ai_evaluation"] is not None:
                record["ai_evaluation"] = json.loads(record["ai_evaluation"])
        return records

    def platform_cooldown_until(self):
        with self._connection() as conn:
            row = conn.execute("SELECT cooldown_until FROM monitor_platform_cooldown").fetchone()
        return row["cooldown_until"] if row else 0

    def trip(self, task_id, now):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT risk_count FROM monitor_tasks WHERE id = ?", (task_id,)).fetchone()
            count = min((row["risk_count"] if row else 0) + 1, len(BACKOFF_SECONDS))
            delay = BACKOFF_SECONDS[count - 1]
            until = now + delay
            conn.execute("""
                UPDATE monitor_tasks SET risk_count = ?, cooldown_until = ? WHERE id = ?
            """, (count, until, task_id))
            conn.execute("""
                INSERT INTO monitor_platform_cooldown (id, cooldown_until) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET cooldown_until = MAX(cooldown_until, excluded.cooldown_until)
            """, (until,))
        return delay

    def clear_task_backoff(self, task_id):
        with self._connection() as conn:
            conn.execute("""
                UPDATE monitor_tasks SET risk_count = 0, cooldown_until = 0
                WHERE id = ? AND risk_count > 0
            """, (task_id,))
