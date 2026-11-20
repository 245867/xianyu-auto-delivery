import os, sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["DB_PATH"] = r"G:\have time fish\01-源码\xianyu-auto-delivery\data\xianyu_data.db"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_manager import db_manager
cid = "2219399967630"; iid = "1100023370210"
ok = db_manager.save_item_info(cid, iid, {
    "title": "职场汇报模板精选版｜3套可编辑Word文件",
    "description": "职场汇报模板精选版，含通用周报、个人月报、一页纸进展汇报共3套，Word格式可自行编辑。拍下后自动发送下载链接。",
    "category": "50025446",
    "price": 5.9,
})
ok2 = db_manager.update_item_delivery_settings(cid, iid, auto_delivery_enabled=True, delivery_card_id=7)
print("save", ok, "delivery", ok2)
for r in db_manager.conn.execute("SELECT item_id,item_title,auto_delivery_enabled,delivery_card_id FROM item_info"):
    print(r)
