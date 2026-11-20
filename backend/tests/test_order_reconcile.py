"""
拆解 11：订单全量对账用例（UniversalECommerceBot 的 fulfillment_runtime）。

守住的五件事：
[1] 三个桶分得清 —— 漏单（待发货且本地没有）/ 历史缺档 / 状态滞后 / 一致
[2] 去重靠账本   —— 同一单在两页里出现只算一次
[3] 漏单要报警   —— missing_paid 必须落成一条通知，不能只在日志里
[4] 对账不能反噬 —— 拉取失败返回 ok=False，绝不抛异常打断心跳
[5] 接线正确     —— 挂在心跳里、跑在线程里（不阻塞事件循环）、只扫第 1 页

跑法：python tests/test_order_reconcile.py
"""

import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import order_reconcile as orc

BACKEND = Path(__file__).resolve().parent.parent
FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def section(t):
    print(f"\n[{t}]")


def row(oid, status, **kw):
    d = {"order_id": oid, "order_status": status, "item_id": "I1", "buyer_id": "B1",
         "amount": "9.90", "quantity": "1", "order_time": "2026-11-09 10:00:00"}
    d.update(kw)
    return d


# ---------------------------------------------------------------- [1] 分桶
section("1 三个桶：漏单 / 历史缺档 / 状态滞后 / 一致")
platform = [
    row("A1", "pending_ship"),      # 平台等发货，本地没有 → 漏单
    row("A2", "pending"),           # 同上（已付款未发货）
    row("A3", "completed"),         # 平台已完成，本地没有 → 历史缺档
    row("A4", "shipped"),           # 两边都有，状态一致
    row("A5", "cancelled"),         # 本地有，但本地还停在 processing → 滞后
    row("A1", "pending_ship"),      # 翻页重叠，重复出现
]
local = [
    {"order_id": "A4", "status": "shipped", "system_shipped": 1, "cookie_id": "CK1"},
    {"order_id": "A5", "status": "processing", "system_shipped": 0, "cookie_id": "CK1"},
]
d = orc.diff(platform, local)
check([r["order_id"] for r in d["missing_paid"]] == ["A1", "A2"],
      "待发货且本地没有 = 漏单（A1/A2）")
check([r["order_id"] for r in d["missing_other"]] == ["A3"],
      "已完成且本地没有 = 历史缺档（不报警）")
check([x["order_id"] for x in d["status_drift"]] == ["A5"], "状态滞后单独一桶（A5）")
check(d["ok"] == 1, "一致的单算进 ok（A4）")
check(d["platform_count"] == 5, "重复的 A1 只算一次")
check(d["missing_paid"][0]["item_id"] == "I1", "漏单带着商品号，方便追")

# ---------------------------------------------------------------- [2] 对账动作
section("2 对账动作：登记 + 回写 + 报警")
calls = {"inserted": [], "notified": []}


class FakeDB:
    def __init__(self):
        self.lock = threading.RLock()
        self.orders = {r["order_id"]: dict(r) for r in local}

    def get_order_by_id(self, order_id):
        return self.orders.get(order_id)

    def insert_or_update_order(self, **kw):
        calls["inserted"].append(kw)
        oid = kw["order_id"]
        cur = self.orders.setdefault(oid, {"order_id": oid, "cookie_id": kw["cookie_id"]})
        if kw.get("order_status"):
            cur["status"] = kw["order_status"]
        return True


fake_db = types.ModuleType("db_manager")
fake_db.db_manager = FakeDB()
sys.modules["db_manager"] = fake_db

fake_seller = types.ModuleType("seller_api")
def page_report(items, failed_pages=None, has_more=False, pages_read=1):
    return {"items": items, "failed_pages": failed_pages or [],
            "has_more": has_more, "pages_read": pages_read}


fake_seller.fetch_sold_order_pages = lambda *a, **kw: page_report([{"raw": 1}])
fake_seller.parse_sold_order = lambda x: platform[0]
sys.modules["seller_api"] = fake_seller

fake_notify = types.ModuleType("notify_service")
fake_notify.send_event = lambda event, title, content, level="info", force=False: (
    calls["notified"].append((event, title)) or {"ok": True})
sys.modules["notify_service"] = fake_notify

res = orc.reconcile("cookie-str", "CK1", pages=1, rows_per_page=20)
check(res["ok"] is True, "对账跑通")
check(res["missing_paid"] == ["A1"], "识别出 1 单漏单")
check(res["registered"] == 1, "漏单被登记进本地库")
check(calls["inserted"][0]["cookie_id"] == "CK1", "登记时带上账号（否则会被 db_manager 拒绝）")
check(calls["notified"] and calls["notified"][0][0] == "risk", "漏单落成了一条 risk 通知")
check("A1" in calls["notified"][0][1] or "漏单" in calls["notified"][0][1], "通知标题点明了漏单")

calls["notified"].clear()
fake_seller.parse_sold_order = lambda x: row("A9", "completed")
res2 = orc.reconcile("cookie-str", "CK1")
check(res2["missing_paid"] == [] and res2["missing_other"] == ["A9"], "只有历史缺档")
check(calls["notified"] == [], "历史缺档不报警（不制造噪音）")

# 状态滞后要回写
def two_rows(*a, **kw):
    return page_report([{"raw": 1}, {"raw": 2}])


fake_seller.fetch_sold_order_pages = two_rows
fake_seller.parse_sold_order = lambda x: (row("A5", "shipped") if x["raw"] == 1 else row("A4", "shipped"))
calls["inserted"].clear()
res3 = orc.reconcile("cookie-str", "CK1")
check(res3["status_drift"] == [{"order_id": "A5", "local": "processing", "platform": "shipped"}],
      "漂移明细对得上")
check(any(k["order_id"] == "A5" and k["order_status"] == "shipped" for k in calls["inserted"]),
      "漂移以平台为准回写到本地")
check(res3["refreshed"] == 1, "只统计确认写入的状态更新")

section("3 部分成功、重复同步与账号隔离")
fake_seller.parse_sold_order = lambda x: x
fake_db.db_manager.orders["OTHER"] = {"order_id": "OTHER", "status": "shipped", "cookie_id": "CK2"}
fake_seller.fetch_sold_order_pages = lambda *a, **kw: page_report(
    [row("NEW", "pending_ship"), row("OTHER", "completed")], failed_pages=[2], has_more=True
)
calls["notified"].clear()
partial = orc.reconcile("cookie-str", "CK1", pages=2, notify_missing=False)
check(partial["registered"] == 1 and partial["failed"] == 1, "同步登记成功页并报告归属冲突")
check(partial["failed_pages"] == [2] and partial["has_more"], "记录失败页和分页状态")
check(fake_db.db_manager.orders["OTHER"]["cookie_id"] == "CK2", "其他账号订单归属保留")
check(calls["notified"] == [], "手动同步只显示结果")
retry = orc.reconcile("cookie-str", "CK1", pages=2, notify_missing=False)
check(retry["registered"] == 0, "重复同步不会重复登记")
with_history = fake_db.db_manager.insert_or_update_order
fake_db.db_manager.insert_or_update_order = lambda **kw: False
fake_seller.fetch_sold_order_pages = lambda *a, **kw: page_report([row("FAIL", "completed")])
failed = orc.reconcile("cookie-str", "CK1")
check(failed["registered"] == 0 and failed["failed"] == 1, "数据库返回失败可见")
fake_db.db_manager.insert_or_update_order = with_history
fake_seller.fetch_sold_order_pages = lambda *a, **kw: page_report([])
empty = orc.reconcile("cookie-str", "CK1")
check(empty["ok"] and empty["platform_count"] == 0, "已成功拉取空订单页")

# ---------------------------------------------------------------- [4] 不能反噬
section("4 拉取失败不抛异常")
def boom(*a, **kw):
    raise RuntimeError("限流 RGV587")


fake_seller.fetch_sold_order_pages = boom
r = orc.reconcile("cookie-str", "CK1")
check(r["ok"] is False and "RGV587" in r["error"], "返回 ok=False + 原因，不抛异常")

# ---------------------------------------------------------------- [5] 接线
section("5 接线：心跳里、线程里、只扫第 1 页")
src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")
check("import order_reconcile as _orc" in src, "心跳里引用了对账模块")
check("await asyncio.to_thread(_orc.reconcile, self.cookies_str, self.cookie_id, 1, 20)" in src,
      "用 to_thread 跑（sync requests 不能堵事件循环）、只扫 1 页 20 单")
hb = src.split("async def heartbeat_loop")[1][:5000]
check("_orc.reconcile" in hb, "确实挂在 heartbeat_loop 里，不是死代码")
check("订单对账异常（不影响心跳）" in hb, "失败只 warn，不断心跳")

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
