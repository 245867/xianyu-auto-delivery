"""
售罄自动上架路由（A6）
- GET  /sold_out                        页面
- GET  /api/sold-out/status             配置 + 白名单数 + 上轮结果 + 最近日志
- POST /api/sold-out/config             保存开关/演练/间隔/冷却/上限
- GET  /api/sold-out/watch              白名单列表（可按 cookie_id 过滤）
- POST /api/sold-out/watch              加入/更新一条（必须绑定模板）
- POST /api/sold-out/watch/remove       移出白名单
- POST /api/sold-out/watch/toggle       停用/启用单条（不动配置）
- POST /api/sold-out/run                立即巡检一轮（force：忽略 enabled，但仍守夜间禁令）
- POST /api/sold-out/check              单件只读体检（给页面上的"先看看"用）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

import sold_out_relist as svc

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@router.get("/sold_out", include_in_schema=False)
async def sold_out_page():
    return FileResponse(PROJECT_ROOT / "static" / "sold_out.html")


@router.get("/api/sold-out/status")
async def sold_out_status():
    try:
        return {"ok": True, **svc.get_status()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.post("/api/sold-out/config")
async def sold_out_config(body: Dict[str, Any] = Body(...)):
    try:
        cfg = svc.save_config(body or {})
        return {"ok": True, "config": cfg}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.get("/api/sold-out/watch")
async def sold_out_watch(cookie_id: str = ""):
    try:
        return svc.list_watch(cookie_id)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "items": []}


@router.post("/api/sold-out/watch")
async def sold_out_watch_add(body: Dict[str, Any] = Body(...)):
    return svc.add_watch(
        cookie_id=str(body.get("cookie_id") or ""),
        item_id=str(body.get("item_id") or ""),
        title=str(body.get("title") or ""),
        price=body.get("price"),
        quantity=body.get("quantity") or 1,
        template_id=body.get("template_id") or 0,
        material_id=body.get("material_id") or 0,
        variables=body.get("variables") or {},
    )


@router.post("/api/sold-out/watch/remove")
async def sold_out_watch_remove(body: Dict[str, Any] = Body(...)):
    return svc.remove_watch(str(body.get("cookie_id") or ""),
                            str(body.get("item_id") or ""))


@router.post("/api/sold-out/watch/toggle")
async def sold_out_watch_toggle(body: Dict[str, Any] = Body(...)):
    return svc.toggle_watch(str(body.get("cookie_id") or ""),
                            str(body.get("item_id") or ""),
                            bool(body.get("active")))


@router.post("/api/sold-out/run")
async def sold_out_run(body: Dict[str, Any] = Body(default={})):
    force = bool((body or {}).get("force", True))
    try:
        return svc.run_round(force=force)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.post("/api/sold-out/check")
async def sold_out_check(body: Dict[str, Any] = Body(...)):
    """只读体检：不写入、不上架，页面用来确认"这条现在到底是不是售罄"。"""
    item_id = str(body.get("item_id") or "").strip()
    cookie_id = str(body.get("cookie_id") or "").strip()
    if not item_id or not cookie_id:
        return {"ok": False, "error": "需要 cookie_id 与 item_id"}
    from opportunity_detail import resolve_cookie
    _, cstr = resolve_cookie(cookie_id, fallback_first=False)
    if not cstr:
        return {"ok": False, "error": "账号 Cookie 不存在"}
    try:
        return svc.check_item(cstr, item_id, svc._app_key())
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# 路由加载时顺手启动巡检守护线程（幂等；未启用时线程空转，不产生请求）
svc.start_scheduler()
