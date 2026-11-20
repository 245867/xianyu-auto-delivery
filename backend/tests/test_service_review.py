"""
服务型商品复盘回归测试（P2-4）
==============================
锁死：

1. 服务单识别：标题命中关键词 / 显式 item_id，非服务商品不进分母；
2. 咨询口径：只统计带服务商品标识的提问，未带标识的单列；
3. 答上率、复购率、完成/退款率的分母正确；
4. 首响、工时、退款原因码、服务单类型按缺失展示，不估算；
5. 账号过滤生效。

跑法（在 backend 目录）：python3 tests/test_service_review.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
os.environ["SERVICE_REVIEW_CONFIG"] = str(Path(tempfile.mkdtemp(prefix="svc_review_")) / "cfg.json")

import service_review as svc  # noqa: E402

FAILS: list[str] = []
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ok   {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def section(title: str) -> None:
    print(f"\n[{title}]")


def q(i, item, user, outcome, cid="shopA"):
    return {"id": i, "cookie_id": cid, "chat_id": "c" + user, "user_id": user,
            "item_id": item, "raw": "问题" + str(i), "outcome": outcome,
            "created_at": "2026-11-12 10:00:00"}


def o(oid, item, buyer, status, amount="19.9", cid="shopA"):
    return {"order_id": oid, "cookie_id": cid, "item_id": item, "buyer_id": buyer,
            "status": status, "amount": amount, "quantity": "1",
            "created_at": "2026-11-12 09:00:00", "updated_at": "2026-11-12 12:00:00"}


QUESTIONS = [
    q(1, "itemA", "u1", "hit"),
    q(2, "itemA", "u1", "none"),
    q(3, "itemA", "u2", "manual"),
    q(4, "itemB", "u3", "hit"),      # 非服务商品 → 未带服务标识
    q(5, "", "u4", "hit"),           # 没 item_id → 未带标识
]

ORDERS = [
    o("o1", "itemA", "b1", "completed"),
    o("o2", "itemA", "b1", "completed"),   # 复购
    o("o3", "itemA", "b2", "refunding", "29.9"),
    o("o4", "itemB", "b3", "completed"),   # 非服务
]

TITLES = {("shopA", "itemA"): "职场汇报代写服务",
          ("shopA", "itemB"): "职场汇报模板包"}

CFG = {"service_keywords": ["代写"], "service_item_ids": []}


section("1 服务单与订单指标")
r = svc.review(days=30, questions=QUESTIONS, orders=ORDERS, item_titles=TITLES, config=CFG)
s = r["services"]
check("只识别服务单 3 单", s["orders_total"] == 3)
check("完成 2 单", s["orders_completed"] == 2)
check("退款中 1 单", s["orders_refunding"] == 1)
check("完成率 2/3", s["completion_rate"] == round(2/3, 4))
check("退款率 1/3", s["refund_rate"] == round(1/3, 4))
check("买家数 2", s["buyers"] == 2)
check("复购买家 1", s["repeat_buyers"] == 1)
check("复购率 1/2", s["repeat_rate"] == 0.5)
check("GMV 合计 69.7", s["gmv_yuan"] == 69.7)
check("明细带标题", any(x.get("_title") == "职场汇报代写服务" for x in s["order_list"]))
check("模板包不在明细里", all(x["item_id"] != "itemB" for x in s["order_list"]))

section("2 咨询口径")
c = r["consult"]
check("咨询条数 3（只算 itemA）", c["questions_total"] == 3)
check("咨询人数 2", c["consultants"] == 2)
check("答上 2", c["answered"] == 2)
check("答上率 2/3", c["answer_rate"] == round(2/3, 4))
check("未答上 1", c["unanswered_count"] == 1)
check("未答上原话带出", len(c["unanswered"]) == 1 and c["unanswered"][0]["raw"] == "问题2")
check("未带商品标识 2", c["questions_unlinked"] == 2)
check("转化率 3/2", c["conversion"] == 1.5)

section("3 缺失字段不估算")
fields = {m["field"] for m in r["missing_fields"]}
check("首响耗时列入缺失", "首响耗时" in fields)
check("交付工时列入缺失", "交付工时" in fields)
check("退款原因码列入缺失", "退款原因码" in fields)
check("服务单类型列入缺失", "服务单类型" in fields)
check("每个缺失都有原因", all(m.get("reason") for m in r["missing_fields"]))
check("没有伪造首响数值", "first_response_hours" not in r["services"] and
      "first_response_hours" not in r["consult"])

section("4 显式 item_id 优先于关键词")
cfg2 = {"service_keywords": [], "service_item_ids": ["itemB"]}
r2 = svc.review(days=30, questions=QUESTIONS, orders=ORDERS, item_titles=TITLES, config=cfg2)
check("只认 itemB", r2["services"]["orders_total"] == 1)
check("itemA 不计入", all(x["item_id"] != "itemA" for x in r2["services"]["order_list"]))

section("5 账号过滤")
r3 = svc.review(days=30, cookie_id="shopA", questions=QUESTIONS, orders=ORDERS,
                item_titles=TITLES, config=CFG)
check("本账号服务单 3", r3["services"]["orders_total"] == 3)
r4 = svc.review(days=30, cookie_id="shopB", questions=QUESTIONS, orders=ORDERS,
                item_titles=TITLES, config=CFG)
check("别的账号 0", r4["services"]["orders_total"] == 0 and r4["consult"]["questions_total"] == 0)

section("6 空数据不崩")
r5 = svc.review(days=30, questions=[], orders=[], item_titles={}, config=CFG)
check("返回 ok", r5["ok"] is True)
check("比率都是 0", r5["services"]["completion_rate"] == 0 and r5["consult"]["answer_rate"] == 0)

print("\n" + "=" * 56)
if FAILS:
    print(f"❌ {len(FAILS)} 条失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print(f"✅ 全部通过（{PASSES} 项）")