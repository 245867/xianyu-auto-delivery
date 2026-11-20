"""
诊断通知覆盖（底座项）
=======================
以前只有三个事件桶：`polish / risk / publish`。于是有两类**沉默的故障**：

- 账号掉线：页面上的灯是灰的，可没人会一直盯页面，以为还在自动发货；
- 定时诊断：`auto_scan` 到点跑完写进库里，出了"全店没浏览"也不说一声。

本测试锁四件事：

1. 新增 `account` / `diagnosis` 两个事件桶，老配置文件读进来不会丢这两个开关；
2. 账号通知：掉线推一条、同状态窗口内不重复、恢复只在报过掉线之后才推；
3. 诊断通知：**只在高危信号时推**，中低信号不打扰；
4. 接线：掉线挂在连接状态切换点、诊断挂在 `run()` 末尾、页面有开关。

全程离线：HTTP 用替身，不碰真实 webhook（也不读真实通知配置）。

跑法（在 backend 目录）：python3 tests/test_notify_coverage.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="notify_cov_")
os.environ.pop("NOTIFY_STATE_DEDUPE_SECONDS", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify_service as ns  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
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


# ---------------------------------------------------------------- 替身
ns.CONFIG_FILE = Path(_TMP) / "notify_config.json"
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


def write_cfg(events=None, enabled=True):
    cfg = {"enabled": enabled, "custom": "http://notify.invalid/hook",
           "events": events or {}}
    ns.CONFIG_FILE.write_text(json.dumps(cfg), encoding="utf-8")


write_cfg({})

# ---------------------------------------------------------------- 1 事件桶
section("1 五个事件桶，老配置不丢新开关")

check("EVENTS 里有 account", "account" in ns.EVENTS, str(ns.EVENTS))
check("EVENTS 里有 diagnosis", "diagnosis" in ns.EVENTS)
check("默认全开（含新增两个）",
      set(ns.DEFAULTS["events"]) == set(ns.EVENTS),
      str(ns.DEFAULTS["events"]))

write_cfg({"polish": False})
cfg = ns.get_config()
check("老配置关掉的桶保持关", cfg["events"]["polish"] is False)
check("老配置里没有的新桶默认开", cfg["events"]["account"] is True
      and cfg["events"]["diagnosis"] is True)
check("事件名写错的键被忽略", "not_an_event" not in ns.get_config()["events"])

saved = ns.save_config({"events": {"account": False}})
check("保存后 account 关、其他仍开",
      saved["events"]["account"] is False
      and saved["events"]["diagnosis"] is True
      and saved["events"]["risk"] is True)
write_cfg({})   # 后面的发送测试恢复成全开

# ---------------------------------------------------------------- 2 账号通知
section("2 账号掉线 / 恢复")

SENT.clear()
r = ns.notify_account_state("acc1", "failed", "连接被平台关闭")
check("发了一条", len(SENT) == 1 and r.get("skipped") is False, str(len(SENT)))
msg = SENT[0] if SENT else {}
check("事件桶是 account", msg.get("event") == "account", str(msg.get("event")))
check("标题写明掉线", "掉线" in str(msg.get("title")), str(msg.get("title")))
check("原因带进正文", "连接被平台关闭" in str(msg.get("content")))
check("正文告诉用户影响", "影响" in str(msg.get("content")))
check("级别是 warn", msg.get("level") == "warn", str(msg.get("level")))

SENT.clear()
r = ns.notify_account_state("acc1", "failed", "又掉了一次")
check("同一状态窗口内不重复推", r.get("skipped") is True and SENT == [], str(r.get("results")))

SENT.clear()
r = ns.notify_account_state("acc1", "recovered", "重连成功")
check("恢复会推一条", len(SENT) == 1 and "恢复" in str(SENT[0].get("title")),
      str(SENT[0].get("title")) if SENT else "")

SENT.clear()
r = ns.notify_account_state("acc2", "recovered", "正常重连")
check("没报过掉线的账号不发恢复（不制造噪音）",
      r.get("skipped") is True and SENT == [], str(r.get("results")))

SENT.clear()
r = ns.notify_account_state("acc1", "exploded")
check("未知状态直接拒绝", r.get("ok") is False and SENT == [], str(r.get("results")))

SENT.clear()
ns.notify_account_state("acc1", "paused", "操作员停用")
check("暂停也走同一个桶", SENT and SENT[0].get("event") == "account"
      and "暂停" in str(SENT[0].get("title")), str(SENT[:1]))

# ---------------------------------------------------------------- 3 诊断摘要
section("3 诊断摘要：只推高危")

low_report = {
    "ok": True, "cookie_id": "acc1", "at": "2026-11-14T09:00:00",
    "shop_signals": [
        {"code": "ops_dense_publish", "level": "mid", "title": "今天发布量偏大",
         "evidence": "今日已发布 16 条", "action": "剩下的明天再发"},
        {"code": "ops_dense_polish", "level": "low", "title": "擦亮偏多",
         "evidence": "今日擦亮 120 次", "action": "一天一次够用"},
    ],
    "actions": [{"action": "剩下的明天再发"}],
}
SENT.clear()
r = ns.notify_diagnosis_summary(low_report, "acc1")
check("只有中低信号：不推", r.get("skipped") is True and SENT == [], str(r.get("results")))

high_report = {
    "ok": True, "cookie_id": "acc1", "at": "2026-11-14T09:00:00",
    "shop_signals": [
        {"code": "account_wide", "level": "high", "title": "全店普遍没浏览",
         "evidence": "9/12 件商品浏览 <50（75%）", "action": "先看账号违规记录"},
        {"code": "recent_unknown", "level": "high", "title": "近期有结果未知的发布",
         "evidence": "最近记录里 2 条 submission_unknown", "action": "去我发布的核对"},
        {"code": "ops_dense_publish", "level": "mid", "title": "今天发布量偏大",
         "evidence": "今日已发布 16 条", "action": "剩下的明天再发"},
    ],
    "actions": [{"action": "先看账号违规记录", "level": "high"},
                {"action": "去我发布的核对", "level": "high"}],
}
SENT.clear()
r = ns.notify_diagnosis_summary(high_report, "acc1")
check("有高危：推一条", len(SENT) == 1 and r.get("skipped") is False)
msg = SENT[0] if SENT else {}
check("事件桶是 diagnosis", msg.get("event") == "diagnosis")
check("标题给出高危条数", "2 条高危" in str(msg.get("title")), str(msg.get("title")))
check("正文带证据", "9/12 件商品浏览" in str(msg.get("content")))
check("正文带该做的事", "先看账号违规记录" in str(msg.get("content")))
check("中低信号不进正文", "今天发布量偏大" not in str(msg.get("content")))

# ---------------------------------------------------------------- 4 开关
section("4 关掉开关就不发")

write_cfg({"account": False, "diagnosis": False})
SENT.clear()
r = ns.notify_account_state("acc9", "failed", "x")
check("account 关：跳过", r.get("skipped") is True and SENT == [], str(r.get("results")))
r = ns.notify_diagnosis_summary(high_report, "acc1")
check("diagnosis 关：跳过", r.get("skipped") is True and SENT == [], str(r.get("results")))
write_cfg({})

# ---------------------------------------------------------------- 5 接线
section("5 接线")


def wired(path: Path, needles: list[str]) -> tuple[bool, str]:
    src = path.read_text(encoding="utf-8")
    miss = [n for n in needles if n not in src]
    return (not miss), ",".join(miss)


ok, miss = wired(BACKEND / "XianyuAutoAsync.py",
                 ['notify_account_state(self.cookie_id, "failed"',
                  'notify_account_state(self.cookie_id, "recovered"'])
check("掉线挂在连接状态切换点", ok, miss)
ok, miss = wired(BACKEND / "cookie_manager.py",
                 ['notify_account_state(cookie_id, "resumed" if enabled else "paused"'])
check("暂停/恢复挂在 update_cookie_status", ok, miss)
ok, miss = wired(BACKEND / "shop_diagnosis.py",
                 ["_save_run(cookie_id, True, len(items), report)",
                  "notify_diagnosis_summary(report, cookie_id)"])
check("诊断挂在 run() 末尾", ok, miss)

shop_src = (BACKEND / "shop_diagnosis.py").read_text(encoding="utf-8")
check("诊断通知在落库之后", shop_src.find("_save_run(cookie_id, True")
      < shop_src.find("notify_diagnosis_summary(report, cookie_id)"))

html = (BACKEND.parent / "static" / "notify.html").read_text(encoding="utf-8")
check("页面有账号状态开关", 'id="ev-account"' in html)
check("页面有诊断开关", 'id="ev-diagnosis"' in html)
check("保存时带上两个新桶", "account: $('ev-account').checked" in html
      and "diagnosis: $('ev-diagnosis').checked" in html)
check("读取时兼容没配过的配置", "ev.account !== false" in html and "ev.diagnosis !== false" in html)

print("\n" + "=" * 56)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print(f"✅ 全部通过（{PASSES} 项）")