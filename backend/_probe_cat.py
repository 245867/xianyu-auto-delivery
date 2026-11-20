import os, sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opportunity_detail as od, opportunity_publisher as op
cookie = open(os.path.join(os.environ["TEMP"], "xy_cookie.txt"), "r", encoding="utf-8").read().strip()
items = [{"itemId": "1101246588106", "title": "夸克网盘年卡"}]
out = od.enrich_items(items, cookie, op.WEB_APP_KEY)
print("stats:", json.dumps(out, ensure_ascii=False))
it = items[0]
print(it.get("itemId"), "want", it.get("want_count"), "view", it.get("browse_count"), "sold", it.get("sold_count"), "source", it.get("want_source"), "stale", it.get("want_stale"))
