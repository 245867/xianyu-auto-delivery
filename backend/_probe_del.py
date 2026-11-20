import os, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["DB_PATH"] = r"G:\have time fish\01-源码\xianyu-auto-delivery\data\xianyu_data.db"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_manager import db_manager
print("deleted", db_manager.delete_item_info("2219399967630", "1100839041022"))
print([tuple(r) for r in db_manager.conn.execute("SELECT item_id,item_title,auto_delivery_enabled,delivery_card_id FROM item_info")])
