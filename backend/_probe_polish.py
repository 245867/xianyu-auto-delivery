import os, sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests, polish_service as ps, opportunity_publisher as op
rows = requests.get("http://127.0.0.1:8080/cookies/details", headers={"Authorization": "Bearer 4oVHGzXjY3XLBYIPBhdknYkJoLvriO8chRGMEvtR_Xc"}, timeout=10).json()
row = rows[0] if isinstance(rows, list) else rows
print(json.dumps(ps.polish_item(row["value"], "1100023370210", op.WEB_APP_KEY), ensure_ascii=False)[:300])
