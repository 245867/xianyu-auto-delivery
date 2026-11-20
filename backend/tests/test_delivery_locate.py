"""
异常订单会话定位回归测试（P0-2）
================================
锁死：

1. 有 chat_id → 步骤里给会话编号；缺 chat_id 回落 buyer_id / 订单号；
2. 整单定位优先取"有会话编号"的那张卡；
3. 整单进度（已发/待处理）来自账本，不手算；
4. 不编造直达链接：deep_link 为空、entry_verified=False；
5. 找不到订单返回 None，不抛异常。

跑法（在 backend 目录）：python3 tests/test_delivery_locate.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="delivery_locate_")
os.environ["DELIVERY_RELIABILITY_DB"] = str(Path(_TMP) / "delivery.db")
os.environ["DELIVERY_RELIABILITY_CONFIG"] = str(Path(_TMP) / "delivery.json")

import delivery_reliability as svc  # noqa: E402

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


section("1 有会话编号")
svc.ensure_order("o1", "shopA", ["卡1", "卡2"], item_id="item1",
                 chat_id="chat-xyz", buyer_id="buyer-1")
svc.mark_sent("o1", "shopA", 0, "已发")
ctx = svc.locate_context("o1", "shopA", 0)
check("返回上下文", ctx is not None)
check("带 chat_id", ctx["chat_id"] == "chat-xyz")
check("has_chat_id=True", ctx["has_chat_id"] is True)
check("missing 为空", ctx["missing"] == [])
check("步骤提到会话编号", any("chat-xyz" in s for s in ctx["manual_steps"]))
check("整单总数=2", ctx["order_total"] == 2)
check("已发=1", ctx["order_sent"] == 1)
check("待处理=0（另一张 pending 不算）", ctx["order_unresolved"] == 0)
check("不编造直达链接", ctx["deep_link"] == "" and ctx["entry_verified"] is False)

section("2 缺会话编号，回落买家 ID")
svc.ensure_order("o2", "shopB", ["卡"], buyer_id="buyer-2")
ctx = svc.locate_context("o2", "shopB", 0)
check("has_chat_id=False", ctx["has_chat_id"] is False)
check("missing 含 chat_id", "chat_id" in ctx["missing"])
check("步骤提到买家 ID", any("buyer-2" in s for s in ctx["manual_steps"]))

section("3 两者都缺，按订单号人工找")
svc.record_missing("o3", "shopC", 0, 1, reason="内容取不到")
ctx = svc.locate_context("o3", "shopC", 0)
check("missing 含 chat_id 和 buyer_id",
      "chat_id" in ctx["missing"] and "buyer_id" in ctx["missing"])
check("步骤提到订单号", any("o3" in s for s in ctx["manual_steps"]))
check("步骤提示联系买家", any("联系买家" in s for s in ctx["manual_steps"]))
check("失败卡记为待处理", ctx["order_unresolved"] == 1)

section("4 整单定位优先取有会话编号的卡")
svc.ensure_order("o4", "shopD", ["a", "b", "c"])
# 直接给第 3 张补 chat_id（模拟后续回填）
import sqlite3  # noqa: E402
conn = sqlite3.connect(svc.DB_PATH)
conn.execute("UPDATE delivery_cards SET chat_id='chat-late' WHERE order_id='o4' AND idx=2")
conn.commit(); conn.close()
ctx = svc.locate_context("o4", "shopD")  # idx 缺省 = 整单
check("整单定位选中有编号的卡", ctx["chat_id"] == "chat-late")
check("idx 指向第 3 张", ctx["idx"] == 2)
check("整单总数=3", ctx["order_total"] == 3)

section("5 不存在的订单")
check("返回 None", svc.locate_context("nope", "shopX", 0) is None)
check("整单查不存在也返回 None", svc.locate_context("nope") is None)

print("\n" + "=" * 56)
if FAILS:
    print(f"❌ {len(FAILS)} 条失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print(f"✅ 全部通过（{PASSES} 项）")
