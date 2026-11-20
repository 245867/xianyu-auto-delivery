"""Admin API for competitor price-rank guard targets."""

import inspect

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field


class TargetPayload(BaseModel):
    item_id: str = Field(min_length=1, max_length=100)
    search_keyword: str = Field(min_length=1, max_length=200)
    price_floor: float = Field(gt=0)
    price_ceiling: float = Field(gt=0)
    under_cut_delta: float = Field(default=0.1, ge=0)
    is_active: bool = True


def create_price_rank_guard_router(database, require_admin):
    if inspect.isfunction(require_admin) or inspect.ismethod(require_admin):
        admin_guard = require_admin
    else:
        def admin_guard():
            return require_admin()

    router = APIRouter(
        prefix="/api/price-rank-guard",
        tags=["price-rank-guard"],
        dependencies=[Depends(admin_guard)],
    )

    @router.get("/targets")
    def list_targets():
        return {"targets": database.list_competitor_monitor_targets()}

    @router.post("/targets", status_code=201)
    def create_target(payload: TargetPayload):
        try:
            target = database.save_competitor_monitor_target(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"target": target}

    @router.put("/targets/{item_id}")
    def update_target(item_id: str, payload: TargetPayload):
        if payload.item_id != item_id:
            raise HTTPException(400, "商品 ID 与路径不一致")
        if database.get_competitor_monitor_target(item_id) is None:
            raise HTTPException(404, "竞品盯盘目标不存在")
        try:
            target = database.save_competitor_monitor_target(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"target": target}

    @router.delete("/targets/{item_id}")
    def delete_target(item_id: str):
        if not database.delete_competitor_monitor_target(item_id):
            raise HTTPException(404, "竞品盯盘目标不存在")
        return {"deleted": True}

    return router
