"""
热搜词 / 爆款词榜路由（B1）

- GET  /hot_keywords                    页面
- GET  /api/hot-keywords/status         配置 + 运行状态 + 最近扫描 + 历史
- POST /api/hot-keywords/config         保存配置（种子词/开关/阈值）
- GET  /api/hot-keywords/board          取词榜（默认最近一次）
- GET  /api/hot-keywords/trend          单个词的历史走势
- POST /api/hot-keywords/scan           手动触发一轮扫描（后台跑，不阻塞请求）
- POST /api/hot-keywords/verify         对单个词真搜一次（供给/价格/真实想要数）
- GET  /api/hot-keywords/logs           扫描日志
- GET  /api/hot-keywords/pool           词池（trial/active/retired）+ 迁移记录
- POST /api/hot-keywords/revive         人工把 retired 的词拉回 trial
- POST /api/hot-keywords/seed-from-pool 把 active 词按均分补进种子词（填空位，不覆盖）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

import hot_keywords as svc

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 定时器只起线程，是否真扫由配置里的 enabled 决定（默认关）
svc.start_scheduler()


@router.get("/hot_keywords", include_in_schema=False)
async def hot_keywords_page():
    return FileResponse(PROJECT_ROOT / "static" / "hot_keywords.html")


@router.get("/api/hot-keywords/status")
async def hot_keywords_status():
    try:
        return {"ok": True, **svc.status()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.post("/api/hot-keywords/config")
async def hot_keywords_config(body: Dict[str, Any] = Body(...)):
    try:
        return {"ok": True, "config": svc.save_config(body or {})}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.get("/api/hot-keywords/board")
async def hot_keywords_board(limit: int = 50, day: str = ""):
    try:
        return {"ok": True, **svc.get_board(limit=limit, day=day or None)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "words": []}


@router.get("/api/hot-keywords/trend")
async def hot_keywords_trend(word: str, days: int = 30):
    try:
        return {"ok": True, "word": word, "points": svc.word_trend(word, days)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "points": []}


@router.post("/api/hot-keywords/scan")
async def hot_keywords_scan(body: Dict[str, Any] = Body(default={})):
    try:
        seeds = body.get("seeds") if isinstance(body, dict) else None
        return svc.start_scan(seeds)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.post("/api/hot-keywords/verify")
async def hot_keywords_verify(body: Dict[str, Any] = Body(...)):
    try:
        return await svc.verify_word(str(body.get("word") or ""),
                                     str(body.get("cookie_id") or ""))
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.get("/api/hot-keywords/pool")
async def hot_keywords_pool(state: str = "", limit: int = 200):
    try:
        return {"ok": True, **svc.pool_overview(state=state, limit=limit),
                "log": svc.pool_log(50)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "words": []}


@router.post("/api/hot-keywords/revive")
async def hot_keywords_revive(body: Dict[str, Any] = Body(...)):
    try:
        word = str(body.get("word") or "")
        return {"ok": svc.revive_word(word, "页面上人工恢复"), "word": word}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.post("/api/hot-keywords/seed-from-pool")
async def hot_keywords_seed_from_pool():
    try:
        return svc.seed_from_pool()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.get("/api/hot-keywords/logs")
async def hot_keywords_logs(limit: int = 20):
    try:
        return {"ok": True, "logs": svc.scan_logs(limit=limit)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "logs": []}
