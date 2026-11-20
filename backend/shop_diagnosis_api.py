"""
店铺数据复盘 + 限流归因路由（B2）
- GET  /shop_diagnosis                     页面
- GET  /api/shop-diagnosis/status          配置 + 已跟踪商品数 + 有快照的日期
- POST /api/shop-diagnosis/config          保存 detail_budget / auto_scan / hour / minute / keep_days
- POST /api/shop-diagnosis/run             跑一轮复盘（只读；with_detail 控制是否补详情）
- GET  /api/shop-diagnosis/last            上一次报告（不重新请求平台）
- GET  /api/shop-diagnosis/history         ?cookie_id=&item_id=&days=14 单件曲线
- GET  /api/shop-diagnosis/accounts        可用账号列表（cookie_id + 备注）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

import shop_diagnosis as svc
from opportunity_detail import resolve_cookie

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _app_key() -> str:
    try:
        from config import config
        return str(config.get("APP_CONFIG.app_key", ""))
    except Exception:
        return ""


def _accounts() -> list:
    out = []
    try:
        import cookie_manager as cm
        cookies = getattr(getattr(cm, "manager", None), "cookies", None) or {}
    except Exception:
        cookies = {}
    if not cookies:
        try:
            from db_manager import db_manager
            cookies = db_manager.get_all_cookies() or {}
        except Exception:
            cookies = {}
    for cid, value in cookies.items():
        if not value:
            continue
        out.append({"cookie_id": str(cid), "nick": "", "remark": "", "ok": bool(str(value).strip())})
    # 补备注/用户名（取得到就显示，取不到就只显示 id）
    try:
        from db_manager import db_manager
        for a in out:
            info = db_manager.get_cookie_details(a["cookie_id"])
            if isinstance(info, dict):
                a["nick"] = str(info.get("remark") or info.get("username") or "")
    except Exception:
        pass
    return out


@router.get("/shop_diagnosis", include_in_schema=False)
async def shop_diagnosis_page():
    return FileResponse(PROJECT_ROOT / "static" / "shop_diagnosis.html")


@router.get("/api/shop-diagnosis/status")
async def shop_diagnosis_status():
    try:
        return {"ok": True, **svc.status(), "accounts": _accounts()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.get("/api/shop-diagnosis/accounts")
async def shop_diagnosis_accounts():
    try:
        return {"ok": True, "accounts": _accounts()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "accounts": []}


@router.post("/api/shop-diagnosis/config")
async def shop_diagnosis_config(body: Dict[str, Any] = Body(...)):
    try:
        return {"ok": True, "config": svc.save_config(body or {})}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.post("/api/shop-diagnosis/run")
async def shop_diagnosis_run(body: Dict[str, Any] = Body(default={})):
    body = body or {}
    cookie_id = str(body.get("cookie_id") or "").strip()
    cid, cstr = resolve_cookie(cookie_id, fallback_first=not cookie_id)
    if not cstr:
        return {"ok": False, "error": "没有可用账号 Cookie，请先在主界面登录闲鱼"}
    app_key = _app_key()
    if not app_key:
        return {"ok": False, "error": "global_config.yml 里没有 APP_CONFIG.app_key"}
    try:
        report = svc.run(cid, cstr, app_key,
                         with_detail=bool(body.get("with_detail", True)),
                         detail_budget=body.get("detail_budget"))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:180]}"}
    if not report.get("ok"):
        return report
    report["cookie_id"] = cid
    return report


@router.get("/api/shop-diagnosis/last")
async def shop_diagnosis_last(cookie_id: str = ""):
    try:
        rep = svc.last_report(cookie_id)
        if not rep:
            return {"ok": False, "error": "还没有跑过复盘"}
        return rep
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.get("/api/shop-diagnosis/history")
async def shop_diagnosis_history(cookie_id: str = "", item_id: str = "", days: int = 14):
    if not item_id:
        return {"ok": False, "error": "需要 item_id", "rows": []}
    try:
        return {"ok": True, "rows": svc.history(cookie_id, item_id, days=days)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "rows": []}


# 路由加载时启动定时复盘线程（幂等；auto_scan 关着时空转，不产生请求）
svc.start_scheduler()
