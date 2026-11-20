"""
通知发送重试与失败留痕（L2-1）
===============================
以前 `send_event` 一次 POST 定生死：网关抖一下 502、限流 429、连接被重置，
这条"账号掉线"就静默消失了——通知系统自己坏了，却是最不该静默的那一个。

本测试锁五件事：

1. 可重试的失败（408/425/429/5xx/异常）会退避重试，成功即停；
2. 不可重试的失败（400/401/403/404）**不重试**——key 错重试只是连续撞墙；
3. 全部失败时 `ok=False`，并写一条失败留痕；
4. 留痕里**没有 webhook 地址、没有消息正文**，且最多保留 50 条；
5. 页面和接口能读到留痕（`get_recent_failures` 新的在前）。

全程离线：HTTP 用替身、`_sleep` 被替换，不真睡也不碰真实 webhook。

跑法（在 backend 目录）：python3 tests/test_notify_retry.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="notify_retry_")
os.environ.pop("NOTIFY_RETRY_ATTEMPTS", None)
os.environ.pop("NOTIFY_RETRY_BACKOFF_SECONDS", None)

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
ns.FAILURES_FILE = Path(_TMP) / "notify_failures.json"

SLEPT: list[float] = []
ns._sleep = lambda s: SLEPT.append(s)

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRET_KEY_123"


class _Resp:
    def __init__(self, code: int, text: str = "ok"):
        self.status_code = code
        self.text = text


class _Seq:
    """按顺序吐出预设响应；元素为 int（状态码）或 Exception 实例。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, url, json=None, timeout=None):
        self.calls += 1
        item = self.script.pop(0) if self.script else 200
        if isinstance(item, Exception):
            raise item
        return _Resp(item)


def setup(channels: dict | None = None):
    cfg = {"enabled": True,
           "events": {e: True for e in ns.EVENTS},
           "timeout": 3}
    cfg.update(channels if channels is not None else {"wecom": WEBHOOK})
    ns.save_config(cfg)


def reset_log():
    try:
        ns.FAILURES_FILE.unlink()
    except FileNotFoundError:
        pass


def install(script):
    fake = _Seq(script)
    ns.requests = type("R", (), {"post": staticmethod(fake),
                                 "get": staticmethod(fake)})()
    return fake


# ---------------------------------------------------------------- 1. 可重试
section("可重试失败：2 次 500 后成功")
setup()
reset_log()
SLEPT.clear()
fake = install([500, 500, 200])
r = ns.send_event("account", "测试", "正文", level="warn")
check("最终成功", r.get("ok") is True, str(r.get("results")))
check("尝试 3 次", r.get("attempts", {}).get("wecom") == 3, str(r.get("attempts")))
check("实际请求 3 次", fake.calls == 3, f"calls={fake.calls}")
check("退避 0.8 → 1.6", [round(x, 3) for x in SLEPT] == [0.8, 1.6], str(SLEPT))
check("成功不写失败留痕", ns.get_recent_failures() == [], str(ns.get_recent_failures()))

section("429 限流：重试一次后成功")
reset_log()
SLEPT.clear()
fake = install([429, 200])
r = ns.send_event("risk", "测试", "正文")
check("最终成功", r.get("ok") is True, str(r.get("results")))
check("尝试 2 次", r.get("attempts", {}).get("wecom") == 2, str(r.get("attempts")))
check("退避 1 次", len(SLEPT) == 1, str(SLEPT))

section("连接异常：先抛错后成功")
reset_log()
fake = install([ConnectionError("connection reset by peer"), 200])
r = ns.send_event("publish", "测试", "正文")
check("异常也重试并成功", r.get("ok") is True, str(r.get("results")))
check("尝试 2 次", r.get("attempts", {}).get("wecom") == 2, str(r.get("attempts")))

# ---------------------------------------------------------------- 2. 不重试
section("不可重试失败：401 只打一次")
reset_log()
SLEPT.clear()
fake = install([401, 200])
r = ns.send_event("account", "测试", "正文")
check("判定失败", r.get("ok") is False, str(r.get("results")))
check("只尝试 1 次", r.get("attempts", {}).get("wecom") == 1, str(r.get("attempts")))
check("没有退避", SLEPT == [], str(SLEPT))
check("错误摘要含状态码", "HTTP 401" in r["results"].get("wecom", ""), r["results"].get("wecom", ""))

# ---------------------------------------------------------------- 3. 全失败留痕
section("全部失败：写留痕且字段完整")
reset_log()
fake = install([502, 502, 502])
r = ns.send_event("account", "账号掉线", "账号：abc\n原因：token 过期")
check("判定失败", r.get("ok") is False, str(r.get("results")))
check("尝试 3 次", r.get("attempts", {}).get("wecom") == 3, str(r.get("attempts")))
items = ns.get_recent_failures()
check("留痕 1 条", len(items) == 1, str(len(items)))
if items:
    it = items[0]
    check("事件名正确", it.get("event") == "account", str(it.get("event")))
    check("渠道名正确", it.get("channel") == "wecom", str(it.get("channel")))
    check("次数正确", it.get("attempts") == 3, str(it.get("attempts")))
    check("含状态码", "502" in str(it.get("error")), str(it.get("error")))
    raw = json.dumps(items, ensure_ascii=False)
    check("不含 webhook 地址", "qyapi.weixin.qq.com" not in raw and "SECRET_KEY" not in raw, raw[:120])
    check("不含消息正文", "token 过期" not in raw and "账号掉线" not in raw, raw[:120])

# ---------------------------------------------------------------- 4. 上限与顺序
section("留痕上限 50 条、新的在前")
reset_log()
ns.FAILURES_FILE.parent.mkdir(parents=True, exist_ok=True)
seed = [{"time": f"2026-01-01T00:00:{i:02d}", "event": "account",
         "channel": "wecom", "attempts": 1, "error": "HTTP 500"} for i in range(60)]
ns.FAILURES_FILE.write_text(json.dumps(seed), encoding="utf-8")
install([500, 500, 500])
ns.send_event("account", "测试", "正文")
raw_items = json.loads(ns.FAILURES_FILE.read_text(encoding="utf-8"))
check("落盘 50 条", len(raw_items) == 50, str(len(raw_items)))
check("保留的是最新的", raw_items[-1]["time"] != "2026-01-01T00:00:00", raw_items[-1]["time"])
recent = ns.get_recent_failures(limit=5)
check("接口取 5 条", len(recent) == 5, str(len(recent)))
check("新的在前", recent[0]["time"] >= recent[-1]["time"], recent[0]["time"])

# ---------------------------------------------------------------- 5. 接口接线
section("接线：接口与页面")
api_src = (BACKEND / "notify_api.py").read_text(encoding="utf-8")
check("有失败记录接口", "/api/notify/failures" in api_src)
check("测试接口返回尝试次数", "attempts" in api_src)
page_src = (BACKEND.parent / "static" / "notify.html").read_text(encoding="utf-8")
check("页面展示失败记录", "failureBox" in page_src and "/api/notify/failures" in page_src)
svc_src = (BACKEND / "notify_service.py").read_text(encoding="utf-8")
check("服务层导出 get_recent_failures", "get_recent_failures" in svc_src.split("__all__")[-1])
check("重试常量可配置", "NOTIFY_RETRY_ATTEMPTS" in svc_src)

# ---------------------------------------------------------------- 6. 跳过态
section("跳过态：没配渠道时不发也不留痕")
reset_log()
setup({})
install([200])
r = ns.send_event("account", "测试", "正文")
check("跳过", r.get("skipped") is True, str(r.get("results")))
check("无留痕", ns.get_recent_failures() == [], str(ns.get_recent_failures()))
setup()
r = ns.send_event("account", "测试", "正文")  # 渠道已恢复但脚本已耗尽 → 默认 200
check("恢复渠道后能发", r.get("ok") is True, str(r.get("results")))

print()
if FAILS:
    print(f"FAILED {len(FAILS)}：")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print(f"全部通过（{PASSES} 项）")
