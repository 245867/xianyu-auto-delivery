import sqlite3, json, sys
from sqlite_runtime import connect_sqlite
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
for label, path in [("root", r"G:\have time fish\01-源码\xianyu-auto-delivery\data\xianyu_data.db"), ("backend", r"G:\have time fish\01-源码\xianyu-auto-delivery\backend\data\xianyu_data.db")]:
    conn = connect_sqlite(path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    cols = [r[1] for r in cur.execute("PRAGMA table_info(item_info)").fetchall()] if "item_info" in tables else []
    items = []
    if "item_info" in tables:
        items = [dict(r) for r in cur.execute("SELECT item_id, item_title, item_price, auto_delivery_enabled, delivery_card_id FROM item_info").fetchall()]
    orders = []
    if "orders" in tables:
        orders = [dict(r) for r in cur.execute("SELECT order_id, item_id, order_status, amount, created_at FROM orders ORDER BY created_at DESC LIMIT 5").fetchall()]
    cards = []
    if "delivery_cards" in tables:
        cards = [dict(r) for r in cur.execute("SELECT id, name FROM delivery_cards LIMIT 10").fetchall()]
    print(label, "tables:", len(tables))
    print(label, "item_cols:", cols)
    print(label, "items:", json.dumps(items, ensure_ascii=False)[:800])
    print(label, "orders:", json.dumps(orders, ensure_ascii=False)[:800])
    print(label, "cards:", json.dumps(cards, ensure_ascii=False)[:400])
    conn.close()
