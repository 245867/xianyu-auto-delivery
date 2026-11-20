"""
方向一：退款后处置账本

要守住的东西：
[1] 已经发货的退款：记损失上限，标注"收不回来"
[2] 还没发货的退款：损失 0，标成"停发即可"
[3] 同一单重复来（refunding → refunding）只记一笔、只通知一次
[4] close：没记过退款的关单**不能**算退款（普通关单很常见）
[5] 统计口径：退款单数 / 损失 / 有损失的 / 可回收的 / 待处理
[6] 没发给买家的退款不该打扰人（不推通知）

跑法：cd backend && python3 tests/test_refund_aftercare.py
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="refund_"))
os.environ["REFUND_AFTERCARE_DB"] = str(_TMP / "refund.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


# ---------------------------------------------------------------- 假依赖
ORDERS = {}
CARDS = {}
NOTIFIED = []


class _FakeOrders:
    def get_order_by_id(self, order_id):
        return ORDERS.get(order_id) or {}


class _FakeDR:
    @staticmethod
    def cards(order_id, cookie_id):
        return CARDS.get((order_id, cookie_id), [])


def _fake_notify(*a, **kw):
    NOTIFIED.append((a, kw))
    return True


sys.modules["db_manager"] = type("M", (), {"db_manager": _FakeOrders()})
sys.modules["delivery_reliability"] = _FakeDR
sys.modules["notify_service"] = type("M", (), {"send_event": staticmethod(_fake_notify)})

import refund_aftercare as ra  # noqa: E402


def sent(idx):
    return {"idx": idx, "status": "sent", "payload": f"卡{idx}"}


def pending(idx):
    return {"idx": idx, "status": "pending", "payload": ""}


# ---------------------------------------------------------------- [1]
section("1 已发货的退款：记损失上限")
ORDERS["O1"] = {"order_id": "O1", "item_id": "IT1", "buyer_id": "B1", "amount": "9.90"}
CARDS[("O1", "CK")] = [sent(0), sent(1), sent(2)]
r = ra.record("O1", "CK", "refunding")
check(r["ok"] is True, "记账成功")
check(r["sent_cards"] == 3 and r["total_cards"] == 3, "已发 3/3 张")
check(abs(r["loss"] - 9.9) < 1e-6, "损失 9.9（按订单金额，口径是上限）")
check(r["recoverable"] is False, "不可回收")
check("收不回来" in r["action"], "处置建议写明收不回来")
check(len(NOTIFIED) == 1, "推了一条通知（已发货的退款要人知道）")

# ---------------------------------------------------------------- [2]
section("2 还没发货的退款：损失 0")
ORDERS["O2"] = {"order_id": "O2", "item_id": "IT2", "buyer_id": "B2", "amount": "¥19.90"}
CARDS[("O2", "CK")] = [pending(0), pending(1)]
r2 = ra.record("O2", "CK", "refunding")
check(abs(r2["loss"]) < 1e-9, "损失 0（一张都没发出去）")
check(r2["recoverable"] is True, "可回收：停发即可")
check(len(NOTIFIED) == 1, "没有发给买家 → 不打扰人")

# ---------------------------------------------------------------- [3]
section("3 幂等：同一单重复来只记一笔")
r1b = ra.record("O1", "CK", "refunding")
check(r1b["fresh"] is False, "第二次不是新建")
check(len(NOTIFIED) == 1, "通知没有重复发")
st = ra.stats(30)
check(st["count"] == 2, "总数还是 2 单（不是 3）")

# ---------------------------------------------------------------- [4]
section("4 close：普通关单不能算退款")
r3 = ra.close("O9", "CK")
check(r3["ok"] is True and "普通关单" in r3.get("skipped", ""), "没记过退款的关单被跳过")
ra.close("O1", "CK")
row = [x for x in ra.open_items(10) if x["order_id"] == "O1"][0]
check(row["status"] == "cancelled", "记过退款的单被更新为 cancelled")

# ---------------------------------------------------------------- [5]
section("5 统计口径")
st = ra.stats(30)
check(st["count"] == 2, "退款单数 2")
check(st["with_sent"] == 1, "有损失的 1 单")
check(st["recoverable"] == 1, "可回收的 1 单")
check(abs(st["loss"] - 9.9) < 1e-6, "总损失 9.9")
check(st["open"] == 2, "两单都还没处理")
check(len(ra.open_items(10, only_loss=True)) == 1, "只看有损失的：1 条")

# ---------------------------------------------------------------- [6]
section("6 人工标记已处理")
check(ra.resolve(row["id"], "已拉黑买家") is True, "标记成功")
check(ra.resolve(99999, "x") is False, "不存在的 id 返回 False")
st2 = ra.stats(30)
check(st2["open"] == 1, "待处理少了一条")

# ---------------------------------------------------------------- [7]
section("7 接线")
src = (BACKEND / "order_status_handler.py").read_text(encoding="utf-8")
check("import refund_aftercare" in src, "订单状态处理器 import refund_aftercare")
check("refund_aftercare.on_status(" in src, "退款状态变更时调用 on_status")
check("on_status(order_id, cookie_id, str(new_status)" in src, "传入的是新状态（不是字符串常量）")

srv = (BACKEND / "reply_server.py").read_text(encoding="utf-8")
check("refund_api" in srv, "退款路由已注册")

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")