"""Admin routes and FastAPI lifecycle for the sourcing monitor."""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from monitor_engine import MonitorEngine
from monitor_storage import DEFAULT_PATH, MonitorStore, MonitorTask


@asynccontextmanager
async def monitor_lifespan(app):
    path = os.getenv("MONITOR_DB_PATH") or DEFAULT_PATH
    store = await asyncio.to_thread(MonitorStore, path)
    engine = MonitorEngine(store)
    app.state.monitor_store = store
    scheduler = asyncio.create_task(engine.run(), name="monitor-engine")
    try:
        yield
    finally:
        engine.stop()
        await scheduler


def create_monitor_router(require_admin):
    router = APIRouter(prefix="/api/monitor", tags=["monitor"], dependencies=[Depends(require_admin)])

    @router.get("/tasks")
    def list_tasks(request: Request):
        return {"ok": True, "tasks": request.app.state.monitor_store.list_tasks()}

    @router.post("/tasks", status_code=201)
    def create_task(task: MonitorTask, request: Request):
        return {"ok": True, "task": request.app.state.monitor_store.create_task(task)}

    @router.get("/tasks/{task_id}")
    def read_task(task_id: int, request: Request):
        task = request.app.state.monitor_store.get_task(task_id)
        if task is None:
            raise HTTPException(404, "监控任务不存在")
        return {"ok": True, "task": task}

    @router.put("/tasks/{task_id}")
    def update_task(task_id: int, task: MonitorTask, request: Request):
        updated = request.app.state.monitor_store.update_task(task_id, task)
        if updated is None:
            raise HTTPException(404, "监控任务不存在")
        return {"ok": True, "task": updated}

    @router.delete("/tasks/{task_id}")
    def delete_task(task_id: int, request: Request):
        if not request.app.state.monitor_store.delete_task(task_id):
            raise HTTPException(404, "监控任务不存在")
        return {"ok": True}

    @router.get("/records")
    def list_records(
        request: Request,
        task_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        return {"ok": True, "records": request.app.state.monitor_store.list_records(
            task_id, limit=limit, offset=offset,
        )}

    return router
