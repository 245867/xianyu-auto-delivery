"""Authenticated API for persistent opportunity fulfillment tasks."""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query

from opportunity_automation import (
    FulfillmentOrchestrator,
    FulfillmentOwnershipError,
    FulfillmentStore,
    FulfillmentValidationError,
)


_SERVICE_LOCK = threading.Lock()
_SERVICE: FulfillmentOrchestrator | None = None


def get_fulfillment_service() -> FulfillmentOrchestrator:
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = FulfillmentOrchestrator(store=FulfillmentStore())
    return _SERVICE


def _run_task(task_id: str) -> None:
    get_fulfillment_service().execute_task(task_id)


def create_router(
    get_current_user: Callable[..., Dict[str, Any]],
) -> APIRouter:
    router = APIRouter(prefix="/api/opportunity/fulfillment", tags=["opportunity-fulfillment"])

    @router.post("/tasks")
    def create_fulfillment_task(
        background_tasks: BackgroundTasks,
        body: Dict[str, Any] = Body(...),
        current_user: Dict[str, Any] = Depends(get_current_user),
    ):
        service = get_fulfillment_service()
        try:
            task = service.create_task(int(current_user["user_id"]), body)
        except FulfillmentOwnershipError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FulfillmentValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if task.get("status") == "ready_to_publish":
            background_tasks.add_task(_run_task, task["id"])
        return {"ok": True, "task": task}

    @router.get("/tasks")
    def list_fulfillment_tasks(
        limit: int = Query(default=50, ge=1, le=200),
        current_user: Dict[str, Any] = Depends(get_current_user),
    ):
        service = get_fulfillment_service()
        tasks = service.store.list_tasks(int(current_user["user_id"]), limit=limit)
        return {"ok": True, "tasks": tasks}

    @router.get("/tasks/{task_id}")
    def get_fulfillment_task(
        task_id: str,
        current_user: Dict[str, Any] = Depends(get_current_user),
    ):
        service = get_fulfillment_service()
        task = service.store.get_task(task_id, user_id=int(current_user["user_id"]))
        if not task:
            raise HTTPException(status_code=404, detail="履约任务不存在")
        return {
            "ok": True,
            "task": task,
            "events": service.store.events(
                task_id,
                user_id=int(current_user["user_id"]),
            ),
        }

    @router.post("/tasks/{task_id}/recover")
    def recover_fulfillment_task(
        task_id: str,
        background_tasks: BackgroundTasks,
        current_user: Dict[str, Any] = Depends(get_current_user),
    ):
        service = get_fulfillment_service()
        task = service.store.get_task(task_id, user_id=int(current_user["user_id"]))
        if not task:
            raise HTTPException(status_code=404, detail="履约任务不存在")
        try:
            task = service.recover_task(task_id)
        except FulfillmentValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if service.should_execute_after_recover(task):
            background_tasks.add_task(_run_task, task_id)
        return {
            "ok": True,
            "task": task,
            "scheduled": service.should_execute_after_recover(task),
        }

    return router


__all__ = ["create_router", "get_fulfillment_service"]
