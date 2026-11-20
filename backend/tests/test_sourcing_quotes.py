from datetime import datetime, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sourcing_quotes as sourcing


def test_partial_refresh_keeps_new_price_and_only_restores_the_failed_keyword(monkeypatch):
    keywords = ["查价甲", "查价乙"]
    source = "kajiage"
    previous_stamp = "2026-11-17T09:00:00+00:00"
    new_stamp = "2026-11-18T09:00:00+00:00"
    state = {"stamp": previous_stamp, "updated": False}
    monkeypatch.setattr(sourcing, "PROVIDER_ORDER", (source,))
    monkeypatch.setattr(sourcing, "CATEGORY_KEYWORD_ANCHORS", ())
    monkeypatch.setattr(sourcing, "_load_hot_catalog", lambda: (keywords, {}))
    monkeypatch.setattr(
        sourcing, "_utc_now", lambda: datetime.fromisoformat(state["stamp"]).astimezone(timezone.utc)
    )
    monkeypatch.setattr(sourcing, "_snapshot", None)
    monkeypatch.setattr(sourcing, "_expires_at", 0.0)
    monkeypatch.setattr(sourcing, "_last_success", {})

    def fetch(source, keyword):
        if keyword == "查价乙" and state["updated"]:
            raise ValueError("模拟第二个关键词失败")
        prices = {"共同报价": 10, "首轮报价": 8} if keyword == "查价甲" else {
            "共同报价": 10, "第二关键词报价": 12
        }
        if state["updated"]:
            prices = {"共同报价": 15}
        quotes = [
            {
                "source": source,
                "supplier": "测试供货商",
                "title": title,
                "url": f"https://example.com/{title}",
                "price": price,
                "updated_at": state["stamp"],
                "link_type": "product",
            }
            for title, price in prices.items()
        ]
        return quotes, state["stamp"]

    monkeypatch.setattr(sourcing, "_fetch_for_source", fetch)
    first = sourcing.current_quotes()
    assert first["providers"][0]["status"] == "ok"
    state.update(stamp=new_stamp, updated=True)
    assert sourcing.current_quotes() is first
    refreshed = sourcing.current_quotes(force_refresh=True)
    rows = {row["title"]: row for row in refreshed["quotes"]}
    assert refreshed["providers"][0]["status"] == "partial"
    assert rows["共同报价"]["price"] == 15
    assert rows["共同报价"]["fetched_at"] == new_stamp
    assert rows["共同报价"]["stale"] is False
    assert rows["第二关键词报价"]["price"] == 12
    assert rows["第二关键词报价"]["fetched_at"] == previous_stamp
    assert rows["第二关键词报价"]["stale"] is True
    assert "首轮报价" not in rows


def test_kajiage_uses_a_product_link_only_when_the_offer_supplies_one(monkeypatch):
    monkeypatch.setattr(
        sourcing,
        "_http_get_text",
        lambda _url: (
            '{"offers":[{"productName":"Test Card","siteName":"Supplier","price":10,'
            '"productUrl":"https://example.com/product/1"},'
            '{"productName":"Test Card","siteName":"Another","price":11}]}'
        ),
    )
    quotes, _stamp = sourcing._fetch_kajiage("Test Card")
    by_supplier = {row["supplier"]: row for row in quotes}
    assert by_supplier["Supplier"]["link_type"] == "product"
    assert by_supplier["Supplier"]["url"] == "https://example.com/product/1"
    assert by_supplier["Another"]["link_type"] == "source"
    assert by_supplier["Another"]["url"] == f"{sourcing.KAJIAGE_ORIGIN}/"


def test_a_provider_root_url_is_a_source_page_not_a_product_link():
    source_url = "https://kajiage.com/"
    root_quote = sourcing._make_quote(
        title="Test Card",
        price=10,
        supplier="Supplier",
        url="https://example.com/",
        source="kajiage",
        source_url=source_url,
        updated_at="2026-11-18T10:00:00+08:00",
        link_type="product",
    )
    query_quote = sourcing._make_quote(
        title="Test Card",
        price=10,
        supplier="Supplier",
        url="https://example.com/?goods=1",
        source="kajiage",
        source_url=source_url,
        updated_at="2026-11-18T10:00:00+08:00",
        link_type="product",
    )
    assert root_quote["link_type"] == "source"
    assert query_quote["link_type"] == "product"
