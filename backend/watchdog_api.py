"""
后台任务停滞检测路由（L2-2）
- GET /api/watchdog/status   所有已登记后台任务的最后心跳（只读）
"""

from __future__ import annotations

from fastapi import APIRouter

import task_watchdog

router = APIRouter()


@router.get("/api/watchdog/status")
async def watchdog_status():
    """当前后台任务心跳；stalled 表示超过 2.5 倍节奏没有动静。"""
    rows = task_watchdog.snapshot()
    return {"ok": True,
            "tasks": rows,
            "stalled": [r for r in rows if r["state"] == "stalled"]}
