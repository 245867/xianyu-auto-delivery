import json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
path = r"G:\have time fish\01-源码\xianyu-auto-delivery\data\market_listings.json"
data = json.load(open(path, encoding="utf-8"))
kw = data.get("keywords") or {}
for name, entry in list(kw.items())[:2]:
    items = entry.get("items") or []
    print("kw", name, "items", len(items))
    if items:
        it = items[0]
        print("item keys:", sorted(it.keys()))
        print("want_count:", it.get("want_count"), "price:", it.get("price"), "title:", it.get("title"))
        raw = json.dumps(it.get("raw_data") or {}, ensure_ascii=False)
        print("raw head:", raw[:1500])
