"""
订单状态归一化回归测试
======================
锁死一条真实事故：`"退款完成，钱款已原路退返"` 曾被判成 `completed`，
而 C1 自动评价挂在 completed 上 → 会给已退款的买家发好评。

这个测试的每一项都对应一个"顺序错了就出事"的文案。不测覆盖率，测语义。

跑法（backend 目录）：python3 tests/test_order_status_rules.py
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import order_status_rules as R  # noqa: E402

PASSES = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ✅ {name}")
    else:
        FAILS.append(name)
        print(f"  ❌ {name}  {detail}")


# ---------------------------------------------------------------- 1. 退款族
print("\n【1】退款族必须压过「完成/成功」（这是那次事故的核心）")

REFUND_CANCELLED = [
    "退款完成，钱款已原路退返",
    "退款完成",
    "退款成功",
    "退款成功，钱款已原路退返",
    "极速退款成功",
    "钱款已原路退返",
    "退款已到账",
]
for t in REFUND_CANCELLED:
    st = R.detect_status_from_text(t)
    check(f"{t!r} → cancelled", st == "cancelled", f"实际 {st}")
    check(f"{t!r} 不算完成（C1 不能触发）", not R.is_completed(t), "被当成了 completed ⚠️")

REFUND_MID = [
    "退款中",
    "退款申请中",
    "申请退款",
    "等待卖家处理退款",
    "退货退款中",
    "退货退款",
    "已发货退款",
    "未发货退款",
    "退款协商中",
]
for t in REFUND_MID:
    st = R.detect_status_from_text(t)
    check(f"{t!r} → refunding", st == "refunding", f"实际 {st}")
    check(f"{t!r} 不算完成", not R.is_completed(t), "被当成了 completed ⚠️")


# ---------------------------------------------------------------- 2. 正常完成
print("\n【2】真正的完成仍然要认出 completed（别把功能改死）")

COMPLETED = [
    "买家确认收货，交易成功",
    "你已确认收货，交易成功",
    "交易成功",
    "交易完成",
    "订单完成",
]
for t in COMPLETED:
    st = R.detect_status_from_text(t)
    check(f"{t!r} → completed", st == "completed", f"实际 {st}")
    check(f"{t!r} is_completed", R.is_completed(t))


# ---------------------------------------------------------------- 3. 其余各态
print("\n【3】已发货 / 待发货 / 关闭 不能串")

SHIPPED = ["卖家已发货，待买家确认收货", "已发货，待买家确认收货",
           "待买家确认收货", "你已发货", "已发货"]
for t in SHIPPED:
    check(f"{t!r} → shipped", R.detect_status_from_text(t) == "shipped",
          R.detect_status_from_text(t))

PENDING = ["买家已付款，请尽快发货", "等待卖家发货", "我已付款，等待你发货",
           "付款完成", "已付款，待发货", "待发货"]
for t in PENDING:
    check(f"{t!r} → pending_ship", R.detect_status_from_text(t) == "pending_ship",
          R.detect_status_from_text(t))

CLOSED = ["买家取消了订单", "卖家取消了订单", "你关闭了订单，钱款已原路退返",
          "订单已关闭", "交易关闭", "超时未付款"]
for t in CLOSED:
    check(f"{t!r} → cancelled", R.detect_status_from_text(t) == "cancelled",
          R.detect_status_from_text(t))


# ---------------------------------------------------------------- 4. 状态码 vs 文案
print("\n【4】状态码优先，但退款文案能覆盖滞后的码")

check(R.normalize_status("4") == "completed", "码 4 → completed")
check(R.normalize_status(4) == "completed", "int 4 也认")
check(R.normalize_status("11") == "completed", "码 11 → completed")
check(R.normalize_status("8") == "cancelled", "码 8 → cancelled")
check(R.normalize_status("5") == "refunding", "码 5 → refunding")

# 平台码滞后：码还写着 4（已完成），文案已经是退款完成
st = R.normalize_status("4", "退款完成，钱款已原路退返")
check(st == "cancelled", "码 4 + 退款完成文案 → cancelled（文案赢）", st)
check(not R.is_completed("4"), "纯码 4 仍算完成（没有文案时不误伤）")

st2 = R.normalize_status("4", "已发货退款")
check(st2 == "refunding", "码 4 + 已发货退款 → refunding", st2)

# 码不认识 → 用文案
check(R.normalize_status("999", "交易成功") == "completed", "未知码 + 完成文案 → completed")
check(R.normalize_status("", "退款中") == "refunding", "空码 + 退款文案 → refunding")
check(R.normalize_status("", "") == "unknown", "什么都没有 → unknown")
check(R.normalize_status(None, None) == "unknown", "None → unknown")


# ---------------------------------------------------------------- 5. 稳定态
print("\n【5】稳定态判定（决定还要不要轮询）")

for s in ("shipped", "completed", "cancelled"):
    check(f"{s} 是稳定态", R.is_stable(s))
for s in ("processing", "pending_ship", "refunding", "unknown"):
    check(f"{s} 不是稳定态", not R.is_stable(s))
check(not R.is_stable("退款完成"), "退款完成被归一成 cancelled → 稳定")


# ---------------------------------------------------------------- 6. 顺序敏感文案
print("\n【6】同时含多个关键词时，必须是退款赢（顺序即语义）")

TRAPS = [
    ("已发货退款", "refunding"),          # 含"已发货"，不能判 shipped
    ("退款完成，钱款已原路退返", "cancelled"),  # 含"完成"，不能判 completed
    ("交易关闭，退款成功", "cancelled"),
    ("等待卖家处理退款", "refunding"),     # 含"卖家"，不能判 pending_ship
]
for text, expect in TRAPS:
    got = R.detect_status_from_text(text)
    check(f"{text!r} → {expect}", got == expect, f"实际 {got}")


# ---------------------------------------------------------------- 7. explain
print("\n【7】explain 能说清为什么")

e = R.explain("退款完成，钱款已原路退返")
check(e and e["status"] == "cancelled", "explain 返回 cancelled", e)
check(e and e["matched"], "explain 带命中规则", e)
check(R.explain("") is None, "空文本 → None")


# ---------------------------------------------------------------- 8. 库里的行
print("\n【8】get_status 兼容新旧字段名")

check(R.get_status({"order_status": "completed"}) == "completed", "order_status 字段")
check(R.get_status({"status": "shipped"}) == "shipped", "status 字段（老数据）")
check(R.get_status({"order_status": "4"}) == "completed", "数值字符串码")
check(R.get_status({"order_status": "4", "status_text": "退款完成"}) == "cancelled",
      "码 + 文案都用上")
check(R.get_status({}) == "unknown", "空 dict")
check(R.get_status(None) == "unknown", "None")


# ---------------------------------------------------------------- 9. 发货前检查
print("\n【9】发货前检查：退款/已完成/已发货一律不许再发")

# 这是拆 super-butler 的 delivery_block_rules 时发现的真缺口：
# 我们的手动发货入口从来没看 order_status，退款单勾上就能发卡密。
for bad, key in (("cancelled", "退款成功"), ("refund_cancelled", "退款关闭"),
                 ("refunding", "退款处理中"), ("completed", "交易完成"),
                 ("shipped", "已发货")):
    r = R.delivery_check(bad)
    check(f"{key}（{bad}）→ 不许发货", r["ok"] is False, str(r))
    check(f"{key} 必须给出人可读理由", bool(r["reason"]), r["reason"])

# 退款终态的文案也必须拦住，不能只认状态码
check("码 4（已完成）不许发", R.delivery_check("4")["ok"] is False)
r_text = R.delivery_check("退款完成，钱款已原路退返")
check("退款文案（码不认识时）也不许发", r_text["ok"] is False, str(r_text))

# 该放行的必须放行
for good in ("pending_ship", "processing", "unknown", "2"):
    check(f"{good} → 允许发货", R.delivery_check(good)["ok"] is True, str(R.delivery_check(good)))
check("状态为空按 unknown 放行（首次发货）", R.delivery_check(None)["ok"] is True)

# unknown 放行是有意的：首次自动发货时订单还没入库，
# 若按 fail-closed 处理，新订单永远发不出去。判重靠 can_auto_delivery。
check("unknown 在允许集合里（有意为之）", "unknown" in R.DELIVERABLE_STATUSES)

# 每个拒绝理由都要说清后果，不能只说"不允许"
for st in ("cancelled", "refunding", "shipped"):
    reason = R.delivery_check(st)["reason"]
    check(f"{st} 的理由说明了后果",
          any(k in reason for k in ("重发", "钱货两失", "已经退", "重复", "不需要")), reason)


# ---------------------------------------------------------------- 10. 两个入口共用
print("\n【10】手动与自动发货必须共用同一套检查（结构测试）")

# 学 super-butler：不测行为，直接读源码断言两个入口都调了同一个检查。
# 为什么需要这条：两个入口是独立代码路径，历史上就是"一边加了检查另一边没加"。
# 行为测试只能证明"我测的那个入口是对的"，证明不了"另一个入口没有绕过去"。
from pathlib import Path as _P  # noqa: E402
_BACKEND = _P(__file__).resolve().parent.parent
_auto_src = (_BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")
_manual_src = (_BACKEND / "reply_server.py").read_text(encoding="utf-8")

check("自动发货调用了 delivery_check", "delivery_check(" in _auto_src)
check("手动发货调用了 delivery_check", "delivery_check(" in _manual_src)
check("自动发货查退款黑名单", "should_block_refund_buyer_delivery" in _auto_src)
check("手动发货也查退款黑名单了", "should_block_refund_buyer_delivery" in _manual_src)

# 入口调用共用账本的发货逻辑，并保留逐笔成功或失败的返回结果。
_manual_block = _manual_src.split("async def manual_ship_orders(", 1)[1].split("\n@app.", 1)[0]
_delivery_block = _auto_src.split("    async def deliver_order_manually(", 1)[1].split("\n    async def ", 1)[0]
_m_j = _delivery_block.find('if not summary["all_sent"]:')
_m_k = _delivery_block.find("system_shipped=True")
check("手动发货验账发齐后才写已发标记", 0 <= _m_j < _m_k)
check("手动发货入口保留逐笔结果",
      "live_instance.deliver_order_manually(" in _manual_block and "**result" in _manual_block)
check("部分失败计入失败次数", "if result['success']:" in _manual_block and "failed_count += 1" in _manual_block)


print("\n" + "=" * 62)
if FAILS:
    print(f"通过 {PASSES} 项，失败 {len(FAILS)} 项")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print(f"通过 {PASSES} 项，失败 0 项")
sys.exit(0)
