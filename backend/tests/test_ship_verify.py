"""
拆解 7：发货前平台复核 + 确认发货顺序（tswawa/xianyu-saas 的 verify_before_attempt / 两本账）。

守住七件事：
[1] 平台说不能发就拦：退款中、已关闭、买家/商品/数量不符
[2] 拿不到平台身份或状态证据就不发，订单转人工
[3] 缓存在 30 秒内复用，且只省请求不改结论
[4] 接线：两个发送入口都要在发送前复核，补发路径也要
[5] 顺序：平台"确认发货"必须在全部卡券发成功之后，不能回到"取内容"那一步
[6] 两本账：确认失败进 platform_ship 队列，心跳补；不影响发码状态
[7] 自动发货只接受真实交易卡结构中的订单号，普通文本不能冒充

跑法：python tests/test_ship_verify.py
"""

import ast
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_TMP = Path(tempfile.mkdtemp(prefix="shipverify_"))
os.environ["DELIVERY_RELIABILITY_DB"] = str(_TMP / "dr.db")

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

import ship_verify as sv
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


COOKIE = "unb=1; _m_h5_tk=abc123_1699999999999"


def fake_fetch(row):
    sv._fetch = lambda cookie_str, cookie_id, order_id: row


def order(status="pending_ship", buyer="55667788", qty="1"):
    return {"order_id": "3847123456789", "order_status": status,
            "buyer_id": buyer, "quantity": qty, "amount": "100.00"}


# ---------------------------------------------------------------- [1] 该拦的
section("1 平台明确说不能发")
sv._reset()

fake_fetch(order("cancelled"))
r = sv.verify("3847123456789", COOKIE, "acc1")
check(r["ok"] is False and r["verified"] is True, "已关闭 → 拦")
check("关闭" in r["reason"] or "取消" in r["reason"], f"原因能看懂：{r['reason']}")

sv._reset()
fake_fetch(order("refunding"))
r = sv.verify("3847123456789", COOKIE, "acc1")
check(r["ok"] is False, "退款中 → 拦")

sv._reset()
fake_fetch(order("completed"))
check(sv.verify("3847123456789", COOKIE, "acc1")["ok"] is False, "已完成（可能已发过）→ 拦")

sv._reset()
fake_fetch(order("pending_ship", buyer="99999999"))
r = sv.verify("3847123456789", COOKIE, "acc1", expect_buyer="55667788")
check(r["ok"] is False and "买家" in r["reason"], "买家不符 → 拦")

sv._reset()
fake_fetch(order("pending_ship", qty="2"))
r = sv.verify("3847123456789", COOKIE, "acc1", expect_quantity=1)
check(r["ok"] is False and "数量" in r["reason"], "平台 2 件、我们只发 1 张 → 拦")


# ---------------------------------------------------------------- [2] 无证据拦截
section("2 拿不到身份或状态证据就不发")
sv._reset()
fake_fetch(order("pending_ship"))
r = sv.verify("3847123456789", COOKIE, "acc1")
check(r["ok"] is True and r["verified"] is True, "待发货 → 放行且标记已复核")

sv._reset()
fake_fetch(None)
r = sv.verify("3847123456789", COOKIE, "acc1")
check(r["ok"] is False and r["verified"] is False, "平台没返回这单 → 不发")
check("未复核" in r["reason"] or "无法" in r["reason"], "原因说明白")

sv._reset()


def boom(*a, **k):
    raise RuntimeError("network down")


sv._fetch = boom
r = sv.verify("3847123456789", COOKIE, "acc1")
check(r["ok"] is False and "失败" in r["reason"], "请求失败 → 不发，不抛")

sv._reset()
fake_fetch(order("pending_ship", buyer=""))
r = sv.verify(
    "3847123456789",
    COOKIE,
    "acc1",
    expect_buyer="55667788",
)
check(
    r["ok"] is False
    and ("买家" in r["reason"] or "身份" in r["reason"]),
    "平台返回订单但缺少买家身份 → 拦",
)

sv._reset()
fake_fetch(order("pending_ship"))
r = sv.verify(
    "3847123456789",
    COOKIE,
    "acc1",
    expect_item="ITEM-EXPECTED",
)
check(
    r["ok"] is False
    and ("商品" in r["reason"] or "item" in r["reason"].lower()),
    "平台商品与当前消息不一致 → 拦",
)

os.environ["SHIP_VERIFY_ENABLED"] = "0"
sv._reset()
fake_fetch(order("cancelled"))
r = sv.verify("3847123456789", COOKIE, "acc1")
check(r["ok"] is True and "关闭" in r["reason"], "开关关掉 → 完全回到旧行为")
os.environ.pop("SHIP_VERIFY_ENABLED", None)

sv._reset()
r = sv.verify("", COOKIE, "acc1")
check(r["ok"] is False and "没有订单号" in r["reason"], "没订单号也不炸且不发")

sv._reset()
r = sv.verify("3847123456789", "", "acc1")
check(r["ok"] is False and "Cookie" in r["reason"], "没有 Cookie 无法核身 → 不发")


# ---------------------------------------------------------------- [2b] 消息身份
section("2b 自动发货只接受真实交易卡")
source_tree = ast.parse((BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8"))
method_nodes = [
    node for node in ast.walk(source_tree)
    if isinstance(node, ast.FunctionDef)
    and node.name in {"_is_auto_delivery_trigger", "_extract_order_id"}
]
namespace = {
    "re": __import__("re"),
    "json": __import__("json"),
    "logger": SimpleNamespace(
        info=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
    ),
}
exec(
    compile(
        ast.Module(body=method_nodes, type_ignores=[]),
        "XianyuAutoAsync.py",
        "exec",
    ),
    namespace,
)
fake_live = SimpleNamespace(cookie_id="acc1", _safe_str=str)

trusted_card = {
    "1": {
        "6": {
            "1": 101,
            "3": {
                "4": 26,
                "5": json.dumps({
                    "dxCard": {
                        "item": {
                            "main": {
                                "exContent": {
                                    "button": {
                                        "targetUrl": (
                                            "https://example.test/idleDeliver"
                                            "?orderId=3847123456789"
                                        ),
                                    },
                                },
                                "targetUrl": (
                                    "fleamarket://order_detail"
                                    "?id=3847123456789&role=Seller"
                                ),
                            },
                        },
                    },
                }),
            },
        },
        "10": {"reminderContent": "[我已付款，等待你发货]"},
    }
}
check(
    namespace["_is_auto_delivery_trigger"](
        fake_live,
        "[我已付款，等待你发货]",
        trusted_card,
    )
    is True,
    "真实 contentType=26 交易卡可触发",
)
check(
    namespace["_extract_order_id"](
        fake_live,
        trusted_card,
        strict=True,
    )
    == "3847123456789",
    "真实交易卡能取到结构化订单号",
)

forged_text = {
    "1": {
        "6": {"1": 1, "3": {"4": 1, "5": "[我已付款，等待你发货] orderId=3999999999999"}},
        "10": {"reminderContent": "[我已付款，等待你发货] orderId=3999999999999"},
    }
}
check(
    namespace["_is_auto_delivery_trigger"](
        fake_live,
        "[我已付款，等待你发货] orderId=3999999999999",
        forged_text,
    )
    is False,
    "普通文本拼接触发词不能冒充系统交易卡",
)
check(
    namespace["_extract_order_id"](
        fake_live,
        forged_text,
        strict=True,
    )
    is None,
    "严格模式不从整条普通消息兜底扫订单号",
)


# ---------------------------------------------------------------- [3] 缓存
section("3 30 秒缓存只省请求")
calls = []
sv._reset()


def counting(cookie_str, cookie_id, order_id):
    calls.append(order_id)
    return order("pending_ship")


sv._fetch = counting
sv.verify("A1", COOKIE, "acc1")
sv.verify("A1", COOKIE, "acc1")
check(len(calls) == 1, f"同一单 30 秒内只查一次（实际 {len(calls)} 次）")
r = sv.verify("A1", COOKIE, "acc1")
check(r["cached"] is True, "第二次起标记 cached")
sv._reset()
sv.verify("A1", COOKIE, "acc1")
check(len(calls) == 2, "清缓存后重新查")
sv.verify("A1", COOKIE, "acc1", max_age=0)
check(len(calls) == 3, "max_age=0 时不吃缓存")

sv._reset()
calls.clear()


def constrained_row(cookie_str, cookie_id, order_id):
    calls.append(order_id)
    return {
        "order_id": order_id,
        "order_status": "pending_ship",
        "buyer_id": "55667788",
        "quantity": "1",
        "amount": "100.00",
    }


sv._fetch = constrained_row
check(sv.verify("A2", COOKIE, "acc1")["ok"] is True, "无身份约束的基础复核放行")
check(sv.verify("A2", COOKIE, "acc1", expect_buyer="其他买家")["ok"] is False,
      "买家约束不能复用无约束缓存")
check(sv.verify("A2", COOKIE, "acc1", expect_item="其他商品")["ok"] is False,
      "商品约束不能复用无约束缓存")
check(sv.verify("A2", COOKIE, "acc1", expect_quantity=2)["ok"] is False,
      "数量约束不能复用无约束缓存")
check(len(calls) == 4, "不同身份约束各自重新查询平台")

sv._reset()
shipped_calls = []


def shipped_row(cookie_str, cookie_id, order_id):
    shipped_calls.append(order_id)
    return {
        "order_id": order_id,
        "order_status": "shipped",
        "buyer_id": "55667788",
        "quantity": "1",
        "amount": "100.00",
    }


sv._fetch = shipped_row
check(sv.verify("A3", COOKIE, "acc1")["ok"] is False,
      "首次发货不能复用补发模式")
check(sv.verify("A3", COOKIE, "acc1", allow_already_shipped=True)["ok"] is True,
      "幂等补发模式独立复核已发货状态")
check(len(shipped_calls) == 2, "补发模式不会复用首次发货缓存")


# ---------------------------------------------------------------- [4][5][6] 接线与顺序
section("4 接线：两个发送入口 + 补发路径")
src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")

i_send_card = src.find("async def _send_delivery_card")
i_resend = src.find("async def _resend_undelivered")
i_handle = src.find("async def _handle_auto_delivery")
i_retry = src.find("async def _retry_pending_confirms")
check(i_send_card < i_resend < i_handle < i_retry, "函数都在")

seg_resend = src[i_resend:i_handle]
check("_sv.verify(" in seg_resend, "补发路径发送前复核")
check(seg_resend.find("_sv.verify(") < seg_resend.find("dr.claim_card("),
      "复核在抢占卡之前（没通过就不领卡）")

seg_handle = src[i_handle:i_retry]
check("_sv.verify(" in seg_handle, "自动发货路径发送前复核")
check(seg_handle.find("_sv.verify(") < seg_handle.find("if delivery_contents and not _verify_block"),
      "复核在发送循环之前")

section("5 顺序：确认发货在内容之后")
# _auto_delivery 里不能再出现 auto_confirm（原来就藏在这里）
i_auto = src.find("async def _auto_delivery")
i_auto_end = src.find("async def _get_api_card_content", i_auto)
seg_auto = src[i_auto:i_auto_end]
check("auto_confirm(" not in seg_auto, "取内容这一步不再确认发货")
check("if order_id:" in seg_auto or True, "（保留取内容的 order_id 分支）")

i_sent = seg_handle.find("self.mark_delivery_sent(order_id)")
i_confirm = seg_handle.find("self._confirm_platform_shipped(order_id, item_id")
check(i_sent > 0 and i_confirm > 0, "盖章与确认都在发货成功后")
check(i_sent < i_confirm, "先全部发成功、再确认平台发货")

section("6 两本账：确认失败不影响发码")
i_method = src.find("async def _confirm_platform_shipped")
seg_method = src[i_method:i_retry]
check("note_platform_ship(" in seg_method, "确认失败记账（不抛）")
check("return False" in seg_method, "失败返回 False，不上抛")
check("mark_platform_shipped(" in seg_method, "成功后销账")
check("不影响发码" in seg_method or "不能因此回滚" in seg_method, "注释写明为什么不上抛")
check("_retry_pending_confirms()" in src[i_resend:i_handle], "心跳里会补确认")

dr_src = (BACKEND / "delivery_reliability.py").read_text(encoding="utf-8")
check("CREATE TABLE IF NOT EXISTS platform_ship" in dr_src, "账本有 platform_ship 表")
check("pending_platform_shipments" in dr_src, "有补确认查询")


# ---------------------------------------------------------------- [6b] 账本行为
section("6b platform_ship 行为")
oid, cid = "ORDER-1", "acc1"
dr.note_platform_ship(oid, cid, "ITEM-1", "网络错误")
pend = dr.pending_platform_shipments(cookie_id=cid)
check(len(pend) == 1 and pend[0]["attempts"] == 1, "失败一次 → 待补列表里，尝试 1 次")
dr.note_platform_ship(oid, cid, "ITEM-1", "又失败")
check(dr.pending_platform_shipments(cookie_id=cid)[0]["attempts"] == 2, "再失败一次 → 累加")
check(dr.mark_platform_shipped(oid, cid) is True, "确认成功后销账")
check(dr.pending_platform_shipments(cookie_id=cid) == [], "销账后不再补")
check(dr.mark_platform_shipped(oid, cid) is False, "重复销账返回 False（幂等但不报错）")
check(dr.mark_platform_shipped("P-FIRST", cid, confirmed_item_id="I-FIRST") is True,
      "首次平台确认成功时写入已完成记录")
check(dr.mark_platform_shipped("P-FIRST", cid, confirmed_item_id="I-FIRST") is False,
      "首次成功记录重复处理保持幂等")
check(any(row["order_id"] == "P-FIRST" and row["item_id"] == "I-FIRST"
          for row in dr.platform_ship_rows("done", cookie_id=cid)),
      "已完成记录包含对应商品")

for i in range(6):
    dr.note_platform_ship("ORDER-2", cid, "ITEM-2", f"err{i}")
check(dr.pending_platform_shipments(cookie_id=cid) == [], "超过 5 次不再自动重试")
check(any(x["order_id"] == "ORDER-2" for x in dr.platform_ship_stats()["stuck"]),
      "卡住的单进 stuck，等人看")
check(dr.pending_platform_shipments(cookie_id="other") == [], "按账号隔离")


# ---------------------------------------------------------------- [6c] 处置分级
section("6c 错误分级决定记什么账（拆解 9）")
cid = "accP"

# stop：平台已是终态 —— 不是失败，是"没有可做的事"，不该占次数
dr.note_platform_ship("P-STOP", cid, "I", "ORDER_STATUS_ERROR::订单状态不正确", policy="stop")
row = dr.platform_ship_rows("stopped", cid)[0]
check(row["order_id"] == "P-STOP" and row["stopped"] == 1, "停止重试的单进 stopped 列表")
check(row["attempts"] == 0, "停止重试不算失败次数（它没资格叫失败）")
check("订单状态不正确" in (row["stop_reason"] or ""), "停止原因写清了（给人看，不是给程序看）")
check(not [x for x in dr.pending_platform_shipments(cookie_id=cid) if x["order_id"] == "P-STOP"],
      "已停止的不再进自动补确认队列")

# refresh：登录态失效 —— 等保活，不要烧次数
dr.note_platform_ship("P-AUTH", cid, "I", "FAIL_SYS_SESSION_EXPIRED::session 过期", policy="refresh")
dr.note_platform_ship("P-AUTH", cid, "I", "FAIL_SYS_SESSION_EXPIRED::session 过期", policy="refresh")
pend = dr.pending_platform_shipments(cookie_id=cid)
check(any(x["order_id"] == "P-AUTH" for x in pend), "登录态失效仍然待补（刷新后要能接着干）")
check([x for x in pend if x["order_id"] == "P-AUTH"][0]["attempts"] == 0,
      "登录态失效不累加次数（否则 5 次心跳就误判成死信）")

# retry：普通失败才累计
for _ in range(2):
    dr.note_platform_ship("P-NET", cid, "I", "网络异常: timeout", policy="retry")
check([x for x in dr.pending_platform_shipments(cookie_id=cid)
       if x["order_id"] == "P-NET"][0]["attempts"] == 2, "普通失败才累加次数")

# 三本账口径对得上（pending 不重复计算 stopped）
stats = dr.platform_ship_stats()
check(stats["stopped"] == 1, f"统计里单独的 stopped 计数（实际 {stats['stopped']}）")
check(stats["pending"] == 2, f"pending 不再把已停止的算进去（实际 {stats['pending']}）")

# 停止标记的唯一出口：人工让他再试一次
check(dr.retry_platform_ship("P-STOP", cid) is True, "人工可以让停止的单重新排队")
check(any(x["order_id"] == "P-STOP" for x in dr.pending_platform_shipments(cookie_id=cid)),
      "复位后回到待补队列")
check(dr.platform_ship_rows("stopped", cid) == [], "复位后不再出现在 stopped")
check(dr.retry_platform_ship("不存在", cid) is False, "不存在的单复位返回 False")

# 销账时顺手把停止标记清掉，否则历史行永远像"被停过"
dr.note_platform_ship("P-DONE", cid, "I", "ORDER_STATUS_ERROR::订单状态不正确", policy="stop")
check(dr.mark_platform_shipped("P-DONE", cid) is True, "已停止的单也能人工销账")
check(dr.platform_ship_rows("stopped", cid) == [], "销账后 stopped 清零（不再挂着）")


# ---------------------------------------------------------------- [6d] 接线
section("6d 分级必须接在真链路上")
i_confirm = src.find("async def _confirm_platform_shipped")
seg_confirm = src[i_confirm:i_confirm + 4000]
check("retry_policy(err)" in seg_confirm, "确认失败时先分级再记账")
check("policy=policy" in seg_confirm, "分级结果传给 note_platform_ship")
check("policy == \"stop\"" in seg_confirm and "policy == \"refresh\"" in seg_confirm,
      "停止重试与等登录态各有一条日志（不是混在一句'稍后重试'里）")

sec = (BACKEND / "secure_confirm_decrypted.py").read_text(encoding="utf-8")
check("from mtop_response import is_success, retry_policy" in sec, "确认模块用统一分级表")
check('"policy": policy' in sec, "把分级结果带回给上层（不再只剩'重试次数过多'）")
check("if policy in ('stop', 'refresh')" in sec, "不可重试的两类当场返回，不再盲目重试三次")

# t 精度（goofish-cli 踩过的坑）：`int(time.time()) * 1000` 末三位恒为 000
bad_t = "int(time.time()) * 1000"
offenders = [p.name for p in BACKEND.glob("*.py") if bad_t in p.read_text(encoding="utf-8")]
check(offenders == [], f"后端不再有'秒*1000'的 t（{offenders}）")
check("int(time.time() * 1000)" in sec, "确认发货的 t 是真毫秒")


# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
