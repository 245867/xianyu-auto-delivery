"""
C3 发货可靠性用例。

守住的六件事：
[1] 逐卡记账   —— (order_id, cookie_id, idx) 唯一，重复登记不覆盖已有内容
[2] 逐卡幂等   —— 已发过的卡永远不再发；重启后依然有效
[3] 租约安全   —— 进程发到一半崩了，过期租约转 unknown，绝不被自动接管重发
[3b] 租约令牌  —— 接管之后，旧 worker 迟到的结论不许改状态（拆解 8）
[4] 结果未知   —— unknown 绝不自动补发（否则买家可能拿到两张卡）
[5] 次数上限   —— failed 补到 max_attempts 就停手，转人工
[6] 顺序正确   —— 源码里必须先发后盖章（mark_delivery_sent 在发送循环之后）

跑法：python tests/test_delivery_reliability.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="c3_"))
os.environ["DELIVERY_RELIABILITY_DB"] = str(_TMP / "d.db")
os.environ["DELIVERY_RELIABILITY_CONFIG"] = str(_TMP / "d.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import delivery_reliability as dr

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def section(t):
    print(f"\n[{t}]")


ORDER = "2001"
COOKIE = "acc1"


def reset():
    if dr.DB_PATH.exists():
        dr.DB_PATH.unlink()
    if dr.CONFIG_PATH.exists():
        dr.CONFIG_PATH.unlink()
    dr.save_config(dict(dr.DEFAULT_CONFIG))


# ---------------------------------------------------------------- [1] 逐卡记账
section("1 逐卡记账")
reset()

check(dr.ensure_order(ORDER, COOKIE, ["卡A", "__IMAGE_SEND__7|http://x/a.png"],
                      item_id="i1", chat_id="c1", buyer_id="b1") == 2,
      "两张卡登记成功")
check(dr.ensure_order(ORDER, COOKIE, ["卡A2", "卡B2"]) == 0, "重复登记不新建行")
rows = dr.cards(ORDER, COOKIE)
check([r["payload"] for r in rows] == ["卡A", "7|http://x/a.png"],
      "重复登记不覆盖已存内容（重发必须发同一张卡）")
check([r["kind"] for r in rows] == ["text", "image"], "图片/文本类型识别正确")

check(dr.ensure_order("", COOKIE, ["x"]) == 0, "没有订单号不记账")
check(dr.parse_content("普通文本")["kind"] == "text", "parse_content 文本")
check(dr.parse_content("__IMAGE_SEND__1|u")["payload"] == "1|u", "parse_content 图片去掉前缀")

section("暂停期间保留未发送卡")
reset()
dr.ensure_order(ORDER, COOKIE, ["原内容"])
paused_claim = dr.claim_card(ORDER, COOKIE, 0)
token = paused_claim["card"]["lease_token"]
check(not dr.defer_unsent_card(ORDER, COOKIE, 0, "stale"), "旧令牌不能延后当前租约")
check(dr.defer_unsent_card(ORDER, COOKIE, 0, token), "暂停前尚未发送的卡可延后")
deferred = dr.cards(ORDER, COOKIE)[0]
check(deferred["status"] == "failed" and deferred["attempts"] == 0
      and deferred["payload"] == "原内容", "保留内容且归还未使用的发送次数")
check(dr.claim_card(ORDER, COOKIE, 0)["claimed"], "恢复后可认领原卡")

# ---------------------------------------------------------------- [2] 逐卡幂等
section("2 逐卡幂等")
reset()
dr.ensure_order(ORDER, COOKIE, ["卡A", "卡B"])

c0 = dr.claim_card(ORDER, COOKIE, 0, owner="A")
check(c0["claimed"] and c0["reason"] in ("new", "retry"), "首张卡可发")
check(not dr.claim_card(ORDER, COOKIE, 0, owner="B")["claimed"], "发之前别人抢不到（running）")

dr.mark_sent(ORDER, COOKIE, 0)
dr.mark_failed(ORDER, COOKIE, 1, "测试错误")
dr.ensure_order(ORDER, "another-account", ["另一账号的卡"])
dr.mark_failed(ORDER, "another-account", 0, "测试错误")
diagnostic = next(r for r in dr.unresolved() if r["cookie_id"] == COOKIE)
check(
    (diagnostic["order_total"], diagnostic["order_sent"], diagnostic["order_unresolved"]) == (2, 1, 1),
    "整单进度按账号和订单汇总，待处理行包含已发卡的数量",
)
c = dr.claim_card(ORDER, COOKIE, 0, owner="B")
check(not c["claimed"] and c["reason"] == "sent", "已发的卡永远不再发")
check(dr.summary(ORDER, COOKIE)["sent"] == 1, "汇总里记到 1 张")

check(not dr.claim_card(ORDER, COOKIE, 9, owner="B")["claimed"], "不存在的卡不抢")

# ---------------------------------------------------------------- [3] 租约
section("3 过期租约不得自动接管")
reset()
dr.save_config({"lease_seconds": 1})
dr.ensure_order(ORDER, COOKIE, ["卡A"])
dr.claim_card(ORDER, COOKIE, 0, owner="A")
check(not dr.claim_card(ORDER, COOKIE, 0, owner="B")["claimed"], "租约内不可抢")
time.sleep(1.1)
c = dr.claim_card(ORDER, COOKIE, 0, owner="B")
check(not c["claimed"] and c["reason"] == "unknown",
      "租约过期 → 抢占路径原子转 unknown，不自动接管")
check(dr.cards(ORDER, COOKIE)[0]["status"] == "unknown",
      "过期租约已经落成 unknown")
check(dr.cards(ORDER, COOKIE)[0]["attempts"] == 1,
      "未知结果不消耗第二次发送次数")

reset()
dr.save_config({"lease_seconds": 1})
dr.ensure_order(ORDER, COOKIE, ["卡A"])
dr.claim_card(ORDER, COOKIE, 0, owner="A")
time.sleep(1.1)
check(dr.reap_expired_leases() == 1, "独立收尸路径仍能处理过期 sending")
# 拆解 2 修正：不能退回 pending。进程可能死在 ws.send 之后、mark_sent 之前，
# 消息已经到了买家手机上，退回 pending 会被补发扫描再发一张卡（卡密是钱）。
check(dr.cards(ORDER, COOKIE)[0]["status"] == "unknown",
      "过期租约 → unknown（不是 pending），交给人工确认")
check(not [r for r in dr.resend_candidates() if r["order_id"] == ORDER],
      "unknown 不进补发候选：宁可让人看一眼，也不给买家两张卡")
check(dr.summary(ORDER, COOKIE)["need_human"] == [0], "出现在待人工列表")
check(dr.force_retry(ORDER, COOKIE, 0, "人工核对后要求补发"), "人工可以强制再补一次")
manual = [r for r in dr.resend_candidates() if r["order_id"] == ORDER]
check(len(manual) == 1 and manual[0]["status"] == "manual_retry",
      "人工放行后立即进入补发候选，不必等 10 分钟冷却或下一条买家消息")

# ---------------------------------------------------------------- [3b] 令牌守卫
section("3b 租约令牌：旧 worker 的结论不许覆盖接管者")
reset()
dr.save_config({"lease_seconds": 1, "max_attempts": 9})
dr.ensure_order(ORDER, COOKIE, ["卡A"])

a = dr.claim_card(ORDER, COOKIE, 0, owner="A")
tk_a = a["card"]["lease_token"]
check(bool(tk_a), "抢占时发了租约号")
time.sleep(1.1)
check(dr.reap_expired_leases() == 1, "旧租约先按安全策略转 unknown")
check(dr.force_retry(ORDER, COOKIE, 0, "人工核对后放行"), "人工明确放行后重新排队")
b = dr.claim_card(ORDER, COOKIE, 0, owner="B")
tk_b = b["card"]["lease_token"]
check(b["claimed"] and tk_b != tk_a, "人工放行后的执行者拿到新租约号")

check(dr.mark_failed(ORDER, COOKIE, 0, "A 迟到的失败", token=tk_a) is False,
      "A 拿着过期令牌收尾 → 拒绝写入")
check(dr.cards(ORDER, COOKIE)[0]["status"] == "sending", "B 的 sending 没被 A 改成 failed")
check(dr.mark_sent(ORDER, COOKIE, 0, "A 迟到的成功", token=tk_a) is False,
      "A 拿过期令牌标 sent 也不行（两个方向都要挡）")
check(dr.release_card(ORDER, COOKIE, 0, "A 迟到的释放", token=tk_a) is False,
      "release_card 同样受守卫——它会把 B 正在发的卡退回待发")
check(dr.cards(ORDER, COOKIE)[0]["status"] == "sending", "状态仍是 B 持有中")

check(dr.mark_sent(ORDER, COOKIE, 0, "B 成功", token=tk_b) is True, "持牌者 B 正常收尾")
check(dr.cards(ORDER, COOKIE)[0]["status"] == "sent", "状态已 sent")
check(dr.cards(ORDER, COOKIE)[0]["lease_token"] == "", "收尾会把令牌清空（不会留着误导下一次）")
check(dr.mark_sent(ORDER, COOKIE, 0, "B 重复", token=tk_b) is True,
      "同一令牌重复调用仍返回 True（幂等，不报错）")

# 不带令牌 = 人工/管理动作，必须还能用（页面上的"确认已送达""放弃"不能失效）
reset()
dr.ensure_order(ORDER, COOKIE, ["卡A"])
dr.claim_card(ORDER, COOKIE, 0, owner="A")
check(dr.mark_unknown(ORDER, COOKIE, 0, "人工放弃") is True,
      "不带令牌的人工动作不受守卫限制")

# ---------------------------------------------------------------- [4] 结果未知
section("4 结果未知禁止补发")
reset()
dr.ensure_order(ORDER, COOKIE, ["卡A"])
dr.claim_card(ORDER, COOKIE, 0)
dr.mark_unknown(ORDER, COOKIE, 0, "ws 超时")
c = dr.claim_card(ORDER, COOKIE, 0)
check(not c["claimed"] and c["reason"] == "unknown", "unknown 不允许自动重发")
check(not [r for r in dr.resend_candidates() if r["order_id"] == ORDER], "unknown 不进补发候选")
check(dr.summary(ORDER, COOKIE)["need_human"] == [0], "unknown 出现在待人工列表")

# 发送异常分级：只有 websocket 写出之前能证明未提交的异常可记 failed
reset()
dr.ensure_order(ORDER, COOKIE, ["卡A", "卡B"])
c0 = dr.claim_card(ORDER, COOKIE, 0)
c1 = dr.claim_card(ORDER, COOKIE, 1)
check(dr.record_send_exception(
    ORDER, COOKIE, 0, dr.DeliverySubmissionNotAttempted("ws 不存在"),
    token=c0["card"]["lease_token"]), "发送前异常可记 failed")
check(dr.cards(ORDER, COOKIE)[0]["status"] == "failed",
      "可证明未提交的异常允许以后重试")
check(dr.record_send_exception(
    ORDER, COOKIE, 1, RuntimeError("send returned exception"),
    token=c1["card"]["lease_token"]), "发送阶段异常落账成功")
check(dr.cards(ORDER, COOKIE)[1]["status"] == "unknown",
      "无法证明未提交的异常一律 unknown，禁止自动重发")

# ---------------------------------------------------------------- [5] 次数上限
section("5 次数上限")
reset()
dr.save_config({"max_attempts": 2, "resend_delay_minutes": 0})
dr.ensure_order(ORDER, COOKIE, ["卡A"])
n = 0
while True:
    c = dr.claim_card(ORDER, COOKIE, 0, owner=f"o{n}")
    if not c["claimed"]:
        break
    dr.mark_failed(ORDER, COOKIE, 0, "boom")
    n += 1
check(n == 2, f"最多尝试 2 次（实际 {n}）")
check(dr.claim_card(ORDER, COOKIE, 0)["reason"] == "exhausted", "用满次数后拒绝，交给人工")

reset()
dr.save_config({"resend_delay_minutes": 10})
dr.ensure_order(ORDER, COOKIE, ["卡A"])
dr.claim_card(ORDER, COOKIE, 0)
dr.mark_failed(ORDER, COOKIE, 0)
check(not dr.resend_candidates(), "失败后 10 分钟内不进补发队列（避免和状态同步抢跑）")

# ---------------------------------------------------------------- [6] 内容没取到
section("6 内容没取到也要留痕")
reset()
dr.ensure_order(ORDER, COOKIE, ["卡A"])
dr.record_missing(ORDER, COOKIE, 1, 2, "发货内容获取失败")
rows = dr.cards(ORDER, COOKIE)
check(len(rows) == 3, "缺的 2 张也落库")
check(all(r["status"] == "failed" for r in rows[1:]), "缺的卡记为 failed")
check(not [r for r in dr.resend_candidates() if r["idx"] > 0],
      "空内容的卡不进补发（重试也没用，必须人工补）")
check(dr.summary(ORDER, COOKIE)["need_human"] == [1, 2], "缺卡进待人工列表")

# ---------------------------------------------------------------- [7] 全发完
section("7 全发完才算完成")
reset()
dr.ensure_order(ORDER, COOKIE, ["卡A", "卡B"])
for i in range(2):
    dr.claim_card(ORDER, COOKIE, i)
    dr.mark_sent(ORDER, COOKIE, i)
s = dr.summary(ORDER, COOKIE)
check(s["all_sent"] is True and s["sent"] == 2, "两张都发完 → all_sent")
check(s["need_human"] == [], "没有待人工")

# ---------------------------------------------------------------- [8] 接线
section("8 源码接线（顺序不能反）")
src = (Path(__file__).resolve().parent.parent / "XianyuAutoAsync.py").read_text(encoding="utf-8")
for method in ("deliver_order_manually", "_resend_undelivered", "_handle_auto_delivery"):
    _, method_src = src.split(f"    async def {method}(", 1)
    method_src = method_src.split("\n    async def ", 1)[0]
    i_send = method_src.find("await self._send_delivery_card(")
    i_mark = method_src.find("self.mark_delivery_sent(order_id)")
    check(i_send >= 0 and i_mark > i_send, f"{method} 先发后确认")
check("dr.ensure_order(" in src, "发货前先登记账本")
check("dr.record_missing(" in src, "内容没取到的卡也留痕")
check("_resend_undelivered" in src, "补发扫描已接进心跳")
check(src.count("dr.record_send_exception(") >= 3,
      "手动、补发、自动发货三条路径都按发送阶段记录异常")
check("dr.mark_failed(order_id, self.cookie_id, i, last_err, token=_tk)" not in src,
      "自动发货不再把任意发送异常直接记 failed")
check("_inline_try" not in src,
      "无平台回执时不再对可疑的 WebSocket 写出做就地自动重试")
check("dr.reap_expired_leases()" in src,
      "自动发货发送前会主动清理过期租约")
check("DeliverySubmissionUnknown(" in src,
      "WebSocket 写出异常显式标成结果未知")
check('_all_done = bool((_sum or {}).get("all_sent") is True)' in src,
      "有发货账本时，整单完成必须由账本 all_sent 确认")
check("if _all_done is None:" not in src,
      "账本汇总失败不得回退为 WebSocket 写出行数确认整单完成")
check("发货账本汇总失败" in src and "整单不得确认完成" in src,
      "账本汇总失败会留下关键日志并转人工")

# 拆解 8：两个发送入口收尾时都必须把本次抢占的令牌带上
i_tk = src.find("_tk = str((_cl.get(\"card\")")
check(i_tk > 0, "主发送路径从抢占结果里取令牌")
seg_main = src[i_tk:i_tk + 6000]
check("token=_tk" in seg_main, "主路径 mark_sent/mark_failed 带令牌")
i_resend = src.find("async def _resend_undelivered")
i_resend_end = src.find("async def _handle_auto_delivery")
seg_resend = src[i_resend:i_resend_end]
check("_tk = str(card.get(\"lease_token\")" in seg_resend, "补发路径取令牌")
check(seg_resend.count("token=_tk") >= 2, "补发路径的成功与失败都带令牌")


# ---------------------------------------------------------------- [9] 老库升级
section("9 老库补列（升级不丢数据）")
import sqlite3
_olddb = _TMP / "old.db"
if _olddb.exists():
    _olddb.unlink()
_c = sqlite3.connect(str(_olddb))
_c.execute("""CREATE TABLE delivery_cards (
  order_id TEXT NOT NULL, cookie_id TEXT NOT NULL, idx INTEGER NOT NULL,
  item_id TEXT DEFAULT '', chat_id TEXT DEFAULT '', buyer_id TEXT DEFAULT '',
  kind TEXT DEFAULT 'text', payload TEXT DEFAULT '', status TEXT NOT NULL,
  attempts INTEGER DEFAULT 0, last_error TEXT DEFAULT '',
  lease_owner TEXT DEFAULT '', lease_until REAL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (order_id, cookie_id, idx))""")
_c.execute("INSERT INTO delivery_cards VALUES "
           "('OLD1','acc1',0,'','','','text','卡密X','sent',1,'','','0','2026-01-01','2026-01-01')")
# 拆解 9 前的老 platform_ship（没有 stopped / stop_reason）
_c.execute("""CREATE TABLE platform_ship (
  order_id TEXT NOT NULL, cookie_id TEXT NOT NULL, item_id TEXT DEFAULT '',
  attempts INTEGER DEFAULT 0, last_error TEXT DEFAULT '', done INTEGER DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (order_id, cookie_id))""")
_c.execute("INSERT INTO platform_ship VALUES ('OLD1','acc1','I1',2,'网络错误',0,"
           "'2026-01-01','2026-01-01')")
_c.commit()
_c.close()

_save = dr.DB_PATH
dr.DB_PATH = _olddb
try:
    row = dr.cards("OLD1", "acc1")
    check(len(row) == 1, "老库的卡还在")
    check("lease_token" in row[0], "打开时自动补了 lease_token 列")
    check("spec" in row[0], "打开时自动补了 spec 列")
    check(row[0]["spec"] == "", "老数据的规格留空，不替它猜")
    check(row[0]["payload"] == "卡密X", "老数据内容没被改动")
    check(dr.summary("OLD1", "acc1")["all_sent"] is True, "老数据照常能算汇总")
    ps = dr.platform_ship_rows("pending", "acc1")
    check(len(ps) == 1, "老 platform_ship 记录还在")
    check("stopped" in ps[0] and "stop_reason" in ps[0], "打开时自动补了 stopped / stop_reason 列")
    check(ps[0]["attempts"] == 2 and ps[0]["last_error"] == "网络错误", "老数据没被改动")
finally:
    dr.DB_PATH = _save


# ---------------------------------------------------------------- [10] 规格
section("10 规格列：只记真的给了的（拆解 39 §八-1）")
reset()
dr.ensure_order("SPEC1", COOKIE, ["卡A"], item_id="i9", chat_id="c9",
                buyer_id="b9", spec="红色 / 均码")
row = dr.cards("SPEC1", COOKIE)[0]
check(row.get("spec") == "红色 / 均码", f"登记时写入规格（{row.get('spec')!r}）")
check(row["payload"] == "卡A", "规格不影响发的卡密内容")

dr.ensure_order("SPEC1", COOKIE, ["卡A"], spec="蓝色")
check(dr.cards("SPEC1", COOKIE)[0]["spec"] == "红色 / 均码", "重复登记不覆盖已记规格")

dr.ensure_order("SPEC2", COOKIE, ["卡B"])
check(dr.cards("SPEC2", COOKIE)[0]["spec"] == "", "没给规格就留空（不拿商品名凑）")

dr.record_missing("SPEC3", COOKIE, 0, 1, "内容获取失败", item_id="i9",
                  chat_id="c9", buyer_id="b9", spec="L")
_missing = [r for r in dr.unresolved() if r["order_id"] == "SPEC3"]
check(len(_missing) == 1 and _missing[0].get("spec") == "L",
      f"缺内容登记也带规格（{_missing[0].get('spec') if _missing else None!r}）")
check(dr.unresolved() and "spec" in dr.unresolved()[0], "待处理清单把规格一起返回")

ctx = dr.locate_context("SPEC1", COOKIE, 0)
check(ctx.get("spec") == "红色 / 均码", f"定位上下文带规格（{ctx.get('spec')!r}）")
ctx2 = dr.locate_context("SPEC2", COOKIE, 0)
check(ctx2.get("spec") == "", "定位上下文不替人补规格")

_src = (Path(__file__).resolve().parent.parent / "XianyuAutoAsync.py").read_text(encoding="utf-8")
check('ctx["order_spec"] = order_spec' in _src, "发货流程把解析出的规格放进上下文")
check(_src.count('spec=deliver_ctx.get("order_spec") or ""') == 2,
      "登记卡与缺内容登记两条路径都带规格")

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
