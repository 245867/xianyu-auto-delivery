"""
后台任务停滞检测（L2-2）
=========================
锁五件事：

1. 登记/心跳/注销的基本语义：没登记的名字打心跳不会凭空造出任务；
2. 停滞判定：超过 `max(90秒, 节奏×2.5)` 才算停滞，节奏配置变了阈值跟着变；
3. 通知：新停滞推一条、不重复推、6 小时后重复提醒、恢复推一条；
4. 任务正常退出（注销）不会留下"恢复"通知；
5. 接线：六个调度循环 + 账号心跳都真的 register/beat，页面和接口能读到。

全程离线：`notify_service` 用替身替掉，绝不会真发 webhook。

跑法（在 backend 目录）：python3 tests/test_task_watchdog.py
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from pathlib import Path

os.environ["TASK_WATCHDOG_DISABLED"] = "1"  # 本节不启线程，避免干扰

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import task_watchdog as tw  # noqa: E402

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


# ---------------------------------------------------------------- 通知替身
class _NotifyStub:
    SENT: list[dict] = []

    @staticmethod
    def send_event(event, title, content, level="info", force=False):
        _NotifyStub.SENT.append({"event": event, "title": title,
                                 "content": content, "level": level})
        return {"ok": True, "skipped": False, "results": {"wecom": "ok"}, "attempts": {"wecom": 1}}


sys.modules["notify_service"] = _NotifyStub  # type: ignore[assignment]


def reset():
    tw._reset_for_tests()
    _NotifyStub.SENT.clear()


# ---------------------------------------------------------------- 1. 基本语义
section("登记与心跳")
reset()
tw.beat("没登记的任务")
check("未登记的名字打心跳不造任务", tw.snapshot() == [], str(tw.snapshot()))

tw.register("demo", 10, "示例任务")
rows = tw.snapshot()
check("登记后能看到", len(rows) == 1 and rows[0]["name"] == "demo", str(rows))
check("状态正常", rows[0]["state"] == "ok", rows[0]["state"])
check("说明文字保留", rows[0]["note"] == "示例任务", rows[0]["note"])

t0 = tw._TASKS["demo"]["registered_at"]
check("10 秒节奏的下限是 90 秒", tw.scan(t0 + 89) == [], str(tw.scan(t0 + 89)))
stalled = tw.scan(t0 + 95)
check("超过 90 秒判停滞", len(stalled) == 1, str(stalled))
check("从未心跳有标记", stalled and stalled[0]["never_beaten"] is True, str(stalled))

tw.beat("demo")
beat_at = tw._TASKS["demo"]["last"]
check("心跳后不再停滞", tw.scan(beat_at + 50) == [], str(tw.scan(beat_at + 50)))
check("心跳确实更新了时间", beat_at > 0, str(beat_at))

tw.register("hourly", 3600, "每小时任务")
h0 = tw._TASKS["hourly"]["registered_at"]
check("1 小时节奏：40 分钟不算停滞",
      all(x["name"] != "hourly" for x in tw.scan(h0 + 2400)), str(tw.scan(h0 + 2400)))
check("1 小时节奏：2.6 小时算停滞",
      any(x["name"] == "hourly" for x in tw.scan(h0 + 9360)),
      str(tw.scan(h0 + 9360)))

section("重新登记与注销")
tw.register("demo", 30, "示例任务（改节奏）")
check("改节奏不影响已登记心跳", tw._TASKS["demo"]["interval"] == 30, str(tw._TASKS["demo"]))
tw.unregister("demo")
check("注销后消失", all(r["name"] != "demo" for r in tw.snapshot()), str(tw.snapshot()))

# ---------------------------------------------------------------- 2. 通知流程
section("停滞通知：推一次、重复提醒、恢复")
reset()
tw.register("job", 10, "测试任务")
j0 = tw._TASKS["job"]["registered_at"]

ev = tw.check_and_notify(j0 + 100)
check("新停滞推 1 条", len(ev) == 1 and ev[0]["action"] == "stall", str(ev))
check("走 account 桶", _NotifyStub.SENT and _NotifyStub.SENT[0]["event"] == "account",
      str(_NotifyStub.SENT[:1]))
check("标题点明任务", "job" in _NotifyStub.SENT[0]["title"], _NotifyStub.SENT[0]["title"])
check("正文给出影响与建议",
      "影响" in _NotifyStub.SENT[0]["content"] and "建议" in _NotifyStub.SENT[0]["content"],
      _NotifyStub.SENT[0]["content"][:60])

ev = tw.check_and_notify(j0 + 200)
check("同一轮停滞不重复推", ev == [], str(ev))

ev = tw.check_and_notify(j0 + 100 + tw.RENOTIFY_SECONDS - 10)
check("不到 6 小时不催", ev == [], str(ev))
ev = tw.check_and_notify(j0 + 100 + tw.RENOTIFY_SECONDS + 10)
check("满 6 小时再催一次", len(ev) == 1 and ev[0]["action"] == "stall", str(ev))

tw.beat("job")
ev = tw.check_and_notify(time.time() + 1)
check("恢复推 1 条", len(ev) == 1 and ev[0]["action"] == "recover", str(ev))
check("恢复也走 account 桶", _NotifyStub.SENT[-1]["event"] == "account",
      _NotifyStub.SENT[-1]["title"])

section("注销不留恢复通知")
reset()
tw.register("job2", 10, "测试任务")
k0 = tw._TASKS["job2"]["registered_at"]
tw.check_and_notify(k0 + 100)
before = len(_NotifyStub.SENT)
tw.unregister("job2")
ev = tw.check_and_notify(k0 + 200)
check("注销后不再推恢复", ev == [] and len(_NotifyStub.SENT) == before, str(ev))

# ---------------------------------------------------------------- 3. 线程
section("检测线程幂等启动")
os.environ.pop("TASK_WATCHDOG_DISABLED", None)
tw = importlib.reload(tw)
first = tw.start_watchdog()
second = tw.start_watchdog()
check("第一次启动成功", first is True, str(first))
check("第二次不重复启动", second is False, str(second))
tw.stop_watchdog()
os.environ["TASK_WATCHDOG_DISABLED"] = "1"
tw = importlib.reload(tw)
check("停用开关生效", tw.start_watchdog() is False)

# ---------------------------------------------------------------- 4. 接线
section("接线：六个调度循环 + 账号心跳")
pairs = [
    ("polish_service.py", "polish-scheduler"),
    ("shop_diagnosis.py", "shop-diagnosis"),
    ("stale_relist.py", "stale-relist"),
    ("sold_out_relist.py", "sold-out-relist"),
    ("hot_keywords.py", "hot-keywords"),
    ("token_keeper.py", "token-keeper"),
]
for fname, task in pairs:
    src = (BACKEND / fname).read_text(encoding="utf-8")
    check(f"{fname} 登记 {task}", f'register("{task}"' in src)
    check(f"{fname} 有 beat", f'beat("{task}")' in src)

hb_src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")
check("心跳循环登记", "_wd.register(_hb_key" in hb_src)
check("心跳成功打拍", "_wd.beat(_hb_key)" in hb_src)
check("心跳退出注销", "_wd.unregister(_hb_key)" in hb_src)
check("心跳注册在 try 之前", hb_src.index("_wd.register(_hb_key") <
      hb_src.index("_wd.beat(_hb_key)"))

section("接线：接口与页面")
api_src = (BACKEND / "watchdog_api.py").read_text(encoding="utf-8")
check("状态接口存在", "/api/watchdog/status" in api_src)
rs_src = (BACKEND / "reply_server.py").read_text(encoding="utf-8")
check("路由已挂载", "watchdog_api" in rs_src and "watchdog_router" in rs_src)
page_src = (BACKEND.parent / "static" / "notify.html").read_text(encoding="utf-8")
check("页面有任务心跳卡片", "watchBox" in page_src and "/api/watchdog/status" in page_src)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}：")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print(f"全部通过（{PASSES} 项）")
