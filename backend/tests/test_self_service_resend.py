"""
买家回复“1/提取”后的自助补发测试。

覆盖：
[1] 只识别独立指令，且只在订单原会话内补发
[2] 两小时窗口、最近订单、账号与买家隔离
[3] 只复用 sent 且非空的卡，不改原卡账本
[4] 同一请求/同一买家冷却窗口的并发去重
[5] 请求结算、逐卡补发进度与失败留痕
[6] 消息入口、发货提示和方法接线

跑法：python tests/test_self_service_resend.py
"""

from __future__ import annotations

import ast
import asyncio
import os
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

_TMP = Path(tempfile.mkdtemp(prefix="self_service_resend_"))
os.environ["DELIVERY_RELIABILITY_DB"] = str(_TMP / "delivery.db")
os.environ["DELIVERY_RELIABILITY_CONFIG"] = str(_TMP / "delivery.json")

BACKEND_DIR = Path(__file__).resolve().parent.parent
SOURCE_PATH = BACKEND_DIR / "XianyuAutoAsync.py"
sys.path.insert(0, str(BACKEND_DIR))

import delivery_reliability as dr
import order_status_rules
import ship_verify

FAILS = []
LOCAL_ORDERS = {}
VOUCHER_ORDERS = set()


def check(condition, message):
    if condition:
        print(f"  ok   {message}")
    else:
        FAILS.append(message)
        print(f"  FAIL {message}")


def section(title):
    print(f"\n[{title}]")


def reset():
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(dr.DB_PATH) + suffix)
        if path.exists():
            path.unlink()
    if dr.CONFIG_PATH.exists():
        dr.CONFIG_PATH.unlink()
    with dr._CFG_LOCK:
        dr._CFG_CACHE.clear()


def iso_at(delta: timedelta) -> str:
    return (datetime.now() - delta).isoformat(timespec="seconds")


def mark_sent_order(order_id: str, cookie_id: str, buyer_id: str,
                    contents: list[str], sent_at: str, *, chat_id: str = "chat-1",
                    item_id: str = "item-1", order_status: str = "pending_ship"):
    dr.ensure_order(
        order_id,
        cookie_id,
        contents,
        item_id=item_id,
        chat_id=chat_id,
        buyer_id=buyer_id,
    )
    for row in dr.cards(order_id, cookie_id):
        claimed = dr.claim_card(order_id, cookie_id, row["idx"], owner="self-service-test")
        if claimed.get("claimed"):
            dr.mark_sent(order_id, cookie_id, row["idx"])
    conn = sqlite3.connect(str(dr.DB_PATH))
    try:
        conn.execute(
            "UPDATE delivery_cards SET created_at=?, updated_at=?, sent_at=? "
            "WHERE order_id=? AND cookie_id=?",
            (sent_at, sent_at, sent_at, order_id, cookie_id),
        )
        conn.commit()
    finally:
        conn.close()
    LOCAL_ORDERS[order_id] = {
        "order_id": order_id,
        "cookie_id": cookie_id,
        "item_id": item_id,
        "buyer_id": buyer_id,
        "order_status": order_status,
    }


def request_row(cookie_id: str, request_key: str):
    conn = sqlite3.connect(str(dr.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM delivery_self_service_requests "
            "WHERE cookie_id=? AND request_key=?",
            (cookie_id, request_key),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------- [1] 精确识别
section("1 精确识别“1/提取”与发货提示")
source = SOURCE_PATH.read_text(encoding="utf-8")
trigger = dr.is_self_service_resend_trigger
append_hint = dr.append_self_service_resend_hint
hint = dr.SELF_SERVICE_RESEND_HINT

for value in ("1", " 1 ", "\t1\n", "提取", " 提取 "):
    check(trigger(value) is True, f"识别独立指令 {value!r}")
for value in ("", None, "10", "1号", "请提取", "提取码", "一"):
    check(trigger(value) is False, f"不误判 {value!r}")

image = "__IMAGE_SEND__7|https://example.test/a.png"
check(append_hint(image) == image, "图片发货内容不追加文字提示")
text_once = append_hint("卡密 ABC-123")
check(text_once.endswith(hint), "文本发货内容末尾追加防漏提示")
check(append_hint(text_once) == text_once, "重复处理不会追加第二遍")
mid_hint = f"开头{hint}后面还有卡密"
check(append_hint(mid_hint).endswith(hint), "已有提示会被归位到文本末尾")
check(append_hint("") == hint, "空文本也形成完整提示")


# ---------------------------------------------------------------- [2] 最近订单
section("2 两小时窗口、最近订单与身份隔离")
reset()
COOKIE = "acc-main"
BUYER = "buyer-main"

mark_sent_order(
    "ORDER-OLD", COOKIE, BUYER, ["三小时前卡密"],
    iso_at(timedelta(hours=3)), chat_id="chat-old",
)
mark_sent_order(
    "ORDER-OLDER", COOKIE, BUYER, ["四十分钟前卡密"],
    iso_at(timedelta(minutes=40)), chat_id="chat-older",
)

dr.ensure_order(
    "ORDER-RECENT",
    COOKIE,
    ["最近文本卡", image, "结果未知卡"],
    item_id="item-recent",
    chat_id="chat-recent",
    buyer_id=BUYER,
)
for idx in (0, 1):
    claimed = dr.claim_card("ORDER-RECENT", COOKIE, idx, owner="self-service-test")
    if claimed.get("claimed"):
        dr.mark_sent("ORDER-RECENT", COOKIE, idx)
unknown = dr.claim_card("ORDER-RECENT", COOKIE, 2, owner="self-service-test")
if unknown.get("claimed"):
    dr.mark_unknown("ORDER-RECENT", COOKIE, 2, "写入结果未知")

recent_at = iso_at(timedelta(minutes=5))
conn = sqlite3.connect(str(dr.DB_PATH))
try:
    conn.execute(
        "UPDATE delivery_cards SET created_at=?, updated_at=?, sent_at=? "
        "WHERE order_id=? AND cookie_id=?",
        (recent_at, recent_at, recent_at, "ORDER-RECENT", COOKIE),
    )
    conn.execute(
        "INSERT INTO delivery_cards "
        "(order_id, cookie_id, idx, item_id, chat_id, buyer_id, spec, kind, payload, "
        " status, attempts, last_error, lease_owner, lease_token, lease_until, "
        " sent_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,'','text','', 'sent',1,'','','',0,'',?,?)",
        (
            "ORDER-RECENT",
            COOKIE,
            3,
            "item-recent",
            "chat-recent",
            BUYER,
            recent_at,
            recent_at,
        ),
    )
    conn.execute(
        "INSERT INTO delivery_cards "
        "(order_id, cookie_id, idx, item_id, chat_id, buyer_id, spec, kind, payload, "
        " status, attempts, last_error, lease_owner, lease_token, lease_until, "
        " sent_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,'','text','失败卡', 'failed',2,'模拟失败','','',0,'',?,?)",
        (
            "ORDER-RECENT",
            COOKIE,
            4,
            "item-recent",
            "chat-recent",
            BUYER,
            recent_at,
            recent_at,
        ),
    )
    conn.commit()
finally:
    conn.close()

mark_sent_order(
    "OTHER-ACCOUNT", "acc-other", BUYER, ["其他账号的一分钟卡"],
    iso_at(timedelta(minutes=1)), chat_id="chat-other-account",
)
mark_sent_order(
    "OTHER-BUYER", COOKIE, "buyer-other", ["同账号其他买家的一分钟卡"],
    iso_at(timedelta(minutes=1)), chat_id="chat-other-buyer",
)

snapshot_conn = sqlite3.connect(str(dr.DB_PATH))
snapshot_conn.row_factory = sqlite3.Row
try:
    before = [
        dict(row) for row in snapshot_conn.execute(
            "SELECT idx, status, payload, updated_at FROM delivery_cards "
            "WHERE order_id=? AND cookie_id=? ORDER BY idx",
            ("ORDER-RECENT", COOKIE),
        ).fetchall()
    ]
finally:
    snapshot_conn.close()

delivery = dr.latest_sent_delivery(COOKIE, BUYER, within_hours=2)
check(delivery is not None and delivery["order_id"] == "ORDER-RECENT",
      "选择当前账号当前买家两小时内最近订单")
check(delivery is not None and delivery["card_count"] == 2,
      "只统计 sent 且非空的卡")
check(delivery is not None and [card["idx"] for card in delivery["cards"]] == [0, 1],
      "排除 unknown 和空 payload")
check(delivery is not None and delivery["cards"][1]["kind"] == "image",
      "保留图片卡类型供发送端恢复标记")
check(delivery is not None and all(card["status"] == "sent" for card in delivery["cards"]),
      "failed 卡不会被自助补发")

dr.latest_sent_delivery(COOKIE, BUYER, within_hours=2)
snapshot_conn = sqlite3.connect(str(dr.DB_PATH))
snapshot_conn.row_factory = sqlite3.Row
try:
    after = [
        dict(row) for row in snapshot_conn.execute(
            "SELECT idx, status, payload, updated_at FROM delivery_cards "
            "WHERE order_id=? AND cookie_id=? ORDER BY idx",
            ("ORDER-RECENT", COOKIE),
        ).fetchall()
    ]
finally:
    snapshot_conn.close()
check(after == before, "查询最近已发订单不修改原卡状态与内容")

reset()
mark_sent_order(
    "ONLY-OLD", COOKIE, BUYER, ["三小时前卡密"],
    iso_at(timedelta(hours=3)),
)
check(dr.latest_sent_delivery(COOKIE, BUYER, within_hours=2) is None,
      "超过两小时的 sent 卡不补")
check(dr.latest_sent_delivery(COOKIE, "nobody", within_hours=2) is None,
      "陌生买家没有可补订单")


# ---------------------------------------------------------------- [3] 请求去重
section("3 请求键与买家冷却窗口")
reset()
first = dr.begin_self_service_request("msg-1", COOKIE, BUYER, "chat-1")
check(first.get("allowed") is True, "首条自助补发请求可执行")
duplicate = dr.begin_self_service_request("msg-1", COOKIE, BUYER, "chat-1")
check(duplicate.get("allowed") is False and duplicate.get("reason") == "duplicate",
      "同一请求重复调用只成功一次")
rapid = dr.begin_self_service_request("msg-2", COOKIE, BUYER, "chat-1")
check(rapid.get("allowed") is False and rapid.get("reason") == "cooldown",
      "同账号同买家不同请求在冷却期内被挡住")
other_buyer = dr.begin_self_service_request("msg-buyer-2", COOKIE, "buyer-2", "chat-2")
check(other_buyer.get("allowed") is True, "不同买家互不阻塞")
other_account = dr.begin_self_service_request("msg-account-2", "acc-2", BUYER, "chat-3")
check(other_account.get("allowed") is True, "不同账号互不阻塞")

check(dr.finish_self_service_request(
    "msg-1", COOKIE, "sent", order_id="ORDER-1", card_count=2,
) is True, "成功请求可结算")
row = request_row(COOKIE, "msg-1")
check(
    row and row["status"] == "sent" and row["order_id"] == "ORDER-1"
    and row["card_count"] == 2,
    "成功结算写入订单、张数和 sent 状态",
)

check(dr.finish_self_service_request(
    "msg-account-2", "acc-2", "unknown", order_id="ORDER-2", last_error="连接中断",
) is True, "失败请求可写入 unknown 状态")
row = request_row("acc-2", "msg-account-2")
check(row and row["status"] == "unknown" and row["last_error"] == "连接中断",
      "失败原因写入请求账本")
try:
    dr.finish_self_service_request("msg-1", COOKIE, "arbitrary")
    invalid_status_rejected = False
except ValueError:
    invalid_status_rejected = True
check(invalid_status_rejected, "拒绝未知请求状态")

reset()
check(dr.begin_self_service_request(
    "empty-1", COOKIE, BUYER, "chat-empty",
).get("allowed") is True, "无货请求先占用买家冷却窗口")
check(dr.finish_self_service_request(
    "empty-1", COOKIE, "no_delivery", last_error="没有已发卡密",
) is True, "无货结果可结算")
check(dr.begin_self_service_request(
    "empty-2", COOKIE, BUYER, "chat-empty",
).get("reason") == "cooldown", "无货结果也受冷却窗口约束")


def concurrent_acquire(keys: list[str]):
    barrier = threading.Barrier(len(keys))
    result_lock = threading.Lock()
    results = []

    def worker(key: str):
        barrier.wait()
        result = dr.begin_self_service_request(key, COOKIE, BUYER, "chat-concurrent")
        with result_lock:
            results.append(result)

    threads = [threading.Thread(target=worker, args=(key,)) for key in keys]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


reset()
same_key_results = concurrent_acquire(["same-message", "same-message"])
check(sum(result.get("allowed") is True for result in same_key_results) == 1,
      "两个线程提交同一请求时只有一个成功")

reset()
different_key_results = concurrent_acquire(["message-a", "message-b"])
check(sum(result.get("allowed") is True for result in different_key_results) == 1,
      "两个线程为同一买家提交不同请求时只有一个成功")
check(any(result.get("reason") == "cooldown" for result in different_key_results),
      "并发竞争中未拿到发送权的请求记录冷却原因")

# ---------------------------------------------------------------- [3b] 逐卡进度
section("3b 自助补发逐卡进度跨请求持久化")
reset()
claim = dr.claim_self_service_card(
    COOKIE,
    BUYER,
    "RESEND-PROGRESS",
    0,
    owner="progress-test",
)
check(claim.get("claimed") is True, "首次补发可领取卡位")
token = str(claim["card"]["lease_token"])
check(
    dr.finish_self_service_card(
        COOKIE,
        BUYER,
        "RESEND-PROGRESS",
        0,
        "sent",
        token=token,
    )
    is True,
    "补发成功后逐卡记为 sent",
)
again = dr.claim_self_service_card(
    COOKIE,
    BUYER,
    "RESEND-PROGRESS",
    0,
    owner="progress-test",
)
check(
    again.get("claimed") is False and again.get("reason") == "sent",
    "同订单同卡位已补发后不会再次领取",
)


# ---------------------------------------------------------------- [4] 源码接线
section("4 消息入口与自助补发方法接线")
i_system_filter = source.find("elif send_message == '已发货':")
i_self_service = source.find(
    "_dr_self_service.is_self_service_resend_trigger(send_message)"
)
i_auto_delivery = source.find(
    "elif self._is_auto_delivery_trigger(send_message, message):"
)
check(
    0 <= i_system_filter < i_self_service < i_auto_delivery,
    "自助补发位于系统消息过滤后、自动发货触发前",
)
check("await self._handle_self_service_resend(" in source,
      "消息入口调用自助补发处理器")
check("session_type=session_type" in source,
      "消息入口把群聊类型传给自助补发处理器")

_, method_source = source.split("    async def _handle_self_service_resend(", 1)
method_source = method_source.split("\n    async def ", 1)[0]
source_tree = ast.parse(source)
method_node = next(
    node for node in ast.walk(source_tree)
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_self_service_resend"
)
method_calls = []
finish_statuses = []
for node in ast.walk(method_node):
    if not isinstance(node, ast.Call):
        continue
    if isinstance(node.func, ast.Name):
        method_calls.append(node.func.id)
    elif isinstance(node.func, ast.Attribute):
        method_calls.append(node.func.attr)
        if node.func.attr == "finish_self_service_request":
            finish_statuses.extend(
                arg.value for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            )
check("dr.begin_self_service_request(" in method_source,
      "处理器先原子登记请求")
check("dr.latest_sent_delivery(self.cookie_id, buyer_id, within_hours=2)" in method_source,
      "处理器只查询当前买家两小时内的已发订单")
check("await self._send_delivery_card(" in method_source,
      "处理器逐张调用现有发货发送口")
check("dr.claim_self_service_card(" in method_source,
      "处理器发送前领取逐卡补发位")
check("dr.finish_self_service_card(" in method_source,
      "处理器发送后逐卡结算补发位")
check("sent" in finish_statuses,
      "全部写帧成功后结算 sent")
check("unknown" in finish_statuses,
      "异常结算 unknown")
check("send_delivery_failure_notification(" in method_source,
      "异常发送失败通知")
for forbidden in ("claim_card(", "mark_sent(", "mark_failed(", "_set_status("):
    check(forbidden.rstrip("(") not in method_calls,
          f"补发不改原卡账本（无调用 {forbidden}）")

check('_dr_content.append_self_service_resend_hint(delivery_content)' in source,
      "无备注文本发货会追加防漏提示")
check('_dr_content.append_self_service_resend_hint(processed_description)' in source,
      "带变量备注的文本发货会追加防漏提示")
check('_dr_content.append_self_service_resend_hint(\n                    f"{processed_description}' in source,
      "普通备注文本发货会追加防漏提示")


# ---------------------------------------------------------------- [5] 处理行为
section("5 自助补发处理行为")


class QuietLogger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def critical(self, *_args, **_kwargs):
        pass


async def to_thread_inline(func, /, *args, **kwargs):
    return func(*args, **kwargs)


gate_write = Mock(return_value=(True, 0))
platform_verify = Mock(return_value={
    "ok": True,
    "verified": True,
    "status": "pending_ship",
    "reason": "",
})

handler_globals = {
    "asyncio": SimpleNamespace(sleep=AsyncMock(), to_thread=to_thread_inline),
    "logger": QuietLogger(),
    "os": os,
}
shared_blacklist = {}
sys.modules["db_manager"] = SimpleNamespace(
    db_manager=SimpleNamespace(
        get_order_by_id=lambda order_id: LOCAL_ORDERS.get(str(order_id)),
        get_cross_shop_blacklist_entry=shared_blacklist.get,
        is_voucher_async_order=lambda order_id, cookie_id: order_id in VOUCHER_ORDERS,
    ),
)
sys.modules["order_status_rules"] = SimpleNamespace(
    delivery_check=order_status_rules.delivery_check,
    cross_shop_block_reason=order_status_rules.cross_shop_block_reason,
)
sys.modules["rate_limiter"] = SimpleNamespace(gate_write=gate_write)
sys.modules["ship_verify"] = SimpleNamespace(verify=platform_verify)
handler_tree = ast.parse(source)
handler_node = next(
    node for node in ast.walk(handler_tree)
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_self_service_resend"
)
exec(
    compile(
        ast.Module(body=[handler_node], type_ignores=[]),
        "XianyuAutoAsync.py",
        "exec",
    ),
    handler_globals,
)
handle_self_service_resend = handler_globals["_handle_self_service_resend"]


class ResendLive:
    def __init__(self, request_key):
        self.cookie_id = COOKIE
        self.cookies_str = "test-cookie"
        self.myid = "seller-main"
        self.request_key = request_key
        self.sent = []
        self.failure_notifications = []
        self.fail_send = False

    def _reply_outbox_key(self, _message_data, _chat_id, _send_message):
        return self.request_key

    def _safe_str(self, exc):
        return str(exc)

    async def _send_delivery_card(
        self, websocket, chat_id, send_user_id, delivery_content,
        index, total, user_url, msg_time,
    ):
        if self.fail_send:
            raise RuntimeError("模拟补发写入失败")
        self.sent.append({
            "websocket": websocket,
            "chat_id": chat_id,
            "buyer_id": send_user_id,
            "content": delivery_content,
            "index": index,
            "total": total,
            "user_url": user_url,
            "msg_time": msg_time,
        })

    async def send_delivery_failure_notification(self, *args):
        self.failure_notifications.append(args)


reset()
mark_sent_order(
    "RESEND-OK",
    COOKIE,
    BUYER,
    ["补发文本卡", image],
    iso_at(timedelta(minutes=3)),
    chat_id="chat-resend",
    item_id="item-resend",
)
resend_live = ResendLive("resend-message-1")
resend_ws = object()
with patch.dict(sys.modules, {"notify_service": SimpleNamespace(send_event=Mock())}):
    asyncio.run(
        handle_self_service_resend(
            resend_live,
            resend_ws,
            {},
            "买家",
            BUYER,
            "1",
            "chat-resend",
            "2026-11-18 16:00:00",
        )
    )

check([item["content"] for item in resend_live.sent] == ["补发文本卡", image],
      "处理器按原顺序补发文本与图片")
check(all(item["chat_id"] == "chat-resend" and item["buyer_id"] == BUYER
          for item in resend_live.sent),
      "补发目标限定当前会话与当前买家")
successful_request = request_row(COOKIE, "resend-message-1")
check(successful_request is not None and successful_request["status"] == "sent"
      and successful_request["order_id"] == "RESEND-OK"
      and successful_request["card_count"] == 2,
      "处理成功后请求账本记录 sent、原订单和卡数")
check(
    platform_verify.call_args is not None
    and platform_verify.call_args.kwargs.get("allow_already_shipped") is True,
    "处理器以已发货幂等模式复核平台订单",
)
check([row["status"] for row in dr.cards("RESEND-OK", COOKIE)] == ["sent", "sent"],
      "补发处理不修改原卡账本")

resend_live.request_key = "resend-message-2"
asyncio.run(
    handle_self_service_resend(
        resend_live,
        resend_ws,
        {},
        "买家",
        BUYER,
        "提取",
        "chat-resend",
        "2026-11-18 16:01:00",
    )
)
check(len(resend_live.sent) == 2, "十分钟内再次回复不会重复补发")

reset()
mark_sent_order(
    "RESEND-BLOCKED", COOKIE, BUYER, ["待拦截卡密"], iso_at(timedelta(minutes=2)),
    chat_id="chat-resend", item_id="item-resend",
)
shared_blacklist[BUYER] = {"buyer_id": BUYER}
blocked_live = ResendLive("resend-blocked-message")
platform_verify.reset_mock()
asyncio.run(handle_self_service_resend(
    blocked_live, object(), {}, "买家", BUYER, "提取", "chat-resend", "2026-11-18 16:02:00",
))
check(not blocked_live.sent and not platform_verify.called
      and request_row(COOKIE, "resend-blocked-message")["status"] == "failed",
      "跨店黑名单拦截自助补发")
shared_blacklist.clear()

reset()
mark_sent_order(
    "RESEND-FAIL",
    COOKIE,
    BUYER,
    ["会失败的卡"],
    iso_at(timedelta(minutes=2)),
    chat_id="chat-resend-fail",
    item_id="item-resend-fail",
)
failed_live = ResendLive("resend-failed-message")
failed_live.fail_send = True
notify_event = Mock()
with patch.dict(sys.modules, {"notify_service": SimpleNamespace(send_event=notify_event)}):
    asyncio.run(
        handle_self_service_resend(
            failed_live,
            object(),
            {},
            "买家",
            BUYER,
            "1",
            "chat-resend-fail",
            "2026-11-18 16:02:00",
        )
    )
failed_request = request_row(COOKIE, "resend-failed-message")
check(failed_request is not None and failed_request["status"] == "unknown",
      "补发写入异常时请求账本记录 unknown")
check(failed_request is not None and "模拟补发写入失败" in failed_request["last_error"],
      "补发异常原因写入请求账本")
check(len(failed_live.failure_notifications) == 1,
      "补发异常会发送失败通知")
check(notify_event.call_count == 1,
      "补发异常会写入风险事件")
check([row["status"] for row in dr.cards("RESEND-FAIL", COOKIE)] == ["sent"],
      "补发失败也不回写原卡状态")

def configure_successful_handler_dependencies():
    gate_write.reset_mock()
    gate_write.side_effect = None
    gate_write.return_value = (True, 0)
    platform_verify.reset_mock()
    platform_verify.side_effect = None
    platform_verify.return_value = {
        "ok": True,
        "verified": True,
        "status": "pending_ship",
        "reason": "",
    }


configure_successful_handler_dependencies()
reset()
group_buyer = "buyer-group"
group_live = ResendLive("group-message")
mark_sent_order(
    "RESEND-GROUP",
    COOKIE,
    group_buyer,
    ["群聊中不应公开的卡密"],
    iso_at(timedelta(minutes=2)),
    chat_id="group-chat",
    item_id="item-group",
)
asyncio.run(
    handle_self_service_resend(
        group_live,
        object(),
        {},
        "群成员",
        group_buyer,
        "1",
        "group-chat",
        "2026-11-18 16:03:00",
        session_type="30",
    )
)
check(not group_live.sent, "群聊消息不会发送任何补发卡密")
check(request_row(COOKIE, "group-message") is None,
      "群聊消息不会登记自助补发请求")


def run_handler(
    live,
    buyer_id,
    *,
    command="1",
    chat_id="chat-safety",
    session_type="1",
):
    notify_event = Mock()
    with patch.dict(
        sys.modules,
        {"notify_service": SimpleNamespace(send_event=notify_event)},
    ):
        asyncio.run(
            handle_self_service_resend(
                live,
                object(),
                {},
                "买家",
                buyer_id,
                command,
                chat_id,
                "2026-11-18 16:10:00",
                session_type=session_type,
            )
        )
    return notify_event


for blocked_status in ("refunding", "cancelled", "completed"):
    configure_successful_handler_dependencies()
    reset()
    blocked_buyer = f"buyer-{blocked_status}"
    blocked_order = f"ORDER-{blocked_status.upper()}"
    blocked_chat = f"chat-{blocked_status}"
    mark_sent_order(
        blocked_order,
        COOKIE,
        blocked_buyer,
        ["不应补发的卡"],
        iso_at(timedelta(minutes=2)),
        chat_id=blocked_chat,
        item_id=f"item-{blocked_status}",
        order_status=blocked_status,
    )
    blocked_live = ResendLive(f"blocked-{blocked_status}")
    run_handler(
        blocked_live,
        blocked_buyer,
        chat_id=blocked_chat,
    )
    blocked_request = request_row(COOKIE, f"blocked-{blocked_status}")
    check(
        not blocked_live.sent
        and blocked_request is not None
        and blocked_request["status"] == "failed"
        and blocked_status in blocked_request["last_error"],
        f"订单状态 {blocked_status} 时拒绝自助补发",
    )

configure_successful_handler_dependencies()
reset()
voucher_buyer = "buyer-voucher"
voucher_order = "ORDER-VOUCHER"
voucher_chat = "chat-voucher"
mark_sent_order(
    voucher_order,
    COOKIE,
    voucher_buyer,
    ["充值订单不应补发卡密"],
    iso_at(timedelta(minutes=2)),
    chat_id=voucher_chat,
    item_id="item-voucher",
)
VOUCHER_ORDERS.add(voucher_order)
voucher_live = ResendLive("blocked-voucher")
run_handler(voucher_live, voucher_buyer, chat_id=voucher_chat)
voucher_request = request_row(COOKIE, "blocked-voucher")
check(
    not voucher_live.sent
    and voucher_request is not None
    and voucher_request["status"] == "failed"
    and "异步充值" in voucher_request["last_error"],
    "异步充值订单拒绝自助补发卡密",
)
VOUCHER_ORDERS.clear()

configure_successful_handler_dependencies()
reset()
rejected_buyer = "buyer-platform-rejected"
rejected_order = "ORDER-PLATFORM-REJECTED"
rejected_chat = "chat-platform-rejected"
mark_sent_order(
    rejected_order,
    COOKIE,
    rejected_buyer,
    ["平台拒绝后不应出现的卡"],
    iso_at(timedelta(minutes=2)),
    chat_id=rejected_chat,
    item_id="item-platform-rejected",
)
platform_verify.return_value = {
    "ok": False,
    "verified": True,
    "status": "refunding",
    "reason": "平台订单正在退款",
}
rejected_live = ResendLive("platform-rejected-message")
run_handler(rejected_live, rejected_buyer, chat_id=rejected_chat)
rejected_request = request_row(COOKIE, "platform-rejected-message")
check(
    not rejected_live.sent
    and rejected_request is not None
    and rejected_request["status"] == "failed"
    and "平台订单正在退款" in rejected_request["last_error"],
    "平台复核拒绝时停止自助补发并留痕",
)

configure_successful_handler_dependencies()
reset()
shipped_buyer = "buyer-already-shipped"
shipped_order = "ORDER-ALREADY-SHIPPED"
shipped_chat = "chat-already-shipped"
mark_sent_order(
    shipped_order,
    COOKIE,
    shipped_buyer,
    ["已发货订单的补发卡"],
    iso_at(timedelta(minutes=2)),
    chat_id=shipped_chat,
    item_id="item-already-shipped",
)
platform_verify.return_value = {
    "ok": True,
    "verified": True,
    "status": "shipped",
    "reason": "平台已发货，允许同订单幂等补发",
}
shipped_live = ResendLive("already-shipped-message")
run_handler(shipped_live, shipped_buyer, chat_id=shipped_chat)
check(
    [item["content"] for item in shipped_live.sent] == ["已发货订单的补发卡"]
    and platform_verify.call_args.kwargs.get("allow_already_shipped") is True,
    "平台已发货订单允许幂等自助补发",
)

ship_verify._cache.clear()
with (
    patch.object(ship_verify, "enabled", return_value=True),
    patch.object(
        ship_verify,
        "_fetch",
        return_value={
            "order_status": "已发货",
            "buyer_id": shipped_buyer,
            "quantity": 1,
        },
    ),
):
    shipped_allowed = ship_verify.verify(
        shipped_order,
        "test-cookie",
        COOKIE,
        expect_buyer=shipped_buyer,
        max_age=0,
        allow_already_shipped=True,
    )
    shipped_denied_for_first_delivery = ship_verify.verify(
        shipped_order,
        "test-cookie",
        COOKIE,
        expect_buyer=shipped_buyer,
        max_age=0,
        allow_already_shipped=False,
    )
check(
    shipped_allowed["ok"] is True
    and shipped_allowed["status"] == "shipped"
    and shipped_denied_for_first_delivery["ok"] is False,
    "平台已发货只放行幂等补发，不放行首次发货",
)

configure_successful_handler_dependencies()
reset()
first_limit_buyer = "buyer-first-write-limited"
first_limit_order = "ORDER-FIRST-WRITE-LIMITED"
first_limit_chat = "chat-first-write-limited"
mark_sent_order(
    first_limit_order,
    COOKIE,
    first_limit_buyer,
    ["首张限流不能发送"],
    iso_at(timedelta(minutes=2)),
    chat_id=first_limit_chat,
    item_id="item-first-write-limited",
)
gate_write.return_value = (False, 12)
first_limit_live = ResendLive("first-write-limited-message")
run_handler(first_limit_live, first_limit_buyer, chat_id=first_limit_chat)
first_limit_request = request_row(COOKIE, "first-write-limited-message")
check(
    not first_limit_live.sent
    and first_limit_request is not None
    and first_limit_request["status"] == "failed"
    and "12 秒" in first_limit_request["last_error"],
    "首张写入限流时零发送并记录 failed",
)

configure_successful_handler_dependencies()
reset()
partial_limit_buyer = "buyer-partial-write-limited"
partial_limit_order = "ORDER-PARTIAL-WRITE-LIMITED"
partial_limit_chat = "chat-partial-write-limited"
mark_sent_order(
    partial_limit_order,
    COOKIE,
    partial_limit_buyer,
    ["第一张已发", "第二张被限流"],
    iso_at(timedelta(minutes=2)),
    chat_id=partial_limit_chat,
    item_id="item-partial-write-limited",
)
gate_write.side_effect = [(True, 0), (False, 30)]
partial_limit_live = ResendLive("partial-write-limited-message")
partial_limit_notify = run_handler(
    partial_limit_live,
    partial_limit_buyer,
    chat_id=partial_limit_chat,
)
partial_limit_request = request_row(COOKIE, "partial-write-limited-message")
check(
    [item["content"] for item in partial_limit_live.sent] == ["第一张已发"],
    "部分发送后限流不会继续发送后续卡",
)
check(
    partial_limit_request is not None
    and partial_limit_request["status"] == "unknown"
    and "1/2" in partial_limit_request["last_error"]
    and "30 秒" in partial_limit_request["last_error"],
    "部分发送后限流结算 unknown 并记录已发数量",
)
check(
    len(partial_limit_live.failure_notifications) == 1
    and partial_limit_notify.call_count == 1,
    "部分发送后限流触发失败通知与风险事件",
)

configure_successful_handler_dependencies()
partial_limit_conn = sqlite3.connect(str(dr.DB_PATH))
try:
    partial_limit_conn.execute(
        "UPDATE delivery_self_service_requests SET updated_at=? "
        "WHERE cookie_id=? AND request_key=?",
        (
            iso_at(timedelta(minutes=11)),
            COOKIE,
            "partial-write-limited-message",
        ),
    )
    partial_limit_conn.commit()
finally:
    partial_limit_conn.close()

partial_retry_live = ResendLive("partial-write-limited-retry")
run_handler(
    partial_retry_live,
    partial_limit_buyer,
    chat_id=partial_limit_chat,
)
check(
    [item["content"] for item in partial_retry_live.sent] == ["第二张被限流"],
    "冷却后重试只发送上次未完成的卡，不重复第一张",
)
partial_retry_request = request_row(COOKIE, "partial-write-limited-retry")
check(
    partial_retry_request is not None
    and partial_retry_request["status"] == "sent"
    and partial_retry_request["card_count"] == 1,
    "重试请求按本次实际补发张数结算",
)

configure_successful_handler_dependencies()
reset()
chat_scope_buyer = "buyer-chat-scope"
chat_scope_order = "ORDER-CHAT-SCOPE"
mark_sent_order(
    chat_scope_order,
    COOKIE,
    chat_scope_buyer,
    ["只能在原订单会话补发的卡"],
    iso_at(timedelta(minutes=2)),
    chat_id="chat-original-order",
    item_id="item-chat-scope",
)
chat_scope_live = ResendLive("wrong-chat-message")
run_handler(
    chat_scope_live,
    chat_scope_buyer,
    chat_id="chat-unrelated",
)
chat_scope_request = request_row(COOKIE, "wrong-chat-message")
check(
    not chat_scope_live.sent
    and chat_scope_request is not None
    and chat_scope_request["status"] == "failed"
    and "会话" in chat_scope_request["last_error"],
    "同一买家在非订单会话回复 1 时不会补发卡密",
)


# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for failure in FAILS:
        print("   -", failure)
    sys.exit(1)
print("✅ 全部通过")
