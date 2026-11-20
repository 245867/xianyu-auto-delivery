"""
收货提醒只读预演回归测试（P1-3）
================================
锁死：

1. 只对"已发货且超过等待时间"的单出候选；
2. 已完成/退款/取消/状态不明/未发货 → 跳过，且原因可读；
3. 缺会话编号默认跳过（可配置列出）；
4. 按订单去重：已提醒到上限就不再出现，没到上限仍出现并带已提醒次数；
5. 预演不写去重账、永远不开真发（send_enabled=False / dry_run=True）；
6. 状态变化后重扫：已完成的单即使之前提醒过也跳过。

跑法（在 backend 目录）：python3 tests/test_receipt_reminder.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="receipt_reminder_")
os.environ["RECEIPT_REMINDER_DB"] = str(Path(_TMP) / "reminder.db")
os.environ["RECEIPT_REMINDER_CONFIG"] = str(Path(_TMP) / "reminder.json")

import receipt_reminder as rr  # noqa: E402

FAILS: list[str] = []
PASSES = 0

NOW = datetime(2026, 11, 13, 12, 0, 0)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ok   {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def section(title: str) -> None:
    print(f"\n[{title}]")


def order(oid, status, updated, cid="shopA", **kw):
    return {"order_id": oid, "cookie_id": cid, "status": status,
            "updated_at": updated, "item_id": "i-" + oid,
            "buyer_id": "b-" + oid, "amount": "9.9", "quantity": "1", **kw}


CHATS = {"o1": "chat-1", "o2": "chat-2", "o3": "chat-3", "o4": "chat-4", "o5": "chat-5"}


def scan(orders, chats=None):
    return rr.scan(orders=orders, chat_ids=CHATS if chats is None else chats, now=NOW)


section("1 候选与时间门槛")
res = scan([
    order("o1", "shipped", "2026-11-11 08:00:00"),   # 52h 前 → 候选
    order("o2", "shipped", "2026-11-13 10:00:00"),   # 2h 前 → 跳过
])
ids = {c["order_id"] for c in res["candidates"]}
skipped = {s["order_id"]: s["reason"] for s in res["skipped"]}
check("超时的在候选里", "o1" in ids)
check("未到时间的不在候选里", "o2" not in ids)
check("未到时间给原因", "还没到提醒时间" in skipped.get("o2", ""))
check("候选带等待小时", res["candidates"][0]["waited_hours"] >= 24)
check("dry_run=True", res["dry_run"] is True)
check("send_enabled=False", res["send_enabled"] is False)
check("有发送闸说明", "只预演" in res["send_block_reason"])

section("2 排除态")
res = scan([
    order("o3", "completed", "2026-11-10 08:00:00"),
    order("o4", "refunding", "2026-11-10 08:00:00"),
    order("o5", "unknown", "2026-11-10 08:00:00"),
    order("o6", "cancelled", "2026-11-10 08:00:00"),
    order("o7", "pending_ship", "2026-11-10 08:00:00"),
])
sk = {s["order_id"]: s["reason"] for s in res["skipped"]}
check("已完成为跳过", "已确认收货" in sk.get("o3", ""))
check("退款中跳过", "退款中" in sk.get("o4", ""))
check("状态不明跳过", "状态不明" in sk.get("o5", ""))
check("取消跳过", "取消" in sk.get("o6", ""))
check("未发货跳过", "尚未发货" in sk.get("o7", ""))
check("一个候选都没有", res["candidates"] == [])

section("3 缺会话编号")
res = scan([order("o8", "shipped", "2026-11-10 08:00:00")], chats={"o8": ""})
check("默认跳过", any(s["order_id"] == "o8" and "缺少会话编号" in s["reason"]
                  for s in res["skipped"]))
rr.save_config({"include_missing_chat": True})
res = scan([order("o8", "shipped", "2026-11-10 08:00:00")], chats={"o8": ""})
check("配置后可列出", any(c["order_id"] == "o8" for c in res["candidates"]))
check("候选 chat_id 为空", [c for c in res["candidates"] if c["order_id"] == "o8"][0]["chat_id"] == "")
rr.save_config({"include_missing_chat": False})

section("4 按订单去重")
orders = [order("o1", "shipped", "2026-11-10 08:00:00")]
check("首次是候选", any(c["order_id"] == "o1" for c in scan(orders)["candidates"]))
rr.mark_reminded("o1", "shopA", "预演后手工记录一次")
res = scan(orders)
check("到上限后不再候选", not any(c["order_id"] == "o1" for c in res["candidates"]))
check("给出去重原因", any(s["order_id"] == "o1" and "已提醒过" in s["reason"]
                    for s in res["skipped"]))
rr.save_config({"max_per_order": 2})
res = scan(orders)
check("上限放宽后又出现", any(c["order_id"] == "o1" for c in res["candidates"]))
check("带已提醒次数", [c for c in res["candidates"] if c["order_id"] == "o1"][0]["reminded_before"] == 1)
rr.save_config({"max_per_order": 1})

section("5 预演不写账")
before = len(rr.recent(limit=1000))
scan([order("o2", "shipped", "2026-11-10 08:00:00")])
after = len(rr.recent(limit=1000))
check("扫描前后去重账行数不变", before == after)

section("6 状态变化后重扫")
rr.mark_reminded("o2", "shopA", "提醒过一次")
res = scan([order("o2", "completed", "2026-11-13 11:00:00")])
check("确认收货后即使提醒过也跳过", any(s["order_id"] == "o2" for s in res["skipped"]))
check("不再出现在候选", not any(c["order_id"] == "o2" for c in res["candidates"]))

section("7 账号暂停 / 离线 / 熔断（拆解 39 §八-4）")
_saved = {k: sys.modules.get(k) for k in ("cookie_manager", "rate_limiter")}


def patch_account(mgr=None, circuit=None):
    """把真模块换掉：这里只验判定顺序和文案，不碰真实连接。"""
    import types
    if mgr is not None:
        cm_mod = types.ModuleType("cookie_manager")
        cm_mod.manager = mgr
        sys.modules["cookie_manager"] = cm_mod
    else:
        sys.modules.pop("cookie_manager", None)
    rl = types.ModuleType("rate_limiter")
    rl.circuit_blocked = lambda cid: circuit or (False, 0)
    sys.modules["rate_limiter"] = rl


class _Mgr:
    def __init__(self, status=None, instances=None):
        self.cookie_status = {"shopA": True} if status is None else status
        self.instances = instances


class _Inst:
    def __init__(self, state):
        self.connection_state = state


try:
    res = rr.scan(orders=[order("o9", "shipped", "2026-11-10 08:00:00")],
                  chat_ids={"o9": "chat-9"}, now=NOW,
                  account_states={"shopA": "账号已暂停（后台已关闭该账号）"})
    sk = {s["order_id"]: s["reason"] for s in res["skipped"]}
    check("注入暂停态：不进候选", res["candidates"] == [])
    check("注入暂停态：原因可读", "账号已暂停" in sk.get("o9", ""))

    res = rr.scan(orders=[order("o9", "shipped", "2026-11-10 08:00:00")],
                  chat_ids={"o9": "chat-9"}, now=NOW, account_states={})
    check("显式空表 = 明确不做账号检查", any(c["order_id"] == "o9" for c in res["candidates"]))

    patch_account(_Mgr(status={"shopA": False}))
    check("后台关掉该账号 → 暂停", "账号已暂停" in (rr._account_state("shopA") or ""))

    patch_account(_Mgr(instances={"other": _Inst("connected")}))
    check("没有这个账号的连接 → 离线", "没有运行中的连接" in (rr._account_state("shopA") or ""))

    patch_account(_Mgr(instances={}))
    check("实例表为空时不臆断（返回 None）", rr._account_state("shopA") is None)

    patch_account(_Mgr(instances={"shopA": _Inst("disconnected")}))
    check("连接已断开 → 离线", "连接状态 disconnected" in (rr._account_state("shopA") or ""))

    patch_account(_Mgr(instances={"shopA": _Inst("failed")}))
    check("连接失败 → 等重连", "连接失败" in (rr._account_state("shopA") or ""))

    patch_account(_Mgr(instances={"shopA": _Inst("reconnecting")}))
    check("重连中 → 跳过", "重连中" in (rr._account_state("shopA") or ""))

    patch_account(_Mgr(instances={"shopA": _Inst("connected")}))
    check("连接正常 → 可以提醒", rr._account_state("shopA") is None)

    patch_account(_Mgr(instances={"shopA": _Inst("connected")}), circuit=(True, 600))
    check("熔断中 → 跳过（带剩余分钟）", "熔断中" in (rr._account_state("shopA") or "")
          and "10" in (rr._account_state("shopA") or ""))

    check("空账号不做判定", rr._account_state("") is None)

    patch_account(_Mgr(instances={"shopA": _Inst("connected")}), circuit=None)
    res = rr.scan(orders=[order("o10", "shipped", "2026-11-10 08:00:00")],
                  chat_ids={"o10": "chat-10"}, now=NOW)
    sk = {s["order_id"]: s["reason"] for s in res["skipped"]}
    check("默认路径真的会调 _account_state（连接表空 → 不误伤）",
          any(c["order_id"] == "o10" for c in res["candidates"]), str(sk))
finally:
    for _k, _v in _saved.items():
        if _v is None:
            sys.modules.pop(_k, None)
        else:
            sys.modules[_k] = _v

# ---------------------------------------------------------------- 8 关闭边界
section("8 关闭边界：预演与执行入口都不发送")

import asyncio                              # noqa: E402
import types                                # noqa: E402

check("模块常量 SEND_ENABLED 是 False", rr.SEND_ENABLED is False)

# 预演：给一单该提醒的，跑真实 scan
rr._load_orders = lambda cookie_id="", limit=1000: [
    order("o80", "shipped", "2026-11-10 08:00:00")]
rr._get_chat_id = lambda oid: "chat-80"
rr._account_state = lambda cid: None

ledger_before = len(rr.recent(limit=1000))
pv = rr.scan(now=NOW)
check("预演 ok", pv.get("ok") is True, str(pv)[:80])
check("预演声明 dry_run", pv.get("dry_run") is True)
check("预演声明 send_enabled=False", pv.get("send_enabled") is False)
check("预演给出不能发的原因", bool(pv.get("send_block_reason")), str(pv.get("send_block_reason")))
check("候选动作只有 preview_only",
      all(c.get("action") == "preview_only" for c in pv.get("candidates") or []),
      str([c.get("action") for c in pv.get("candidates") or []]))

# 真发入口：直接调 FastAPI 的处理函数（不启服务、不发请求）
_saved_db = sys.modules.get("db_manager")
_m = types.ModuleType("db_manager")
_m.db_manager = type("D", (), {})()
sys.modules["db_manager"] = _m
try:
    import care_api   # noqa: E402
finally:
    if _saved_db is None:
        sys.modules.pop("db_manager", None)
    else:
        sys.modules["db_manager"] = _saved_db

# 去重账只有 mark_reminded 会写；把它换成计数器，证明两个入口都没碰它
_written = []
_real_mark = rr.mark_reminded
rr.mark_reminded = lambda *a, **k: (_written.append(a), True)[1]
try:
    pv2 = asyncio.run(care_api.reminder_preview(cookie_id="shopA", limit=1000))
    run2 = asyncio.run(care_api.reminder_run({"cookie_id": "shopA"}))
finally:
    rr.mark_reminded = _real_mark

check("预演接口 ok", pv2.get("ok") is True, str(pv2)[:80])
check("执行入口明确拒绝（ok=False）", run2.get("ok") is False, str(run2)[:120])
check("拒绝理由说清了「尚未启用」", "尚未启用" in str(run2.get("error")), str(run2.get("error")))
check("执行入口只回预演结果", (run2.get("preview") or {}).get("dry_run") is True)
check("两个入口都没有写去重账", _written == [], str(_written))
check("去重账行数没变", len(rr.recent(limit=1000)) == ledger_before,
      f"{ledger_before} → {len(rr.recent(limit=1000))}")

# 源码层：这两个模块里根本没有发送原语，也没有第二个写账入口
rr_src = Path(rr.__file__).read_text(encoding="utf-8")
for tok in ("send_msg", ".send(", "def _send", "sender"):
    check(f"receipt_reminder 无发送原语 {tok!r}", tok not in rr_src)
ca_src = Path(care_api.__file__).read_text(encoding="utf-8")
i_run = ca_src.find("async def reminder_run")
i_next = ca_src.find("@router", i_run + 10)
block = ca_src[i_run:i_next if i_next > 0 else None]
check("reminder_run 只有 scan + 拒绝", "receipt_reminder.scan(" in block and
      '"ok": False' in block, block[:120])
check("reminder_run 里没有任何发送调用",
      not any(tok in block for tok in ("send_msg", ".send(", "sender")))
prod_hits = [p.name for p in BACKEND.glob("*.py")
             if p.name != "receipt_reminder.py"
             and "mark_reminded" in p.read_text(encoding="utf-8", errors="ignore")]
check("mark_reminded 在生产代码里没有调用点（只有它自己定义）",
      prod_hits == [], str(prod_hits))

print("\n" + "=" * 56)
if FAILS:
    print(f"❌ {len(FAILS)} 条失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print(f"✅ 全部通过（{PASSES} 项）")