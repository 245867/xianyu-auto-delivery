"""
拆解 13：缺货整单停发（不发一半）
=================================

来源：嗷嗷鱼 FAQ Q23「买家一次买多件，会消耗几行卡密」——
"如果买家一次买 3 件而你库存只剩 2 行，系统不会发一半就停，会按缺货处理并记录失败原因。"

我们原来的形状：`quantity_to_send = 3`，只取到 1 张内容，**照样把这张发出去**，
剩下的 2 张记成 failed。买家拿到 1/3 的货，退款时那张卡是一次性消耗品、收不回来。

守住的四件事：
[1] 有内容的卡和有缺口的位置都被标成 failed（不是 pending，不能自动补）
[2] 有内容的卡 attempts 不拉满（补货后 force_retry 还能发出去）
[3] 没内容的卡 attempts 拉满（重试无意义，只等人工补发/退款）
[4] 接线：发货主流程里缺货时**跳过发送**，并把原因通知到人

跑法：cd backend && python3 tests/test_stock_shortage.py
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="shortage_"))
os.environ["DELIVERY_RELIABILITY_DB"] = str(_TMP / "dr.db")
os.environ["DELIVERY_RELIABILITY_CONFIG"] = str(_TMP / "dr.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import delivery_reliability as dr  # noqa: E402

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


def rows(order_id, cookie_id="CK"):
    return {r["idx"]: r for r in dr.cards(order_id, cookie_id)}


# ---------------------------------------------------------------- [1]
section("1 缺货：需要 3 张，只有 1 张")
ORDER = "ORD-SHORT"
dr.ensure_order(ORDER, "CK", ["卡A"], item_id="IT1")
dr.record_missing(ORDER, "CK", 1, 2, "发货内容获取失败", item_id="IT1")
n = dr.mark_shortage(ORDER, "CK", 3, 1, "库存不足：需要 3 张，只有 1 张")
r = rows(ORDER)
check(len(r) == 3, "三张卡都在账本里（不是凭空消失）")
check(all(r[i]["status"] == "failed" for i in (0, 1, 2)), "三张全是 failed（没有 pending 悄悄自动补）")
check("库存不足" in (r[0]["last_error"] or ""), "有内容那张也写了缺货原因")
check(n == 3, "返回受影响行数 3（三条都重写了一遍原因，幂等，重写同值无害）")

# ---------------------------------------------------------------- [2]
section("2 有内容的卡：attempts 不拉满，补货后还能发")
check(int(r[0]["attempts"] or 0) < int(dr.get_config()["max_attempts"]),
      "idx0 的 attempts 没到上限 → force_retry 可把它放回补发队列")
check(r[0]["payload"] == "卡A", "内容原样保留（补货后发出去的就是这张）")

# ---------------------------------------------------------------- [3]
section("3 没内容的卡：attempts 拉满")
check(int(r[1]["attempts"] or 0) >= int(dr.get_config()["max_attempts"]), "idx1 拉满")
check(int(r[2]["attempts"] or 0) >= int(dr.get_config()["max_attempts"]), "idx2 拉满")
check(r[1]["payload"] in ("", None), "idx1 没有内容（重试也变不出来）")

# ---------------------------------------------------------------- [2b]
section("2b force_retry 不会把空位放回去（否则发一条空消息）")
check(dr.force_retry(ORDER, "CK", 1) is False, "没内容的 idx1 拒绝放回")
check(rows(ORDER)[1]["status"] == "failed", "idx1 仍是 failed")
check(dr.force_retry(ORDER, "CK", 0) is True, "有内容的 idx0 允许人工放回（补货后发的还是这张）")
check(rows(ORDER)[0]["status"] == "manual_retry", "idx0 进入人工放行的即时补发状态")
check(rows(ORDER)[0]["payload"] == "卡A", "内容没变")

# ---------------------------------------------------------------- [3b]
section("3b 已发出的卡不被回写成 failed")
ORDER2 = "ORD-SENT"
dr.ensure_order(ORDER2, "CK", ["卡X", "卡Y", "卡Z"])
dr.claim_card(ORDER2, "CK", 0)
dr.mark_sent(ORDER2, "CK", 0)
dr.mark_shortage(ORDER2, "CK", 3, 1, "库存不足")
r2 = rows(ORDER2)
check(r2[0]["status"] == "sent", "idx0 仍是 sent（已给买家的不能否认）")
check(r2[1]["status"] == "failed", "idx1 被标 failed")

# ---------------------------------------------------------------- [4]
section("4 接线：主流程缺货时跳过发送")
src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")
check("_stock_short = \"\"" in src, "发货流程里有 _stock_short 初值")
check("dr.mark_shortage(order_id, self.cookie_id, quantity_to_send," in src, "缺货时调 mark_shortage")
check("and not _stock_short" in src, "发送分支带 _stock_short 闸门（缺货就不发）")
check("整单停发" in src, "日志/通知里写明整单停发")
# 闸门必须真的在发送判断那一行上
line = [ln for ln in src.splitlines() if "if delivery_contents and not _verify_block" in ln]
check(bool(line) and "and not _stock_short" in line[0], "闸门挂在发送条件上，不是别处")

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
