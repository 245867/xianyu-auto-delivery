"""
自动擦亮路由 —— /api/polish 系列 + /polish 页面（补齐①，移植自 zhinianboke polish_task）
- POST /api/polish/run      手动触发全量（或 ?cookie_id= 指定账号）擦亮，后台线程执行
- GET  /api/polish/status   调度配置 / 上次运行 / 最近日志
- GET  /polish              擦亮管理页
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter
from fastapi.responses import FileResponse

from polish_service import (
    get_status, run_polish_batch_async, start_scheduler,
)

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 路由加载时顺手启动每日调度守护线程（幂等）
start_scheduler()


@router.get("/polish", include_in_schema=False)
async def polish_page():
    return FileResponse(PROJECT_ROOT / "static" / "polish.html")


@router.post("/api/polish/run")
async def polish_run(body: Dict[str, Any] = None):
    """手动触发擦亮（body 可空，或 {"cookie_id": "xxx"} 只擦一个账号）。"""
    body = body or {}
    batch_id = run_polish_batch_async(str(body.get("cookie_id") or "").strip() or None)
    return {"ok": True, "batch_id": batch_id, "message": "擦亮任务已启动，后台执行中"}


@router.get("/api/polish/status")
async def polish_status():
    return {"ok": True, **get_status()}
