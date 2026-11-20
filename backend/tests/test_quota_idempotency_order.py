"""
配额与幂等的顺序（施工计划书 B5）
=================================
旧顺序是「先取配额 → 再查幂等」。后果不是少发一条，而是**原因说错**：

- 重复点同一篇内容，第二次先被 60 秒限流拦住，报"发布过于频繁"；
- 用户以为等一分钟就好，实际这条已经提交过，等到明天也一样；
- 真正该做的事（去「我发布的」核对 / 改标题）被藏起来。

新顺序：**只读预检（blocked/duplicate）→ 取配额 → 原子 claim**。
本测试锁三件事：

1. 重复提交不消耗配额，且返回的是"已经提交过"而不是"限流"；
2. `submission_unknown` 仍然禁止自动重发，并附带只读回读结果；
3. 三个发布入口（一键发布 / 模板批量 / 僵尸链接重发）的源码顺序固定，
   以后谁挪回去都会红。

跑法（在 backend 目录）：python3 tests/test_quota_idempotency_order.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="quota_idem_")
os.environ["IDEMPOTENCY_DB"] = str(Path(_TMP) / "idem.db")
os.environ["RATE_LIMIT_STATE"] = str(Path(_TMP) / "rate.json")
os.environ["RATE_LIMIT_CIRCUIT"] = str(Path(_TMP) / "circuit.json")
os.environ["DB_PATH"] = str(Path(_TMP) / "data.db")
os.environ.pop("OPPORTUNITY_SCORE_V2", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# 项目根也进路径：机会模块会 import utils.item_search
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import idempotency                  # noqa: E402
import opportunity_api as oa        # noqa: E402
import rate_limiter                 # noqa: E402
import shop_diagnosis               # noqa: E402

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


# ---------------------------------------------------------------- 接线替身
# 全部替换成离线替身：不连闲鱼、不写用户真实数据目录。
oa._night_ban_ok = lambda: (True, "")
oa._resolve_cookie = lambda cid, user_id: "unb=1; fake_token=1"
oa.extract_token_from_cookie = lambda c: "token_fake"
oa.HISTORY_FILE = Path(_TMP) / "history.json"
PUBLISHED: list[dict] = []


def _fake_publish(cookie_str, item_data, app_key, cookie_id=""):
    PUBLISHED.append({"cookie_id": cookie_id, "title": item_data.get("title")})
    return {"itemId": f"ITEM{len(PUBLISHED)}", "status": "submitted"}


oa.publish_item = _fake_publish
shop_diagnosis.fetch_my_items = lambda *a, **k: {"ok": True, "items": [], "pages": 1}

BODY = {"cookie_id": "acc1", "title": "测试商品·离线", "desc": "描述",
        "imageUrls": ["https://example.com/a.jpg"], "price": 9.9}


def call(body: dict) -> dict:
    return asyncio.run(oa.opportunity_publish(
        dict(body),
        current_user={"user_id": 1},
    ))


# ---------------------------------------------------------------- 1 预检只读
section("1 precheck 是只读的")

key_probe = idempotency.make_key("publish", "acc1", "探针")
check("空键：exists=False", idempotency.precheck(key_probe)["exists"] is False)
check("空键查完仍无记录（没有偷偷写一行）", idempotency.get(key_probe) is None)

# ---------------------------------------------------------------- 2 首次发布
section("2 首次发布照常消耗配额")

r1 = call(BODY)
check("第一次发布成功", r1.get("ok") is True, str(r1)[:80])
check("确实调用了发布", len(PUBLISHED) == 1, str(PUBLISHED))
check("配额用掉 1 次", rate_limiter.daily_count("publish", "acc1") == 1,
      str(rate_limiter.daily_count("publish", "acc1")))
check("返回里仍提示要人工核对", r1.get("requires_readback") is True)

# ---------------------------------------------------------------- 3 重复提交
section("3 重复提交：不扣配额、不说成限流")

rate_limiter.reset()          # 把 60 秒窗口清掉，排除"限流"这个干扰项
r2 = call(BODY)
check("第二次被识别成重复", r2.get("ok") is False)
check("错误说清楚是「已经提交过」", "已经提交过" in str(r2.get("error")), str(r2.get("error")))
check("不再报限流", "限流" not in str(r2.get("error")))
check("状态回显 done", r2.get("status") == "done", str(r2.get("status")))
check("没有真的再发一次", len(PUBLISHED) == 1, str(len(PUBLISHED)))
check("配额一次都没扣", rate_limiter.daily_count("publish", "acc1") == 0,
      str(rate_limiter.daily_count("publish", "acc1")))

# ---------------------------------------------------------------- 4 unknown
section("4 submission_unknown：禁止重发 + 只读回读")

BODY_U = {**BODY, "title": "测试商品·结果未知"}
key_u = idempotency.make_key("publish", "acc1", BODY_U["title"], 9.9, BODY_U["desc"])
idempotency.claim(key_u, "publish", note="离线造一条未知")
idempotency.finish(key_u, "submission_unknown", note="超时")
rate_limiter.reset()

r3 = call(BODY_U)
check("unknown 被挡住", r3.get("ok") is False)
check("说明是「结果未知」", "结果未知" in str(r3.get("error")), str(r3.get("error")))
check("要人工回读", r3.get("requires_readback") is True)
check("带回读结果（只读，不是估算）", (r3.get("readback") or {}).get("ok") is True,
      str(r3.get("readback")))
check("unknown 也不扣配额", rate_limiter.daily_count("publish", "acc1") == 0,
      str(rate_limiter.daily_count("publish", "acc1")))
check("没有真的再发", len(PUBLISHED) == 1, str(len(PUBLISHED)))
check("unknown 状态没被改掉", idempotency.get(key_u)["status"] == "submission_unknown",
      idempotency.get(key_u)["status"])

# ---------------------------------------------------------------- 5 顺序
section("5 三个发布入口的源码顺序")


def order_ok(src: str, pre: str, quota: str, claim: str) -> bool:
    i_pre, i_q, i_claim = src.find(pre), src.find(quota), src.find(claim)
    return 0 <= i_pre < i_q < i_claim


api = (BACKEND / "opportunity_api.py").read_text(encoding="utf-8")
check("一键发布：预检 → 配额 → claim",
      order_ok(api, "idempotency.precheck(idem_key)",
               'rate_limiter.acquire("publish"', "idempotency.claim(idem_key"))

batch = (BACKEND / "product_template.py").read_text(encoding="utf-8")
check("模板批量：预检 → 排队 → claim",
      order_ok(batch, "idempotency.precheck(key)",
               'rate_limiter.wait("publish"', "idempotency.claim(key"))

stale = (BACKEND / "stale_relist.py").read_text(encoding="utf-8")
check("僵尸链接重发：预检 → 配额 → claim",
      order_ok(stale, "idempotency.precheck(key)",
               'rate_limiter.acquire("publish", cid', "idempotency.claim(key"))

check("一键发布的重复判断在两处都走同一个出口",
      api.count("_publish_conflict(") >= 3, str(api.count("_publish_conflict(")))
check("批量里预检命中即跳过（不是继续排队）",
      re.search(r'if pre\.get\("exists"\):.*?"skipped".*?continue', batch, re.S) is not None)
check("僵尸链接里预检命中即跳过",
      re.search(r'if pre\.get\("exists"\) or pre\.get\("blocked"\):.*?continue', stale, re.S) is not None)

# ---------------------------------------------------------------- 6 并发 claim
section("6 并发 claim：同时读到空行时只能有一个赢家")

import threading        # noqa: E402

_orig_row = idempotency._row
_seen = threading.local()


def _race_claim(fn, barrier):
    """两个线程都先读到"这一行不存在"（用屏障卡住），再同时 INSERT。"""
    out: list = [None, None]

    def _w(i):
        _seen.armed = False
        try:
            out[i] = fn()
        except Exception as e:
            out[i] = {"EXC": f"{type(e).__name__}: {e}"}

    def _row_with_barrier(conn, key):
        row = _orig_row(conn, key)
        if not getattr(_seen, "armed", False):
            _seen.armed = True
            if row is None:
                try:
                    barrier.wait()
                except Exception:
                    pass
        return row

    idempotency._row = _row_with_barrier
    try:
        ts = [threading.Thread(target=_w, args=(i,)) for i in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
    finally:
        idempotency._row = _orig_row
    return out


r = _race_claim(lambda: idempotency.claim("publish:race-claim", "publish"),
                threading.Barrier(2, timeout=5))
wins = sum(1 for x in r if isinstance(x, dict) and x.get("claimed"))
check("claim：两个线程只有一个 claimed=True", wins == 1,
      str([x.get("claimed") if isinstance(x, dict) else x for x in r]))
check("claim：输家拿到的是同一行，不是 None",
      all(isinstance(x, dict) and x.get("record") for x in r), str(r)[:120])

r = _race_claim(lambda: idempotency.claim_leased("delivery:race-lease", "delivery",
                                                 owner="o", lease_seconds=60),
                threading.Barrier(2, timeout=5))
wins = sum(1 for x in r if isinstance(x, dict) and x.get("claimed"))
check("claim_leased：也只有一个赢家", wins == 1,
      str([(x.get("claimed"), x.get("reason")) for x in r]))
check("claim_leased：输家的理由是 running（不是 new）",
      sorted(x.get("reason") for x in r) == ["new", "running"],
      str([x.get("reason") for x in r]))

# ---------------------------------------------------------------- 7 并发双提交
section("7 并发双提交：失败方说「已经提交过」，不是「限流」")

DUP = {**BODY, "cookie_id": "acc_dup", "title": "并发双提交·离线"}
key_dup = idempotency.make_key("publish", "acc_dup", DUP["title"], 9.9, DUP["desc"])

rate_limiter.reset()
ok_fill, _ = rate_limiter.acquire("publish", "acc_dup", limit=1, window=60)
check("先把窗口占满（模拟赢家刚拿走配额）", ok_fill is True)

_real_acquire = rate_limiter.acquire


def acquire_after_winner(scope, key="", limit=None, window=None):
    """模拟：赢家在"预检之后、取配额之前"完成了 claim。"""
    if scope == "publish" and key == "acc_dup":
        idempotency.claim(key_dup, "publish", note="winner")
    return _real_acquire(scope, key, limit=limit, window=window)


rate_limiter.acquire = acquire_after_winner
try:
    r = call(DUP)
finally:
    rate_limiter.acquire = _real_acquire
check("失败方被挡", r.get("ok") is False)
check("原因是「已经提交过」", "已经提交过" in str(r.get("error")), str(r.get("error")))
check("不再误报限流", "限流" not in str(r.get("error")))
check("没有真的再发", len(PUBLISHED) == 1, str(len(PUBLISHED)))

# 反向：窗口满但确实没有这条记录 → 仍然是限流（可重试）
rate_limiter.reset()
rate_limiter.acquire("publish", "acc_dup2", limit=1, window=60)
r = call({**DUP, "cookie_id": "acc_dup2", "title": "并发双提交·无关记录"})
check("没有记录时仍报限流（等一分钟可重试）",
      r.get("ok") is False and "限流" in str(r.get("error")), str(r.get("error")))

print("\n" + "=" * 56)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print(f"✅ 全部通过（{PASSES} 项）")
