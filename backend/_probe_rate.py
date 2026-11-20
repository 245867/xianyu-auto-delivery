import json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
data = json.load(open(r"G:\have time fish\01-源码\xianyu-auto-delivery\data\_feed_dump.json", encoding="utf-8"))
for kw in ["星巴克", "麦当劳", "美团", "瑞幸", "肯德基", "滴滴"]:
    rows = [q for q in data.get("quotes") or [] if kw in q.get("title", "")]
    rows.sort(key=lambda q: q["price"])
    print("==", kw, len(rows))
    for q in rows[:5]:
        print(q["price"], q.get("supplier"), q.get("title")[:90])
