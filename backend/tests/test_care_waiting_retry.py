"""
拆解 13：`waiting`（交易未完成）不是终态
==========================================

来源：闲管家 2025.11.03 更新日志「修复求/收小红花偶尔不触发问题」。

我们原来的形状：`claim()` 是 INSERT OR IGNORE，行一旦存在就再也不发。
一单在买家确认收货前被扫到 → 平台回「未完成的交易」→ 落 `waiting` → 永久不再试。
表现就是"偶尔不触发"：取决于那轮扫描早于还是晚于确认收货，且永远查不出来。

守住的四件事：
[1] 冷却期内不重复试（别每轮都去撞同一个接口）
[2] 冷却过后能重新抢（这才是修复本身）
[3] 到上限转人工（不是无限撞）
[4] 其它终态不受影响（sent / skipped / failed 仍然是做了就不再做）

跑法：cd backend && python3 -I tests/test_care_waiting_retry.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="care_wait_"))
os.environ["CARE_DB"] = str(_TMP / "care.db")
os.environ["CARE_CONFIG"] = str(_TMP / "care.json")
os.environ["CARE_ACCOUNT_CONFIG"] = str(_TMP / "care_accounts.json")
os.environ["RATE_LIMIT_STATE"] = str(_TMP / "rate.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.modules["db_manager"] = type(sys)("db_manager")
sys.modules["db_manager"].db_manager = type("X", (), {})()

_RL = type(sys)("rate_limiter")
_RL.acquire = lambda *a, **k: (True, 60)
sys.modules["rate_limiter"] = _RL

import care_message as cm  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def section(t):
    print(f"\n[{t}]")


def age(key, hours):
    """把这条占位的 updated_at 往前推 N 小时，模拟时间流逝。"""
    ts = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = cm._conn()
    try:
        conn.execute("UPDATE care_actions SET updated_at=? WHERE idem_key=?", (ts, key))
        conn.commit()
    finally:
        conn.close()


def row(key):
    conn = cm._conn()
    try:
        r = conn.execute("SELECT * FROM care_actions WHERE idem_key=?", (key,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


# ---------------------------------------------------------------- [1] 冷却
section("1 冷却期内不再试")
g = cm.claim("rate", "ORD-W1", "CK")
check(g.get("ok") is True, "首轮抢到")
key = g["key"]
cm.finish(key, "waiting", "未完成的交易")
age(key, 1)  # 才 1 小时
g2 = cm.claim("rate", "ORD-W1", "CK")
check(g2.get("ok") is False, "1 小时后不给抢（冷却未到）")
check("等确认收货" in (g2.get("reason") or ""), "拒绝原因说清了在等什么")
check(row(key)["status"] == "waiting", "状态仍是 waiting，没有被偷偷改掉")

# ---------------------------------------------------------------- [2] 冷却后
section("2 冷却过后能重新抢（这就是修复本身）")
age(key, cm.WAITING_RETRY_HOURS + 1)
g3 = cm.claim("rate", "ORD-W1", "CK")
check(g3.get("ok") is True, f"{cm.WAITING_RETRY_HOURS} 小时后重新抢到")
check(g3.get("retry") == "waiting", "返回 retry=waiting，调用方能区分首轮和重试")
check(row(key)["status"] == "claimed", "状态回到 claimed")
check(int(row(key)["attempts"] or 0) == 1, "attempts 累加到 1")

# 第二次 waiting 后，attempts 再涨
cm.finish(key, "waiting", "还是未完成")
age(key, cm.WAITING_RETRY_HOURS + 1)
cm.claim("rate", "ORD-W1", "CK")
check(int(row(key)["attempts"] or 0) == 2, "第二次重试后 attempts=2")

# ---------------------------------------------------------------- [3] 上限
section("3 到上限转人工，不是无限撞")
cm.finish(key, "waiting", "还是未完成")   # 先回到 waiting，再假装已经试满
conn = cm._conn()
conn.execute("UPDATE care_actions SET attempts=? WHERE idem_key=?",
             (cm.WAITING_MAX_ATTEMPTS, key))
conn.commit()
conn.close()
age(key, cm.WAITING_RETRY_HOURS + 1)
g4 = cm.claim("rate", "ORD-W1", "CK")
check(g4.get("ok") is False, f"试满 {cm.WAITING_MAX_ATTEMPTS} 次后不再自动抢")
check("转人工" in (g4.get("reason") or ""), "原因写明转人工")
check(row(key)["status"] == "waiting", "仍是 waiting（页面上看得见这单卡住了）")

# ---------------------------------------------------------------- [4] 终态
section("4 其它终态不受影响")
for st in ("sent", "skipped", "failed"):
    oid = f"ORD-{st.upper()}"
    k = cm.claim("rate", oid, "CK")["key"]
    cm.finish(k, st, "x")
    age(k, 999)
    g = cm.claim("rate", oid, "CK")
    check(g.get("ok") is False, f"{st} 过了多久都不再发（一次就是一次）")

# ---------------------------------------------------------------- [5] 小红花
section("5 小红花遇「交易未完成」也要落 waiting，不能落 failed")
_calls = {"n": 0}


def _fake_flower(*a, **k):
    _calls["n"] += 1
    return {"ok": False, "kind": "waiting", "ret": "未完成的交易"}


cm.ask_flower = _fake_flower
out = cm._one_flower({"flower_text": "求个小红花"}, "COOKIE", "ORD-F1")
check(out["status"] == "waiting", "返回 waiting（不是 failed）")
check(row(cm.make_key("flower", "ORD-F1"))["status"] == "waiting", "账本落 waiting")
age(cm.make_key("flower", "ORD-F1"), cm.WAITING_RETRY_HOURS + 1)
g5 = cm.claim("flower", "ORD-F1", "CK")
check(g5.get("ok") is True, "冷却后小红花也能补一次")

# ---------------------------------------------------------------- [6] 接线
section("6 接线：常量与条件更新都在源码里")
src = (Path(__file__).resolve().parent.parent / "care_message.py").read_text(encoding="utf-8")
check("WAITING_RETRY_HOURS" in src and "WAITING_MAX_ATTEMPTS" in src, "两个常量存在")
check("AND status='waiting'" in src, "重试用 status 条件更新（并发下只放一个进来）")
check('"attempts": "INTEGER DEFAULT 0"' in src, "老库会补 attempts 列")

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")