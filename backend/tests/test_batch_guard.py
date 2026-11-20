"""批量发布前置闸回归测试（A5 加固）

背景：文档里反复出现的两条平台规则，此前我方批量发布**一条都没实现**——
1. 「短时间大批量上架/下架」会降权、限流（多份独立来源一致，间隔半小时、一天不超五次）；
2. 违禁词命中要在发布前拦，不能等平台秋后算账。

这里锁死四件事：
- 单账号单日真实发布上限 20 条（低于 rate_limiter 的 200 兜底，专管发布）；
- 配额是「本批 + 今日已发」一起算，不是只看单批大小；
- dry-run 不受配额限制（预览永远是安全的）；
- 渲染结果命中违禁词时 create_job 直接拒绝，且报错文案要说清是违禁词而不是"变量没填"。

跑法（backend 目录）：python3 tests/test_batch_guard.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="batch_guard_")
os.environ["TEMPLATE_DB"] = str(Path(_TMP) / "tpl.db")
os.environ["RATE_LIMIT_STATE"] = str(Path(_TMP) / "rl.json")

import product_template as pt  # noqa: E402
import rate_limiter as rl  # noqa: E402

FAILS: list[str] = []
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ok   {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} {detail}")


def _tpl() -> int:
    r = pt.create_template({"name": "t", "title_tpl": "{{name}} 二手",
                            "desc_tpl": "{{name}}，成色好，支持验货",
                            "price": 100, "quantity": 1, "delivery": "包邮"})
    assert r.get("ok"), r
    return r["id"]


def _rows(n: int, prefix: str = "a"):
    return [{"name": f"{prefix}{i}", "price": 100} for i in range(n)]


def test_cap_constant():
    print("\n[1] 上限口径")
    check("单日发布上限 = 20", pt.DAILY_PUBLISH_CAP == 20, str(pt.DAILY_PUBLISH_CAP))
    check("比限流器总兜底 200 更保守", pt.DAILY_PUBLISH_CAP < rl.DAILY_CAP,
          f"{pt.DAILY_PUBLISH_CAP} vs {rl.DAILY_CAP}")


def test_dry_run_unlimited():
    print("\n[2] dry-run 不受配额限制")
    tid = _tpl()
    r = pt.create_job(tid, _rows(50, "dry"), "acc-dry", dry_run=True)
    check("50 行 dry-run 通过", r.get("ok") is True, str(r))
    check("dry-run 不消耗配额", rl.daily_count("publish", "acc-dry") == 0,
          str(rl.daily_count("publish", "acc-dry")))


def test_cap_enforced():
    print("\n[3] 真实发布按日累计")
    tid = _tpl()

    r = pt.create_job(tid, _rows(21, "over"), "acc1", dry_run=False)
    check("单批 21 行直接拒绝", r.get("ok") is False, str(r))
    check("报错提到单日上限", "单日上限" in str(r.get("error")), str(r.get("error")))

    r = pt.create_job(tid, _rows(10, "ok"), "acc1", dry_run=False)
    check("10 行允许", r.get("ok") is True, str(r))

    # 模拟已发 15 条
    for _ in range(15):
        rl.acquire("publish", "acc2", limit=1000, window=1)
    check("acc2 今日 15 条", rl.daily_count("publish", "acc2") == 15)
    r = pt.create_job(tid, _rows(6, "part"), "acc2", dry_run=False)
    check("15 + 6 超限被拒", r.get("ok") is False, str(r))
    r = pt.create_job(tid, _rows(5, "part"), "acc2", dry_run=False)
    check("15 + 5 刚好放行", r.get("ok") is True, str(r))

    for _ in range(5):
        rl.acquire("publish", "acc2", limit=1000, window=1)
    check("acc2 已满 20", rl.daily_count("publish", "acc2") == 20)
    r = pt.create_job(tid, _rows(1, "last"), "acc2", dry_run=False)
    check("满了之后 1 行也拒", r.get("ok") is False, str(r))
    check("报错建议明天继续", "明天" in str(r.get("error")), str(r.get("error")))

    # 账号之间互不影响
    r = pt.create_job(tid, _rows(3, "other"), "acc3", dry_run=False)
    check("另一个账号不受影响", r.get("ok") is True, str(r))


def test_blocked_keywords():
    print("\n[4] 违禁词在 create_job 前就被拦")
    tid = _tpl()
    bad = [{"name": "iPhone 加微信详聊", "price": 100}]
    r = pt.create_job(tid, bad, "acc4", dry_run=True)
    check("违禁词拒绝建批", r.get("ok") is False, str(r))
    check("报错说明是违禁词", "违禁" in str(r.get("error")), str(r.get("error")))
    check("problems 里点名具体词",
          any("微信" in i for p in (r.get("problems") or []) for i in p["issues"]),
          str(r.get("problems")))

    warn_only = [{"name": "iPhone 13 正品保证", "price": 100}]
    r2 = pt.create_job(tid, warn_only, "acc4", dry_run=True)
    check("WARN 级（正品/保证）不拦", r2.get("ok") is True, str(r2))

    r3 = pt.preview(tid, warn_only)
    w = (r3["rows"][0].get("warnings") or [])
    check("但预览里给出提示", any("正品" in x for x in w), str(w))


def main():
    test_cap_constant()
    test_dry_run_unlimited()
    test_cap_enforced()
    test_blocked_keywords()

    print(f"\n{'=' * 56}")
    if FAILS:
        print(f"失败 {len(FAILS)} 项 / 通过 {PASSES} 项")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print(f"全部通过（{PASSES} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
