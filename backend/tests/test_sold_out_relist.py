import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import opportunity_detail
import rate_limiter
import sold_out_relist as relist


class SoldOutRelistTests(unittest.TestCase):
    def run_with_result(self, state, result):
        with (
            patch.object(relist, "get_config", return_value={
                "enabled": True, "night_ban": False, "dry_run": False,
                "max_per_round": 1, "cooldown_hours": 0,
            }),
            patch.object(relist, "get_state", return_value=state),
            patch.object(relist, "_save_state"),
            patch.object(relist, "_notify"),
            patch.object(opportunity_detail, "resolve_cookie", return_value=("shop", "test")),
            patch.object(rate_limiter, "acquire", return_value=(True, 0)),
            patch.object(rate_limiter, "daily_count", return_value=0),
            patch.object(rate_limiter, "jitter"),
            patch.object(relist, "check_item", return_value={"ok": True, "sold_out": True}) as check,
            patch.object(relist, "relaunch", return_value=result) as submit,
        ):
            return relist.run_round(), check, submit

    def test_relist_switches_watch_to_new_item(self):
        state = {"watchlist": {"shop::old": {
            "cookie_id": "shop", "item_id": "old", "active": True, "template_id": 7,
        }}}
        self.run_with_result(state, {"ok": True, "item_id": "new"})
        self.assertNotIn("shop::old", state["watchlist"])
        self.assertEqual(state["watchlist"]["shop::new"]["template_id"], 7)
        _, check, _ = self.run_with_result(state, {"ok": True, "item_id": "new"})
        self.assertEqual(check.call_args.args[1], "new")

    def test_unknown_submission_pauses_watch(self):
        state = {"watchlist": {"shop::old": {
            "cookie_id": "shop", "item_id": "old", "active": True,
        }}}
        self.run_with_result(state, {"ok": False, "kind": "unknown"})
        self.assertFalse(state["watchlist"]["shop::old"]["active"])
        _, _, submit = self.run_with_result(state, {"ok": True, "item_id": "new"})
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
