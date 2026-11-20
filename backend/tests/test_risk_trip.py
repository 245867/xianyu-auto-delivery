"""
风控观测统一出口（底座项）
===========================
拆解 20 的结论：撞风控只会停本轮，十分钟后定时任务醒来照样往同一个账号上写，
等于每隔十分钟去撞一次墙。底座里已经有"熔断落盘"（`rate_limiter.trip_circuit`），
但入口不齐、而且熔断之后**没人告诉用户**：

- 擦亮：只数失败条数，跑完整轮才凭"失败率过半"猜一句警告，期间继续挨个撞；
- 评价/小红花、发布：熔断了，但只在日志里留一行，页面上灯还是亮的。

本测试锁五件事：

1. 擦亮单条收到平台风控文本 → 立刻熔断该账号、中止本轮，不再试剩下的商品；
2. 普通失败（商品不存在、超时）**不**熔断——分不清就别把整店停摆；
3. 熔断挡住所有写操作（发布/发货/擦亮），读操作照常；
4. 熔断时推一条 `risk` 通知，同一熔断窗口内不重复推；
5. 三处熔断点都接到同一个通知出口；限流中止的提示不再被覆盖。

全程离线：HTTP、DB、睡眠都用替身，不读写真实 data 目录。

跑法（在 backend 目录）：python3 tests/test_risk_trip.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="risk_trip_"))
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import rate_limiter as rl  # noqa: E402

rl.STATE_FILE = _TMP / "rate_state.json"
rl.CIRCUIT_FILE = _TMP / "circuit.json"
rl.reset()
rl.reset_circuit()

import notify_service as ns  # noqa: E402

ns.CONFIG_FILE = _TMP / "notify_config.json"
ns.CONFIG_FILE.write_text(json.dumps(
    {"enabled": True, "custom": "http://notify.invalid/hook", "events": {}},
    ensure_ascii=False), encoding="utf-8")

SENT: list[dict] = []


class _Resp:
    status_code = 200
    text = "ok"


def _fake_post(url, json=None, timeout=None):
    SENT.append(dict(json or {}))
    return _Resp()


def _fake_get(url, timeout=None):
    SENT.append({"url": url})
    return _Resp()


ns.requests.post = _fake_post
ns.requests.get = _fake_get

# 假 DB：在导入 polish_service 之前塞进 sys.modules，避免碰到真实 data/xianyu.db
fake_db = types.ModuleType("db_manager")
ITEMS = {"acc1": [{"item_id": f"i{i}"} for i in range(5)]}


class _DB:
    def get_items_by_cookie(self, cid):
        return ITEMS.get(cid) or []

    def get_all_cookies(self):
        return {}


fake_db.db_manager = _DB()
sys.modules["db_manager"] = fake_db

import polish_service as ps  # noqa: E402

ps._resolve_cookies = lambda: {"acc1": "unb=1; _m_h5_tk=abcdef_1; cookie2=x"}
ps._append_log = lambda entry: None
ps._update_state = lambda **kv: None
rl.jitter = lambda a, b: 0.0   # 逐商品 8–15 秒在测试里没有意义

PASSES = 0
FAILS: list[str] = []


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


def events(name: str) -> list[dict]:
    return [m for m in SENT if m.get("event") == name]


# ---------------------------------------------------------------- 1 识别
section("1 哪些失败算风控")

cases = [
    ("RGV587_ERROR::SM 哎哟喂,被挤爆啦", True),
    ("USER_VALIDATE 请进行滑块验证", True),
    ("FAIL_SYS_PUNISH 账号被处罚", True),
    ("IDLEITEM_NOT_FOUND 商品不存在", False),
    ("擦亮异常: HTTPSConnectionPool read timed out", False),
    ("响应非JSON: <html>502</html>", False),
    ("", False),
]
for text, want in cases:
    got, _ = ps._is_risk_failure(text)
    check(f"{'风控' if want else '非风控'}：{text[:26] or '（空）'}", got is want, f"got={got}")


# ---------------------------------------------------------------- 2 擦亮撞墙即停
section("2 擦亮单条撞风控：熔断 + 中止本轮")

rl.reset()
rl.reset_circuit()
rl.RULES["polish"] = (50, 8)     # 让配额不成为中止原因
CALLS: list[str] = []


def _risk_script(cookie_str, item_id, app_key, retry_count=0):
    CALLS.append(item_id)
    return {"success": False, "message": "RGV587_ERROR::SM 哎哟喂,被挤爆啦",
            "cookie_str": cookie_str}


ps.polish_item = _risk_script
SENT.clear()
summary = ps.run_polish_batch("acc1")
acc = (summary.get("accounts") or {}).get("acc1") or {}

check("只试了第一条就停", len(CALLS) == 1, f"calls={len(CALLS)}")
check("账户摘要标记 risk", acc.get("risk") is True, str(acc))
check("摘要写明熔断", "熔断" in str(acc.get("note")), str(acc.get("note")))
check("失败计数仍然记着", acc.get("fail") == 1, str(acc.get("fail")))

blocked, remain = rl.circuit_blocked("acc1")
check("熔断已落盘", blocked is True and remain > 0, f"{blocked}/{remain}s")
check("熔断原因带擦亮", "擦亮" in str(rl.circuit_status().get("reason")),
      str(rl.circuit_status().get("reason")))

risk_msgs = events("risk")
check("推了一条 risk 通知", len(risk_msgs) == 1, str(len(risk_msgs)))
msg = risk_msgs[0] if risk_msgs else {}
check("标题写明账号触发风控", "触发平台风控" in str(msg.get("title")), str(msg.get("title")))
check("正文说明影响", "影响" in str(msg.get("content")))
check("正文给出停手建议", "别连续重试" in str(msg.get("content")))
check("级别 warn", msg.get("level") == "warn", str(msg.get("level")))
check("擦亮批次摘要照常推", len(events("polish")) == 1, str(len(events("polish"))))

# ---------------------------------------------------------------- 3 去重
section("3 同一熔断窗口内不重复推")

SENT.clear()
r = ns.notify_risk_trip("acc1", "发货", "同样撞墙", 600)
check("第二次被去重", r.get("skipped") is True and events("risk") == [], str(r.get("results")))
r = ns.notify_risk_trip("acc2", "发货", "另一个号", 600)
check("换一个号照常推", r.get("skipped") is False and len(events("risk")) == 1,
      str(r.get("results")))

# ---------------------------------------------------------------- 4 熔断范围
section("4 熔断挡住写、不挡读")

ok_w, _ = rl.acquire("publish", "acc1")
check("发布被挡", ok_w is False)
ok_w, _ = rl.acquire("delivery", "acc1")
check("发货被挡", ok_w is False)
ok_w, _ = rl.acquire("polish", "acc1")
check("擦亮被挡", ok_w is False)
ok_r, _ = rl.acquire("search", "acc1")
check("读（搜索）照常", ok_r is True)
ok_r, _ = rl.acquire("detail", "acc1")
check("读（详情）照常", ok_r is True)

# ---------------------------------------------------------------- 5 普通失败不熔断
section("5 普通失败不熔断（不误伤）")

rl.reset()
rl.reset_circuit()
rl.RULES["polish"] = (50, 8)
CALLS.clear()


def _biz_fail(cookie_str, item_id, app_key, retry_count=0):
    CALLS.append(item_id)
    return {"success": False, "message": "IDLEITEM_NOT_FOUND 商品不存在",
            "cookie_str": cookie_str}


ps.polish_item = _biz_fail
SENT.clear()
summary = ps.run_polish_batch("acc1")
acc = (summary.get("accounts") or {}).get("acc1") or {}

check("五个商品都试过", len(CALLS) == 5, f"calls={len(CALLS)}")
check("没有熔断", rl.circuit_blocked("acc1")[0] is False)
check("没有 risk 通知", events("risk") == [], str(len(events("risk"))))
check("仍然给失败率告警", "失败率" in str(acc.get("warning")), str(acc.get("warning")))
check("没有标 risk", acc.get("risk") is None, str(acc.get("risk")))

# ---------------------------------------------------------------- 6 限流提示
section("6 限流中止的提示不再被覆盖")

rl.reset()
rl.reset_circuit()
rl.RULES["polish"] = (1, 8)
CALLS.clear()


def _ok(cookie_str, item_id, app_key, retry_count=0):
    CALLS.append(item_id)
    return {"success": True, "message": "擦亮成功", "cookie_str": cookie_str}


ps.polish_item = _ok
SENT.clear()
summary = ps.run_polish_batch("acc1")
acc = (summary.get("accounts") or {}).get("acc1") or {}
check("配额只放行第一条", len(CALLS) == 1, f"calls={len(CALLS)}")
check("摘要保留限流说明", "触发限流" in str(acc.get("note")), str(acc.get("note")))
check("成功数照记", acc.get("success") == 1, str(acc.get("success")))

# ---------------------------------------------------------------- 7 接线
section("7 三处熔断点接到同一出口")


def wired(path: Path, needles: list[str]) -> tuple[bool, str]:
    src = path.read_text(encoding="utf-8")
    miss = [n for n in needles if n not in src]
    return (not miss), ",".join(miss)


ok, miss = wired(BACKEND / "polish_service.py",
                 ["_is_risk_failure", "rate_limiter.trip_circuit", "notify_risk_trip"])
check("擦亮：识别 → 熔断 → 通知", ok, miss)
ok, miss = wired(BACKEND / "care_message.py",
                 ["trip_circuit", "notify_risk_trip"])
check("评价/小红花：熔断 → 通知", ok, miss)
ok, miss = wired(BACKEND / "opportunity_publisher.py",
                 ["trip_circuit", "notify_risk_trip"])
check("发布：熔断 → 通知", ok, miss)
check("通知函数在 __all__ 里", "notify_risk_trip" in ns.__all__)

print("\n" + "=" * 56)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print(f"✅ 全部通过（{PASSES} 项）")