"""
账号暂停与恢复（底座项）
=========================
暂停不是一个布尔字段，它有三个必须同时成立的含义，本测试逐个锁住：

1. **暂停要落盘**：重启后仍然暂停（`cookie_status` 表），启动时跳过（Start.py）；
2. **暂停要真的停**：停掉任务；暂停期间换 Cookie 也不能把任务悄悄拉起来
   （旧实现 `update_cookie()` 无条件重启任务，等于"换一次登录就解除暂停"）；
3. **暂停/恢复要能看见**：两个方向都推 `account` 通知——页面上的灰灯没人会一直盯。

另外固定一条恢复语义：暂停期间到期的发货租约在恢复后回收成 `unknown`，
**不会自动补发**（unknown 永不自动重发，见 delivery_reliability）。

跑法（在 backend 目录）：python3 tests/test_account_pause.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="acct_pause_")
os.environ["DB_PATH"] = str(Path(_TMP) / "data.db")
for _k in ("NOTIFY_STATE_DEDUPE_SECONDS",):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 通知替身要先占位：cookie_manager 是在函数里 import 的，运行时取 sys.modules
NOTIFY: list[tuple] = []
_fake_notify = types.ModuleType("notify_service")
_fake_notify.notify_account_state = lambda cid, state, reason="": NOTIFY.append((cid, state))
_fake_notify.notify_diagnosis_summary = lambda *a, **k: None
sys.modules["notify_service"] = _fake_notify

import cookie_manager as cm_mod   # noqa: E402
import db_manager as dbm_mod      # noqa: E402

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


db = dbm_mod.db_manager
db.save_cookie("acc_off", "cookie_off_value", 1)
db.save_cookie_status("acc_off", False)          # 已暂停
db.save_cookie("acc_on", "cookie_on_value", 1)
db.save_cookie_status("acc_on", True)            # 正常

LOOP = asyncio.new_event_loop()
_loop_thread = threading.Thread(target=LOOP.run_forever, daemon=True)
_loop_thread.start()

STARTED: list[str] = []


async def _fake_run_xianyu(cookie_id, cookie_value, user_id=None):
    STARTED.append(cookie_id)
    await asyncio.sleep(3600)


mgr = cm_mod.CookieManager(LOOP)
mgr._run_xianyu = _fake_run_xianyu

# ---------------------------------------------------------------- 1 落盘
section("1 暂停要落盘（重启后仍然暂停）")

check("数据库里记着暂停状态", db.get_all_cookie_status().get("acc_off") is False)
check("CookieManager 从库里读回暂停状态", mgr.get_cookie_status("acc_off") is False)
check("启用账号读回为启用", mgr.get_cookie_status("acc_on") is True)
check("启动时只启动启用的账号（Start.py 口径）",
      "跳过禁用的 Cookie" in (BACKEND.parent / "Start.py").read_text(encoding="utf-8"))

# ---------------------------------------------------------------- 2 换 Cookie
section("2 暂停期间换 Cookie 不拉起任务（旧实现的 bug）")

mgr.update_cookie("acc_off", "cookie_off_value_v2", save_to_db=False)
time.sleep(0.25)

check("Cookie 值确实更新了", mgr.cookies.get("acc_off") == "cookie_off_value_v2")
check("没有启动任务", "acc_off" not in STARTED, str(STARTED))
check("tasks 里也没有它", "acc_off" not in mgr.tasks)
check("状态仍是暂停", mgr.get_cookie_status("acc_off") is False)

mgr.update_cookie("acc_on", "cookie_on_value_v2", save_to_db=False)
time.sleep(0.25)

check("启用账号换 Cookie 照常起任务", "acc_on" in STARTED, str(STARTED))
check("tasks 里有启用账号", "acc_on" in mgr.tasks)

# ---------------------------------------------------------------- 3 通知
section("3 暂停 / 恢复都要推一条")

NOTIFY.clear()
mgr.update_cookie_status("acc_off", True)
time.sleep(0.3)
check("恢复时推 resumed", ("acc_off", "resumed") in NOTIFY, str(NOTIFY))
check("恢复后任务起来了", "acc_off" in STARTED, str(STARTED))

NOTIFY.clear()
mgr.update_cookie_status("acc_on", False)
time.sleep(0.3)
check("暂停时推 paused", ("acc_on", "paused") in NOTIFY, str(NOTIFY))
check("暂停后任务被移除", "acc_on" not in mgr.tasks)

NOTIFY.clear()
mgr.update_cookie_status("acc_on", False)
time.sleep(0.1)
check("状态没变就不重复推", NOTIFY == [], str(NOTIFY))

# ---------------------------------------------------------------- 4 恢复语义
section("4 恢复后的语义：到期租约回收成 unknown，不自动补发")

dr = (BACKEND / "delivery_reliability.py").read_text(encoding="utf-8")
i_reap = dr.find("def reap_expired_leases")
check("租约过期只会进 unknown", 0 < i_reap and "status='unknown'" in dr[i_reap:i_reap + 1200])
i_cand = dr.find("def resend_candidates")
cand_body = dr[i_cand:i_cand + 900]
cand_sql = cand_body.replace('"', "'")
check(
    "补发候选只取 failed 或人工放行的 manual_retry，unknown 不在里面",
    "card.status='manual_retry'" in cand_sql
    and "card.status='failed'" in cand_sql
    and "card.status='unknown'" not in cand_sql,
      cand_body.split("SELECT")[1][:60].replace("\n", " ") if "SELECT" in cand_body else "")
check("注释写明排除 unknown 的理由", "明确排除 unknown" in cand_body)
check("unknown 想要重发只能人工 force_retry", "def force_retry" in dr
      and "只有人能决定这一步" in dr[dr.find("def force_retry"):dr.find("def force_retry") + 600])

# ---------------------------------------------------------------- 5 接线
section("5 接线：掉线通知挂在状态切换点")

async_src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")
i_set = async_src.find("def _set_connection_state")
i_fail = async_src.find('notify_account_state(self.cookie_id, "failed"', i_set)
i_rec = async_src.find('notify_account_state(self.cookie_id, "recovered"', i_set)
check("掉线通知在 _set_connection_state 里", 0 < i_set < i_fail)
check("恢复通知只在 FAILED/RECONNECTING 之后",
      0 < i_rec and "ConnectionState.FAILED, ConnectionState.RECONNECTING" in async_src[i_rec - 300:i_rec],
      async_src[i_rec - 300:i_rec].replace("\n", " ")[-60:])

cm_src = (BACKEND / "cookie_manager.py").read_text(encoding="utf-8")
i_pause = cm_src.find('notify_account_state(cookie_id, "resumed" if enabled else "paused"')
check("暂停/恢复通知在 update_cookie_status 里", i_pause > 0)

print("\n" + "=" * 56)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print(f"✅ 全部通过（{PASSES} 项）")
