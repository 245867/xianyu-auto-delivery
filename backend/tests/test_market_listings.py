import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import market_listings


def card_args(content):
    return {
        "wantNum": "0",
        "serviceUtParams": json.dumps([
            {"args": {"content": content}},
        ]),
    }


def test_search_want_label_is_parsed():
    assert market_listings._want_count_from_card(card_args("1087人想要")) == 1087
    assert market_listings._want_count_from_card(card_args("1.2万人想要")) == 12000


def test_missing_or_unrelated_labels_remain_unknown():
    assert market_listings._want_count_from_card(card_args("年卡")) is None
    assert market_listings._want_count_from_card({
        "wantNum": "0",
        "serviceUtParams": json.dumps([
            {"args": {"content": "累计降价"}},
            {"args": {"content": "刚刚发布"}},
        ]),
    }) is None


def test_positive_want_num_is_only_used_when_no_label_is_present():
    assert market_listings._want_count_from_card({"wantNum": "27"}) == 27


def test_parse_search_card_keeps_visible_want_label_as_search_evidence():
    card = {
        "data": {
            "item": {
                "main": {
                    "exContent": {
                        "title": "爱奇艺黄金VIP月卡 官方直充",
                        "itemId": "1234567890",
                    },
                    "clickParam": {
                        "args": {
                            "item_id": "1234567890",
                            "displayPrice": "13.90",
                            "serviceUtParams": json.dumps([
                                {"args": {"content": "1087人想要"}},
                            ]),
                        },
                    },
                },
            },
        },
    }

    parsed = market_listings.parse_search_card(card)

    assert parsed is not None
    assert parsed["want_count"] == 1087
    assert parsed["want_source"] == "search"
    assert parsed["want_stale"] is False
