"""
拆解 10：回复发件箱用例（falses00/Falses-Goofish-GuardAgent 的 core/reply_outbox.py）。

守住的六件事：
[1] 抢占       —— pending → sending，attempts+1；同一个 key 只能有一个在飞
[2] 永不重发   —— sent / skipped 之后再 begin 一律 done
[3] 未知不补   —— 发送中崩掉（租约过期）转 unknown，绝不自动重发
[4] 只补 pending —— 一个字节都没发出去的才自动补
[5] 人工出口   —— unknown/failed 能放行也能判"不用再发"；每个停止态都有出口
[6] 接线正确   —— 先登记后发送（源码顺序）、心跳里有补发、路由已注册

跑法：python tests/test_reply_outbox.py
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="outbox_"))
os.environ["REPLY_OUTBOX_DB"] = str(_TMP / "outbox.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reply_outbox as ro

BACKEND = Path(__file__).resolve().parent.parent
FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def section(t):
    print(f"\n[{t}]")


# ---------------------------------------------------------------- [1] 抢占
section("1 抢占：pending → sending，attempts+1")
r1 = ro.begin("CK1", "M1", chat_id="c1", item_id="i1", user_id="u1", reply_text="在的")
check(r1["ok"] is True, "第一次登记就抢到了")
check(r1["status"] == "sending", "状态是 sending（不是直接 sent）")
check(r1["record"]["attempts"] == 1, "attempts 从 0 加到 1")
check(r1["record"]["reply_text"] == "在的", "内容和路由信息都落盘了")
check(ro.status_of("CK1", "M1") == "sending", "status_of 看到 sending")

r2 = ro.begin("CK1", "M1", chat_id="c1", reply_text="另一句")
check(r2["ok"] is False, "同一个 key 第二次抢不到")
check(r2["reason"] == "in_flight", "原因是 in_flight（别人正在发）")
check(ro.get("CK1", "M1")["attempts"] == 1, "抢不到不会平白加次数")

# ---------------------------------------------------------------- [2] 终态
section("2 终态：发过的消息永远不再发")
rec = ro.finish("CK1", "M1", "sent")
check(rec["status"] == "sent" and rec["sent_at"], "收尾为 sent 并写了 sent_at")
r3 = ro.begin("CK1", "M1", reply_text="重推一次")
check(r3["ok"] is False and r3["reason"] == "done", "重推时抢不到（done）")
check(ro.status_of("CK1", "M1") == "sent", "状态保持 sent")

a = ro.begin("CK1", "M2", reply_text="x")
ro.finish("CK1", "M2", "skipped", "合规闸拦下")
check(ro.begin("CK1", "M2")["reason"] == "done", "skipped 也是终态，不再发")

# ---------------------------------------------------------------- [3] 未知
section("3 未知不补：发送中崩溃 → unknown")
ro.begin("CK1", "M3", chat_id="c3", user_id="u3", reply_text="y")
conn = sqlite3.connect(str(ro.DB_PATH)); conn.isolation_level = None
conn.execute("UPDATE reply_outbox SET updated_at='2000-01-01T00:00:00' WHERE source_msg_id='M3'")
conn.close()
check(ro.status_of("CK1", "M3") == "unknown", "租约过期的 sending 被读成 unknown")
check(ro.get("CK1", "M3")["status"] == "unknown", "落库也是 unknown（不是还在 sending）")
check(ro.begin("CK1", "M3")["reason"] == "unknown", "unknown 不会被自动重发")
check(all(r["source_msg_id"] != "M3" for r in ro.pending_rows()), "缺补发队列里没有它")
check("不自动重发" in (ro.get("CK1", "M3")["last_error"] or ""), "原因写清楚了")

# 直接崩在 finish 之前（还是 sending）也不能被当成 pending
ro.begin("CK1", "M4", chat_id="c4", user_id="u4", reply_text="z")
check(all(r["source_msg_id"] != "M4" for r in ro.pending_rows()), "sending 不在补发队列里")

# ---------------------------------------------------------------- [4] 只补 pending
section("4 只有 pending 会自动补")
ro.begin("CK1", "M5", chat_id="c5", user_id="u5", reply_text="p")
# 模拟"登记了但没抢到发送权"：手工回退到 pending
sqlite3.connect(str(ro.DB_PATH)).execute(
    "UPDATE reply_outbox SET status='pending' WHERE source_msg_id='M5'").connection.commit()
pend = ro.pending_rows("CK1")
check([r["source_msg_id"] for r in pend] == ["M5"], "pending 只有 M5")
check(ro.pending_rows("CK2") == [], "按账号隔离：另一个账号看不到")
# M4 刚登记时是"活的 sending"（另一个 worker 还在发），没人会动它；
# 只有租约过期才说明干活的人没了 —— 手工把时间拨回去模拟进程崩溃。
conn = sqlite3.connect(str(ro.DB_PATH)); conn.isolation_level = None
conn.execute("UPDATE reply_outbox SET updated_at='2000-01-01T00:00:00' WHERE source_msg_id='M4'")
conn.close()
check(ro.reap_stale() == 1, "reap_stale 把 M4 这个卡住的 sending 收掉")
check(ro.status_of("CK1", "M4") == "unknown", "M4 变成 unknown")

# ---------------------------------------------------------------- [5] 人工出口
section("5 人工出口：停止态必须能放行")
check(ro.release("CK1", "M3") is True, "unknown → pending 放行成功")
check(ro.status_of("CK1", "M3") == "pending", "放行后回到 pending（下一轮会补）")
check(ro.release("CK1", "M3") is False, "pending 不是停止态，放行返回 False")
check(ro.release("CK1", "NOPE") is False, "不存在的记录返回 False")
ro.begin("CK1", "M6", reply_text="q")
ro.finish("CK1", "M6", "unknown", "断线")
check(ro.finish("CK1", "M6", "skipped", "人工：买家已收到")["status"] == "skipped",
      "unknown 可以被人工判成 skipped")
check(ro.finish("CK1", "M1", "failed", "迟到的结论")["status"] == "sent",
      "已 sent 的不会被迟到的结论覆盖")

# ---------------------------------------------------------------- [5b] 账
section("5b 记账口径")
s = ro.stats()
check(s["pending"] == 2, f"pending=2（M3/M5 实际 {s['pending']}）")
check(s["sent"] == 1, "sent=1")
check(s["skipped"] == 2, "skipped=2（M2/M6）")
check(s["unknown"] == 1, "unknown=1（M4）")
check(s["hold"] == s["failed"] + s["unknown"], "hold = failed + unknown")

# ---------------------------------------------------------------- [6] 老库升级
section("6 老库升级：缺列补列，数据不动")
old = _TMP / "old.db"
c = sqlite3.connect(str(old)); c.isolation_level = None
c.execute("""CREATE TABLE reply_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cookie_id TEXT NOT NULL, source_msg_id TEXT NOT NULL,
    reply_text TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER DEFAULT 0,
    last_error TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (cookie_id, source_msg_id))""")
c.execute("INSERT INTO reply_outbox (cookie_id, source_msg_id, reply_text, status, attempts,"
          " created_at, updated_at) VALUES ('OLD','M1','老数据','sent',1,'2026-01-01','2026-01-01')")
c.close()
_old_env = os.environ["REPLY_OUTBOX_DB"]
os.environ["REPLY_OUTBOX_DB"] = str(old)
import importlib
importlib.reload(ro)
check(ro.get("OLD", "M1")["reply_text"] == "老数据", "老数据还在")
cols = {r[1] for r in sqlite3.connect(str(old)).execute("PRAGMA table_info(reply_outbox)")}
check({"chat_id", "item_id", "user_id", "payload_kind", "payload", "sent_at"} <= cols,
      "老表补齐了六列")
check(ro.stats()["sent"] == 1, "统计读得到老记录")
os.environ["REPLY_OUTBOX_DB"] = _old_env
importlib.reload(ro)

# ---------------------------------------------------------------- [7] 接线
section("7 接线：先登记后发送，心跳里补发")
src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")
i_begin = src.find("_ob_key = self._outbox_begin(")
i_send = src.find("await self.send_msg(websocket, chat_id, send_user_id, reply)")
check(i_begin > 0 and i_send > i_begin, "begin 在发送之前（先登记后发送）")
check(src.find("_outbox_end(_ob_key, \"sent\")", i_send) > i_send, "文本发成功后收尾为 sent")
check("self._outbox_end(_ob_key, \"unknown\"" in src, "发送抛异常记 unknown")
check("if _ob_key is False:" in src, "抢不到就不发")
check("_ob_state in (\"sent\", \"skipped\", \"sending\", \"unknown\", \"held\")" in src,
      "上层先用发件箱挡住重推（重启后仍然有效；held 也不重复生成）")
check("await self._resume_reply_outbox(ws)" in src, "心跳里挂了补发")
check("await self._resume_reply_outbox(ws)" in src.split("async def heartbeat_loop")[1][:4000],
      "补发确实在 heartbeat_loop 里，不是死代码")
check("pending_rows(self.cookie_id, limit=3)" in src, "补发只取 pending 且限量（不刷屏）")

srv = (BACKEND / "reply_server.py").read_text(encoding="utf-8")
check("from reply_outbox_api import router as reply_outbox_router" in srv, "路由已注册")
api = (BACKEND / "reply_outbox_api.py").read_text(encoding="utf-8")
check("/api/reply/outbox/release" in api and "/api/reply/outbox/skip" in api,
      "两个人工出口都有接口")

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")