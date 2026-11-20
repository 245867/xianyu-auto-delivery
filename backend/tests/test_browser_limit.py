"""
拆解 1e：浏览器并发限流用例。

守住五件事：
[1] 上限推算   —— 弱机单开（≤2 核 或 ≤4.5G 内存），中配 2，宽裕 3；环境变量可覆盖
[2] 槽位归还   —— 调用方原样 `await browser.close()` 就归还，写不写 finally 都行
[3] 失败/取消不泄槽 —— 启动抛错、等待被取消，都不能把槽位扣死（扣死 = 永久少一个浏览器）
[4] 真的排上队   —— limit=1 时两个任务不会同时 launch
[5] 接线齐全   —— 5 处 chromium 启动全都改走 launch_browser，没有漏网的裸 launch

跑法：python tests/test_browser_limit.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import browser_limit as bl

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def section(t):
    print(f"\n[{t}]")


class FakeBrowser:
    def __init__(self, on_close=None):
        self.closed = 0
        self.connected = True          # 属性透传用
        self._on_close = on_close

    async def close(self, *a, **k):
        # 真 playwright 重复 close 是安全的 no-op，这里照做；
        # 要守的是「槽位不能被还两次」，那由 LimitedBrowser 里的 _released 标记保证。
        self.closed += 1
        if self.closed == 1 and self._on_close:
            self._on_close()


class FakeChromium:
    def __init__(self, fail=False, delay=0.0):
        self.calls = []
        self.fail = fail
        self.delay = delay
        self.active = 0
        self.peak = 0

    async def launch(self, **opts):
        self.calls.append(opts)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("launch failed")
        self.active += 1
        self.peak = max(self.peak, self.active)

        def _done():
            self.active -= 1

        return FakeBrowser(_done)


class FakePW:
    def __init__(self, chromium):
        self.chromium = chromium


def free_slots():
    """当前还剩几个槽位（只取，不还）。"""
    sem = bl._get_semaphore()
    got = 0
    while sem.acquire(blocking=False):
        got += 1
    for _ in range(got):
        sem.release()
    return got


def reset(limit=1):
    bl._reset_cache()
    os.environ[bl.ENV_KEY] = str(limit)
    bl.get_limit()


# ---------------------------------------------------------------- [1] 上限推算
section("1 上限推算")
_real_cpu, _real_mem = os.cpu_count, bl._total_memory_gb

for env_val, cpu, mem, want, label in [
    ("1", 16, 32.0, 1, "环境变量可强制单开"),
    ("3", 2, 4.0, 3, "环境变量优先于推算"),
    ("abc", 2, 4.0, 1, "环境变量无效 → 回落到推算"),
    (None, 2, 32.0, 1, "双核 → 1（弱机一律单开）"),
    (None, 16, 4.0, 1, "内存 ≤4.5G → 1"),
    (None, 4, 32.0, 2, "四核 → 2"),
    (None, 16, 8.0, 2, "内存 ≤8.5G → 2"),
    (None, 8, 32.0, 3, "八核 32G → 3"),
]:
    bl._reset_cache()
    if env_val is None:
        os.environ.pop(bl.ENV_KEY, None)
    else:
        os.environ[bl.ENV_KEY] = env_val
    os.cpu_count = lambda c=cpu: c
    bl._total_memory_gb = lambda m=mem: m
    check(bl.get_limit() == want, f"{label} → {want}")

os.cpu_count, bl._total_memory_gb = _real_cpu, _real_mem


# ---------------------------------------------------------------- [2] 槽位归还
section("2 槽位归还")


async def t_release():
    reset(1)
    chrom = FakeChromium()
    check(free_slots() == 1, "起步 1 个槽位")
    b = await bl.launch_browser(FakePW(chrom), {"headless": True}, "测试")
    check(free_slots() == 0, "启动后槽位被占用")
    check(b.connected is True, "属性透传（browser.connected）")
    check(chrom.calls[0] == {"headless": True}, "launch 参数原样透传")
    await b.close()
    check(free_slots() == 1, "close() 归还槽位，调用方不用额外写代码")

    await b.close()
    check(free_slots() == 1, "重复 close 不会把槽位还多（不会超发）")


asyncio.run(t_release())


# ---------------------------------------------------------------- [3] 失败/取消不泄槽
section("3 失败、取消都不泄槽")


async def t_no_leak():
    reset(1)
    chrom = FakeChromium(fail=True)
    try:
        await bl.launch_browser(FakePW(chrom), {}, "启动失败")
        check(False, "启动失败应当抛错")
    except RuntimeError as e:
        check("launch failed" in str(e), "启动失败原样抛出")
    check(free_slots() == 1, "启动失败立刻归还槽位（否则永久占住）")

    # 等待中被取消
    reset(1)
    hold = await bl.launch_browser(FakePW(FakeChromium()), {}, "占位")
    waiter = asyncio.create_task(bl.launch_browser(FakePW(FakeChromium()), {}, "排队"))
    await asyncio.sleep(0.3)
    check(not waiter.done(), "没有空槽时排队而不是抢占")
    waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass
    await hold.close()
    check(free_slots() == 1, "取消排队者后槽位没有丢（不用 to_thread 的原因）")

    # 超时
    reset(1)
    bl.ACQUIRE_TIMEOUT = 0.3
    hold = await bl.launch_browser(FakePW(FakeChromium()), {}, "占位")
    try:
        await bl.launch_browser(FakePW(FakeChromium()), {}, "超时")
        check(False, "超时应当抛 TimeoutError")
    except TimeoutError as e:
        check("等待浏览器空闲" in str(e), "超时报错说清是等浏览器槽位")
    await hold.close()
    check(free_slots() == 1, "超时后槽位仍完好")
    bl.ACQUIRE_TIMEOUT = 300


asyncio.run(t_no_leak())


# ---------------------------------------------------------------- [4] 真的排上队
section("4 真的排上队")


async def t_serialize():
    reset(1)
    chrom = FakeChromium(delay=0.15)
    pw = FakePW(chrom)

    async def one():
        b = await bl.launch_browser(pw, {}, "并发")
        await asyncio.sleep(0.05)   # 模拟真实占用；不睡的话三个任务会依次一闪而过
        await b.close()

    await asyncio.gather(one(), one(), one())
    check(chrom.peak == 1, f"limit=1 时同时只有一个 Chromium 在跑（实测峰值 {chrom.peak}）")
    check(len(chrom.calls) == 3, "三个任务都跑完了，没有丢")

    reset(3)
    chrom2 = FakeChromium(delay=0.15)
    pw2 = FakePW(chrom2)

    async def one2():
        b = await bl.launch_browser(pw2, {}, "并发")
        await asyncio.sleep(0.05)
        await b.close()

    await asyncio.gather(one2(), one2(), one2())
    check(chrom2.peak == 3, "limit=3 时允许三个并行")


asyncio.run(t_serialize())


# ---------------------------------------------------------------- [5] 接线齐全
section("5 接线齐全")
root = Path(__file__).resolve().parent.parent
src_a = (root / "XianyuAutoAsync.py").read_text(encoding="utf-8")
src_o = (root / "order_status_query_playwright.py").read_text(encoding="utf-8")

check("chromium.launch(" not in src_a, "XianyuAutoAsync.py 里没有裸的 chromium.launch")
check("chromium.launch(" not in src_o, "order_status_query_playwright.py 里没有裸的 launch")
check(src_a.count("_launch_browser(") == 4,
      f"XianyuAutoAsync.py 4 处启动都走限流（实得 {src_a.count('_launch_browser(')}）")
check(src_o.count("launch_browser(") == 1, "订单状态查询已接入（launch_browser 调用）")
for purpose in ["商品详情", "扫码取Cookie", "Cookie刷新（页面）", "Cookie刷新 "]:
    check(purpose in src_a, f"用途标注存在：{purpose.strip()}")


# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
