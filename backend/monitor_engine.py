"""Asynchronous, read-only polling of low-price marketplace listings."""

import asyncio
import math
import random
import time
import unicodedata
from collections import OrderedDict
from urllib.parse import urlsplit

from loguru import logger

import notify_service
import rate_limiter
from config import config
from market_listings import search_market_listings
from monitor_storage import BACKOFF_SECONDS, MonitorStore, MonitorTask
from mtop_response import RISK_MARKERS, has_marker, is_success, ret_text
from opportunity_detail import DETAIL_API, DETAIL_SPM, DETAIL_VERSION
from opportunity_publisher import _mtop_request, extract_token_from_cookie


PLATFORM_PAUSE_MARKERS = (
    "RGV587", "FAIL_SYS_USER_VALIDATE", "HTTP 429", "FAIL_SYS_TRAFFIC_LIMIT",
    "RATE_LIMIT", "请求过于频繁", "限频", *RISK_MARKERS,
)
BUY_VERDICTS = {"buy", "strong_buy"}
EMPTY_EVALUATION = {"profit_rate": None, "verdict": None, "risk_level": None}


def evaluate_item(item_data):
    """Default evaluation hook for the radar."""
    return dict(EMPTY_EVALUATION)


def evaluate_item_with_vision(item_data):
    """调用多模态 AI 审图与价值评估引擎 (支持 Craft-Anything 电商视觉联动)"""
    try:
        from ai_vision_evaluator import AIVisionEvaluator
        evaluator = AIVisionEvaluator()
        item_id = str(item_data.get("item_id") or "")
        title = item_data.get("title") or ""
        price = float(item_data.get("price") or 0.0)
        description = item_data.get("description") or ""
        image_urls = item_data.get("image_urls") or ([item_data.get("main_image")] if item_data.get("main_image") else [])

        res = evaluator.evaluate_item(
            item_id=item_id,
            title=title,
            price=price,
            description=description,
            image_urls=image_urls,
        )

        # 检查是否需要自动生成 Craft-Anything 转售海报
        if res.verdict in ("strong_buy", "buy"):
            try:
                evaluator.create_relist_posters_with_craft_anything(res)
            except Exception as e:
                logger.warning(f"自动生成 Craft-Anything 转售海报失败: {e}")

        risk_level = "HIGH" if res.verdict == "avoid" else ("LOW" if res.verdict == "strong_buy" else "MEDIUM")
        return {
            "profit_rate": res.profit_rate,
            "verdict": res.verdict,
            "risk_level": risk_level,
            "estimated_market_price": res.estimated_market_price,
            "estimated_profit": res.estimated_profit,
            "condition": res.condition,
            "summary": res.summary,
            "generated_posters": res.generated_posters,
        }
    except Exception as exc:
        logger.warning(f"evaluate_item 执行异常，回退默认空值: {exc}")
    return dict(EMPTY_EVALUATION)


def resolve_enabled_account():
    import cookie_manager
    from db_manager import db_manager
    from opportunity_guard import check_allowed

    if not check_allowed()[0]:
        return ""
    manager = cookie_manager.manager
    if manager is not None:
        cookies = manager.get_enabled_cookies()
    else:
        states = db_manager.get_all_cookie_status()
        cookies = {cookie_id: cookie for cookie_id, cookie in db_manager.get_all_cookies().items()
                   if states.get(cookie_id, True)}
    return next((cookie for cookie in cookies.values() if extract_token_from_cookie(cookie)), "")


def _object(value):
    return value if isinstance(value, dict) else {}


def _identifier(value):
    if type(value) not in (int, str):
        return None
    value = str(value).strip()
    return value if value.isascii() and value.isdigit() and value.lstrip("0") else None


def _timestamp(value):
    digits = _identifier(value)
    return int(digits) if digits and len(digits) <= 18 else None


def _price(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _image_url(value):
    if not isinstance(value, str):
        return None
    value = f"https:{value}" if value.startswith("//") else value
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    return value if parts.scheme in {"http", "https"} and parts.hostname and not parts.username else None


def search_listings(cookie, task):
    result = search_market_listings(cookie, task["keyword"], newest_first=True,
                                    app_key=str(config.get("APP_CONFIG.app_key", "")))
    if not result["ok"]:
        raise RuntimeError(result["error"])
    listings = []
    for item in result["items"]:
        data = _object(item["raw_data"].get("data"))
        main = _object(_object(data.get("item")).get("main"))
        display = _object(main.get("exContent"))
        args = _object(_object(main.get("clickParam")).get("args"))
        category = (_identifier(data.get("categoryId")) or _identifier(args.get("cCatId"))
                    or _identifier(display.get("categoryId")))
        listings.append({**item, "category_id": category,
                         "published_at": _timestamp(args.get("publishTime"))})
    return listings


def fetch_detail(cookie, listing):
    raw, _ = _mtop_request(
        cookie, DETAIL_API, {"itemId": listing["item_id"]}, DETAIL_VERSION, DETAIL_SPM,
        str(config.get("APP_CONFIG.app_key", "")), timeout=15,
    )
    response_status = ret_text(raw)
    if has_marker(response_status, PLATFORM_PAUSE_MARKERS) or not is_success(response_status):
        raise RuntimeError(response_status or "详情响应缺少 ret")
    data = _object(raw.get("data"))
    detail = _object(data.get("itemDO"))
    if _identifier(detail.get("itemId")) != listing["item_id"]:
        raise ValueError("详情商品 ID 缺失或不一致")
    seller = _object(data.get("sellerDO"))
    credit = _object(_object(seller.get("idleFishCreditTag")).get("trackParams"))
    images = detail.get("imageInfos")
    image_urls = []
    if isinstance(images, list):
        image_urls = list(dict.fromkeys(url for image in images
                                       if (url := _image_url(_object(image).get("url")))))
    price = _price(detail.get("soldPrice"))
    return {
        "title": str(detail.get("title") or listing["title"]),
        "description": str(detail.get("desc") or ""),
        "price": price if price is not None else listing["price"],
        "image_urls": image_urls,
        "published_at": _timestamp(detail.get("gmtCreate")) or listing.get("published_at"),
        "seller_id": _identifier(seller.get("sellerId")),
        "seller_credit": _identifier(credit.get("sellerLevel")),
    }


def _matches(task, item):
    price = _price(item.get("price"))
    if price is None or not task["min_price"] <= price <= task["max_price"]:
        return False
    if task["category_id"] and task["category_id"] != item.get("category_id"):
        return False
    text = unicodedata.normalize("NFKC", f"{item['title']}\n{item.get('description', '')}").casefold()
    return not any(unicodedata.normalize("NFKC", word).casefold() in text
                   for word in task["exclude_words"])


class PollPaused(Exception):
    def __init__(self, seconds, marker=None):
        super().__init__(seconds)
        self.seconds = seconds
        self.marker = marker


class MonitorEngine:
    def __init__(
        self, store: MonitorStore, *, resolve_account=resolve_enabled_account,
        search=search_listings, detail=fetch_detail, evaluator=evaluate_item,
        notify=notify_service.notify_monitor_item, acquire=rate_limiter.acquire, clock=time.time,
    ):
        self.store = store
        self.resolve_account = resolve_account
        self.search = search
        self.detail = detail
        self.evaluator = evaluator
        self.notify = notify
        self.acquire = acquire
        self.clock = clock
        self._recent = OrderedDict()
        self._platform_lock = asyncio.Lock()
        self._platform_until = 0
        self._stop = asyncio.Event()

    def _remember(self, item_id):
        self._recent[item_id] = None
        if len(self._recent) > 8192:
            self._recent.popitem(last=False)

    async def _current_task(self, task):
        active = await asyncio.to_thread(self.store.get_task, task["id"])
        if (self._stop.is_set() or not active or
                any(task[field] != active[field] for field in MonitorTask.model_fields)):
            raise PollPaused(task["interval_seconds"])
        return active

    async def _check_platform(self, task):
        active = await self._current_task(task)
        until = await asyncio.to_thread(self.store.platform_cooldown_until)
        remaining = math.ceil(max(until, self._platform_until, active["cooldown_until"]) - self.clock())
        if remaining > 0:
            raise PollPaused(remaining)

    async def _take_quota(self, scope):
        allowed, wait = await asyncio.to_thread(self.acquire, scope, "global")
        if not allowed:
            raise PollPaused(max(1, wait))

    async def _request(self, task, operation, *args):
        try:
            return await asyncio.to_thread(operation, *args)
        except Exception as exc:
            marker = next((marker for marker in PLATFORM_PAUSE_MARKERS
                           if has_marker(str(exc), (marker,))), None)
            if marker is None:
                raise
            now = self.clock()
            try:
                delay = await asyncio.to_thread(self.store.trip, task["id"], now)
            except Exception:
                delay = BACKOFF_SECONDS[-1]
                logger.exception("捡漏雷达无法保存冷却状态")
            self._platform_until = now + delay
            logger.warning("捡漏雷达任务 {} 收到平台限制 {}，暂停 {} 秒", task["id"], marker, delay)
            raise PollPaused(delay, marker) from exc

    async def _inspect(self, task, cookie, listing):
        item_id = listing["item_id"]
        async with self._platform_lock:
            if item_id in self._recent:
                return None
            await self._check_platform(task)
            claimed = await asyncio.to_thread(self.store.claim, task["id"], listing)
            if not claimed:
                self._remember(item_id)
                return None
            try:
                await self._take_quota("detail")
            except Exception:
                await asyncio.to_thread(self.store.release_unfetched, item_id)
                raise
            self._remember(item_id)
            try:
                return await self._request(task, self.detail, cookie, listing)
            except PollPaused as exc:
                await asyncio.to_thread(self.store.record_error, item_id, exc.marker)
                raise
            except Exception as exc:
                await asyncio.to_thread(self.store.record_error, item_id, type(exc).__name__)
                logger.warning("捡漏雷达商品 {} 详情读取失败（{}）", item_id, type(exc).__name__)
                return None

    async def _process_item(self, task, listing, detail):
        item_id = listing["item_id"]
        cover = _image_url(listing.get("main_image"))
        item = {**listing, **detail, "item_url": f"https://www.goofish.com/item?id={item_id}",
                "test_mode": task["test_mode"]}
        item["image_urls"] = item.get("image_urls") or ([cover] if cover else [])
        if not _matches(task, item):
            await asyncio.to_thread(self.store.save_details, item, None, "ignored")
            return
        await asyncio.to_thread(self.store.save_details, item, None)
        try:
            await self._current_task(task)
            evaluation = await asyncio.to_thread(self.evaluator, item)
            evaluation = {**EMPTY_EVALUATION, **evaluation}
            should_push = task["test_mode"] or evaluation["verdict"] in BUY_VERDICTS
            status = "ignored" if not should_push and evaluation["verdict"] in {"neutral", "avoid"} else "discovered"
            await asyncio.to_thread(self.store.save_details, item, evaluation, status)
            if not should_push:
                return
            await self._current_task(task)
            result = await asyncio.to_thread(self.notify, {**item, "ai_evaluation": evaluation})
            if result.get("ok") and not result.get("skipped"):
                await asyncio.to_thread(self.store.mark_pushed, item_id)
            elif not result.get("ok"):
                await asyncio.to_thread(self.store.record_error, item_id, "通知发送失败")
        except PollPaused:
            raise
        except Exception as exc:
            await asyncio.to_thread(self.store.record_error, item_id, type(exc).__name__)
            logger.warning("捡漏雷达商品 {} 评估或通知失败（{}）", item_id, type(exc).__name__)

    async def poll_task(self, task_id: int):
        """Poll one task and return the delay before its next run."""
        task = await asyncio.to_thread(self.store.get_task, task_id)
        if not task or not task["is_enabled"]:
            return 120
        try:
            async with self._platform_lock:
                await self._check_platform(task)
                cookie = await asyncio.to_thread(self.resolve_account)
                if not cookie:
                    return task["interval_seconds"]
                await self._take_quota("search")
                listings = await self._request(task, self.search, cookie, task)
            for listing in listings:
                item_id = _identifier(listing.get("item_id"))
                if not item_id or item_id in self._recent or not _matches(task, listing):
                    continue
                listing = {**listing, "item_id": item_id}
                detail = await self._inspect(task, cookie, listing)
                if detail is not None:
                    await self._process_item(task, listing, detail)
            await asyncio.to_thread(self.store.clear_task_backoff, task_id)
        except PollPaused as exc:
            return exc.seconds
        except Exception as exc:
            logger.warning("捡漏雷达任务 {} 轮询失败（{}）", task_id, type(exc).__name__)
        return task["interval_seconds"]

    async def run(self):
        workers = {}
        due = {}
        try:
            while not self._stop.is_set():
                now = self.clock()
                for task_id, worker in list(workers.items()):
                    if worker.done():
                        del workers[task_id]
                        try:
                            delay = worker.result()
                        except Exception:
                            logger.exception("捡漏雷达任务 {} 调度失败", task_id)
                            delay = 120
                        due[task_id] = now + delay + random.uniform(0, 5)

                tasks = await asyncio.to_thread(self.store.list_tasks, enabled_only=True)
                enabled_ids = {task["id"] for task in tasks}
                due = {task_id: when for task_id, when in due.items() if task_id in enabled_ids}
                for task in tasks:
                    due.setdefault(task["id"], now + random.uniform(0, 5))
                for task_id in sorted(due, key=due.get):
                    if len(workers) >= 4 or self._stop.is_set() or due[task_id] > now:
                        break
                    if task_id not in workers:
                        workers[task_id] = asyncio.create_task(self.poll_task(task_id))
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=2)
                except asyncio.TimeoutError:
                    continue
        except Exception:
            logger.exception("捡漏雷达调度器异常退出")
            raise
        finally:
            self._stop.set()
            await asyncio.gather(*workers.values(), return_exceptions=True)

    def stop(self):
        self._stop.set()
