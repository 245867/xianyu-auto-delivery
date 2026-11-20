"""
C2 路由：长期零出单的定时重新发布（半自动）

- GET  /stale_relist                         页面
- GET  /api/stale/status                     配置 + 待下架数 + 上轮结果 + 日志
- POST /api/stale/config                     保存开关/阈值/演练
- POST /api/stale/preview                    只读扫描：列出候选 + 每条为什么该/不该翻新
- POST /api/stale/bind                       给某件商品绑定模板与素材
- POST /api/stale/run                        跑一轮（force 忽略 enabled，仍守夜间禁令）
- GET  /api/stale/pending                    待下架旧链接清单（含直达链接）
- POST /api/stale/pending/mark               人工标记"已下架"（我们不验证，只记账）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse
from loguru import logger

import stale_relist as svc

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@router.get("/stale_relist", include_in_schema=False)
async def stale_page():
    f = PROJECT_ROOT / "static" / "stale_relist.html"
    if f.exists():
        return FileResponse(f)
    return FileResponse(PROJECT_ROOT / "static" / "index.html")


@router.get("/api/stale/status")
async def stale_status():
    try:
        return {"ok": True, **svc.get_status()}
    except Exception as e:
        logger.error(f"读取僵尸链接重发状态失败: {e}")
        return {"ok": False, "error": str(e)[:200]}


@router.post("/api/stale/config")
async def stale_config(body: Dict[str, Any] = Body(...)):
    try:
        cfg = svc.save_config(body or {})
        return {"ok": True, "config": cfg}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.post("/api/stale/preview")
async def stale_preview(body: Dict[str, Any] = Body(default_factory=dict)):
    """
    只读预览：拉一次列表 + 候选的详情，逐条给出判定理由。**不发布、不写库。**

    这个接口是给"开开关之前先看清楚"用的。返回里的 `candidates` 每条都带
    `rework`/`reason`/`blockers`/`has_template`，页面照原样渲染即可。
    """
    cookie_id = str((body or {}).get("cookie_id") or "").strip()
    if not cookie_id:
        return {"ok": False, "error": "cookie_id 必填"}
    from opportunity_detail import resolve_cookie
    _, cstr = resolve_cookie(cookie_id, fallback_first=False)
    if not cstr:
        return {"ok": False, "error": "该账号没有可用 Cookie"}
    try:
        return svc.scan(cookie_id, cstr, svc._app_key())
    except Exception as e:
        logger.error(f"僵尸链接预览失败: {e}")
        return {"ok": False, "error": str(e)[:200]}


@router.post("/api/stale/bind")
async def stale_bind(body: Dict[str, Any] = Body(...)):
    return svc.bind(
        cookie_id=str(body.get("cookie_id") or ""),
        item_id=str(body.get("item_id") or ""),
        template_id=body.get("template_id") or 0,
        material_id=body.get("material_id") or 0,
        price=body.get("price"),
        variables=body.get("variables") or {},
    )


@router.post("/api/stale/run")
async def stale_run(body: Dict[str, Any] = Body(default_factory=dict)):
    force = bool((body or {}).get("force", True))
    try:
        return svc.run_round(force=force)
    except Exception as e:
        logger.error(f"僵尸链接重发一轮失败: {e}")
        return {"ok": False, "error": str(e)[:200]}


@router.get("/api/stale/pending")
async def stale_pending(cookie_id: str = "", include_done: bool = False):
    try:
        return svc.list_pending(cookie_id, include_done=include_done)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "items": []}


@router.post("/api/stale/pending/mark")
async def stale_pending_mark(body: Dict[str, Any] = Body(...)):
    """人工点"我已下架"。**我们无法验证是否真的下架了**，这里只记账。"""
    return svc.mark_down(str(body.get("cookie_id") or ""),
                         str(body.get("item_id") or ""),
                         bool(body.get("done", True)))


# 路由加载时启动守护线程（幂等；未启用时空转，不产生请求）
svc.start_scheduler()
