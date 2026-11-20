"""
C1 账号隔离：评价 / 小红花 不能串号（半开专项收尾）
=====================================================

原来的写法是 `rate_limiter.acquire("write", "care")`——所有账号共用一个 key。
两个后果，都只在真跑起来之后才会暴露：

1. 熔断是按账号记的，可闸门按 "care" 查 → **正在罚站的账号照样把评价写出去**；
2. 日上限也按 "care" 记 → 几个账号把 200 次合成一本账，谁先用完谁挨饿。

本测试用**真实的 rate_limiter**（临时状态文件）和两个模拟账号跑 `_one_rate`
与 `_one_flower` 两个真实入口：

[1] A 被熔断时，两个入口都不调用发送函数，返回 rate_limited；
[2] A 的拦截不影响 B，B 照常能写；
[3] 同一账号的评价与小红花共用 "write" 1 次/60 秒，日上限按账号各记各的；
[4] 被限流拦下的那次会 release 占位，等窗口过了能重来。

`cookie_id` 为空时仍保留 `"care"` 兜底，但**当前可达入口都会传非空**：
`care_api.care_run` 直接拒绝空 cookie_id；`order_status_handler` 传的是账号实例的
cookie_id；`run_once` 用 cookie_id 去读按账号配置，空值只会读到全局默认。
没有证据表明存在空值入口，所以不删这个兜底、也不为它加分支。

跑法：cd backend && python3 tests/test_care_account_isolation.py
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="care_iso_"))
os.environ["CARE_DB"] = str(_TMP / "care.db")
os.environ["CARE_CONFIG"] = str(_TMP / "care.json")
os.environ["CARE_ACCOUNT_CONFIG"] = str(_TMP / "care_accounts.json")
os.environ["RATE_LIMIT_STATE"] = str(_TMP / "rate.json")
os.environ["RATE_LIMIT_CIRCUIT"] = str(_TMP / "circuit.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# db_manager 仍然要隔离：care_message 在 run_once 里会读 cookie 表
sys.modules["db_manager"] = type(sys)("db_manager")
sys.modules["db_manager"].db_manager = type("X", (), {})()

import care_message as cm          # noqa: E402
import rate_limiter                # noqa: E402

FAILS = []
PASSES = 0


def check(label, cond, extra=""):
    """注意参数顺序是（说明, 条件）：本文件所有调用都按这个顺序写。
    写反了字符串恒真，整段会静默全绿——第一次跑就踩过一次。"""
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ok   {label}")
    else:
        FAILS.append(label + (f"  ← {extra}" if extra else ""))
        print(f"  FAIL {label}" + (f"  ← {extra}" if extra else ""))


def section(t):
    print(f"\n[{t}]")


SENT = {"rate": [], "flower": []}


def fake_submit_rate(cookie_str, trade_id, feedback, app_key="", cookie_id=""):
    SENT["rate"].append((trade_id, cookie_id))
    return {"kind": "ok", "ret": "rate-ok"}


def fake_ask_flower(cookie_str, order_id, text, app_key="", cookie_id=""):
    SENT["flower"].append((order_id, cookie_id))
    return {"kind": "ok", "ret": "flower-ok"}


cm.submit_rate = fake_submit_rate
cm.ask_flower = fake_ask_flower
cm.find_in_pending = lambda *a, **k: {"ok": True, "found": True, "trade_id": "T-FOUND"}

CFG = {"rate_template": "thanks", "flower_template": "default",
       "auto_rate": True, "ask_flower": True}
ROW = {"bizOrderId": "T-REAL", "buyerRateStatus": "4", "rateResult": "1"}


def snapshot():
    return {k: list(v) for k, v in SENT.items()}


# ---------------------------------------------------------------- [1] A 熔断
section("1 A 熔断：两个入口都不许调发送函数")

rate_limiter.reset()
rate_limiter.reset_circuit()
secs = rate_limiter.trip_circuit("accA", minutes=10, reason="离线：账号 A 撞墙")
check("A 已进熔断", rate_limiter.circuit_blocked("accA")[0] is True, str(secs))

before = snapshot()
r_rate = cm._one_rate(CFG, "ck", "ORD-A1", ROW, app_key="k", cookie_id="accA")
r_flower = cm._one_flower(CFG, "ck", "ORD-A2", app_key="k", cookie_id="accA")

check("评价被拦成 rate_limited", r_rate.get("status") == "rate_limited", str(r_rate))
check("小红花被拦成 rate_limited", r_flower.get("status") == "rate_limited", str(r_flower))
check("评价的发送函数一次都没调", snapshot()["rate"] == before["rate"], str(SENT["rate"]))
check("小红花的发送函数一次都没调", snapshot()["flower"] == before["flower"],
      str(SENT["flower"]))
check("A 的 write 日计数为 0（没写就不记账）",
      rate_limiter.daily_count("write", "accA") == 0,
      str(rate_limiter.daily_count("write", "accA")))

# 被拦下的那单占位必须撤销，否则窗口一过也发不出去
re_claim = cm.claim("rate", "ORD-A1", "", variant="thanks")
check("限流拦下后占位已撤销（窗口过了能重来）", re_claim.get("ok") is True, str(re_claim))
cm.release(re_claim["key"])

# ---------------------------------------------------------------- [2] B 照写
section("2 A 罚站不影响 B：B 照常写")

rate_limiter.reset("write")
r_b = cm._one_rate(CFG, "ck", "ORD-B1", ROW, app_key="k", cookie_id="accB")
check("B 的评价成功", r_b.get("status") == "sent", str(r_b))
check("B 的发送函数调了 1 次，且带的是 B 的 cookie_id",
      SENT["rate"] == [("T-REAL", "accB")], str(SENT["rate"]))
check("A 仍然没有发送记录", ("T-REAL", "accA") not in SENT["rate"], str(SENT["rate"]))
check("B 的 write 日计数 = 1", rate_limiter.daily_count("write", "accB") == 1,
      str(rate_limiter.daily_count("write", "accB")))
check("A 的 write 日计数仍是 0", rate_limiter.daily_count("write", "accA") == 0,
      str(rate_limiter.daily_count("write", "accA")))

# ---------------------------------------------------------------- [3] 同账号配额
section("3 同一账号：评价与小红花共用 write 1 次/60 秒")

r_f = cm._one_flower(CFG, "ck", "ORD-B2", app_key="k", cookie_id="accB")
check("刚发过评价，紧接着的小红花被同账号配额拦住",
      r_f.get("status") == "rate_limited", str(r_f))
check("小红花发送函数没被调", SENT["flower"] == [], str(SENT["flower"]))
check("配额被拒不影响日计数（仍为 1）",
      rate_limiter.daily_count("write", "accB") == 1,
      str(rate_limiter.daily_count("write", "accB")))

rate_limiter.reset("write")     # 相当于 60 秒过去
r_f2 = cm._one_flower(CFG, "ck", "ORD-B2", app_key="k", cookie_id="accB")
check("窗口过去后小红花能发", r_f2.get("status") == "sent", str(r_f2))
check("小红花也记在 B 名下", SENT["flower"] == [("ORD-B2", "accB")], str(SENT["flower"]))
check("B 的 write 日计数 = 2", rate_limiter.daily_count("write", "accB") == 2,
      str(rate_limiter.daily_count("write", "accB")))

# ---------------------------------------------------------------- [4] 解熔断后 A
section("4 解熔断后 A 恢复，且不影响 B 的账本")

rate_limiter.reset_circuit("accA")
r_a2 = cm._one_rate(CFG, "ck", "ORD-A3", ROW, app_key="k", cookie_id="accA")
check("A 解熔断后能写", r_a2.get("status") == "sent", str(r_a2))
check("A 的日计数从 0 起算，不继承 B 的",
      rate_limiter.daily_count("write", "accA") == 1,
      str(rate_limiter.daily_count("write", "accA")))
check("B 的日计数没被 A 动过", rate_limiter.daily_count("write", "accB") == 2,
      str(rate_limiter.daily_count("write", "accB")))

# ---------------------------------------------------------------- [5] 源码口径
section("5 两个入口都用账号做 key")

src = Path(cm.__file__).read_text(encoding="utf-8")
check("评价与小红花各一处按账号取配额",
      src.count('acquire("write", cookie_id or "care")') == 2,
      str(src.count('acquire("write", cookie_id or "care")')))
check("不再有固定 key 的写法",
      'acquire("write", "care")' not in src)

print("\n" + "=" * 56)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print(f"✅ 全部通过（{PASSES} 项）")
