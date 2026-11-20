"""
发货后回看 / 新商品前 N 单提示（拆解 39 §八-2、§八-6）
=====================================================
两个都只是**只读视图**，所以测试的重点不是"会不会发错"，而是：

1. 窗口按 updated_at 过滤，窗口外、未发货的单一律不进；
2. 四类异常信号各自能触发，且计数与明细一致（不虚报、不漏报）；
3. 没有异常的单不会硬凑一条；
4. 新商品提示只按订单数算，阈值是"≤ N"，不拦截、不改状态；
5. 只读：不改入参、不做任何写操作。

跑法（在 backend 目录）：python3 tests/test_delivery_insights.py
"""

from __future__ import annotations

import copy
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import delivery_insights as di  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
FAILS: list[str] = []
PASSES = 0

NOW = datetime(2026, 11, 13, 12, 0, 0)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ok   {name}" + (f"（{detail}）" if detail else ""))
    else:
        FAILS.append(name + (f"（{detail}）" if detail else ""))
        print(f"  FAIL {name}" + (f"（{detail}）" if detail else ""))


def section(title: str) -> None:
    print(f"\n[{title}]")


def order(oid: str, status: str, updated: str, item: str = "I1",
          cookie: str = "acc1") -> dict:
    return {"order_id": oid, "status": status, "updated_at": updated,
            "created_at": "2026-11-01 00:00:00", "item_id": item,
            "cookie_id": cookie}


# ---------------------------------------------------------------- 1 窗口
section("1 窗口与状态过滤")

orders = [
    order("A1", "shipped", "2026-11-13 10:00:00"),          # 窗口内、已发货
    order("A2", "shipped", "2026-11-10 10:00:00"),          # 窗口外
    order("A3", "pending", "2026-11-13 11:00:00"),          # 未发货
    order("A4", "refunding", "2026-11-13 11:30:00"),        # 窗口内、退款中
]
cards = {
    "A1": {"unresolved": 0, "total": 2, "sent": 2, "spec": "红色 / 均码", "item_id": "I1"},
    "A4": {"unresolved": 0, "total": 1, "sent": 1, "spec": "", "item_id": "I1"},
}

r = di.after_sale_review(now=NOW, orders=orders, cards=cards, platform={})
check("只扫窗口内已发货的单（A1/A4）", r["scanned"] == 2, str(r["scanned"]))
check("窗口外的不进", all(x["order_id"] != "A2" for x in r["flagged"]))
check("未发货的不进（哪怕窗口内）", all(x["order_id"] != "A3" for x in r["flagged"]))
check("退款中的单被标出来", any(x["order_id"] == "A4" for x in r["flagged"]))
check("dry_run 明确为真", r.get("dry_run") is True)
check("窗口小时数回显", r["window_hours"] == 24, str(r["window_hours"]))

r1 = di.after_sale_review(now=NOW, hours=1, orders=orders, cards=cards, platform={})
check("窗口收窄到 1h：10:00 的单被排除", r1["scanned"] == 1, str(r1["scanned"]))
check("窗口收窄后只剩退款中那一单", [x["order_id"] for x in r1["flagged"]] == ["A4"])

# ---------------------------------------------------------------- 2 信号
section("2 四类异常信号")

cards2 = {
    "B1": {"unresolved": 2, "total": 3, "sent": 1, "spec": "蓝色", "item_id": "I2"},
    "B3": {"unresolved": 0, "total": 1, "sent": 1, "spec": "", "item_id": "I2"},
}
orders2 = [
    order("B1", "shipped", "2026-11-13 09:00:00", "I2"),     # 账本还有卡没发
    order("B2", "shipped", "2026-11-13 09:10:00", "I2"),     # 账本完全没记录
    order("B3", "shipped", "2026-11-13 09:20:00", "I2"),     # 正常
    order("B4", "refund_cancelled", "2026-11-13 09:30:00", "I2"),  # 售后 + 无账本
]
platform = {"B3": "平台确认发货卡住（已试 3 次）：超时"}

r2 = di.after_sale_review(now=NOW, orders=orders2, cards=cards2, platform=platform)
by_id = {x["order_id"]: x for x in r2["flagged"]}
codes = {oid: {f["code"] for f in row["flags"]} for oid, row in by_id.items()}

check("账本有未发卡 → delivery_incomplete", codes.get("B1") == {"delivery_incomplete"}, str(codes.get("B1")))
check("未发张数写进文案", "2 张卡" in by_id["B1"]["flags"][0]["text"], by_id["B1"]["flags"][0]["text"])
check("账本没记录 → no_ledger", codes.get("B2") == {"no_ledger"}, str(codes.get("B2")))
check("平台卡住 → platform_pending", codes.get("B3") == {"platform_pending"}, str(codes.get("B3")))
check("退款关闭 + 无账本 → 两个信号都标", codes.get("B4") == {"after_sale", "no_ledger"}, str(codes.get("B4")))
check("正常单不进明细", "B3" not in codes or codes["B3"] == {"platform_pending"})
check("没有异常的单不会硬凑", len(r2["flagged"]) == 4, str(len(r2["flagged"])))
check("计数与明细一致",
      r2["counts"]["delivery_incomplete"] == 1 and r2["counts"]["no_ledger"] == 2
      and r2["counts"]["after_sale"] == 1 and r2["counts"]["platform_pending"] == 1,
      str(r2["counts"]))
check("信号多的排前面", r2["flagged"][0]["order_id"] == "B4", r2["flagged"][0]["order_id"])
check("规格随账本带出（有的才有）", by_id["B1"]["spec"] == "蓝色", by_id["B1"]["spec"])
check("没记录规格时给空串，不猜", by_id["B2"]["spec"] == "")

# ---------------------------------------------------------------- 3 正常单
section("3 全部正常")

r3 = di.after_sale_review(
    now=NOW,
    orders=[order("C1", "completed", "2026-11-13 11:00:00", "I3")],
    cards={"C1": {"unresolved": 0, "total": 1, "sent": 1, "spec": "M", "item_id": "I3"}},
    platform={})
check("扫到了", r3["scanned"] == 1)
check("没有异常就是空表", r3["flagged"] == [])
check("四个计数都是 0", set(r3["counts"].values()) == {0}, str(r3["counts"]))

# ---------------------------------------------------------------- 4 只读
section("4 只读")

orders_in = [order("D1", "refunding", "2026-11-13 08:00:00")]
cards_in = {"D1": {"unresolved": 1, "total": 2, "sent": 1, "spec": "", "item_id": "I1"}}
platform_in = {"D1": "已停止"}
before = copy.deepcopy((orders_in, cards_in, platform_in))
di.after_sale_review(now=NOW, orders=orders_in, cards=cards_in, platform=platform_in)
check("不改订单入参", orders_in == before[0])
check("不改账本入参", cards_in == before[1])
check("不改平台入参", platform_in == before[2])
check("局限写明了窗口是按 updated_at 近似", any("updated_at" in x for x in r["limitations"]),
      str(r["limitations"]))
check("局限写明了只读", any("只读" in x for x in r["limitations"]), str(r["limitations"]))

# ---------------------------------------------------------------- 5 前 N 单
section("5 新商品前 N 单提示")

def ord_n(oid, item, created="2026-11-10 10:00:00", cookie="acc1"):
    return {"order_id": oid, "item_id": item, "cookie_id": cookie,
            "created_at": created, "updated_at": created, "status": "shipped"}


new_orders = [ord_n(f"N{i}", "NEW", created=f"2026-11-1{i} 10:00:00") for i in range(3)]  # 正好 3 单
old_orders = [ord_n(f"O{i}", "OLD") for i in range(4)]          # 4 单 > 阈值
one_orders = [ord_n("S1", "SINGLE")]                            # 1 单
other_cookie = [ord_n("X1", "OTHER", cookie="acc2")]            # 别的账号

r5 = di.first_n_order_prompts(n=3, cookie_id="acc1",
                                orders=new_orders + old_orders + one_orders + other_cookie)
ids = [x["item_id"] for x in r5["items"]]
check("≤ 阈值的商品都在（NEW / SINGLE）", ids == ["SINGLE", "NEW"], str(ids))
check("超过阈值的商品不提示（OLD）", "OLD" not in ids)
check("指定账号时别的账号的单不算进来", "OTHER" not in ids)

r5_all = di.first_n_order_prompts(n=3, orders=other_cookie)
check("不指定账号时按全部账号汇总（不是漏数据）",
      [x["item_id"] for x in r5_all["items"]] == ["OTHER"], str(r5_all["items"]))
check("阈值回显", r5["threshold"] == 3, str(r5["threshold"]))
check("提示里写明不拦截", "不拦截" in r5["note"], r5["note"])
check("dry_run 明确为真", r5.get("dry_run") is True)

new_item = [x for x in r5["items"] if x["item_id"] == "NEW"][0]
check("订单数算对", new_item["order_count"] == 3, str(new_item["order_count"]))
check("订单号都列出来", sorted(new_item["order_ids"]) == ["N0", "N1", "N2"], str(new_item["order_ids"]))
check("提示语带阈值", "阈值 3" in new_item["hint"], new_item["hint"])
check("最近一单是时间最大的", new_item["latest_order_id"] == "N2", new_item["latest_order_id"])

r5b = di.first_n_order_prompts(n=1, orders=new_orders + one_orders)
check("阈值改成 1：只有 1 单的商品留下", [x["item_id"] for x in r5b["items"]] == ["SINGLE"],
      str([x["item_id"] for x in r5b["items"]]))

r5c = di.first_n_order_prompts(n=3, cookie_id="acc2", orders=new_orders + other_cookie)
check("按账号筛", [x["item_id"] for x in r5c["items"]] == ["OTHER"],
      str([x["item_id"] for x in r5c["items"]]))

r5d = di.first_n_order_prompts(n=3, orders=[{"order_id": "Z1", "created_at": "2026-11-11"}])
check("缺 item_id 的行直接忽略", r5d["items"] == [])

# ---------------------------------------------------------------- 6 接线
section("6 接线")

api = (BACKEND / "delivery_reliability_api.py").read_text(encoding="utf-8")
check("有 after-sale-review 路由", "/api/delivery/after-sale-review" in api)
check("有 new-item-checks 路由", "/api/delivery/new-item-checks" in api)
check("路由调的是只读函数", "delivery_insights.after_sale_review" in api
      and "delivery_insights.first_n_order_prompts" in api)

html = (BACKEND.parent / "static" / "delivery_reliability.html").read_text(encoding="utf-8")
check("发货页挂着 24h 回看卡片", "rvWrap" in html and "loadReview()" in html)
check("发货页挂着前 N 单卡片", "niWrap" in html and "loadNewItems()" in html)
check("页面对空规格显示「规格未提供」", "规格未提供" in html)

print("\n" + "=" * 56)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print(f"✅ 全部通过（{PASSES} 项）")