"""
Dynamic Price Rank Guard & Competitor Under-cutting Engine
===========================================================
REA 落地自《拆解 53：商业闭源「全天候智能改价保排位与竞品雷达动态盯盘矩阵」》
提供竞品最低价精准卡位计算、同行缺货涨价时顶价回弹保毛利、
底价防亏损刚性拦截、以及单日最大改价频次防风控抑制器。
"""

import asyncio
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import List, Tuple
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from loguru import logger

import goofish_open
import rate_limiter
from config import config
from price_ladder import PriceLadder

CALLBACK_PATH = "/api/price-rank-guard/callback"
DEFAULT_INTERVAL_SECONDS = 300


def calculate_target_price(
    current_price: float,
    competitor_prices: List[float],
    price_floor: float,
    price_ceiling: float,
    under_cut_delta: float = 0.1,
    daily_revision_count: int = 0,
    max_daily_revisions: int = 5,
) -> Tuple[float, bool, str]:
    """
    根据竞品在售价、保底底价与顶价计算最佳卡位售价。
    :param current_price: 当前商品标价
    :param competitor_prices: 竞品有效在售价格列表
    :param price_floor: 刚性保底底价 (低于此价绝不跟单)
    :param price_ceiling: 期望最高售价 (同行无竞争或断货时回归顶价)
    :param under_cut_delta: 微调压价差额 (默认 0.1 元)
    :param daily_revision_count: 今日已改价次数
    :param max_daily_revisions: 单日最大允许改价次数 (默认 5 次)
    :return: (target_price, should_revise, reason_code)
             reason_code: 'UNDER_CUT', 'FLOOR_GUARD', 'CEILING_REBOUND',
                          'RATE_LIMIT_TRIPPED', 'NO_CHANGE'
    """
    cur_p = round(float(current_price), 2)
    p_floor = round(float(price_floor), 2)
    p_ceil = round(float(price_ceiling), 2)

    # 1. 单日改价频次刚性防风控熔断
    if daily_revision_count >= max_daily_revisions:
        return cur_p, False, "RATE_LIMIT_TRIPPED"

    # 2. 过滤异常与非正数竞品价格，仅以搜索结果前三个有效在售价卡位
    valid_competitors = []
    for price in competitor_prices:
        try:
            value = float(price)
        except (TypeError, ValueError, OverflowError):
            continue
        if value > 0:
            valid_competitors.append(round(value, 2))
        if len(valid_competitors) == 3:
            break

    # 3. 若无有效竞品（同行断货、下架或处于冷门蓝海） -> 回归顶价收割毛利
    if not valid_competitors:
        if abs(cur_p - p_ceil) >= 0.05:
            return p_ceil, True, "CEILING_REBOUND"
        return cur_p, False, "NO_CHANGE"

    # 4. 提取有效竞品最低价并计算卡位价
    min_comp = min(valid_competitors)
    proposed_price = round(min_comp - under_cut_delta, 2)

    # 5. 刚性底价拦截 (Floor Guard)
    if proposed_price < p_floor:
        if abs(cur_p - p_floor) >= 0.05:
            return p_floor, True, "FLOOR_GUARD"
        return cur_p, False, "NO_CHANGE"

    # 6. 顶价上限保护 (不得高于期望顶价)
    if proposed_price > p_ceil:
        proposed_price = p_ceil

    # 7. 判定是否与当前售价存在微调差异 (变动 >= 0.05 元触发改价)
    if abs(proposed_price - cur_p) >= 0.05:
        return proposed_price, True, "UNDER_CUT"

    return cur_p, False, "NO_CHANGE"


def _to_cents(amount: float) -> int:
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"))
        cents = value * 100
        if not value.is_finite() or cents % 1 or cents <= 0:
            raise ValueError("价格必须为正数，且最多保留两位小数")
        return int(cents)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("价格格式无效") from exc


def _first_three_prices(items: List[dict], own_item_id: str) -> List[float]:
    prices: List[float] = []
    for item in items:
        if not isinstance(item, dict) or str(item.get("item_id")) == own_item_id:
            continue
        try:
            price = float(item.get("price"))
        except (TypeError, ValueError, OverflowError):
            continue
        if price > 0:
            prices.append(price)
        if len(prices) == 3:
            break
    return prices


def _effective_daily_count(target: dict, now) -> int:
    last = target.get("last_revised_at")
    day = now.strftime("%Y-%m-%d")
    if not last or str(last)[:10] != day:
        return 0
    try:
        return max(0, int(target.get("daily_revision_count") or 0))
    except (TypeError, ValueError):
        return 0


def _default_alert(title: str, content: str) -> None:
    logger.warning("{}: {}", title, content)
    try:
        import notify_service

        notify_service.send_event("risk", title, content, level="warn")
    except Exception as exc:
        logger.warning("竞品改价预警通知失败，已跳过: {}", type(exc).__name__)


class PriceRankGuard:
    """Scan monitored items, under-cut the first three competitors, and hedge on price."""

    def __init__(self, database, *, detail, search, edit, resolve_account, app_key,
                 max_daily_revisions: int = 5, search_rows: int = 30,
                 alert=None, acquire=None, gate_write=None):
        if type(max_daily_revisions) is not int or max_daily_revisions < 1:
            raise ValueError("max_daily_revisions 必须为正整数")
        self.database = database
        self.detail = detail
        self.search = search
        self.edit = edit
        self.resolve_account = resolve_account
        self.app_key = str(app_key or "")
        self.max_daily_revisions = max_daily_revisions
        self.search_rows = search_rows
        self.alert = alert or _default_alert
        self.acquire = acquire
        self.gate_write = gate_write
        self.ladder = PriceLadder(database.db_path)

    def _load_detail(self, item_id: str):
        try:
            reply = self.detail(item_id=item_id) if callable(self.detail) else self.detail
        except Exception as exc:
            logger.warning("商品 {} 详情读取异常，已跳过：{}", item_id, type(exc).__name__)
            return None
        if not isinstance(reply, dict) or reply.get("code") != 0:
            return None
        data = reply.get("data")
        if not isinstance(data, dict):
            return None
        product_id = data.get("product_id")
        price = data.get("price")
        shops = data.get("publish_shop") or []
        if (type(product_id) is not int or product_id <= 0
                or type(price) is not int or price <= 0
                or data.get("spec_type") != 1 or len(shops) != 1
                or str(shops[0].get("item_id")) != item_id
                or data.get("sku_items")):
            return None
        return product_id, price

    def _competitors(self, keyword: str, item_id: str, cookie: str, cache: dict):
        if keyword in cache:
            return cache[keyword]
        if self.acquire is not None:
            allowed, retry = self.acquire("search", "global")
            if not allowed:
                logger.warning("竞品搜索配额不足，{} 秒后重试：{}", retry, keyword)
                cache[keyword] = None
                return None
        try:
            result = self.search(
                cookie, keyword, app_key=self.app_key, rows=self.search_rows, newest_first=False,
            )
        except Exception as exc:
            logger.warning("竞品搜索调用异常，已跳过本轮：{}", type(exc).__name__)
            cache[keyword] = None
            return None
        if not isinstance(result, dict) or result.get("ok") is not True:
            error = result.get("error") if isinstance(result, dict) else ""
            logger.warning("竞品搜索失败，已跳过本轮：{}", str(error)[:160])
            cache[keyword] = None
            return None
        prices = _first_three_prices(result.get("items") or [], item_id)
        cache[keyword] = prices
        return prices

    def scan_once(self, callback_base: str, now=None):
        moment = now or datetime.now(timezone.utc)
        if not hasattr(moment, "utcoffset") or moment.utcoffset() is None:
            raise ValueError("扫描时间须包含时区")
        moment = moment.astimezone(timezone.utc)
        targets = self.database.list_competitor_monitor_targets(active_only=True)
        cookie = self.resolve_account()
        cache: dict = {}
        scanned = 0
        changed = 0
        for target in targets:
            scanned += 1
            item_id = str(target["item_id"])
            if not cookie:
                logger.warning("竞品盯盘缺少可用账号，已跳过本轮")
                break
            loaded = self._load_detail(item_id)
            if loaded is None:
                logger.warning("商品 {} 详情不支持自动改价，已跳过", item_id)
                continue
            product_id, price = loaded
            prices = self._competitors(target["search_keyword"], item_id, cookie, cache)
            if prices is None:
                continue
            floor = float(target["price_floor"])
            ceiling = float(target["price_ceiling"])
            delta = float(target["under_cut_delta"])
            if prices and min(prices) - delta < floor:
                self.alert(
                    "竞品价格跌破底价",
                    f"商品 {item_id} 竞品最低价 {min(prices):.2f} 元，已锁定底价 {floor:.2f} 元",
                )
            target_price, should_revise, reason = calculate_target_price(
                current_price=price / 100,
                competitor_prices=prices,
                price_floor=floor,
                price_ceiling=ceiling,
                under_cut_delta=delta,
                daily_revision_count=_effective_daily_count(target, moment),
                max_daily_revisions=self.max_daily_revisions,
            )
            if not should_revise:
                if reason == "RATE_LIMIT_TRIPPED":
                    logger.warning("商品 {} 达到单日改价上限，熔断至次日", item_id)
                continue
            if self.gate_write is not None and not self.gate_write(item_id)[0]:
                logger.warning("商品 {} 写操作熔断中，跳过改价", item_id)
                continue
            if not self.database.reserve_competitor_revision(
                    item_id, self.max_daily_revisions, moment):
                logger.warning("商品 {} 达到单日改价上限，熔断至次日", item_id)
                continue
            operation_id = uuid.uuid4().hex
            notify_url = f"{callback_base.rstrip('/')}{CALLBACK_PATH}?" + urlencode(
                {"operation_id": operation_id})

            def submit(product, new_price):
                return self.edit(product, new_price, notify_url)

            state = self.ladder.apply_exact(
                product_id, item_id, price, _to_cents(target_price), submit,
                now=int(moment.timestamp()), operation_id=operation_id,
            )
            if state == "pending":
                changed += 1
                logger.info("商品 {} 已提交改价至 {:.2f} 元（{}）",
                            item_id, target_price, reason)
            else:
                logger.warning("商品 {} 改价未提交（{}）", item_id, state)
        return {"scanned": scanned, "changed": changed}


async def run_scheduler(database, stop):
    import market_listings

    settings = config.get("PRICE_RANK_GUARD") or {}
    callback_base = settings.get("callback_base")
    if not isinstance(callback_base, str):
        logger.error("竞品盯盘需要配置公开 HTTPS 回调域名")
        return
    url = urlsplit(callback_base)
    if (url.scheme != "https" or not url.hostname or url.path not in ("", "/")
            or url.query or url.fragment or url.username or url.password):
        logger.error("竞品盯盘需要配置公开 HTTPS 回调域名")
        return
    account_id = str(settings.get("account_id") or "").strip()
    app_key = config.get("APP_CONFIG.app_key")
    open_settings = goofish_open.get_config()
    if (not account_id or not app_key or not database.get_cookie(account_id)
            or not open_settings["enabled"] or open_settings["dry_run"]
            or not goofish_open.is_configured(open_settings)):
        logger.error("竞品盯盘需要可用账号与已授权的闲管家实盘通道")
        return
    try:
        max_daily = int(settings.get("max_daily_revisions", 5))
        search_rows = int(settings.get("search_rows", 30))
        interval = max(60, int(settings.get("scan_interval_seconds", DEFAULT_INTERVAL_SECONDS)))
    except (TypeError, ValueError):
        logger.error("竞品盯盘调度配置无效")
        return

    def edit(product_id, new_price, notify_url):
        reply = goofish_open.call("product.edit", {
            "product_id": product_id, "price": new_price, "notify_url": notify_url,
        }, allow_write=True)
        if reply.get("code") != 0:
            raise ValueError("商品改价响应缺少成功状态")

    guard = await asyncio.to_thread(
        PriceRankGuard,
        database,
        detail=goofish_open.get_product,
        search=market_listings.search_market_listings,
        edit=edit,
        resolve_account=lambda: database.get_cookie(account_id),
        app_key=str(app_key),
        max_daily_revisions=max_daily,
        search_rows=search_rows,
        acquire=rate_limiter.acquire,
        gate_write=rate_limiter.gate_write,
    )
    while not stop.is_set():
        try:
            await asyncio.to_thread(guard.scan_once, callback_base.rstrip("/"))
        except Exception as exc:
            logger.warning("竞品盯盘扫描中断（{}）", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except asyncio.TimeoutError:
            continue


def create_price_rank_guard_callback_router(database):
    router = APIRouter()

    @router.post(CALLBACK_PATH, include_in_schema=False)
    async def callback(request: Request, operation_id: str, appid: str, timestamp: int, sign: str):
        if config.get("PRICE_RANK_GUARD.enabled", False) is not True:
            raise HTTPException(404, "竞品盯盘未启用")
        settings = goofish_open.get_config()
        now = int(time.time())
        if not settings["app_secret"] or appid != settings["app_key"] or abs(now - timestamp) > 300:
            raise HTTPException(403, "回调签名无效")
        body = await request.body()
        try:
            text = body.decode("utf-8")
        except UnicodeError as exc:
            raise HTTPException(400, "回调编码无效") from exc
        expected = goofish_open.sign(appid, text, str(timestamp), settings["app_secret"])
        if not hmac.compare_digest(expected.encode(), sign.encode()):
            raise HTTPException(403, "回调签名无效")
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise HTTPException(400, "回调内容无效") from exc
        if not isinstance(payload, dict) or payload.get("task_result") not in (1, 2):
            raise HTTPException(400, "回调内容无效")
        ladder = await asyncio.to_thread(PriceLadder, database.db_path)
        state = await asyncio.to_thread(
            ladder._finish, operation_id, payload["task_result"] == 1, now, confirmation=payload,
        )
        if not state:
            raise HTTPException(404, "改价任务不匹配")
        if state == "failed":
            logger.warning("商品 {} 平台改价未确认成功", payload["product_id"])
        return {"result": "success", "msg": "接收成功"}

    return router
