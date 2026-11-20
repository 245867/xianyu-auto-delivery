"""
回复发件箱路由（拆解 10）

- GET  /reply_outbox                     页面（停止态的人工出口）
- GET  /api/reply/outbox                 统计 + 待人工处理的回复 + 待补发条数
- POST /api/reply/outbox/release         放行（unknown/failed/held → pending，下一次心跳补发）
- POST /api/reply/outbox/skip            人已确认：这条不用再发了（unknown → skipped）
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from loguru import logger

import reply_outbox as svc

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@router.get("/reply_outbox", include_in_schema=False)
async def outbox_page():
    f = PROJECT_ROOT / "static" / "reply_outbox.html"
    if f.exists():
        return FileResponse(f)
    return FileResponse(PROJECT_ROOT / "static" / "index.html")


@router.get("/api/reply/outbox")
async def outbox_status():
    try:
        return {"ok": True, "stats": svc.stats(),
                "pending": svc.rows("pending", limit=20),
                "hold": svc.rows("unknown", limit=50) + svc.rows("failed", limit=50),
                # 三档里的 draft：默认扣下不发（status=held），等人放行或弃用。
                # 老数据里也有"加闸之前已经发出去"的 draft 行，页面上按状态区分。
                "draft": svc.rows_by_tier("draft", limit=30)}
    except Exception as e:
        logger.error(f"回复发件箱查询失败: {e}")
        return {"ok": False, "error": str(e)}


@router.post("/api/reply/outbox/release")
async def outbox_release(payload: dict = None):
    payload = payload or {}
    try:
        ok = svc.release(payload.get("cookie_id", ""), payload.get("source_msg_id", ""))
        return {"ok": bool(ok), **({} if ok else {"error": "这条记录不在停止态（可能已经收尾）"})}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/reply/outbox/skip")
async def outbox_skip(payload: dict = None):
    payload = payload or {}
    try:
        rec = svc.finish(payload.get("cookie_id", ""), payload.get("source_msg_id", ""),
                        "skipped", "人工确认：不再发送")
        return {"ok": bool(rec), **({} if rec else {"error": "记录不存在"})}
    except Exception as e:
        return {"ok": False, "error": str(e)}