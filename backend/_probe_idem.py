import sqlite3, json, sys
from sqlite_runtime import connect_sqlite
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
for p in (r"G:\have time fish\01-源码\xianyu-auto-delivery\data\idempotency.db",):
    con = connect_sqlite(p); con.row_factory = sqlite3.Row
    tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    print("db", p, tabs)
    for t in tabs:
        for r in con.execute(f"SELECT * FROM {t} ORDER BY rowid DESC LIMIT 20"):
            d = dict(r)
            s = json.dumps(d, ensure_ascii=False)
            print(t, s[:400])
