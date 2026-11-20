"""Admin endpoints for reviewed fission drafts and single-item publication."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import goofish_open
import rate_limiter
from config import config
from item_fission_matrix import parent_ready
from opportunity_publisher import PublishError, _validate_receipt, publish_item
from product_template import BATCH_INTERVAL, DAILY_PUBLISH_CAP, _night_ban


class Variant(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    variant_title: str = Field(min_length=1, max_length=30)
    cover_image_url: str = Field(min_length=1)


class FissionBatch(BaseModel):
    variants: list[Variant] = Field(min_length=5, max_length=10)


class FissionPublication(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    desc: str = Field(min_length=1)
    price: float = Field(gt=0)
    quantity: int = Field(gt=0)
    delivery: Literal["包邮", "按距离计费", "一口价", "无需邮寄"]
    postPrice: float = Field(default=0, ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


def create_fission_router(matrix, require_admin):
    router = APIRouter(prefix="/api/item-fission", tags=["item-fission"],
                       dependencies=[Depends(require_admin)])

    @router.get("/parents/{cookie_id}/{parent_item_id}")
    def list_tasks(cookie_id: str, parent_item_id: str):
        return {"tasks": matrix.list(cookie_id, parent_item_id)}

    @router.post("/parents/{cookie_id}/{parent_item_id}", status_code=201)
    def create_tasks(cookie_id: str, parent_item_id: str, batch: FissionBatch):
        try:
            tasks = matrix.create(cookie_id, parent_item_id, [variant.model_dump() for variant in batch.variants])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"tasks": tasks}

    @router.post("/tasks/{cookie_id}/{fission_id}/publish")
    def publish_task(cookie_id: str, fission_id: str, request: FissionPublication):
        settings = goofish_open.get_config()
        if (config.get("ITEM_FISSION.enabled") is not True or not settings["enabled"] or settings["dry_run"]
                or not goofish_open.is_configured(settings)):
            raise HTTPException(409, "裂变发布需要启用已授权的闲管家实盘通道")
        cookies = matrix.database.get_cookie(cookie_id)
        app_key = config.get("APP_CONFIG.app_key")
        if not cookies or not app_key or not matrix.database.get_cookie_status(cookie_id):
            raise HTTPException(409, "账号未启用、缺少凭证或发布配置")
        payload = request.model_dump(exclude={"width", "height"})

        def prepare(parent_item_id, cover_image_url):
            _validate_receipt({"url": cover_image_url, "width": request.width, "height": request.height})
            if not _night_ban()[0] or rate_limiter.daily_count("publish", cookie_id) >= DAILY_PUBLISH_CAP:
                return False
            if not parent_ready(matrix, cookie_id, parent_item_id):
                return False
            return rate_limiter.acquire("publish", cookie_id, limit=1, window=BATCH_INTERVAL)[0]

        def submit(item):
            item["imageUrls"] = [{"url": item["imageUrls"][0], "width": request.width, "height": request.height}]
            return publish_item(cookies, item, str(app_key), cookie_id=cookie_id)

        try:
            status = matrix.publish(cookie_id, fission_id, payload, submit, ready=prepare)
        except (ValueError, PublishError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except goofish_open.OpenPlatformError as exc:
            raise HTTPException(503, "母商品状态暂时无法核实") from exc
        return {"status": status}

    return router
