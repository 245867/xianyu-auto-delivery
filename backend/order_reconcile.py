"""
订单全量对账（拆解 11：`yelinyuan798-commits/UniversalECommerceBot` 的 `core/fulfillment_runtime.py`）
=========================================================================

**我们的订单是怎么进库的：靠消息。** 买家付款 → 平台推一条交易消息 → `handle_message`
认出订单号 → `insert_or_update_order` → 发货。整条链路的入口只有一个：**那条消息**。

于是有一个很贵、但平时看不见的失败模式：**平台有单，我们没单。**
断线几分钟、消息被风控吞掉、进程正好在重启、平台干脆没推——买家付了钱，
在我们的界面里这单根本不存在，C3 的补发扫描也扫不到它（补发只认库里的单）。
等到买家来问"怎么还不发货"，已经过去半小时了。

UniversalECommerceBot 的做法是 `fulfillment_runtime`：**每 30 秒从第 1 页全量重扫，
靠账本去重，而不是靠"上次扫到哪"。** 这一条值得抄，因为它把
"增量游标"这个隐形状态从系统里删掉了：

- 游标一旦错位（比如某次翻页失败后把 page 记成 3），漏掉的那一单**永远不会再出现**；
- 重扫没有游标可错，成本只是一次接口调用（`sold.get` 一次 20 单）；
- 去重靠 `orders.order_id` 这个天然主键，不靠内存、不靠时间窗口。

## 我们的落地（三处不同）

1. **节拍不是 30 秒。** 我们挂在心跳上，约 10 分钟一轮，每轮只扫第 1 页（20 单）。
   闲鱼卖家一天几十单，10 分钟内的新单必然落在第 1 页；扫更多页是浪费配额。
2. **只登记 + 报警，不自动发货。** 发货要过三道闸门（状态/黑名单/重复发货）和
   A3 的平台复核，而"从订单列表反推发货"没有消息上下文（买家会话、聊天里的规格），
   等于绕过闸门去发货。对账的职责是**让漏单可见**：单进库之后，买家再发一句话、
   或者人工点一下，就走正常链路了。
3. **多一个桶：`status_drift`。** 平台说"待发货"、我们库里有单但还停在"处理中"，
   这不是漏单，是本地状态滞后——同样值得报，因为 C3 的补发和平台确认都读本地状态。

## 口径

| 桶 | 含义 | 处置 |
|---|---|---|
| `missing_paid` | 平台有单（待发货/已付款），本地没有 | **报警**，买家可能已经在等 |
| `missing_other` | 平台有单，本地没有，但已经发货/完成/关闭 | 静默登记，补历史 |
| `status_drift` | 两边都有，状态不一致 | 以平台为准更新本地 |
| `ok` | 一致 | — |
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from loguru import logger

# 平台上"这单还欠一次发货"的状态（normalize_status 之后）
LIVE_STATUSES = ("pending", "pending_ship", "processing", "refunding")


def diff(platform_rows: List[Dict[str, Any]],
         local_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    纯函数：平台的一份列表 vs 本地的一份列表，算出四类差异。

    platform_rows: `seller_api.parse_sold_order()` 的产物
    local_rows:    数据库订单（order_id、status）
    """
    local = {}
    for r in local_rows or []:
        oid = str((r or {}).get("order_id") or "")
        if oid:
            local[oid] = r or {}

    missing_paid: List[Dict[str, Any]] = []
    missing_other: List[Dict[str, Any]] = []
    drift: List[Dict[str, Any]] = []
    ok = 0
    seen: set = set()

    for row in platform_rows or []:
        oid = str((row or {}).get("order_id") or "")
        if not oid or oid in seen:
            continue
        seen.add(oid)
        p_status = str(row.get("order_status") or "")
        mine = local.get(oid)
        if not mine:
            (missing_paid if p_status in LIVE_STATUSES else missing_other).append(row)
            continue
        l_status = str(mine.get("status") or "")
        if p_status and p_status != "unknown" and l_status != p_status:
            drift.append({"order_id": oid, "local": l_status, "platform": p_status,
                          "platform_row": row, "system_shipped": mine.get("system_shipped")})
        else:
            ok += 1

    return {
        "platform_count": len(seen),
        "local_count": len(local),
        "missing_paid": missing_paid,
        "missing_other": missing_other,
        "status_drift": drift,
        "ok": ok,
    }


def _register(row: Dict[str, Any], cookie_id: str) -> bool:
    """把平台的一行登记进本地库。已存在时只补空字段（insert_or_update_order 自带 merge）。"""
    try:
        from db_manager import db_manager
        return db_manager.insert_or_update_order(
            order_id=str(row.get("order_id") or ""),
            item_id=str(row.get("item_id") or "") or None,
            buyer_id=str(row.get("buyer_id") or "") or None,
            spec_name=str(row.get("spec_name") or "") or None,
            spec_value=str(row.get("spec_value") or "") or None,
            quantity=str(row.get("quantity") or "") or None,
            amount=str(row.get("amount") or "") or None,
            order_status=str(row.get("order_status") or "") or None,
            cookie_id=cookie_id,
            created_at=str(row.get("order_time") or "") or None,
            receiver_name=str(row.get("receiver_name") or "") or None,
            receiver_phone=str(row.get("receiver_phone") or "") or None,
            receiver_address=str(row.get("receiver_address") or "") or None,
        )
    except Exception as e:
        logger.warning(f"对账登记订单 {row.get('order_id')} 失败: {e}")
        return False


def reconcile(cookie_str: str, cookie_id: str, pages: int = 1, rows_per_page: int = 20,
              days: Optional[int] = 2, register: bool = True,
              notify_missing: bool = True) -> Dict[str, Any]:
    """
    拉平台第 1..pages 页，和本地库对一遍。

    永不抛异常：拉不到就返回 `{"ok": False, "error": ...}`，不能因为对账失败影响心跳。
    """
    try:
        import seller_api
        poll_started_ns = time.time_ns()
        report = seller_api.fetch_sold_order_pages(
            cookie_str, days=days, rows_per_page=rows_per_page, max_pages=pages, cookie_id=cookie_id
        )
    except Exception as e:
        logger.warning(f"【{cookie_id}】订单对账拉取失败: {type(e).__name__}")
        return {"ok": False, "error": str(e)}

    platform_rows: Dict[str, Dict[str, Any]] = {}
    failed = 0
    for raw in report["items"]:
        try:
            row = seller_api.parse_sold_order(raw)
            if not row["order_id"]:
                failed += 1
                continue
            platform_rows.setdefault(row["order_id"], row)
        except Exception:
            failed += 1

    registered = refreshed = 0
    observed_statuses: Dict[str, str] = {}
    try:
        from db_manager import db_manager
        with db_manager.lock:
            account_rows = []
            local_rows = []
            for order_id, row in platform_rows.items():
                local = db_manager.get_order_by_id(order_id)
                if local and local["cookie_id"] not in (None, "", cookie_id):
                    failed += 1
                    continue
                if (local and local.get("dropship_status") and row.get("in_refund")
                        and row["order_status"] != "cancelled"):
                    row = {**row, "order_status": "refunding"}
                account_rows.append(row)
                if local and local["cookie_id"] == cookie_id:
                    local_rows.append(local)

            d = diff(account_rows, local_rows)
            if register:
                for row in d["missing_paid"] + d["missing_other"]:
                    if _register(row, cookie_id):
                        registered += 1
                    else:
                        failed += 1
                for item in d["status_drift"]:
                    order_id = item["order_id"]
                    if not db_manager.insert_or_update_order(order_id=order_id,
                                                              order_status=item["platform"],
                                                              cookie_id=cookie_id):
                        failed += 1
                        continue
                    updated = db_manager.get_order_by_id(order_id)
                    if updated and updated["status"] == item["platform"]:
                        refreshed += 1
                for row in account_rows:
                    if row.get("api_status") != "sold.get":
                        continue
                    status = "refunding" if row.get("in_refund") else row["order_status"]
                    if status not in ("completed", "refunding", "cancelled"):
                        continue
                    order_id = row["order_id"]
                    local = db_manager.get_order_by_id(order_id)
                    if local and local["cookie_id"] == cookie_id:
                        observed_statuses[order_id] = status
    except Exception as e:
        logger.warning(f"【{cookie_id}】订单对账读写本地库失败: {type(e).__name__}")
        return {"ok": False, "error": "本地订单读写失败",
                "platform_count": len(platform_rows), "registered": registered, "refreshed": refreshed,
                "failed": failed, "failed_pages": report["failed_pages"], "has_more": report["has_more"]}

    if observed_statuses:
        try:
            from delivery_reliability import record_platform_order_observations
            record_platform_order_observations(cookie_id, observed_statuses, poll_started_ns)
        except Exception as e:
            failed += len(observed_statuses)
            logger.warning(f"【{cookie_id}】发货对账写入失败: {type(e).__name__}")

    out = {
        "ok": report["pages_read"] > 0,
        "cookie_id": cookie_id,
        "platform_count": len(platform_rows),
        "local_count": d["local_count"],
        "missing_paid": [r.get("order_id") for r in d["missing_paid"]],
        "missing_other": [r.get("order_id") for r in d["missing_other"]],
        "status_drift": [{"order_id": i["order_id"], "local": i["local"], "platform": i["platform"]}
                         for i in d["status_drift"]],
        "ok_count": d["ok"],
        "registered": registered,
        "refreshed": refreshed,
        "failed": failed,
        "failed_pages": report["failed_pages"],
        "has_more": report["has_more"],
    }
    if not out["ok"]:
        out["error"] = "卖家端订单页拉取失败"
    if notify_missing and out["missing_paid"]:
        logger.error(f"【{cookie_id}】⚠️ 对账发现漏单 {len(out['missing_paid'])} 单（平台待发货、本地没有）: "
                     f"{out['missing_paid'][:5]}")
        try:
            import notify_service
            notify_service.send_event(
                "risk", "⚠️ 发现漏单",
                f"账号 {cookie_id}：平台有 {len(out['missing_paid'])} 单待发货，本地库里没有"
                f"（多半是那段时间消息没收到）。订单号：{', '.join(out['missing_paid'][:5])}")
        except Exception:
            pass
    elif out["missing_other"] or out["status_drift"]:
        logger.info(f"【{cookie_id}】对账：补登记 {registered} 单历史，"
                    f"状态滞后 {len(out['status_drift'])} 单")
    return out


__all__ = ["LIVE_STATUSES", "diff", "reconcile"]
