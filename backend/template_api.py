"""
商品模板 + 批量发布路由（A5）
- GET  /templates                          模板与批次页面
- GET  /api/templates                      模板列表
- POST /api/templates                      新建
- GET  /api/templates/{id}                 详情
- POST /api/templates/{id}                 修改
- DELETE /api/templates/{id}               删除
- POST /api/templates/{id}/preview         变量灌入模板，返回渲染与校验（不落库）
- POST /api/templates/{id}/jobs            建批次（dry_run 默认 true）
- GET  /api/templates/jobs/{job_id}        批次详情
- GET  /api/templates/jobs                 批次列表
- POST /api/templates/jobs/{job_id}/run    开始/继续（后台线程）
- POST /api/templates/jobs/{job_id}/resume 人工核对后续跑
- POST /api/templates/jobs/{job_id}/cancel 取消
- POST /api/templates/parse-rows           把粘贴的表格文本解析成行
- GET  /api/templates/stats                统计
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

import product_template as pt

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parent.parent

_run_locks: Dict[str, threading.Lock] = {}


@router.get("/templates", include_in_schema=False)
async def templates_page():
    return FileResponse(PROJECT_ROOT / "static" / "templates.html")


@router.get("/api/templates/stats")
async def templates_stats():
    try:
        return pt.stats()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.get("/api/templates")
async def templates_list(keyword: str = ""):
    try:
        return pt.list_templates(keyword=keyword)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "items": []}


@router.post("/api/templates")
async def templates_create(body: Dict[str, Any] = Body(...)):
    return pt.create_template(body or {})


@router.get("/api/templates/jobs")
async def templates_jobs(limit: int = 20):
    try:
        return pt.list_jobs(limit=limit)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "items": []}


@router.get("/api/templates/jobs/{job_id}")
async def templates_job_detail(job_id: str):
    return pt.get_job(job_id)


@router.post("/api/templates/parse-rows")
async def templates_parse_rows(body: Dict[str, Any] = Body(...)):
    """
    把粘贴的表格解析成变量行。
    text 用 tab / 逗号 / 竖线分隔；headers 为列名（与模板占位符同名）。
    例：headers=["学校","专业"]，text="华南理工\t计算机\n中山大学\t法学"
    """
    text = str(body.get("text") or "")
    headers = body.get("headers") or []
    if not isinstance(headers, list) or not headers:
        return {"ok": False, "error": "请先给出列名（与模板占位符同名）"}
    sep = "\t" if "\t" in text else ("|" if "|" in text else ",")
    rows, bad = [], []
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(sep)]
        if len(parts) < len(headers):
            bad.append({"line": i, "text": line[:80], "error": "列数不足"})
            continue
        rows.append({str(h): parts[j] for j, h in enumerate(headers)})
        if len(rows) >= pt.MAX_BATCH_ROWS:
            break
    return {"ok": True, "rows": rows, "count": len(rows), "bad": bad, "sep": repr(sep)}


@router.get("/api/templates/{template_id}")
async def templates_detail(template_id: int):
    return pt.get_template(template_id)


@router.post("/api/templates/{template_id}")
async def templates_update(template_id: int, body: Dict[str, Any] = Body(...)):
    return pt.update_template(template_id, body or {})


@router.delete("/api/templates/{template_id}")
async def templates_delete(template_id: int):
    return pt.delete_template(template_id)


@router.post("/api/templates/{template_id}/preview")
async def templates_preview(template_id: int, body: Dict[str, Any] = Body(...)):
    rows: List[Dict[str, Any]] = body.get("rows") or []
    return pt.preview(template_id, rows)


@router.post("/api/templates/{template_id}/jobs")
async def templates_create_job(template_id: int, body: Dict[str, Any] = Body(...)):
    return pt.create_job(
        template_id,
        body.get("rows") or [],
        str(body.get("cookie_id") or "").strip(),
        dry_run=bool(body.get("dry_run", True)),
        failure_policy=str(body.get("failurePolicy") or "skip"),
    )


@router.post("/api/templates/jobs/{job_id}/run")
async def templates_run_job(job_id: str, body: Dict[str, Any] = Body(default=None)):
    body = body or {}
    j = pt.get_job(job_id)
    if not j.get("ok"):
        return j
    lock = _run_locks.setdefault(job_id, threading.Lock())
    if lock.locked():
        return {"ok": False, "error": "该批次正在执行中"}

    def _worker():
        with lock:
            try:
                pt.run_job(job_id, max_items=int(body.get("max_items") or 0))
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "job_id": job_id, "message": "批次已启动，进度请刷新查看"}


@router.post("/api/templates/jobs/{job_id}/resume")
async def templates_resume_job(job_id: str):
    return pt.resume_job(job_id)


@router.post("/api/templates/jobs/{job_id}/reset-queue")
async def templates_reset_job_queue(job_id: str,
                                    body: Dict[str, Any] = Body(...)):
    try:
        seq = int(body.get("seq"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "seq 必须是整数"}
    return pt.reset_job_unknown(
        job_id,
        seq=seq,
        outcome=str(body.get("outcome") or ""),
        reason=str(body.get("reason") or ""),
        matched_item_id=str(body.get("matched_item_id") or ""),
    )


@router.post("/api/templates/jobs/{job_id}/cancel")
async def templates_cancel_job(job_id: str):
    return pt.cancel_job(job_id)
