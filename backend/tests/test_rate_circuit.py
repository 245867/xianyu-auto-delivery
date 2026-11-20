"""
拆解 3：持久化写操作熔断（goofish-cli 的 guard.py / limiter.py）。

守住五件事：
[1] 熔断按账号记，落盘，跨"重启"有效
[2] 写操作被拦，读操作（search/detail）照常
[3] 全局熔断同时拦所有账号
[4] 到期自动失效；人工可解；状态文件坏了**放行**（后台常驻，不该因为一个坏文件停业）
[5] 风控真的会触发它（care_message 里 stopped=risk 处接线）

跑法：python tests/test_rate_circuit.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="circuit_"))
os.environ["RATE_LIMIT_STATE"] = str(_TMP / "rate.json")
os.environ["RATE_LIMIT_CIRCUIT"] = str(_TMP / "circuit.json")
os.environ["CIRCUIT_BREAK_MINUTES"] = "10"

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import rate_limiter as rl

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def section(t):
    print(f"\n[{t}]")


CIRCUIT = Path(os.environ["RATE_LIMIT_CIRCUIT"])


def fresh():
    rl.reset_circuit()
    rl.reset()


# ---------------------------------------------------------------- [1] 按账号
section("1 按账号熔断，跨进程有效")
fresh()
secs = rl.trip_circuit("acc1", reason="风控")
check(secs == 600, f"默认 10 分钟（实际 {secs}s）")
ok, retry = rl.acquire("publish", "acc1")
check(ok is False, "被熔断的账号拿不到发布配额")
check(540 <= retry <= 600, f"给出的等待时间是剩余秒数（实际 {retry}）")

# 「重启」= 重新读文件。模块变量全没了也不影响：状态只在文件里。
rl._loaded = False
rl._mem.clear()
rl._daily.clear()
check(rl.acquire("publish", "acc1")[0] is False, "清掉内存后依然熔断（说明真的落盘了）")
check(CIRCUIT.exists(), "熔断状态文件存在")
check("acc1" in json.loads(CIRCUIT.read_text(encoding="utf-8"))["accounts"],
      "按账号存（不是一刀切全局）")

check(rl.acquire("publish", "acc2")[0] is True, "别的账号不受影响")


# ---------------------------------------------------------------- [2] 读写分开
section("2 只拦写，不拦读")
fresh()
rl.trip_circuit("acc1")
check(rl.acquire("detail", "global")[0] is True, "detail 是读 → 照常")
check(rl.acquire("search", "global")[0] is True, "search 是读 → 照常")
for scope in ("write", "publish", "polish", "delivery", "media"):
    ok, _ = rl.acquire(scope, "acc1")
    check(ok is False, f"{scope} 是写 → 拦住")


# ---------------------------------------------------------------- [3] 全局
section("3 全局熔断")
fresh()
rl.trip_circuit("")
check(rl.acquire("publish", "any-account")[0] is False, "全局熔断拦所有账号")
check(rl.acquire("publish", "another")[0] is False, "换账号也拦")
st = rl.circuit_status()
check(st["global_seconds"] > 0, "状态里能看到全局剩余")


# ---------------------------------------------------------------- [4] 到期 / 解除 / 坏文件
section("4 到期、解除、坏文件")
fresh()
rl.trip_circuit("acc1")
st = rl.circuit_status()
check(st["accounts"].get("acc1", 0) > 0, "状态里能看到账号剩余")

# 把到期时间改到过去 = 等它自己过期
raw = json.loads(CIRCUIT.read_text(encoding="utf-8"))
raw["accounts"]["acc1"] = time.time() - 1
CIRCUIT.write_text(json.dumps(raw), encoding="utf-8")
check(rl.circuit_blocked("acc1")[0] is False, "到期后自动失效")
check(rl.acquire("publish", "acc1")[0] is True, "到期后能继续发布")

rl.trip_circuit("acc1")
rl.reset_circuit("acc1")
rl.reset()                     # 清掉刚才那次成功留下的窗口计数，单独验熔断
check(rl.acquire("publish", "acc1")[0] is True, "人工解除后恢复")

rl.trip_circuit("acc1")
CIRCUIT.write_text("{ this is not json", encoding="utf-8")
check(rl.circuit_blocked("acc1") == (False, 0), "状态文件坏了 → 放行（不因为坏文件停业）")
rl.reset()
check(rl.acquire("publish", "acc1")[0] is True, "坏文件时写操作照常")

CIRCUIT.write_text(json.dumps({"accounts": {"acc9": "垃圾"}}), encoding="utf-8")
check(rl.circuit_blocked("acc9")[0] is False, "字段类型不对也不炸")
rl.reset_circuit()


# ---------------------------------------------------------------- [5] 接线
section("5 风控真的会触发熔断")
src = (BACKEND / "care_message.py").read_text(encoding="utf-8")
i_trip = src.find("trip_circuit(cookie_id")
check(i_trip > 0, "care_message 风控处接了熔断")
seg = src[max(0, i_trip - 400):i_trip + 200]
check('stopped == "risk"' in seg, "只在风控（不是鉴权/网络）时熔断")
check("cookie_id" in seg, "按账号记，不会一刀切全店")

i_loop_break = src.find('if st == "risk_stop":')
check(0 < i_loop_break < i_trip, "在中断本轮之后记录（先停手、再记账）")

# 熔断优先于配额：配额还有没有都不该放行
fresh()
rl.trip_circuit("acc1")
ok, retry = rl.acquire("publish", "acc1")
check(ok is False and retry >= 1, "先看熔断再看窗口（顺序对了）")


# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
