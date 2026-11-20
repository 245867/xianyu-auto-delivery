"""
Token 保活路由 —— /api/token 系列（A1）

- GET  /api/token/status   开关/每账号心跳与换 token 状态/最近日志
- POST /api/token/check    立即检查一轮（后台线程，不阻塞请求）
- GET  /token              状态页
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

from token_keeper import start_keeper, run_once, get_status, save_config

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 路由加载时启动守护线程（幂等）；开关在 global_config.yml
start_keeper()

_last_check = {"running": False, "result": None}


def _check_worker() -> None:
    try:
        _last_check["result"] = run_once(force=True)
    except Exception as exc:
        _last_check["result"] = {"ok": False, "error": str(exc)[:200]}
    finally:
        _last_check["running"] = False


@router.get("/token", include_in_schema=False)
async def token_page():
    return FileResponse(PROJECT_ROOT / "static" / "token.html")


@router.get("/api/token/status")
async def token_status():
    try:
        data = get_status()
        data["last_check"] = _last_check
        return {"ok": True, **data}
    except Exception as exc:
        return {"ok": False, "error": f"状态读取失败：{str(exc)[:160]}"}


@router.post("/api/token/config")
async def token_config(body: Dict[str, Any] = Body(...)):
    """保存开关与间隔（只影响保活节奏，不碰账号凭据）。"""
    try:
        return {"ok": True, "config": save_config(body or {})}
    except Exception as exc:
        return {"ok": False, "error": f"保存失败：{str(exc)[:160]}"}


@router.post("/api/token/check")
async def token_check(body: Dict[str, Any] = None):
    if _last_check["running"]:
        return {"ok": True, "message": "检查进行中，请稍候"}
    _last_check["running"] = True
    threading.Thread(target=_check_worker, daemon=True, name="token-check").start()
    return {"ok": True, "message": "已开始检查（每账号会错开 3–8 秒）"}
