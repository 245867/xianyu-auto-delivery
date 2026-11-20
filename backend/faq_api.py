"""
买家提问 / FAQ 建议路由（方向三）

- GET  /faq                          页面
- GET  /api/faq?days=30&cookie_id=   覆盖率 + 最近没答上的 + 建议词条
- POST /api/faq/add_keyword          采用一条建议（追加，不覆盖）
- POST /api/faq/import_starter       导入起手词条（只补缺）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter
from fastapi.responses import FileResponse
from loguru import logger

import faq_kb as svc

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@router.get("/faq", include_in_schema=False)
async def faq_page():
    f = PROJECT_ROOT / "static" / "faq.html"
    if f.exists():
        return FileResponse(f)
    return FileResponse(PROJECT_ROOT / "static" / "index.html")


@router.get("/api/faq")
async def faq_summary(days: int = 30, cookie_id: str = ""):
    try:
        cid = str(cookie_id or "").strip() or None
        return {"ok": True, **svc.summary(days=max(1, min(365, int(days))), cookie_id=cid)}
    except Exception as e:
        logger.error(f"FAQ 统计失败: {e}")
        return {"ok": False, "error": str(e)}


@router.post("/api/faq/add_keyword")
async def faq_add_keyword(payload: Dict[str, Any] = None):
    payload = payload or {}
    try:
        return svc.add_keyword(str(payload.get("cookie_id") or ""),
                               payload.get("keyword") or "", payload.get("reply") or "")
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/faq/import_starter")
async def faq_import_starter(payload: Dict[str, Any] = None):
    payload = payload or {}
    cid = str(payload.get("cookie_id") or "").strip()
    if not cid:
        return {"ok": False, "error": "先选一个账号：词条是按账号存的"}
    try:
        return svc.import_starter(cid)
    except Exception as e:
        return {"ok": False, "error": str(e)}