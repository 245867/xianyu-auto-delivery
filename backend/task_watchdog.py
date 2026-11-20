"""
后台任务停滞检测（L2-2）
=========================
这个应用里有一批"每 N 分钟做一件事"的后台线程：每日擦亮调度、店铺诊断、
僵尸链接重发、售罄重发、热门词扫描、Token 保活、每个账号的心跳。

它们共同的写法是 `while True: try: 干活 except: pass; sleep(...)`。这种写法
能扛住单次异常，但扛不住两件事：

1. **线程死了**：异常发生在 try 之外（例如 `get_config()` 抛错、改配置读到了
   坏 JSON），线程直接退出，日志里通常只有一行，之后永远安静；
2. **线程活着但卡住**：某次 `requests.get` 没超时、某个锁没释放，线程停在
   那里，日志停在某一行，看起来一切正常。

两种情况下页面都还是绿的，通知一条不响，而"每天自动擦亮""自动重发"已经
停了几天——这是比掉线更难发现的沉默故障。

做法（只做检测，不自动重启）：
- 每个循环启动时 `register(name, interval, note)`，每轮结束 `beat(name)`；
- 一个**独立线程**（不是 asyncio 任务）每分钟扫一次：距上次心跳超过
  `max(90秒, interval × 2.5)` 就推一条 `account` 通知；
- 同一次停滞只推一条，恢复时再推一条"已恢复"；持续停滞每 6 小时提醒一次；
- `GET /api/watchdog/status` 给页面/人工看当前所有任务的最后一次心跳。

**为什么用独立线程而不是 asyncio 任务**：事件循环被同步调用堵住时，asyncio
任务也一起停摆，正好漏掉最该发现的那种卡死。线程的 `time.time()` 不受影响。

**为什么不自动重启线程**：重启线程会把"卡住的原因"一起藏起来（例如账号被
风控、Cookie 失效，重启后立刻又卡一次），而且两个线程同时跑同一批数据比停着
更危险。这里只负责"让人知道"。

停用：环境变量 `TASK_WATCHDOG_DISABLED=1`（测试用）。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger

# 距上次心跳超过 interval × GRACE 就判定停滞；下限 90 秒，
# 免得一个 10 秒的循环因为一次 GC 或慢请求就误报。
DEFAULT_GRACE = 2.5
MIN_STALL_SECONDS = 90.0
# 持续停滞的重复提醒间隔（第一次停滞立即推，之后每 6 小时一次）
RENOTIFY_SECONDS = 6 * 3600

try:
    CHECK_SECONDS = max(5.0, float(os.getenv("TASK_WATCHDOG_CHECK_SECONDS", "60")))
except Exception:
    CHECK_SECONDS = 60.0
DISABLED = os.getenv("TASK_WATCHDOG_DISABLED", "").strip() in ("1", "true", "yes")

_lock = threading.RLock()
_TASKS: Dict[str, Dict[str, Any]] = {}
_NOTIFIED: Dict[str, Dict[str, float]] = {}
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def register(name: str, interval_seconds: float, note: str = "",
             grace: float = DEFAULT_GRACE) -> None:
    """
    登记一个"应该每 interval_seconds 跳一次"的任务。在循环线程启动时调用。
    重复登记（例如账号重连后重启同一个循环）会保留上次心跳时间，只更新节奏。
    """
    name = str(name or "").strip()
    if not name:
        return
    interval = max(1.0, float(interval_seconds or 60))
    with _lock:
        prev = _TASKS.get(name) or {}
        _TASKS[name] = {
            "interval": interval,
            "grace": max(1.0, float(grace or DEFAULT_GRACE)),
            "note": note or prev.get("note") or "",
            "last": prev.get("last"),
            "registered_at": prev.get("registered_at") or time.time(),
        }
    start_watchdog()


def unregister(name: str) -> None:
    """任务正常结束（例如账号被停用、线程按配置退出）时注销，别报假停滞。"""
    with _lock:
        _TASKS.pop(str(name or ""), None)
        _NOTIFIED.pop(str(name or ""), None)


def beat(name: str) -> None:
    """每轮干完活打一拍。没登记的名字直接忽略（防止拼错名字造出幽灵任务）。"""
    with _lock:
        task = _TASKS.get(str(name or ""))
        if task is not None:
            task["last"] = time.time()


def _threshold(task: Dict[str, Any]) -> float:
    return max(MIN_STALL_SECONDS, float(task["interval"]) * float(task["grace"]))


def scan(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """返回当前处于停滞状态的任务（纯读，不发通知）。"""
    now = time.time() if now is None else float(now)
    out: List[Dict[str, Any]] = []
    with _lock:
        items = [(n, dict(t)) for n, t in _TASKS.items()]
    for name, task in items:
        last = task.get("last")
        base = float(last or task.get("registered_at") or now)
        age = max(0.0, now - base)
        limit = _threshold(task)
        if age > limit:
            out.append({
                "name": name,
                "note": task.get("note") or "",
                "interval": task["interval"],
                "last_beat": last,
                "age_seconds": round(age, 1),
                "limit_seconds": round(limit, 1),
                "never_beaten": last is None,
            })
    out.sort(key=lambda x: x["age_seconds"], reverse=True)
    return out


def snapshot(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """所有已登记任务的状态（页面展示用，含正常的）。"""
    now = time.time() if now is None else float(now)
    rows: List[Dict[str, Any]] = []
    with _lock:
        items = [(n, dict(t)) for n, t in _TASKS.items()]
    for name, task in items:
        last = task.get("last")
        base = float(last or task.get("registered_at") or now)
        age = max(0.0, now - base)
        limit = _threshold(task)
        rows.append({
            "name": name,
            "note": task.get("note") or "",
            "interval": task["interval"],
            "last_beat": last,
            "last_beat_text": (datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S")
                               if last else ""),
            "age_seconds": round(age, 1),
            "limit_seconds": round(limit, 1),
            "state": "stalled" if age > limit else "ok",
        })
    rows.sort(key=lambda x: (x["state"] != "stalled", -x["age_seconds"]))
    return rows


def _fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分钟"
    return f"{seconds // 86400} 天 {(seconds % 86400) // 3600} 小时"


def _notify(title: str, content: str, level: str = "warn") -> None:
    try:
        from notify_service import send_event
        send_event("account", title, content, level=level)
    except Exception as e:
        logger.warning(f"看门狗通知发送失败，已跳过: {e}")


def check_and_notify(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """
    扫一次并推送：新停滞推一条、持续停滞每 6 小时一条、恢复推一条。
    返回本次产生的通知事件（测试用）。
    """
    now = time.time() if now is None else float(now)
    events: List[Dict[str, Any]] = []
    stalled = {s["name"]: s for s in scan(now)}

    with _lock:
        newly = [n for n in stalled if n not in _NOTIFIED]
        recovered = [n for n in list(_NOTIFIED) if n not in stalled]
        repeated = [n for n, v in _NOTIFIED.items()
                    if n in stalled and now - float(v.get("last_notify") or 0) >= RENOTIFY_SECONDS]
        for n in newly:
            _NOTIFIED[n] = {"since": now, "last_notify": now}
        for n in repeated:
            _NOTIFIED[n]["last_notify"] = now
        for n in recovered:
            _NOTIFIED.pop(n, None)

    for name in newly + repeated:
        s = stalled[name]
        state_line = ("从未有过心跳" if s["never_beaten"]
                      else f"上次心跳 {datetime.fromtimestamp(s['last_beat']).strftime('%Y-%m-%d %H:%M:%S')}")
        content = "\n".join([
            f"任务：{name}" + (f"（{s['note']}）" if s["note"] else ""),
            f"应有节奏：约每 {_fmt_age(s['interval'])} 一次",
            f"现状：{state_line}，已 {_fmt_age(s['age_seconds'])}没动静",
            "影响：这个后台任务大概率已经不干活了；页面不会报错，只有这条通知会说。",
            "建议：先看日志最后一条时间；重启应用可恢复，但若隔一段时间又出现，"
            "说明卡住的原因还在（网络、Cookie、库锁），别只重启了事。",
        ])
        _notify(f"⚠️ 后台任务停滞：{name}", content, level="warn")
        events.append({"action": "stall", "name": name, "age_seconds": s["age_seconds"]})

    for name in recovered:
        _notify(f"✅ 后台任务已恢复：{name}",
                f"任务：{name}\n心跳已恢复，之前那次停滞结束。\n"
                f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", level="info")
        events.append({"action": "recover", "name": name})
    return events


def _loop() -> None:
    while not _stop.wait(CHECK_SECONDS):
        try:
            check_and_notify()
        except Exception:
            pass


def start_watchdog() -> bool:
    """幂等启动检测线程。返回是否本次真正启动。"""
    global _thread
    if DISABLED:
        return False
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="task-watchdog")
        _thread.start()
        return True


def stop_watchdog() -> None:
    global _thread
    _stop.set()
    with _lock:
        _thread = None


def _reset_for_tests() -> None:
    with _lock:
        _TASKS.clear()
        _NOTIFIED.clear()


__all__ = ["register", "unregister", "beat", "scan", "snapshot",
           "check_and_notify", "start_watchdog", "stop_watchdog",
           "MIN_STALL_SECONDS", "RENOTIFY_SECONDS"]
