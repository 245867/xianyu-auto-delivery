"""
通知推送路由（互联网侧吸收：嗷嗷鱼/Ydisks 多渠道设计）：
- GET  /api/notify/config   读取通知配置
- POST /api/notify/config   保存通知配置
- POST /api/notify/test     测试推送
- GET  /notify              配置页面
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

from notify_service import get_config, save_config, send_test, get_recent_failures

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@router.get("/notify", include_in_schema=False)
async def notify_page():
    return FileResponse(PROJECT_ROOT / "static" / "notify.html")


@router.get("/api/notify/config")
async def notify_get_config():
    return {"ok": True, "config": get_config()}


@router.post("/api/notify/config")
async def notify_save_config(body: Dict[str, Any] = Body(...)):
    cfg = save_config(body or {})
    return {"ok": True, "config": cfg, "message": "已保存"}


@router.post("/api/notify/test")
async def notify_test():
    result = send_test()
    return {"ok": bool(result.get("ok")),
            "skipped": result.get("skipped"),
            "results": result.get("results", {}),
            "attempts": result.get("attempts", {})}


@router.get("/api/notify/failures")
async def notify_failures(limit: int = 20):
    """最近的发送失败（脱敏：不含 webhook 地址与消息正文）。"""
    return {"ok": True, "failures": get_recent_failures(limit)}
