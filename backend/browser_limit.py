"""
浏览器并发限流（拆解 1e：super-butler `utils/browser_limit.py`）

解决的问题：登录、Cookie 刷新、订单状态查询、扫码取 Cookie 这几处各自
`playwright.chromium.launch(...)`，谁也没限着谁。挂 3 个账号、Cookie 刷新定时
撞在同一分钟，就是 3 个 headless Chromium 同时冷启动。

一个 Chromium 冷启动要吃掉一个核几十秒。双核小主机上两三个并存就是 99% CPU，
启动变慢 → 任务堆积 → 更多 Chromium，雪崩。

这里只做一件事：**全进程共用一个信号量**，槽位数按机器实际配置推算，弱机单开。

两个实现细节值得记：
1. 用线程信号量而不是 asyncio.Semaphore —— 这个项目里账号任务跑在**多个事件循环**
   里（`order_status_handler` 的补发线程用 `asyncio.run`，`reply_server` 的扫码/刷新
   用 `new_event_loop`）。asyncio 的同步原语绑定单个 loop，跨 loop 用会直接报错；
   每个 loop 各建一个则总数翻倍，等于没限。
2. `LimitedBrowser` 把归还挂在 `close()` 上 —— 调用方原来的
   `finally: await browser.close()` 不用改一个字，也就不会有人忘记归还。

不要用 `asyncio.to_thread(sem.acquire, ...)` 等槽位：协程被取消（`wait_for` 超时、
账号切换）时线程不会被打断，醒来后会把槽位抢走且永不归还 → 永久少一个槽。
这里改成非阻塞轮询，取消时什么都没拿到。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional

from loguru import logger

ENV_KEY = "MAX_CONCURRENT_BROWSERS"
ACQUIRE_TIMEOUT = 300
POLL_INTERVAL = 0.15

_semaphore: Optional[threading.Semaphore] = None
_semaphore_lock = threading.Lock()
_limit: Optional[int] = None


async def _acquire_async(sem: threading.Semaphore, purpose: str) -> bool:
    """非阻塞轮询拿槽位，可被取消（取消时不会误占）。"""
    deadline = time.monotonic() + ACQUIRE_TIMEOUT
    while True:
        if sem.acquire(blocking=False):
            return True
        if time.monotonic() >= deadline:
            logger.error(f"{purpose}: 等待浏览器空闲超过 {ACQUIRE_TIMEOUT} 秒")
            return False
        await asyncio.sleep(POLL_INTERVAL)


def _total_memory_gb() -> Optional[float]:
    try:
        import psutil
        return psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        return None


def get_limit() -> int:
    """
    同时允许几个浏览器。

    弱机一律单开：排队只是慢一点，挤爆了整机卡死，连消息收发都停。
    可用环境变量 MAX_CONCURRENT_BROWSERS 覆盖（推算不准时）。
    """
    global _limit
    if _limit is not None:
        return _limit

    forced = (os.getenv(ENV_KEY) or "").strip()
    if forced:
        try:
            _limit = max(1, int(forced))
            logger.info(f"浏览器并发上限由 {ENV_KEY} 指定为 {_limit}")
            return _limit
        except ValueError:
            logger.warning(f"{ENV_KEY} 取值无效: {forced!r}，改用自动推算")

    cpu = os.cpu_count() or 2
    mem = _total_memory_gb()
    if cpu <= 2 or (mem is not None and mem <= 4.5):
        _limit = 1
    elif cpu <= 4 or (mem is not None and mem <= 8.5):
        _limit = 2
    else:
        _limit = 3

    mem_desc = f"{mem:.1f}G" if mem is not None else "未知"
    logger.info(f"浏览器并发上限: {_limit}（CPU {cpu} 核，内存 {mem_desc}）")
    return _limit


def _get_semaphore() -> threading.Semaphore:
    global _semaphore
    if _semaphore is None:
        with _semaphore_lock:
            if _semaphore is None:
                _semaphore = threading.Semaphore(get_limit())
    return _semaphore


class LimitedBrowser:
    """浏览器代理：close() 时把槽位还回去，其余属性原样透传。"""

    def __init__(self, browser: Any, semaphore: threading.Semaphore, purpose: str):
        self._browser = browser
        self._semaphore = semaphore
        self._purpose = purpose
        self._released = False
        self._release_lock = threading.Lock()

    def _release(self) -> None:
        with self._release_lock:
            if not self._released:
                self._released = True
                self._semaphore.release()

    async def close(self, *args, **kwargs):
        try:
            return await self._browser.close(*args, **kwargs)
        finally:
            self._release()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._browser, name)


async def launch_browser(playwright: Any, launch_options: Optional[Dict[str, Any]] = None,
                         purpose: str = "浏览器任务") -> LimitedBrowser:
    """
    先取槽位、再启动。没空位就在线程里等（不阻塞事件循环）。

    调用方照旧 `finally: await browser.close()`，槽位随之归还。
    """
    sem = _get_semaphore()
    if not await _acquire_async(sem, purpose):
        raise TimeoutError(
            f"{purpose}: 等待浏览器空闲超过 {ACQUIRE_TIMEOUT} 秒。"
            "可能有浏览器任务卡住未退出，或机器性能不足以支撑当前账号数量。")

    try:
        browser = await playwright.chromium.launch(**(launch_options or {}))
    except BaseException:
        # 启动失败必须立刻归还，否则槽位被永久占住
        sem.release()
        raise
    return LimitedBrowser(browser, sem, purpose)


@contextmanager
def browser_slot(purpose: str = "浏览器任务"):
    """同步槽位（给跑在线程里的同步 Playwright 用）。"""
    sem = _get_semaphore()
    if not sem.acquire(True, ACQUIRE_TIMEOUT):
        raise TimeoutError(
            f"{purpose}: 等待浏览器空闲超过 {ACQUIRE_TIMEOUT} 秒，可能有任务卡死。")
    try:
        yield
    finally:
        sem.release()


async def async_slot(purpose: str = "浏览器任务"):
    """异步槽位：用于"启动与关闭分处两个函数"的场景，需手动 release。"""
    sem = _get_semaphore()
    if not await _acquire_async(sem, purpose):
        raise TimeoutError(
            f"{purpose}: 等待浏览器空闲超过 {ACQUIRE_TIMEOUT} 秒，可能有任务卡死。")
    return sem


def stats() -> Dict[str, Any]:
    return {"limit": get_limit(), "env_override": bool((os.getenv(ENV_KEY) or "").strip())}


def _reset_cache() -> None:
    """仅供测试：清掉缓存的并发上限与信号量，让下次重新读环境变量。"""
    global _semaphore, _limit
    with _semaphore_lock:
        _semaphore = None
        _limit = None


__all__ = ["get_limit", "launch_browser", "LimitedBrowser", "browser_slot",
           "async_slot", "stats", "ENV_KEY", "ACQUIRE_TIMEOUT"]
