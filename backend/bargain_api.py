"""
议价护栏路由（B3）
- GET  /bargain                      页面
- GET  /api/bargain/status           配置 + 诊断 + 近 30 天动作分布 + 最近日志
- POST /api/bargain/config           保存 enabled / max_rounds / 让价上限 / 绝对底价
- POST /api/bargain/test             干跑：给定消息+标价，返回决策（不落会话）
- POST /api/bargain/check-reply      干跑：给定回复+底价，返回校验结果（不动真实会话）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

import bargain_guard as svc

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@router.get("/bargain", include_in_schema=False)
async def bargain_page():
    return FileResponse(PROJECT_ROOT / "static" / "bargain.html")


@router.get("/api/bargain/status")
async def bargain_status():
    try:
        return {"ok": True, **svc.stats()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.post("/api/bargain/config")
async def bargain_config(body: Dict[str, Any] = Body(...)):
    try:
        return {"ok": True, "config": svc.save_config(body or {}),
                "diagnosis": svc.diagnose_floor()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.post("/api/bargain/test")
async def bargain_test(body: Dict[str, Any] = Body(...)):
    """
    用"试算会话"跑一遍决策，**不写真实会话的轮数**（否则页面上点几下就把
    真实买家的议价轮数耗光了）。只落一条 debug 日志。
    """
    msg = str(body.get("message") or "")
    try:
        price = float(body.get("list_price") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "标价必须是数字"}
    if price <= 0:
        return {"ok": False, "error": "标价必须大于 0"}
    try:
        d = svc.decide(msg, price,
                       chat_id="__debug__", cookie_id="__debug__",
                       item_id="__debug__")
        block = svc.prompt_block(d)
        return {"ok": True, "decision": d, "prompt_block": block,
                "note": "这是试算，不计入真实会话轮数"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:180]}"}


@router.post("/api/bargain/check-reply")
async def bargain_check_reply(body: Dict[str, Any] = Body(...)):
    """拿一段模拟回复过保险丝，看看会被改成什么样。"""
    text = str(body.get("reply") or "")
    try:
        floor = float(body.get("floor") or 0)
        list_price = float(body.get("list_price") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "底价/标价必须是数字"}
    if floor <= 0:
        return {"ok": False, "error": "底价必须大于 0"}
    d = {"ok": True, "floor": floor, "list_price": list_price or floor,
         "offer": None, "tier": "debug", "tier_label": "试算",
         "cut_ratio": None, "target_price": floor, "action": "counter"}
    try:
        return {"ok": True, **svc.enforce(text, d)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:180]}"}
