"""
发货前平台复核（拆解 7：`tswawa/xianyu-saas` 的 `verify_before_attempt`）
=====================================================================

**它照出的问题：我们的"该不该发货"是拿本机库里的状态判的，不是拿平台的状态判的。**

消息进来时 `_handle_auto_delivery` 先 `delivery_check(order_status)`，这没错；
但从"判完"到"发出去"之间还隔着：发货延时（可配置，几十秒到几分钟）、
限流等待、多张卡之间的 1 秒间隔、内联重试的退避，还有 C3 的**补发扫描**——
`resend_delay_minutes` 默认 10 分钟。

这十分钟里买家可能已经申请退款、订单可能被关闭、数量可能改过。
拿着旧结论发卡密，等于把货白送。

tswawa 的做法是：**每一次发送尝试之前**（包括重连、重试）重新拉一次平台订单，
核对 订单号 / 商品 / 买家 / 状态 / 数量 / 成交额，任一不符就整单转人工、**不重试**。
它的理由写得很直白：重试只会把同一个错误重复发送。

我们的实现有三条安全边界：

1. **身份或状态拿不到就不发。** 平台接口失败、没返回这单、缺少买家/商品/数量时
   返回 `ok=False`。卡密是不可逆交付，不能把"没证据"解释成"应该允许"。
2. **状态判定复用 `order_status_rules.delivery_check`**，不另写一套。两份状态表必然漂移，
   而漂移的方向永远是"新状态只加在一边"（这句话在 delivery_check 的注释里已经写过一次）。
3. **30 秒缓存。** 多张卡属于同一单，没必要每张卡打一次接口；但补发路径每次都是新调用。
   缓存只活在本进程内，重启即清——它只是省请求，不是状态。

开关 `SHIP_VERIFY_ENABLED=0` 完全回到"只看本机状态"。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Optional

ENV_SWITCH = "SHIP_VERIFY_ENABLED"
CACHE_TTL = 30.0

_cache: Dict[tuple[Any, ...], Dict[str, Any]] = {}
_lock = threading.Lock()


def enabled() -> bool:
    return (os.getenv(ENV_SWITCH, "1") or "1").strip() not in ("0", "false", "False", "off")


def _reset() -> None:
    """仅供测试。"""
    with _lock:
        _cache.clear()


def _fetch(cookie_str: str, cookie_id: str, order_id: str) -> Optional[Dict[str, Any]]:
    """取一单的平台数据。测试会替换它。拿不到返回 None（不是抛）。"""
    import seller_api
    got = seller_api.fetch_orders_by_ids(cookie_str, cookie_id, [order_id])
    return got.get(order_id)


def _qty(value: Any) -> Optional[int]:
    try:
        n = int(str(value).strip())
        return n if n > 0 else None
    except Exception:
        return None


def verify(order_id: str, cookie_str: str, cookie_id: str = "",
           expect_buyer: str = "", expect_item: str = "",
           expect_quantity: Any = None,
           max_age: float = CACHE_TTL,
           allow_already_shipped: bool = False) -> Dict[str, Any]:
    """
    返回 `{ok, verified, status, reason, order, cached}`。

    - `ok=False`：平台明确拒绝、身份不一致，或没有足够证据完成复核 → 调用方不许发。
    - `ok=True, verified=False`：只有显式关闭 `SHIP_VERIFY_ENABLED` 的维护模式。
    - `ok=True, verified=True`：平台确认可发。

    `allow_already_shipped` 只供幂等补发使用。平台明确显示“已发货”时，
    首次发货必须停止，但同一订单已有卡密补发仍可继续；退款、关闭和完成成交
    仍会被拒绝，买家与数量不一致也仍会拦截。
    """
    out: Dict[str, Any] = {"ok": False, "verified": False, "status": "",
                           "reason": "", "order": None, "cached": False}
    order_id = str(order_id or "").strip()
    if not order_id:
        out["reason"] = "没有订单号，跳过复核"
        return out
    if not enabled():
        out["ok"] = True
        out["reason"] = "复核开关已关闭"
        return out
    if not cookie_str:
        out["reason"] = "没有 Cookie，无法完成订单身份复核"
        return out

    now = time.time()
    cache_key = (
        str(cookie_id or ""),
        order_id,
        str(expect_buyer or ""),
        str(expect_item or ""),
        "" if expect_quantity is None else str(expect_quantity),
        bool(allow_already_shipped),
    )
    with _lock:
        hit = _cache.get(cache_key)
        if hit and (now - float(hit.get("ts") or 0)) < float(max_age):
            cached = dict(hit["value"])
            cached["cached"] = True
            return cached

    try:
        row = _fetch(cookie_str, cookie_id, order_id)
    except Exception as e:
        out["reason"] = f"复核请求失败，已停止自动发货：{e}"
        return out

    if not row:
        out["reason"] = "平台未返回该订单（接口关闭或查询失败），无法完成复核"
        return out

    try:
        from order_status_rules import delivery_check
        _dc = delivery_check(row.get("order_status"))
        status = _dc["status"]
        ok, reason = _dc["ok"], _dc["reason"]
    except Exception as exc:
        status = str(row.get("order_status") or "")
        ok, reason = False, f"订单状态规则执行失败，无法确认能否发货：{exc}"

    if allow_already_shipped and not ok and status == "shipped":
        ok = True
        reason = "平台已发货，允许同订单幂等补发"

    if ok:
        got_order_id = str(row.get("order_id") or "")
        if got_order_id and got_order_id != order_id:
            ok, reason = False, f"订单不一致（平台 {got_order_id}，本次 {order_id}）"

    if ok and expect_item:
        got_item = str(row.get("item_id") or "")
        if not got_item:
            ok, reason = False, "平台订单缺少商品身份，无法确认发货归属"
        elif got_item != str(expect_item):
            ok, reason = False, f"商品不一致（平台 {got_item}，本次 {expect_item}）"

    if ok and expect_buyer:
        got_buyer = str(row.get("buyer_id") or "")
        if not got_buyer:
            ok, reason = False, "平台订单缺少买家身份，无法确认发货归属"
        elif got_buyer != str(expect_buyer):
            ok, reason = False, f"买家不一致（平台 {got_buyer}，本次 {expect_buyer}）"

    if ok and expect_quantity is not None:
        want, got = _qty(expect_quantity), _qty(row.get("quantity"))
        if want and not got:
            ok, reason = False, "平台订单缺少数量，无法确认发货张数"
        elif want and got and want != got:
            ok, reason = False, f"数量不一致（平台 {got}，本次 {want}）"

    out.update({"ok": bool(ok), "verified": True, "status": status,
                "reason": reason or "", "order": row})
    with _lock:
        _cache[cache_key] = {"ts": now, "value": dict(out)}
    return out


__all__ = ["verify", "enabled", "ENV_SWITCH", "CACHE_TTL"]
