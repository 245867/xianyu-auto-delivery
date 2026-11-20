"""
租约接管与失败重试：并发下只能有一个赢家（2026-11-15 收尾）
=============================================================

`claim_leased()` 对"已有行"不是插入，而是 UPDATE。原来 UPDATE 只按 key 定位：
两个 owner 同时读到同一条**过期 pending**（或同一条 `failed` + allow_failed），
两条 UPDATE 都会成功——双方都 `claimed=True`、`attempts` 加两次、后写的那个
owner 覆盖前一个。用屏障让两条线程同时读到"可接管"的行使之稳定复现。

守住六件事：
[1] 过期 pending 两 owner 竞争：只有 1 个赢家，attempts 只 +1，lease_owner 是赢家
[2] failed + allow_failed 同样只有一个赢家，attempts 只 +1
[3] 输家必须重读最新状态（拿到 running + 赢家的 lease_owner），不能拿旧判断回答
[4] 租约仍然有效的行不能被接管（running，attempts 不动）
[5] done / submission_unknown 永远不会被覆盖（unknown 还带 blocked=True）
[6] 不并发时的基本口径不变：new=1 次、retry +1、failed 不给 allow 就拒绝

跑法：cd backend && python3 tests/test_lease_takeover.py
"""

import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="lease_take_"))
os.environ["IDEMPOTENCY_DB"] = str(_TMP / "idem.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import idempotency  # noqa: E402

idempotency.get("__warmup__")

FAILS: list[str] = []
PASSES = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ok   {label}")
    else:
        FAILS.append(label + (f"  ← {extra}" if extra else ""))
        print(f"  FAIL {label}" + (f"  ← {extra}" if extra else ""))


def section(title: str) -> None:
    print(f"\n[{title}]")


def raw(sql: str, args=()) -> None:
    conn = sqlite3.connect(str(idempotency.DB_PATH))
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def row(key: str) -> dict:
    return idempotency.get(key) or {}


def make_expired(key: str) -> None:
    """造一条"pending 且租约已过期"的行（worker 崩掉后的真实形状）。"""
    idempotency.claim_leased(key, "delivery", owner="old", lease_seconds=60)
    raw("UPDATE op_idempotency SET lease_until=? WHERE key=?", (time.time() - 5, key))


_orig_row = idempotency._row
_seen = threading.local()


def race(key: str, owners=("A", "B"), allow_failed: bool = False):
    """两个 owner 同时读到"可接管"的行，再同时 UPDATE。"""
    bar = threading.Barrier(len(owners), timeout=5)

    def row_with_barrier(conn, k):
        r = _orig_row(conn, k)
        if not getattr(_seen, "armed", False) and r is not None:
            _seen.armed = True
            try:
                lease = float(r.get("lease_until") or 0)
            except Exception:
                lease = 0.0
            claimable = (r.get("status") == "pending" and lease <= time.time()) \
                or r.get("status") == "failed"
            if claimable:
                try:
                    bar.wait()
                except Exception:
                    pass
        return r

    out: list = [None] * len(owners)

    def worker(i):
        _seen.armed = False
        try:
            out[i] = idempotency.claim_leased(key, "delivery", owner=owners[i],
                                              lease_seconds=60,
                                              allow_failed=allow_failed)
        except Exception as e:
            out[i] = {"EXC": f"{type(e).__name__}: {e}"}

    idempotency._row = row_with_barrier
    try:
        ts = [threading.Thread(target=worker, args=(i,)) for i in range(len(owners))]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
    finally:
        idempotency._row = _orig_row
    return out


def claimed(res) -> list:
    return [bool(isinstance(x, dict) and x.get("claimed")) for x in res]


# ---------------------------------------------------------------- [1] 过期 pending
section("1 过期 pending：两个 owner 同时接管")

K1 = "delivery:expired"
make_expired(K1)
res = race(K1)
wins = claimed(res)
check("只有一个赢家", sum(wins) == 1, str(wins))
check("赢家 reason=lease_taken_over",
      [x.get("reason") for x in res if isinstance(x, dict) and x.get("claimed")]
      == ["lease_taken_over"], str([x.get("reason") for x in res]))
check("attempts 只加一次（1 → 2）", row(K1).get("attempts") == 2, str(row(K1).get("attempts")))
winner = next(x for x in res if isinstance(x, dict) and x.get("claimed"))
check("lease_owner 是赢家", row(K1).get("lease_owner") == winner["record"]["lease_owner"],
      str(row(K1)))
loser = next(x for x in res if isinstance(x, dict) and not x.get("claimed"))
check("输家 reason=running", loser.get("reason") == "running", str(loser.get("reason")))
check("输家读到的是赢家那一版 lease_owner",
      loser["record"]["lease_owner"] == row(K1).get("lease_owner"), str(loser.get("record")))
check("输家看到的 attempts 也是 2", loser["record"]["attempts"] == 2,
      str(loser["record"]["attempts"]))

# ---------------------------------------------------------------- [2] failed 重试
section("2 failed + allow_failed：两个 owner 同时重试")

K2 = "delivery:failed"
idempotency.claim_leased(K2, "delivery", owner="old", lease_seconds=60)
idempotency.finish(K2, "failed", "第一次失败")
check("摆好后 attempts=1", row(K2).get("attempts") == 1, str(row(K2).get("attempts")))
res = race(K2, allow_failed=True)
wins = claimed(res)
check("只有一个赢家", sum(wins) == 1, str(wins))
check("赢家 reason=retry",
      [x.get("reason") for x in res if isinstance(x, dict) and x.get("claimed")] == ["retry"],
      str([x.get("reason") for x in res]))
check("attempts 只加一次（1 → 2）", row(K2).get("attempts") == 2, str(row(K2).get("attempts")))
check("状态回到 pending", row(K2).get("status") == "pending", str(row(K2)))
loser = next(x for x in res if isinstance(x, dict) and not x.get("claimed"))
check("输家 reason=running", loser.get("reason") == "running", str(loser.get("reason")))

# ---------------------------------------------------------------- [3] 有效租约
section("3 租约仍有效：不能被接管")

K3 = "delivery:valid"
idempotency.claim_leased(K3, "delivery", owner="A", lease_seconds=60)
r = idempotency.claim_leased(K3, "delivery", owner="B", lease_seconds=60)
check("B 被拒", r.get("claimed") is False)
check("reason=running", r.get("reason") == "running", str(r.get("reason")))
check("attempts 没动", row(K3).get("attempts") == 1, str(row(K3).get("attempts")))
check("lease_owner 还是 A", row(K3).get("lease_owner") == "A", str(row(K3)))

# ---------------------------------------------------------------- [4] 终态保护
section("4 done / submission_unknown 不被覆盖")

K4 = "delivery:done"
idempotency.claim_leased(K4, "delivery", owner="A", lease_seconds=60)
idempotency.finish(K4, "done", "发完了")
before = row(K4)
r = idempotency.claim_leased(K4, "delivery", owner="B", lease_seconds=60)
check("done 不给抢", r.get("claimed") is False and r.get("reason") == "done", str(r))
check("done 状态没被改", row(K4).get("status") == "done", str(row(K4)))
check("done 的 attempts 没被改", row(K4).get("attempts") == before.get("attempts"),
      f"{before.get('attempts')} → {row(K4).get('attempts')}")

K5 = "delivery:unknown"
idempotency.claim_leased(K5, "delivery", owner="A", lease_seconds=60)
idempotency.finish(K5, "submission_unknown", "超时，结果未知")
before = row(K5)
r = idempotency.claim_leased(K5, "delivery", owner="B", lease_seconds=60, allow_failed=True)
check("unknown 不给抢且 blocked", r.get("claimed") is False and r.get("blocked") is True, str(r))
check("unknown 状态没被改", row(K5).get("status") == "submission_unknown", str(row(K5)))
check("unknown 的 attempts 没被改", row(K5).get("attempts") == before.get("attempts"),
      f"{before.get('attempts')} → {row(K5).get('attempts')}")

# ---------------------------------------------------------------- [5] 基本口径
section("5 不并发时的基本口径")

K6 = "delivery:new"
r = idempotency.claim_leased(K6, "delivery", owner="A", lease_seconds=60)
check("新键：new + attempts=1", r.get("claimed") and r.get("reason") == "new"
      and row(K6).get("attempts") == 1, str(r))

K7 = "delivery:failed-noallow"
idempotency.claim_leased(K7, "delivery", owner="A", lease_seconds=60)
idempotency.finish(K7, "failed", "失败")
r = idempotency.claim_leased(K7, "delivery", owner="B", lease_seconds=60)
check("failed 没给 allow_failed：拒绝", r.get("claimed") is False and r.get("reason") == "failed",
      str(r))
check("attempts 没动", row(K7).get("attempts") == 1, str(row(K7).get("attempts")))

r = idempotency.claim_leased(K7, "delivery", owner="B", lease_seconds=60, allow_failed=True)
check("给 allow_failed：retry + attempts=2",
      r.get("claimed") and r.get("reason") == "retry" and row(K7).get("attempts") == 2, str(r))

# 接管后 finish 清租约（现有接口，不校验 owner；记录行为，不改）
idempotency.finish(K7, "done", "收工")
check("finish 后租约字段被清空",
      row(K7).get("lease_owner") == "" and float(row(K7).get("lease_until") or 0) == 0,
      str({k: row(K7).get(k) for k in ("lease_owner", "lease_until")}))

print("\n" + "=" * 56)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print(f"✅ 全部通过（{PASSES} 项）")
