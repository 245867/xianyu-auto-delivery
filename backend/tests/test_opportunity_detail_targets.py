import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from opportunity_detail import select_detail_targets


class OpportunityDetailTargetTests(unittest.TestCase):
    def test_exact_product_title_is_selected_before_higher_ranked_day_card(self):
        items = [
            {
                "itemId": "daily",
                "title": "哔哩哔哩一天会员登录，或者优酷",
            },
            {
                "itemId": "month-bundle",
                "title": "【账号直充】哔哩哔哩会员月卡+优酷会员月卡",
            },
        ]

        selected = select_detail_targets(
            items,
            limit=1,
            focus_keyword="哔哩哔哩会员月卡+优酷会员月卡",
        )

        self.assertEqual([item["itemId"] for item in selected], ["month-bundle"])

    def test_equal_relevance_keeps_search_order(self):
        items = [
            {"itemId": "first", "title": "哔哩哔哩会员月卡"},
            {"itemId": "second", "title": "哔哩哔哩会员月卡"},
        ]

        selected = select_detail_targets(
            items,
            limit=2,
            focus_keyword="哔哩哔哩会员月卡",
        )

        self.assertEqual([item["itemId"] for item in selected], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
