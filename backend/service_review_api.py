"""
服务型商品复盘路由（P2）

- GET  /service_review        页面
- GET  /api/service/review    复盘数据（只读）
- GET  /api/service/config    服务单识别配置
- POST /api/service/config    保存关键词 / 显式 item_id
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse
from loguru import logger

import service_review as svc

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@router.get("/service_review", include_in_schema=False)
async def service_review_page():
    f = PROJECT_ROOT / "static" / "service_review.html"
    if f.exists():
        return FileResponse(f)
    return FileResponse(PROJECT_ROOT / "static" / "index.html")


@router.get("/api/service/review")
async def service_review_data(days: int = 30, cookie_id: str = ""):
    try:
        return svc.review(days=days, cookie_id=cookie_id)
    except Exception as e:
        logger.error(f"服务型复盘失败: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/api/service/config")
async def service_review_config_get():
    return {"ok": True, "config": svc.get_config()}


@router.post("/api/service/config")
async def service_review_config_save(body: Dict[str, Any] = Body(default_factory=dict)):
    try:
        keywords: List[str] = body.get("service_keywords")
        item_ids: List[str] = body.get("service_item_ids")
        patch: Dict[str, Any] = {}
        if isinstance(keywords, list):
            patch["service_keywords"] = keywords
        if isinstance(item_ids, list):
            patch["service_item_ids"] = item_ids
        return {"ok": True, "config": svc.save_config(patch)}
    except Exception as e:
        return {"ok": False, "error": str(e)}