"""
闲管家开放平台路由（D1）
- GET  /open_platform                       配置页面
- GET  /api/open-platform/status            状态（secret 永不回显）
- POST /api/open-platform/config            保存 AppKey/AppSecret/seller_id/开关
- POST /api/open-platform/self-check        不联网自检
- POST /api/open-platform/test              联网连通性（只读：查店铺授权列表）
- GET  /api/open-platform/apis              接口清单（读/写分组，给页面展示）
- POST /api/open-platform/read              通用只读调用（白名单内，禁写接口）
- GET  /api/open-platform/calls             最近调用日志
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

import goofish_open as op

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@router.get("/open_platform", include_in_schema=False)
async def open_platform_page():
    return FileResponse(PROJECT_ROOT / "static" / "open_platform.html")


@router.get("/api/open-platform/status")
async def status():
    try:
        return op.status()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.post("/api/open-platform/config")
async def save_config(body: Dict[str, Any] = Body(...)):
    try:
        cfg = op.save_config(body or {})
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    # 只回打码结果，绝不把 secret 送回前端
    return op.status()


@router.post("/api/open-platform/self-check")
async def self_check():
    try:
        return op.self_check()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.post("/api/open-platform/test")
async def test_connection():
    """
    连通性验证：只调 `查询闲鱼店铺`（只读），能返回就说明签名与网络都对。
    这是拿到 AppKey 后应该做的第一件事，不产生任何写操作。
    """
    try:
        raw = op.list_shops()
    except op.OpenPlatformError as e:
        hint = {
            "disabled": "开放平台通道未启用，先把开关打开",
            "config": "AppKey / AppSecret 没填全",
            "network": "网络不通或域名被拦",
            "risk": "触发了平台限流，等一分钟再试",
        }.get(e.kind, "接口返回失败，看下面的原文")
        return {"ok": False, "kind": e.kind, "error": str(e)[:300], "hint": hint}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}

    data = raw.get("data") if isinstance(raw, dict) else None
    shops = []
    if isinstance(data, dict):
        for k in ("list", "items", "shop_list", "data"):
            if isinstance(data.get(k), list):
                shops = data[k]
                break
    elif isinstance(data, list):
        shops = data
    return {"ok": True, "shop_count": len(shops),
            "shops": [{"seller_id": s.get("seller_id"), "shop_name": s.get("shop_name")
                       or s.get("user_name") or "", "expires": s.get("authorize_expires")}
                      for s in shops if isinstance(s, dict)][:30],
            "raw_keys": list(raw.keys()) if isinstance(raw, dict) else []}


@router.get("/api/open-platform/apis")
async def apis():
    return {
        "ok": True,
        "read": [{"name": k, "path": v} for k, v in op.READ_APIS.items()],
        "write": [{"name": k, "path": v} for k, v in op.WRITE_APIS.items()],
        "read_count": len(op.READ_APIS),
        "write_count": len(op.WRITE_APIS),
        "limits": {"qps": op.QPS_LIMIT, "daily_per_api": op.DAILY_LIMIT},
    }


@router.post("/api/open-platform/read")
async def read_call(body: Dict[str, Any] = Body(...)):
    """
    通用只读调用，方便在页面上直接验证某个接口能不能用。
    **只允许 READ_APIS 里的接口**，写接口一律拒绝（写要专门的业务路径 + 幂等键）。
    """
    name = str(body.get("api") or "").strip()
    if name not in op.READ_APIS:
        return {"ok": False, "error": f"{name} 不在只读白名单内（写接口请走业务页面）"}
    params = body.get("body") or {}
    if not isinstance(params, dict):
        return {"ok": False, "error": "body 必须是对象"}
    try:
        return {"ok": True, "raw": op.call(name, params)}
    except op.OpenPlatformError as e:
        return {"ok": False, "kind": e.kind, "error": str(e)[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@router.get("/api/open-platform/calls")
async def calls(limit: int = 50):
    out = []
    try:
        if op.CALL_LOG.exists():
            with open(op.CALL_LOG, "r", encoding="utf-8") as f:
                for line in f.readlines()[-max(1, min(500, limit)):]:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except Exception:
                            pass
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "items": []}
    return {"ok": True, "items": out[::-1]}
