"""
拆解 3 补：保活也要认熔断，换了 Cookie 就自动复活。

守住四件事：
[1] 账号在熔断里 → 本轮不 ping、不换 token（心跳也是请求，罚站时敲只会拖长）
[2] 熔断状态在返回里看得见（skipped / remain_seconds），页面能解释"为什么没动"
[3] 换了 Cookie（内容哈希变了）→ 之前的失败清零、熔断解除，立刻恢复工作
[4] Cookie 没变时不会误复活（不能每次轮询都自己解封）

跑法：python tests/test_token_circuit.py
"""

import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="tokcircuit_"))
os.environ["RATE_LIMIT_STATE"] = str(_TMP / "rate.json")
os.environ["RATE_LIMIT_CIRCUIT"] = str(_TMP / "circuit.json")
os.environ["CIRCUIT_BREAK_MINUTES"] = "10"

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import rate_limiter as rl
import token_keeper as tk

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def section(t):
    print(f"\n[{t}]")


# 测试不许碰真实的 data/
tk.STATE_FILE = _TMP / "token_keeper.json"
tk.CONFIG_FILE = _TMP / "token_keeper_config.json"
tk.STATE_FILE.write_text("{}", encoding="utf-8")
tk.time.sleep = lambda *a, **k: None       # 账号间 3–8 秒的错开，测试里不等

COOKIE_A = "unb=1001; _m_h5_tk=aaaa; cookie2=1"
COOKIE_B = "unb=1001; _m_h5_tk=bbbb; cookie2=2"   # 同一个账号换了登录态

CALLS = []


def fake_all_cookies():
    return {"acc1": CURRENT["cookie"]}


CURRENT = {"cookie": COOKIE_A}


def fake_ping(cookie_id, cookie_str):
    CALLS.append(("ping", cookie_id))
    return {"ok": True, "cookie_str": cookie_str}


def fake_refresh(cookie_id, cookie_str):
    CALLS.append(("refresh", cookie_id))
    return {"ok": True, "cookie_str": cookie_str}


tk._all_cookies = fake_all_cookies
tk.ping = fake_ping
tk.refresh_im_token = fake_refresh


def reset_all():
    CALLS.clear()
    rl.reset_circuit()
    rl.reset()
    tk.STATE_FILE.write_text("{}", encoding="utf-8")
    CURRENT["cookie"] = COOKIE_A


def sig(s):
    return hashlib.sha256(s.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- [1] 熔断中不动
section("1 熔断中的账号不 ping")
reset_all()
rl.trip_circuit("acc1", reason="评价触发风控")

r = tk.run_once(force=True)
acc = r["accounts"]["acc1"]
check(CALLS == [], "一次请求都没发（ping/refresh 都是空）")
check(acc.get("skipped") == "风控熔断中", f"结果里写明被跳过（实际 {acc.get('skipped')}）")
check(540 <= int(acc.get("remain_seconds") or 0) <= 600,
      f"剩余秒数透出来（实际 {acc.get('remain_seconds')}）")

# 别的账号不受牵连
tk._all_cookies = lambda: {"acc1": CURRENT["cookie"], "acc2": COOKIE_B}
r = tk.run_once(force=True)
check(("ping", "acc2") in CALLS, "同店别的账号照常保活")
check(("ping", "acc1") not in CALLS, "被熔断的账号依然不动")
tk._all_cookies = fake_all_cookies


# ---------------------------------------------------------------- [2] 记账可读
section("2 状态可读，不假装成功")
reset_all()
rl.trip_circuit("acc1")
r = tk.run_once(force=True)
acc = r["accounts"]["acc1"]
check("ok" in acc, "有 ok 字段（页面不用猜）")
check("last_ping" in acc, "有 last_ping（能看到上一次成功是什么时候）")


# ---------------------------------------------------------------- [3] 换 Cookie 即复活
section("3 换 Cookie → 失败清零、熔断解除")
reset_all()
# 先记一个"旧的失败态 + 旧 Cookie 签名"
tk.STATE_FILE.write_text(
    '{"acc1": {"fail_count": 7, "last_ok": false, "cookie_sig": "%s"}}' % sig(COOKIE_A),
    encoding="utf-8")
rl.trip_circuit("acc1", reason="旧登录态被盯上")
CURRENT["cookie"] = COOKIE_B          # 用户重新登录

r = tk.run_once(force=True)
acc = r["accounts"]["acc1"]
check(("ping", "acc1") in CALLS, "换了 Cookie 就恢复工作（不再被旧熔断挡住）")
check(rl.circuit_blocked("acc1") == (False, 0), "熔断被一起解除（新登录态不该继承旧处罚）")
check(not acc.get("skipped"), "返回里不再标 skipped")
check(int(acc.get("fail_count") or 0) == 0 if "fail_count" in acc else True,
      "失败计数清零")

st = tk._load_state()["acc1"]
check(st.get("cookie_sig") == sig(COOKIE_B), "新 Cookie 的签名已记下")
check(st.get("cookie_updated_at"), "记录换 Cookie 的时间（方便排查）")


# ---------------------------------------------------------------- [4] 不会误复活
section("4 Cookie 没变不该自己解封")
reset_all()
tk.STATE_FILE.write_text(
    '{"acc1": {"fail_count": 3, "cookie_sig": "%s"}}' % sig(COOKIE_A), encoding="utf-8")
rl.trip_circuit("acc1")
r = tk.run_once(force=True)       # 第一次：识别到签名没变
check(CALLS == [], "签名没变 → 依然熔断，不发请求")
check(rl.circuit_blocked("acc1")[0] is True, "熔断还在")

CALLS.clear()
r = tk.run_once(force=True)       # 第二次也不能"顺手"解封
check(CALLS == [], "再轮一次还是不动")
check(rl.circuit_blocked("acc1")[0] is True, "两次都没被误解除")


# ---------------------------------------------------------------- [5] 已保存签名也要能比对（重启后）
section("5 重启后仍能识别换 Cookie")
reset_all()
tk.STATE_FILE.write_text(
    '{"acc1": {"cookie_sig": "%s", "fail_count": 2}}' % sig(COOKIE_A), encoding="utf-8")
rl.trip_circuit("acc1")
CURRENT["cookie"] = COOKIE_B
tk.run_once(force=True)
check(rl.circuit_blocked("acc1") == (False, 0), "进程重启（内存全丢）也能靠落盘签名恢复")


# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
