import os, sys, json, re
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opportunity_publisher as op
cookie = open(os.path.join(os.environ["TEMP"], "xy_cookie.txt"), "r", encoding="utf-8").read().strip()
payload = {"pageNumber":1,"keyword":"夸克网盘年卡","fromFilter":False,"rowsPerPage":30,"sortValue":"","sortField":"","customDistance":"","gps":"","propValueStr":{},"customGps":"","searchReqFromPage":"pcSearch","extraFilterValue":"{}","userPositionJson":"{}"}
raw, _ = op._mtop_request(cookie, op.SEARCH_API, payload, op.SEARCH_VERSION, "", op.WEB_APP_KEY, timeout=20)
cards = ((raw.get("data") or {}).get("resultList") or [])
labels = {}
wants = []
for c in cards:
    main = (((c.get("data") or {}).get("item") or {}).get("main") or {})
    args = ((main.get("clickParam") or {}).get("args") or {})
    item_id = str(args.get("item_id") or "")
    want_num = args.get("wantNum")
    sp = args.get("serviceUtParams")
    entries = []
    if isinstance(sp, str) and sp.strip():
        try: entries = json.loads(sp)
        except Exception: entries = []
    contents = []
    for e in entries:
        if isinstance(e, dict):
            content = (e.get("args") or {}).get("content")
            if content:
                contents.append(str(content))
                labels[str(content)] = labels.get(str(content), 0) + 1
    wants.append((item_id, want_num, [c for c in contents if ("想要" in c or "售" in c or "付款" in c or "浏览" in c or "人" in c)]))
print("all label contents:")
for text, n in sorted(labels.items(), key=lambda x: -x[1]):
    print(n, text)
print("per item:")
for row in wants[:30]:
    print(row)
