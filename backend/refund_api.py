"""
退款后处置账本路由（方向一）

- GET  /refund                 页面
- GET  /api/refund?days=30     统计 + 待处理清单
- POST /api/refund/resolve     标记已处理（备注可选）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter
from fastapi.responses import FileResponse
from loguru import logger

import refund_aftercare as svc

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@router.get("/refund", include_in_schema=False)
async def refund_page():
    f = PROJECT_ROOT / "static" / "refund.html"
    if f.exists():
        return FileResponse(f)
    return FileResponse(PROJECT_ROOT / "static" / "index.html")


@router.get("/api/refund")
async def refund_summary(days: int = 30):
    try:
        return {"ok": True, **svc.summary(days=max(1, min(365, int(days))))}
    except Exception as e:
        logger.error(f"退款账本查询失败: {e}")
        return {"ok": False, "error": str(e)}


@router.post("/api/refund/resolve")
async def refund_resolve(payload: Dict[str, Any] = None):
    payload = payload or {}
    try:
        ok = svc.resolve(int(payload.get("id") or 0), payload.get("note") or "")
        return {"ok": bool(ok), **({} if ok else {"error": "记录不存在"})}
    except Exception as e:
        return {"ok": False, "error": str(e)}