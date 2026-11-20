from datetime import datetime, timedelta
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import opportunity_score_v2 as score
import opportunity_price


def test_record_snapshots_prunes_history_older_than_30_days_before_insert(tmp_path, monkeypatch):
    monkeypatch.setattr(opportunity_price, "DB_PATH", tmp_path / "price_history.db")
    now = datetime.now()
    expired = now - timedelta(days=30, minutes=1)
    recent = now - timedelta(days=30) + timedelta(minutes=1)

    with opportunity_price._conn() as connection:
        connection.executemany(
            "INSERT INTO price_snapshots (day, ts, keyword, item_id, price) VALUES (?, ?, ?, ?, ?)",
            [(timestamp.date().isoformat(), timestamp.isoformat(timespec="seconds"), "关键词", item_id, 10)
             for timestamp, item_id in [(expired, "expired"), (recent, "recent")]],
        )
        connection.execute(
            "CREATE TRIGGER require_pruned_history BEFORE INSERT ON price_snapshots "
            "WHEN EXISTS (SELECT 1 FROM price_snapshots WHERE item_id = 'expired') "
            "BEGIN SELECT RAISE(ABORT, 'expired history was not pruned'); END"
        )
    connection.close()

    assert opportunity_price.record_snapshots("关键词", [{"itemId": "new", "price": 10}]) == 1
    with opportunity_price._conn() as connection:
        item_ids = {row["item_id"] for row in connection.execute("SELECT item_id FROM price_snapshots")}
    connection.close()
    assert item_ids == {"recent", "new"}


def test_stale_want_is_unknown_even_when_a_number_remains():
    item = {
        "title": "过期想要数",
        "want_count": 99999,
        "want_stale": True,
        "view_count": 100000,
        "sold_count": 50,
    }

    rates = score.funnel_rates(item)

    assert rates["want_known"] is False
    assert rates["want_rate"] is None
    assert rates["deal_rate"] is None


def test_legacy_list_source_cannot_enter_rank_sample():
    item = {
        "title": "旧来源",
        "want_count": 88888,
        "want_source": "list",
        "want_stale": False,
        "view_count": 100000,
        "sold_count": 50,
    }

    rates = score.funnel_rates(item)

    assert rates["want_known"] is False
    assert rates["want_rate"] is None


def test_stale_high_number_does_not_outrank_current_search_metric():
    current_rows = [{
        "title": f"当前搜索{i}",
        "want_count": 100 * i,
        "want_source": "search",
        "want_stale": False,
        "view_count": 1000 * i,
        "sold_count": 10 * i,
        "price": 39,
    } for i in range(1, 11)]
    stale = {
        "title": "过期快照",
        "want_count": 999999,
        "want_source": "search",
        "want_stale": True,
        "view_count": 10000,
        "sold_count": 100,
        "price": 39,
    }

    result = score.score_batch([*current_rows, stale], "测试")
    items = {row["title"]: row for row in result["items"]}

    assert items["当前搜索10"]["opportunity_score"] > items["过期快照"]["opportunity_score"]
    assert items["过期快照"]["dimensional"]["want_abs"] == 0.5
    assert items["过期快照"]["dimensional"]["want_rate"] == 0.5
    assert abs(result["meta"]["want_coverage"] - (10 / 11)) < 0.001
    assert any("过期" in reason for reason in items["过期快照"]["reasons"])
