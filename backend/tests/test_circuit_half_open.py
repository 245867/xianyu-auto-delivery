"""
熔断半开探针（L2-3）
=====================
原来的熔断是"10 分钟一到，全速恢复"：排队等着的发布/发货/擦亮同时涌上去，
平台那边看到的是"刚解封就狂发"，于是几分钟内再撞一次，用户只看到"又停了"。

半开要锁八件事：

1. 熔断期内：所有写闸都拦，返回剩余秒数；
2. 刚到期：第一次写放行（探针），紧接着的写在 30 秒间隔内被拦；
3. 第一笔探针后安静 5 分钟：视为恢复，清掉半开记录，之后全速；
4. 探针又撞墙：时长翻倍（10 → 20 → 40 → 封顶 60），不是死循环 10 分钟；
5. 人工解除 / 很久没犯：回到第一档，不把旧账一直翻倍；
6. 接线：发布/擦亮/评价走 acquire、发货逐卡与确认发货走 gate_write；
7. 持续流量（每 30 秒一笔、不再撞墙）也必须满 5 分钟就退出半开——
   关闭计时从"第一笔探针"算，不被后续放行刷新（第 7 节）；
8. 被本地配额/日上限/冷却拦下的请求**不占探针**，否则零写入也会被判成已恢复（第 8 节）。

全程离线：只碰临时目录里的 circuit.json / rate.json。

跑法（在 backend 目录）：python3 tests/test_circuit_half_open.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="half_open_"))
os.environ["RATE_LIMIT_STATE"] = str(_TMP / "rate.json")
os.environ["RATE_LIMIT_CIRCUIT"] = str(_TMP / "circuit.json")
os.environ["CIRCUIT_BREAK_MINUTES"] = "10"
os.environ["CIRCUIT_MAX_MINUTES"] = "60"
os.environ["CIRCUIT_PROBE_SPACING_SECONDS"] = "30"
os.environ["CIRCUIT_CLOSE_AFTER_SECONDS"] = "300"
os.environ.pop("CIRCUIT_ESCALATION_RESET_SECONDS", None)

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import rate_limiter as rl  # noqa: E402

FAILS: list[str] = []
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ok   {name}" + (f"（{detail}）" if detail else ""))
    else:
        FAILS.append(name + (f"（{detail}）" if detail else ""))
        print(f"  FAIL {name}" + (f"（{detail}）" if detail else ""))


def section(title: str) -> None:
    print(f"\n[{title}]")


def fresh():
    rl.reset_circuit()
    rl.reset()


def expire(key: str):
    """把某个账号的到期时间改到过去 = 等它自然到期（不真的等 10 分钟）。"""
    st = json.loads(rl.CIRCUIT_FILE.read_text(encoding="utf-8"))
    st.setdefault("accounts", {})[key] = time.time() - 1
    rl.CIRCUIT_FILE.write_text(json.dumps(st), encoding="utf-8")


def set_probe_age(key: str, seconds: float):
    """假装"那次探针发生在 seconds 秒之前"（等于让时间走过去）。

    opened_at 也要一起挪：它是"半开从哪一刻开始算"的锚点，不跟着走就只是
    改了探针间距、没真的经过时间。
    """
    st = json.loads(rl.CIRCUIT_FILE.read_text(encoding="utf-8"))
    entry = st.setdefault("half", {}).setdefault(key, {})
    entry["probe_at"] = time.time() - seconds
    entry["opened_at"] = time.time() - seconds
    rl.CIRCUIT_FILE.write_text(json.dumps(st), encoding="utf-8")


# ---------------------------------------------------------------- 1. 熔断期
section("熔断期内：写全拦")
fresh()
secs = rl.trip_circuit("acc1", reason="风控")
check("第一次熔断仍是 10 分钟", secs == 600, f"{secs}s")
allowed, retry = rl.gate_write("acc1")
check("写闸拦住", allowed is False and retry > 0, f"{allowed}/{retry}s")
allowed, _ = rl.gate_write("acc2")
check("别的账号不受影响", allowed is True)
check("读配额照常", rl.acquire("detail", "global")[0] is True)

# ---------------------------------------------------------------- 2. 半开
section("刚到期：一次探针，然后按 30 秒排队")
expire("acc1")
a1, r1 = rl.gate_write("acc1")
check("第一笔放行（探针）", a1 is True, f"{a1}/{r1}")
a2, r2 = rl.gate_write("acc1")
check("紧接着第二笔被拦", a2 is False, f"{a2}/{r2}")
check("等待时间不超过一个间隔", 0 < r2 <= 31, f"{r2}s")
st = rl.circuit_status()
check("状态里能看到半开", st.get("half_open", {}).get("acc1") is not None, str(st.get("half_open")))
check("标记正在探针", st["half_open"]["acc1"]["probing"] is True, str(st["half_open"]["acc1"]))

set_probe_age("acc1", 31)
a3, _ = rl.gate_write("acc1")
check("过了 30 秒再放行一笔", a3 is True)

section("探针后安静 5 分钟：视为恢复")
set_probe_age("acc1", 301)
a4, _ = rl.gate_write("acc1")
a5, _ = rl.gate_write("acc1")
check("安静够久后连续两笔都放行", a4 is True and a5 is True, f"{a4}/{a5}")
check("半开记录已清除", "acc1" not in (rl.circuit_status().get("half_open") or {}),
      str(rl.circuit_status().get("half_open")))

# ---------------------------------------------------------------- 3. 翻倍
section("探针又撞墙：10 → 20 → 40 → 封顶 60")
fresh()
rl.trip_circuit("acc1")
expire("acc1")
check("探针放行", rl.gate_write("acc1")[0] is True)
secs2 = rl.trip_circuit("acc1", reason="探针再次风控")
check("第二次 20 分钟", secs2 == 1200, f"{secs2}s")
expire("acc1")
rl.gate_write("acc1")
secs3 = rl.trip_circuit("acc1")
check("第三次 40 分钟", secs3 == 2400, f"{secs3}s")
expire("acc1")
rl.gate_write("acc1")
secs4 = rl.trip_circuit("acc1")
check("第四次封顶 60 分钟", secs4 == 3600, f"{secs4}s")
st = rl.circuit_status()
check("状态里给出下一次的时长", st["half_open"]["acc1"]["next_minutes"] == 60,
      str(st["half_open"]["acc1"]))

section("人工解除：回到第一档")
rl.reset_circuit("acc1")
check("解除后半开记录也没了", "acc1" not in (rl.circuit_status().get("half_open") or {}),
      str(rl.circuit_status().get("half_open")))
check("解除后能写", rl.gate_write("acc1")[0] is True)
again = rl.trip_circuit("acc1")
check("再撞是第一档", again == 600, f"{again}s")

section("很久没犯：从第一档重算")
fresh()
rl.trip_circuit("acc9")
st = json.loads(rl.CIRCUIT_FILE.read_text(encoding="utf-8"))
st["half"]["acc9"]["failures"] = 3
st["half"]["acc9"]["since"] = time.time() - 7 * 3600
st["accounts"]["acc9"] = time.time() - 1
rl.CIRCUIT_FILE.write_text(json.dumps(st), encoding="utf-8")
quiet = rl.trip_circuit("acc9")
check("7 小时后重新从 10 分钟算", quiet == 600, f"{quiet}s")

# ---------------------------------------------------------------- 4. 与配额
section("与配额的关系：熔断优先，半开也排队")
fresh()
rl.trip_circuit("acc1")
ok, retry = rl.acquire("publish", "acc1")
check("熔断期拿不到发布配额", ok is False and retry > 0, f"{ok}/{retry}")
expire("acc1")
ok1, _ = rl.acquire("publish", "acc1")
ok2, retry2 = rl.acquire("publish", "acc1")
check("到期后第一笔发布放行", ok1 is True, str(ok1))
check("第二笔要等探针间隔", ok2 is False and 0 < retry2 <= 31, f"{ok2}/{retry2}")

# ---------------------------------------------------------------- 5. 容错
section("坏文件仍然放行（后台常驻，不停业）")
rl.CIRCUIT_FILE.write_text("{ 坏掉的 json", encoding="utf-8")
check("坏文件时写闸放行", rl.gate_write("acc1")[0] is True)
check("坏文件时读查询放行", rl.circuit_blocked("acc1") == (False, 0))
fresh()

# ---------------------------------------------------------------- 6. 接线
section("接线：发货逐卡、确认发货、发布/擦亮/评价")
src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")
check("补发逐卡过闸", src.count("gate_write(self.cookie_id)") >= 2,
      str(src.count("gate_write(self.cookie_id)")))
check("确认发货过闸", "from rate_limiter import gate_write" in src, "import 存在")
i_card = src.find("# 半开：到期后每 30 秒只放行一张卡当探针")
check("逐卡放行不是整批一次", i_card > 0, str(i_card))

rl_src = (BACKEND / "rate_limiter.py").read_text(encoding="utf-8")
i_acq = rl_src.find("def acquire(")
seg = rl_src[i_acq:i_acq + 2200]
check("acquire 的写 scope 走同一套半开判定（_gate_eval）", "_gate_eval(" in seg, seg[:80])
check("领探针在窗口与日上限之后（_gate_commit 晚于 DAILY_CAP 检查）",
       0 <= seg.find("DAILY_CAP") < seg.find("_gate_commit("),
      f"daily={seg.find('DAILY_CAP')} commit={seg.find('_gate_commit(')}")
check("acquire 不再是先调 gate_write 再查配额", "gate_write(key)" not in seg, seg[:80])
check("gate_write 已导出", '"gate_write"' in rl_src.split("__all__")[-1])
check("状态含 half_open", "half_open" in rl_src)

care_src = (BACKEND / "care_message.py").read_text(encoding="utf-8")
check("评价/小红花按账号取写配额（不再共用 care 这个 key）",
      care_src.count('acquire("write", cookie_id or "care")') == 2,
      str(care_src.count('acquire("write", cookie_id or "care")')))

i_conf = src.find("async def _confirm_platform_shipped")
seg_conf = src[i_conf:i_conf + 2600]
i_cool = seg_conf.find("order_confirm_cooldown")
i_gate = seg_conf.find("gate_write(self.cookie_id)")
check("确认发货先判冷却再过闸（冷却跳过不占探针）",
      0 < i_cool < i_gate, f"cooldown={i_cool} gate={i_gate}")

# ---------------------------------------------------------------- 7. 持续流量
# 这一节开始换成可控时钟：前面几节靠改文件里的时间戳模拟"到期"，够了；
# 这里要模拟"每 30 秒来一笔、连续 6 分钟"，必须能精确推时间。
section("持续流量：每 30 秒一笔且不再撞墙，满 5 分钟必须退出半开")


class _FakeTime:
    def __init__(self, t: float):
        self.t = t

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, float(s))

    def advance(self, s: float) -> None:
        self.t += float(s)


clock = _FakeTime(time.time())
rl.time = clock


def fake_expire(key: str) -> None:
    st = json.loads(rl.CIRCUIT_FILE.read_text(encoding="utf-8"))
    st.setdefault("accounts", {})[key] = clock.t - 1
    rl.CIRCUIT_FILE.write_text(json.dumps(st), encoding="utf-8")


def half_of(key: str = "acc1") -> dict:
    try:
        st = json.loads(rl.CIRCUIT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    v = (st.get("half") or {}).get(key)
    return v if isinstance(v, dict) else {}


fresh()
rl.trip_circuit("acc1", reason="风控")
fake_expire("acc1")
t0 = clock.t
allowed, presence = [], []
for _ in range(11):  # t0, +30, ..., +300
    ok, _retry = rl.gate_write("acc1")
    if ok:
        allowed.append(round(clock.t - t0))
    presence.append((round(clock.t - t0), "acc1" in (rl.circuit_status().get("half_open") or {})))
    clock.advance(30)

check("稳定流量下按 30 秒节奏逐笔放行", allowed == [30 * i for i in range(11)], str(allowed))
check("第 270 秒时仍是半开", dict(presence)[270] is True, str(dict(presence)))
check("满 300 秒退出半开（不再被放行刷新计时）", dict(presence)[300] is False,
      str(dict(presence)))
check("退出后连续三笔全放行", [rl.gate_write("acc1")[0] for _ in range(3)] == [True, True, True])

# ---------------------------------------------------------------- 8. 配额
section("本地配额拒绝：不能吃掉探针")
fresh()
rl.trip_circuit("acc1", reason="风控")
fake_expire("acc1")
rl._mem.setdefault("publish", {})["acc1"] = [clock.t - 1, clock.t - 1]
ok, retry = rl.acquire("publish", "acc1")
check("窗口满时照样被拒", ok is False and retry > 0, f"{ok}/{retry}")
check("被窗口拒绝后没有记探针", not half_of().get("probe_at"), str(half_of()))

fresh()
rl.trip_circuit("acc1", reason="风控")
fake_expire("acc1")
rl._daily["publish:acc1"] = {"day": date.today().isoformat(), "count": rl.DAILY_CAP}
ok, retry = rl.acquire("publish", "acc1")
check("日上限到了被拒", ok is False and retry == 3600, f"{ok}/{retry}")
check("被日上限拒绝后也没有记探针", not half_of().get("probe_at"), str(half_of()))

clock.advance(301)
rl._daily.pop("publish:acc1", None)
ok = rl.gate_write("acc1")[0]
check("零写入过 301 秒：只是取一次探针", ok is True)
check("零写入不能判成恢复（半开仍在）", bool(half_of()), str(half_of()))
check("恢复计时从那笔真实写开始", float(half_of().get("opened_at") or 0) == clock.t,
      str(half_of()))
clock.advance(301)
check("真实探针之后安静 5 分钟才算恢复",
      rl.gate_write("acc1")[0] is True and not half_of(), str(half_of()))

section("旧状态兼容 / 重载")
fresh()
rl.trip_circuit("acc1", reason="风控")
fake_expire("acc1")
st = json.loads(rl.CIRCUIT_FILE.read_text(encoding="utf-8"))
st["half"]["acc1"]["probe_at"] = clock.t - 301
st["half"]["acc1"].pop("opened_at", None)  # 上一版留下的记录，没有 opened_at
rl.CIRCUIT_FILE.write_text(json.dumps(st), encoding="utf-8")
check("只有 probe_at 的旧记录也能结案（不卡死）",
      rl.gate_write("acc1")[0] is True and not half_of(), str(half_of()))

fresh()
rl.trip_circuit("acc1", reason="风控")
fake_expire("acc1")
rl.gate_write("acc1")  # 领探针
rl._mem.clear()
rl._daily.clear()
rl._loaded = False     # 模拟进程重启：内存全丢，只剩两个状态文件
check("重启后仍按半开拦住", rl.gate_write("acc1")[0] is False)
clock.advance(31)
check("重启后过了间隔照常放行", rl.gate_write("acc1")[0] is True)

section("账号隔离：一个账号半开不影响另一个")
fresh()
rl.trip_circuit("acc1", reason="风控")
fake_expire("acc1")
check("acc1 半开：第一笔放、第二笔排队",
      rl.gate_write("acc1")[0] is True and rl.gate_write("acc1")[0] is False)
check("acc2 不受影响，连续三笔全放行",
      [rl.gate_write("acc2")[0] for _ in range(3)] == [True, True, True])

section("并发：同一时刻只放一笔")
import threading  # noqa: E402

fresh()
rl.trip_circuit("acc1", reason="风控")
fake_expire("acc1")
res: list[bool] = []
barrier = threading.Barrier(6)


def _hit() -> None:
    barrier.wait()
    res.append(rl.acquire("media", "acc1")[0])


ths = [threading.Thread(target=_hit) for _ in range(6)]
for t in ths:
    t.start()
for t in ths:
    t.join()
check("6 个并发写只放行 1 笔", res.count(True) == 1, str(res))
check("被拦的 5 笔没有扣掉 media 配额",
      rl.status()["scopes"].get("media", {}).get("acc1", {}).get("used") == 1,
      str(rl.status()["scopes"].get("media")))

print()
if FAILS:
    print(f"FAILED {len(FAILS)}：")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print(f"全部通过（{PASSES} 项）")
