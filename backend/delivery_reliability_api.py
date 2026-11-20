"""
C3 路由：发货可靠性（逐卡记账 + 补发 + 人工收口）

- GET  /delivery_reliability                 页面
- GET  /api/delivery/status                  配置 + 统计 + 待处理清单
- POST /api/delivery/config                  保存配置（自动补发/次数/冷却）
- POST /api/delivery/scan                    只读：列出当前可补发的卡
- POST /api/delivery/retry                   人工要求补发某一张（次数清零）
- POST /api/delivery/resolve                 人工标记"已确认发出"（unknown 的唯一出口）
- POST /api/delivery/abandon                 人工放弃某一张（禁止再自动发）
- POST /api/delivery/reap                    清掉过期租约（卡住的 sending）
- GET  /api/delivery/platform-ship           平台确认发货三本账（待补/已停/卡死）
- POST /api/delivery/platform-retry          人工让某一单重新进补确认队列
- POST /api/delivery/platform-confirmed      人工确认平台那头已经是已发货
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse
from loguru import logger

import delivery_reliability as svc

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@router.get("/delivery_reliability", include_in_schema=False)
async def delivery_page():
    f = PROJECT_ROOT / "static" / "delivery_reliability.html"
    if f.exists():
        return FileResponse(f)
    return FileResponse(PROJECT_ROOT / "static" / "index.html")


@router.get("/api/delivery/status")
async def delivery_status():
    try:
        return {"ok": True, "config": svc.get_config(), **svc.stats(7),
                "unresolved_rows": svc.unresolved(100)}
    except Exception as e:
        logger.error(f"发货可靠性状态查询失败: {e}")
        return {"ok": False, "error": str(e)}


@router.post("/api/delivery/config")
async def delivery_config(body: Dict[str, Any] = Body(...)):
    try:
        return {"ok": True, "config": svc.save_config(body)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/delivery/scan")
async def delivery_scan(limit: int = 50):
    try:
        return {"ok": True, "candidates": svc.resend_candidates(limit=limit)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _target(body: Dict[str, Any]):
    return str(body.get("order_id") or ""), str(body.get("cookie_id") or ""), int(body.get("idx") or 0)


@router.post("/api/delivery/retry")
async def delivery_retry(body: Dict[str, Any] = Body(...)):
    o, c, i = _target(body)
    if not o:
        return {"ok": False, "error": "缺少 order_id"}
    return {"ok": svc.force_retry(o, c, i, "人工要求补发"), "order_id": o, "idx": i}


@router.post("/api/delivery/resolve")
async def delivery_resolve(body: Dict[str, Any] = Body(...)):
    """人工回读确认买家确实收到了 —— unknown 状态的唯一出口。"""
    o, c, i = _target(body)
    if not o:
        return {"ok": False, "error": "缺少 order_id"}
    return {"ok": svc.mark_sent(o, c, i, "人工确认已送达"), "order_id": o, "idx": i}


@router.post("/api/delivery/abandon")
async def delivery_abandon(body: Dict[str, Any] = Body(...)):
    o, c, i = _target(body)
    if not o:
        return {"ok": False, "error": "缺少 order_id"}
    return {"ok": svc.mark_unknown(o, c, i, "人工放弃自动补发"), "order_id": o, "idx": i}


@router.post("/api/delivery/reap")
async def delivery_reap():
    try:
        return {"ok": True, "released": svc.reap_expired_leases()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/delivery/locate")
async def delivery_locate(order_id: str = "", cookie_id: str = "", idx: int = -1):
    """
    异常订单的会话定位上下文（只读）。
    返回已存的 chat_id/buyer_id、整单进度和人工查找步骤；没有验证过直达链接就不给链接。
    """
    if not order_id:
        return {"ok": False, "error": "缺少 order_id"}
    try:
        ctx = svc.locate_context(order_id, cookie_id, idx)
        if ctx is None:
            return {"ok": False, "error": "订单或卡不存在"}
        # 账号归属：只做存在/启用检查，不改任何东西
        try:
            import cookie_manager
            mgr = cookie_manager.manager
            if mgr is not None:
                ctx["account_exists"] = ctx["cookie_id"] in (mgr.cookies or {})
                ctx["account_enabled"] = ctx["account_exists"] and \
                    bool(mgr.get_cookie_status(ctx["cookie_id"]))
        except Exception:
            pass
        return {"ok": True, **ctx}
    except Exception as e:
        logger.error(f"会话定位失败: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/api/delivery/platform-ship")
async def platform_ship_list(cookie_id: str = ""):
    """平台确认发货的三种状态分开列（拆解 9）。"""
    try:
        return {"ok": True,
                "pending": svc.platform_ship_rows("pending", cookie_id),
                "stopped": svc.platform_ship_rows("stopped", cookie_id),
                "stuck": svc.platform_ship_rows("stuck", cookie_id),
                "stats": svc.platform_ship_stats()}
    except Exception as e:
        logger.error(f"平台确认发货查询失败: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/api/delivery/after-sale-review")
async def after_sale_review(hours: int = 24, cookie_id: str = ""):
    """
    发货后 N 小时异常回看（只读，拆解 39 §八-2）。
    返回值里的 limitations 必须跟着展示：窗口是按订单更新时间近似的。
    """
    try:
        import delivery_insights
        return delivery_insights.after_sale_review(cookie_id=cookie_id, hours=hours)
    except Exception as e:
        logger.error(f"发货后回看失败: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/api/delivery/new-item-checks")
async def new_item_checks(n: int = 3, cookie_id: str = ""):
    """新商品前 N 单人工抽查提示（只读、不拦截，拆解 39 §八-6）。"""
    try:
        import delivery_insights
        return delivery_insights.first_n_order_prompts(cookie_id=cookie_id, n=n)
    except Exception as e:
        logger.error(f"新商品抽查提示查询失败: {e}")
        return {"ok": False, "error": str(e)}


@router.post("/api/delivery/platform-retry")
async def platform_retry(body: Dict[str, Any] = Body(...)):
    """人工让某一单重新进补确认队列（清次数 + 清停止标记）。"""
    o = str(body.get("order_id") or "")
    c = str(body.get("cookie_id") or "")
    if not o:
        return {"ok": False, "error": "缺少 order_id"}
    return {"ok": svc.retry_platform_ship(o, c), "order_id": o}


@router.post("/api/delivery/platform-confirmed")
async def platform_confirmed(body: Dict[str, Any] = Body(...)):
    """人工确认平台那头确实已发货（比如在手机上点过了）—— 直接销账。"""
    o = str(body.get("order_id") or "")
    c = str(body.get("cookie_id") or "")
    if not o:
        return {"ok": False, "error": "缺少 order_id"}
    return {"ok": svc.mark_platform_shipped(o, c), "order_id": o}
