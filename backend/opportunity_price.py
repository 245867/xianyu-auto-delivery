"""
价格历史与市场水位（补齐④）—— 移植自 Usagi-org/ai-goofish-monitor
`src/services/price_history_service.py`（MIT），按本地 SQLite 单文件实现。

设计：
- 每次关键词搜索把当次榜单写入 data/price_history.db 的 price_snapshots 表；
  同一天同一商品只保留一行（INSERT OR REPLACE），避免同一关键词反复搜索刷爆历史。
- 聚合口径（30 天窗口，与上游 DEFAULT_HISTORY_WINDOW_DAYS 一致）：
  hist_low=区间最低、hist_avg=区间均价、hist_samples=样本天数、
  price_vs_low_pct=当前价相对历史最低的溢价百分比、hist_trend=最近两次快照涨跌。
- 市场水位 market：当前样本的最新价 → 最低 / P25 / 中位 / 均价；
  suggested_price=中位数 × 0.9（发布定价建议，留出议价空间，不低于 P25×0.95）。
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlite_runtime import connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "price_history.db"

WINDOW_DAYS = 30
KEEP_DAYS = 30


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_snapshots (
            day        TEXT NOT NULL,
            ts         TEXT NOT NULL,
            keyword    TEXT NOT NULL,
            item_id    TEXT NOT NULL,
            title      TEXT DEFAULT '',
            price      REAL NOT NULL,
            want_count INTEGER DEFAULT 0,
            region     TEXT DEFAULT '',
            seller     TEXT DEFAULT '',
            item_url   TEXT DEFAULT '',
            PRIMARY KEY (day, keyword, item_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_keyword_day "
                 "ON price_snapshots(keyword, day)")
    conn.commit()
    return conn


def record_snapshots(keyword: str, items: List[Dict[str, Any]]) -> int:
    """把一次搜索的结果写入快照表（当日去重 upsert）。返回写入行数。"""
    keyword = str(keyword or "").strip()
    if not keyword or not items:
        return 0
    now = datetime.now()
    day = now.date().isoformat()
    ts = now.isoformat(timespec="seconds")
    rows = []
    for it in items:
        item_id = str(it.get("itemId") or "").strip()
        price = it.get("price")
        if not item_id or price is None:
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        # A3：只有详情接口来的想要数才可信（2026 起列表 wantNum 恒 0）。
        # 非详情来源一律写 NULL，避免 0 污染历史与成长值。
        want = it.get("want_count")
        if it.get("want_source") != "detail" or it.get("want_stale"):
            want = None
        rows.append((day, ts, keyword, item_id, str(it.get("title") or "")[:200],
                     price, want,
                     str(it.get("area") or "")[:60], str(it.get("seller") or "")[:80],
                     str(it.get("itemUrl") or "")[:300]))
    if not rows:
        return 0
    conn = _conn()
    try:
        cutoff = now - timedelta(days=KEEP_DAYS)
        conn.execute(
            "DELETE FROM price_snapshots WHERE day <= ? AND ts < ?",
            (cutoff.date().isoformat(), cutoff.isoformat(timespec="seconds")),
        )
        # 同日重复搜索时：新值为 NULL 就保留旧值（别把已经补到的详情想要数冲掉）
        conn.executemany(
            "INSERT INTO price_snapshots "
            "(day, ts, keyword, item_id, title, price, want_count, region, seller, item_url) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(day, keyword, item_id) DO UPDATE SET "
            "  ts=excluded.ts, title=excluded.title, price=excluded.price, "
            "  want_count=COALESCE(excluded.want_count, price_snapshots.want_count), "
            "  region=excluded.region, seller=excluded.seller, item_url=excluded.item_url",
            rows)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def _window_start(days: int) -> str:
    return (date.today() - timedelta(days=max(1, int(days)))).isoformat()


def _per_item_series(keyword: str, days: int) -> Dict[str, List[Dict[str, Any]]]:
    """{item_id: [{day, price, want, title}, ...] 按日期升序}"""
    conn = _conn()
    try:
        series: Dict[str, List[Dict[str, Any]]] = {}
        cur = conn.execute(
            "SELECT item_id, day, price, want_count, title FROM price_snapshots "
            "WHERE keyword=? AND day>=? ORDER BY item_id, day ASC",
            (keyword, _window_start(days)))
        for row in cur.fetchall():
            want = row["want_count"]
            series.setdefault(row["item_id"], []).append(
                {"day": row["day"], "price": float(row["price"]),
                 "want": None if want is None else int(want),
                 "title": row["title"] or ""})
        return series
    finally:
        conn.close()


def _trend(points: List[Dict[str, Any]]) -> str:
    if len(points) < 2:
        return "无历史"
    prev, last = points[-2]["price"], points[-1]["price"]
    if last < prev * 0.97:
        return "降价"
    if last > prev * 1.03:
        return "涨价"
    return "平稳"


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    vals = sorted(values)
    if len(vals) == 1:
        return round(vals[0], 2)
    k = (len(vals) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    frac = k - lo
    return round(vals[lo] + (vals[hi] - vals[lo]) * frac, 2)


def annotate_items(keyword: str, items: List[Dict[str, Any]],
                   days: int = WINDOW_DAYS) -> List[Dict[str, Any]]:
    """给榜单每项附加历史字段（无历史的保持空值，不臆造）。"""
    series = _per_item_series(keyword, days)
    for it in items:
        item_id = str(it.get("itemId") or "")
        points = series.get(item_id) or []
        if not points:
            it.update({"hist_low": None, "hist_avg": None, "hist_samples": 0,
                       "price_vs_low_pct": None, "hist_trend": "无历史",
                       "want_per_day": None, "want_delta": None, "want_samples": 0})
            continue
        prices = [p["price"] for p in points]
        low = min(prices)
        avg = round(sum(prices) / len(prices), 2)
        it["hist_low"] = round(low, 2)
        it["hist_avg"] = avg
        it["hist_samples"] = len(points)
        cur = it.get("price")
        if cur is not None and low > 0:
            it["price_vs_low_pct"] = round((float(cur) - low) / low * 100, 1)
        else:
            it["price_vs_low_pct"] = None
        it["hist_trend"] = _trend(points)

        # 热度成长值（借鉴芹菜工具箱"爆品潜力值"思路，但用真实观测差值而非推算）：
        # 只用"详情来源"的想要数（want 为 None 的快照不参与，A3）。
        # 想要数在窗口内的日均增量；负数视为 0（想要数会因下架/改价波动）
        want_pts = [p for p in points if p.get("want") is not None]
        if len(want_pts) >= 2:
            try:
                from datetime import date as _date
                first_day = _date.fromisoformat(want_pts[0]["day"])
                last_day = _date.fromisoformat(want_pts[-1]["day"])
                span = max(1, (last_day - first_day).days)
                delta = int(want_pts[-1]["want"]) - int(want_pts[0]["want"])
                it["want_delta"] = delta
                it["want_per_day"] = round(max(0, delta) / span, 1)
            except Exception:
                it["want_delta"] = None
                it["want_per_day"] = None
        else:
            it["want_delta"] = None
            it["want_per_day"] = None
        it["want_samples"] = len(want_pts)
    return items


def keyword_summary(keyword: str, days: int = WINDOW_DAYS) -> Dict[str, Any]:
    """市场水位 + 发布定价建议（当前样本每个商品取窗口内最新价）。"""
    keyword = str(keyword or "").strip()
    if not keyword:
        return {"ok": False, "error": "关键词不能为空"}
    series = _per_item_series(keyword, days)
    latest: List[Dict[str, Any]] = []
    for item_id, points in series.items():
        if not points:
            continue
        latest.append({"itemId": item_id, "title": points[-1]["title"],
                       "price": points[-1]["price"], "day": points[-1]["day"],
                       "history_days": len(points)})
    prices = [x["price"] for x in latest]
    market: Dict[str, Any] = {"sample_items": len(prices), "window_days": days}
    suggestion: Dict[str, Any] = {}
    if prices:
        median = round(statistics.median(prices), 2)
        p25 = _percentile(prices, 0.25)
        market.update({
            "min_price": round(min(prices), 2),
            "max_price": round(max(prices), 2),
            "median_price": median,
            "p25_price": p25,
            "avg_price": round(sum(prices) / len(prices), 2),
        })
        suggested = round(median * 0.9, 2)
        floor = round((p25 or min(prices)) * 0.95, 2)
        suggestion = {
            "suggested_price": max(suggested, floor),
            "logic": "中位价×0.9，且不低于P25×0.95（留议价空间但守住水位）",
            "median": median, "p25": p25, "min": round(min(prices), 2),
        }
    latest.sort(key=lambda x: x["price"])
    return {"ok": True, "keyword": keyword, "market": market,
            "suggestion": suggestion, "items": latest[:30]}


__all__ = ["record_snapshots", "annotate_items", "keyword_summary",
           "WINDOW_DAYS", "DB_PATH"]
