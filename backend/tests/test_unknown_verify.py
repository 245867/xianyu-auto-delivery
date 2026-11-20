"""
未知发布人工核对回归测试（P0-1）
================================
锁死五件事：

1. 只有 submission_unknown 能核对，别的状态不能被这个出口改；
2. 三种核对结果语义正确：confirmed→done、not_received→释放可重发、still_unknown→保持禁止；
3. 原发布记录先快照进 op_verify_log，再改/删幂等行——删除幂等键不等于丢记录；
4. 理由必填、结果必须是三选一；
5. 单条核对：处理一个 key 不影响同期另一个未知 key。

跑法（在 backend 目录）：python3 tests/test_unknown_verify.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

# 隔离幂等库：绝不能碰真实 data/idempotency.db
_TMP = tempfile.mkdtemp(prefix="unknown_verify_")
os.environ["IDEMPOTENCY_DB"] = str(Path(_TMP) / "idem.db")

import idempotency as idem  # noqa: E402

FAILS: list[str] = []
PASSES = 0


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


def make_unknown(key: str, note: str = "提交阶段断网") -> None:
    """造一条 unknown 记录（模拟提交阶段异常后的账本状态）。"""
    idem.claim(key, "publish", note="测试标题")
    idem.finish(key, "submission_unknown", note=note)


section("1 只核对 unknown，不碰其他状态")
k_pending = "publish:pendingtest"
idem.claim(k_pending, "publish", note="还在跑")
r = idem.verify_unknown(k_pending, "confirmed", reason="顺手点错了")
check("pending 被拒绝", r.get("ok") is False and "只有 submission_unknown" in r.get("error", ""))
check("pending 状态没被改", idem.get(k_pending)["status"] == "pending")

k_done = "publish:donetest"
idem.claim(k_done, "publish", note="已完成")
idem.finish(k_done, "done", note="itemId=1")
r = idem.verify_unknown(k_done, "not_received", reason="误操作")
check("done 被拒绝", r.get("ok") is False)
check("done 记录还在", idem.get(k_done)["status"] == "done")

r = idem.verify_unknown("publish:ghost", "confirmed", reason="不存在")
check("不存在的 key 被拒绝", r.get("ok") is False)

section("2 参数校验")
k = "publish:paramtest"
make_unknown(k)
check("结果不在三选一里被拒绝",
      idem.verify_unknown(k, "maybe", reason="x").get("ok") is False)
check("理由为空被拒绝",
      idem.verify_unknown(k, "confirmed", reason="   ").get("ok") is False)
check("非法调用后仍是 unknown", idem.get(k)["status"] == "submission_unknown")

section("3 confirmed：关闭为 done，原 note 进留痕")
r = idem.verify_unknown(k, "confirmed", reason="我发布的第 2 页看到同标题",
                        matched_item_id="888")
check("返回 ok", r.get("ok") is True)
check("新状态 done", r.get("new_status") == "done")
row = idem.get(k)
check("账本状态为 done", row["status"] == "done")
check("账本 note 带 itemId 与理由", "888" in row["note"] and "第 2 页" in row["note"])
logs = idem.verify_log(key=k)
check("留痕 1 条", len(logs) == 1)
check("留痕保留原始 note", logs[0]["original_note"] == "提交阶段断网")
check("留痕保留原始状态", logs[0]["original_status"] == "submission_unknown")
check("留痕记录核对结果", logs[0]["outcome"] == "confirmed")
check("留痕记录理由", "第 2 页" in logs[0]["reason"])
check("留痕记录商品编号", logs[0]["matched_item_id"] == "888")
check("留痕有时间", bool(logs[0]["created_at"]))

section("4 not_received：删键可重发，但原记录留痕")
k2 = "publish:notreceived"
make_unknown(k2, note="提交时网关异常")
r = idem.verify_unknown(k2, "not_received", reason="我发布的全翻了，没有这条")
check("返回 ok", r.get("ok") is True)
check("新状态 released", r.get("new_status") == "released")
check("幂等键已释放", idem.get(k2) is None)
again = idem.claim(k2, "publish", note="重新发布")
check("释放后可以重新抢占", again.get("claimed") is True and again.get("blocked") is False)
logs = idem.verify_log(key=k2)
check("留痕仍在", len(logs) == 1 and logs[0]["original_note"] == "提交时网关异常")

section("5 still_unknown：保持禁止重发，只留痕")
k3 = "publish:stillunknown"
make_unknown(k3)
r = idem.verify_unknown(k3, "still_unknown", reason="待审核列表还没出来，晚点再看")
check("返回 ok", r.get("ok") is True)
check("新状态仍是 unknown", r.get("new_status") == "submission_unknown")
row = idem.get(k3)
check("账本仍是 unknown", row["status"] == "submission_unknown")
check("原有 note 未被覆盖", row["note"] == "提交阶段断网")
blocked = idem.claim(k3, "publish")
check("仍然禁止自动重发", blocked.get("blocked") is True)
check("留痕 1 条", len(idem.verify_log(key=k3)) == 1)

section("6 单条核对不串号")
k4 = "publish:batchA"
k5 = "publish:batchB"
make_unknown(k4)
make_unknown(k5)
idem.verify_unknown(k4, "confirmed", reason="只处理 A", matched_item_id="901")
check("A 已 done", idem.get(k4)["status"] == "done")
check("B 仍 unknown", idem.get(k5)["status"] == "submission_unknown")
check("B 无核对留痕", len(idem.verify_log(key=k5)) == 0)

print("\n" + "=" * 56)
if FAILS:
    print(f"❌ {len(FAILS)} 条失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print(f"✅ 全部通过（{PASSES} 项）")
