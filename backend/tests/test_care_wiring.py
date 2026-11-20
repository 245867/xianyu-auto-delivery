"""
C1 接线测试：订单状态机 → 交易后动作

新结构下"确认收货"不再直接发消息，而是**触发这一单的交易后动作**
（自动评价 / 求小红花 / 感谢消息三项各自有开关）。

重点验证：
1. 只有真正从别的状态变成 completed 才触发（早退分支也返回 True 的坑）
2. 动作出错不影响订单状态写入
3. 总开关关着时一个线程都不起
4. 触发是异步的，不阻塞订单状态写入
5. 账号不在线只影响"感谢消息"，不影响评价（评价是纯 HTTP）

运行：
    cd backend && python3 -I tests/test_care_wiring.py
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="care_wire_"))
os.environ["CARE_DB"] = str(_TMP / "care.db")
os.environ["CARE_CONFIG"] = str(_TMP / "care.json")
os.environ["RATE_LIMIT_STATE"] = str(_TMP / "rate.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS, FAIL = 0, 0


def check(cond, label, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label}  {extra}")


# ---------------------------------------------------------------- 替身

ORDERS = {}


class _FakeDB:
    def get_order_by_id(self, order_id):
        return ORDERS.get(order_id)

    def insert_or_update_order(self, order_id=None, order_status=None, cookie_id=None,
                               expected_version=None, **kw):
        o = ORDERS.setdefault(order_id, {"order_id": order_id, "cookie_id": cookie_id,
                                         "order_status": "processing", "version": 1})
        o["order_status"] = order_status
        o["version"] = o.get("version", 1) + 1
        return True

    def is_buyer_refund_blacklisted(self, buyer_id, cookie_id):
        return False


sys.modules["db_manager"] = type(sys)("db_manager")
sys.modules["db_manager"].db_manager = _FakeDB()

_RL = type(sys)("rate_limiter")
_RL.acquire = lambda *a, **k: (True, 0)
sys.modules["rate_limiter"] = _RL

import care_message as cm  # noqa: E402
import order_status_handler as osh_mod  # noqa: E402

# 接线测试不关心真实接口，全部换掉
CALLS = []


def fake_run_once(cookie_id, cookie_str="", order_ids=None, app_key="", sender=None,
                  sleep_fn=None):
    CALLS.append({"cookie_id": cookie_id, "order_ids": order_ids, "sender": sender})
    if sender is not None:
        # 真跑一次 sender，验证桥接没写错
        sender("BUYER", "东西收到啦")
    return {"ok": True, "actions": [], "stopped": "", "counts": {}}


cm.run_once = fake_run_once

# 接线测试默认不等（生产默认 90 秒抖动，测试里必须归零）
cm.save_config({"on_complete_delay_seconds": 0})

handler = osh_mod.OrderStatusHandler()
FIRED = []
handler._run_care_actions = lambda **kw: FIRED.append(kw)


# ---------------------------------------------------------------- 1
print("\n【1】只有真正变成 completed 才触发")

ORDERS.clear()
FIRED.clear()
ORDERS["A1"] = {"order_id": "A1", "order_status": "shipped", "cookie_id": "C1",
                "buyer_id": "B1", "item_id": "I1", "version": 1}

ok = handler.update_order_status("A1", "completed", "C1", "测试")
check(ok is True, "状态更新成功")
check(len(FIRED) == 1, "shipped → completed 触发一次", FIRED)
check(FIRED[0]["order_id"] == "A1", "带上订单号", FIRED)

print("\n【2】状态无变化不触发（早退分支也返回 True 的坑）")
FIRED.clear()
ok = handler.update_order_status("A1", "completed", "C1", "重复消息")
check(ok is True, "重复更新仍返回 True（旧行为不能改）")
check(len(FIRED) == 0, "但没再触发", FIRED)

print("\n【3】其它流转不触发")
FIRED.clear()
ORDERS["A2"] = {"order_id": "A2", "order_status": "processing", "cookie_id": "C1",
                "buyer_id": "B2", "item_id": "I2", "version": 1}
handler.update_order_status("A2", "pending_ship", "C1", "付款")
handler.update_order_status("A2", "shipped", "C1", "发货")
check(len(FIRED) == 0, "付款/发货都不触发", FIRED)
handler.update_order_status("A2", "completed", "C1", "收货")
check(len(FIRED) == 1, "completed 才触发", FIRED)

print("\n【4】动作出错不影响订单状态写入")
ORDERS.clear()
ORDERS["A3"] = {"order_id": "A3", "order_status": "shipped", "cookie_id": "C1",
                "buyer_id": "B3", "item_id": "I3", "version": 1}
h2 = osh_mod.OrderStatusHandler()


def boom(**kw):
    raise RuntimeError("关怀模块炸了")


h2._run_care_actions = boom
ok = h2.update_order_status("A3", "completed", "C1", "收货")
check(ok is True, "动作抛异常，订单状态仍更新成功")
check(ORDERS["A3"]["order_status"] == "completed", "库里确实是 completed")


# ---------------------------------------------------------------- 5
print("\n【5】总开关关着：一个线程都不起")

CALLS.clear()
cm.save_config({"enabled": False})
h3 = osh_mod.OrderStatusHandler()
h3._run_care_actions(order_id="A4", cookie_id="C1", buyer_id="B4", item_id="I4")
time.sleep(0.2)
check(CALLS == [], "未启用 → run_once 一次都没调", CALLS)

print("\n【6】三个动作都没开：也不起线程")
CALLS.clear()
cm.save_config({"enabled": True, "auto_rate": False, "ask_flower": False,
                "thanks_message": False})
h3._run_care_actions(order_id="A4", cookie_id="C1", buyer_id="B4", item_id="I4")
time.sleep(0.2)
check(CALLS == [], "三个动作都没开 → 不跑", CALLS)


# ---------------------------------------------------------------- 7
print("\n【7】开启后：异步触发，不阻塞调用方")

cm.save_config({"enabled": True, "dry_run": True, "auto_rate": True,
                "thanks_message": False, "on_complete_delay_seconds": 0})


def slow_run_once(cookie_id, cookie_str="", order_ids=None, app_key="", sender=None,
                  sleep_fn=None):
    time.sleep(0.4)
    CALLS.append({"cookie_id": cookie_id, "order_ids": order_ids, "sender": sender})
    return {"ok": True, "actions": [], "stopped": "", "counts": {}}


cm.run_once = slow_run_once
CALLS.clear()
h4 = osh_mod.OrderStatusHandler()

t0 = time.time()
h4._run_care_actions(order_id="A5", cookie_id="C1", buyer_id="B5", item_id="I5")
elapsed = time.time() - t0
check(elapsed < 0.35, f"调用方立刻返回（{elapsed:.3f}s，实际动作要 0.4s）")
time.sleep(0.7)
check(len(CALLS) == 1, "后台线程后来真的跑了")
check(CALLS[0]["order_ids"] == ["A5"], "只处理这一单（不刷全店）", CALLS)


# ---------------------------------------------------------------- 8
print("\n【8】账号不在线：跳过感谢消息，评价照跑")
CALLS.clear()


class _NoInst:
    @classmethod
    def get_instance(cls, cid):
        return None


_fake = type(sys)("XianyuAutoAsync")
_fake.XianyuLive = _NoInst
sys.modules["XianyuAutoAsync"] = _fake

cm.save_config({"enabled": True, "dry_run": True, "auto_rate": True,
                "thanks_message": True, "on_complete_delay_seconds": 0})
cm.run_once = fake_run_once
h5 = osh_mod.OrderStatusHandler()
h5._run_care_actions(order_id="A6", cookie_id="OFFLINE", buyer_id="B6", item_id="I6")
time.sleep(0.4)
check(len(CALLS) == 1, "评价照跑（评价是纯 HTTP，不需要在线）", CALLS)
check(CALLS[0]["sender"] is None, "但 sender 为 None（发不出感谢消息）", CALLS)

print("\n【9】账号在线：sender 能真的把消息发出去")
CALLS.clear()
SENT = []


class _Inst:
    def __init__(self):
        self.cookie_id = "C9"

    async def send_msg_once(self, toid, item_id, text):
        SENT.append((toid, item_id, text))


_online = type(sys)("XianyuAutoAsync")
_online.XianyuLive = type("L", (), {"get_instance": classmethod(lambda cls, cid: _Inst())})
sys.modules["XianyuAutoAsync"] = _online

h6 = osh_mod.OrderStatusHandler()
h6._run_care_actions(order_id="A7", cookie_id="C9", buyer_id="BUYER9", item_id="ITEM9")
time.sleep(0.5)
check(len(CALLS) == 1 and callable(CALLS[0]["sender"]), "sender 是个可调用对象", CALLS)
check(len(SENT) == 1 and SENT[0][0] == "BUYER9" and SENT[0][1] == "ITEM9",
      "消息真的经 send_msg_once 发出去了", SENT)

del sys.modules["XianyuAutoAsync"]


print("\n" + "=" * 60)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(0 if FAIL == 0 else 1)
