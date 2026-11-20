"""
卖家端订单直连接口（拆解 1c：super-butler `utils/xianyu_seller_api.py`）
=====================================================================

**拆解结论：这是本轮最值钱的一条——不启浏览器也能拿到订单状态。**

原来每刷一单要走两次 Playwright 页面：`fetch_order_detail_simple` 抓 DOM 拿收货人，
再 `OrderStatusQueryPlaywright` 拦 API 拿状态码和买家 ID。10 单就是 20 次页面导航。

super-butler 的做法是：卖家端有一个 `mtop.taobao.idle.trade.merchant.sold.get`
接口，**一次请求返回 20 单**，订单状态、成交额、数量、收货信息、可执行动作全在里面。
所以它把接口作为快路径，只把接口没覆盖到的订单回退给浏览器。

六条从它的源码里抄来、少一条都会踩坑的经验（全部写进代码注释）：

1. **origin/referer 必须是 www.goofish.com**（2026-11 实测）。用 seller.goofish.com
   会返回 `PERMISSION_EXCEPTION::无权限访问`，带 `idle_site_biz_code` 也一样。
2. **appKey 必须是网页 Web APP_KEY**。用 IM Key 签名会返回
   `FAIL_SYS_PARAMINVALID_ERROR::请求参数非法`，很容易误判成 payload 写错。
3. **`orderIds` 只能是逗号分隔的字符串**，空格或数组会被服务端拒绝。
4. **分页终止条件只能用 `nextPage`**：`totalCount` 在越界页会变成 0，
   拿它算页数会提前结束或死循环。
5. **金额取 `priceVO.totalPrice`（真实成交额）**，不是商品挂牌价；数量取 `buyNum`。
   拿挂牌价当成交额，议价单和多件单全是错的。
6. **令牌过期有两种拼写，其中一个是错的**（`TOKEN_EXPIRED` / `TOKEN_EXOIRED`），
   并且新令牌在**这次失败的响应头**里下发，所以要先合并 Cookie 再重签重试一次。

我们自己额外加的两条（不是抄的，是吃过亏）：

- **失败一律不抛给主流程**：返回空 dict，调用方回退浏览器。订单同步是活的主循环，
  不能被一个可选快路径中断。
- **连续失败会自我熔断**（`_disable_for`）：接口改字段/风控时，不会每一单都去撞一次墙。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, Iterable, List, Optional

import requests
from loguru import logger

from platform_guard import (
    PlatformGuardBlocked,
    admit as guard_admit,
    observe as guard_observe,
)

MTOP_HOST = "https://h5api.m.goofish.com"
SOLD_API = "mtop.taobao.idle.trade.merchant.sold.get"
SOLD_VERSION = "1.0"
EXPOSURE_API = "mtop.taobao.idle.seller.stat.get"
EXPOSURE_VERSION = "1.0"
WEB_ORIGIN = "https://www.goofish.com"
TOKEN_COOKIE = "_m_h5_tk"
SPM_CNT = "a21107h.42826273.0.0"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

ENV_SWITCH = "SELLER_API_ENABLED"
FAIL_COOLDOWN = 600      # 连续失败后冷却秒数
FAIL_THRESHOLD = 3

_disabled_until: Dict[str, float] = {}
_fail_streak: Dict[str, int] = {}


class SellerApiError(RuntimeError):
    def __init__(self, api: str, ret: Iterable[Any]):
        self.api = api
        self.ret = [str(x) for x in (ret or [])]
        super().__init__(f"{api} 调用失败: {'; '.join(self.ret) or '未知错误'}")


def enabled() -> bool:
    """总开关。关掉就完全走原来的浏览器路径（`SELLER_API_ENABLED=0`）。"""
    return (os.getenv(ENV_SWITCH, "1") or "1").strip() not in ("0", "false", "False", "off")


def _blocked(cookie_id: str) -> bool:
    until = _disabled_until.get(cookie_id or "", 0)
    return until > time.time()


def _disable_for(cookie_id: str, seconds: int = FAIL_COOLDOWN) -> None:
    if not cookie_id:
        return
    _disabled_until[cookie_id] = time.time() + seconds
    logger.warning(f"【{cookie_id}】卖家端订单接口暂停 {seconds} 秒，改用浏览器抓取")


def _note_fail(cookie_id: str, reason: str) -> None:
    n = _fail_streak.get(cookie_id or "", 0) + 1
    _fail_streak[cookie_id or ""] = n
    if n >= FAIL_THRESHOLD:
        _fail_streak[cookie_id or ""] = 0
        _disable_for(cookie_id)


def _note_ok(cookie_id: str) -> None:
    _fail_streak[cookie_id or ""] = 0


def _reset_state() -> None:
    """仅供测试。"""
    _disabled_until.clear()
    _fail_streak.clear()


def _split_cookies(cookie_str: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in str(cookie_str or "").split("; "):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def _token(cookie_str: str) -> str:
    """签名令牌取 `_m_h5_tk` 下划线前半段。"""
    return (_split_cookies(cookie_str).get(TOKEN_COOKIE, "") or "").split("_")[0]


def _app_key() -> str:
    # 成交/商品等网页 MTOP 接口使用独立的 Web APP_KEY；
    # global_config.yml 里的 APP_CONFIG.app_key 是 IM Key，签名会因此被判参数非法。
    try:
        from opportunity_publisher import WEB_APP_KEY
        if WEB_APP_KEY:
            return str(WEB_APP_KEY)
    except Exception:
        pass
    try:
        from config import config
        return str(config.get("APP_CONFIG.app_key", "") or "")
    except Exception:
        return ""


def _sign(token: str, t: str, data_val: str, app_key: str) -> str:
    return hashlib.md5(f"{token}&{t}&{app_key}&{data_val}".encode("utf-8")).hexdigest()


def _headers(cookie_str: str) -> Dict[str, str]:
    return {
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "cache-control": "no-cache",
        "content-type": "application/x-www-form-urlencoded",
        # 2026-11 实测：成交接口只认 www 来源；seller 来源会直接无权限
        "origin": WEB_ORIGIN,
        "referer": f"{WEB_ORIGIN}/",
        "pragma": "no-cache",
        "sec-ch-ua": '"Chromium";v="120", "Not-A.Brand";v="24", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": UA,
        "cookie": str(cookie_str or "").replace("\n", "").replace("\r", ""),
    }


def _merge_set_cookies(cookie_str: str, resp: Any) -> str:
    """把响应下发的续期 Cookie 并回本地串（新令牌就在失败的响应里）。"""
    try:
        raw_headers = getattr(resp, "headers", {}) or {}
        items = []
        if hasattr(raw_headers, "getlist"):
            items = raw_headers.getlist("set-cookie") or []
        elif raw_headers.get("set-cookie"):
            items = [raw_headers.get("set-cookie")]
    except Exception:
        return cookie_str
    if not items:
        return cookie_str
    jar = _split_cookies(cookie_str)
    changed = False
    for raw in items:
        pair = str(raw).split(";", 1)[0].strip()
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        if k in (TOKEN_COOKIE, f"{TOKEN_COOKIE}_enc", "cookie2", "sgcookie", "_tb_token_"):
            jar[k] = v
            changed = True
    if not changed:
        return cookie_str
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def _call(api: str, payload: Dict[str, Any], cookie_str: str,
          version: str = "1.0", timeout: int = 20, *,
          retry_expired_token: bool = True) -> Dict[str, Any]:
    """调卖家端接口；业务失败抛 SellerApiError。令牌过期自动重签重试一次。"""
    token = _token(cookie_str)
    if not token:
        raise SellerApiError(api, [f"Cookie 中缺少 {TOKEN_COOKIE}"])
    app_key = _app_key()
    if not app_key:
        raise SellerApiError(api, ["APP_CONFIG.app_key 未配置"])

    last_ret: List[str] = []
    cur_cookie = cookie_str
    for attempt in range(2 if retry_expired_token else 1):
        data_val = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        t = str(int(time.time() * 1000))
        params = {
            "jsv": "2.7.2", "appKey": app_key, "t": t,
            "sign": _sign(token, t, data_val, app_key),
            "v": version, "type": "originaljson", "accountSite": "xianyu",
            "dataType": "json", "timeout": "20000", "api": api,
            "sessionOption": "AutoLoginOnly", "spm_cnt": SPM_CNT,
        }
        request_options = {} if retry_expired_token else {"allow_redirects": False}
        try:
            guard_admit(cur_cookie, scope="seller_api", write=False, sleep=True)
        except PlatformGuardBlocked as exc:
            raise SellerApiError(api, [str(exc)])
        resp = requests.post(f"{MTOP_HOST}/h5/{api}/{version}/", params=params,
                             data={"data": data_val}, headers=_headers(cur_cookie),
                             timeout=timeout, **request_options)
        if not retry_expired_token:
            resp.raise_for_status()
            if resp.status_code != 200:
                raise SellerApiError(api, [f"HTTP {resp.status_code}"])
        # 先合并 Cookie：新令牌就在这次（可能是失败的）响应里
        cur_cookie = _merge_set_cookies(cur_cookie, resp)

        try:
            result = resp.json()
        except Exception:
            raise SellerApiError(api, [f"响应不是 JSON（HTTP {getattr(resp, 'status_code', '?')}）"])

        ret_list = [str(x) for x in (result.get("ret") or [])] if isinstance(result, dict) else []
        guard_observe(
            cur_cookie,
            text=" | ".join(ret_list),
            status_code=int(getattr(resp, "status_code", 0) or 0),
        )
        if any("SUCCESS" in x for x in ret_list):
            return result

        last_ret = ret_list or ["响应缺少 ret"]
        # 令牌过期有两种拼写，其中 EXOIRED 是平台自己的历史拼写错误
        expired = any(("TOKEN_EXPIRED" in x or "TOKEN_EXOIRED" in x
                       or "FAIL_SYS_TOKEN_EMPTY" in x) for x in ret_list)
        if not retry_expired_token or not expired or attempt == 1:
            break
        token = _token(cur_cookie)
        if not token:
            break
        logger.debug(f"{api} 令牌过期，用响应下发的新令牌重试")

    raise SellerApiError(api, last_ret)


def get_sold_orders(cookie_str: str, page_number: int = 1, rows_per_page: int = 20,
                    query_code: str = "ALL", order_ids: List[str] | str = "",
                    search_param: Optional[Dict[str, Any]] = None,
                    timeout: int = 20) -> Dict[str, Any]:
    """查卖出订单列表。`order_ids` 传列表时自动拼成逗号串。"""
    if isinstance(order_ids, (list, tuple)):
        order_ids = ",".join(str(x) for x in order_ids)
    payload = {
        "pageNumber": int(page_number),
        "rowsPerPage": int(rows_per_page),
        "orderIds": order_ids or "",
        "queryCode": query_code or "ALL",
        # 这个字段要求 JSON 字符串，不能直接传对象
        "orderSearchParam": json.dumps(search_param or {}, separators=(",", ":"),
                                       ensure_ascii=False),
    }
    result = _call(SOLD_API, payload, cookie_str, SOLD_VERSION, timeout)
    module = ((result.get("data") or {}).get("module") or {})
    return {
        "items": module.get("items") or [],
        "total_count": module.get("totalCount") or 0,
        "next_page": bool(module.get("nextPage")),
    }


def parse_seller_exposure_stats(response: Dict[str, Any], platform_account_id: str) -> List[Dict[str, Any]]:
    if (not isinstance(response, dict) or response.get("api") != EXPOSURE_API
            or response.get("v") != EXPOSURE_VERSION):
        raise ValueError("卖家统计接口或版本不匹配")
    statuses = response.get("ret")
    if (not isinstance(statuses, list) or not statuses or not isinstance(statuses[0], str)
            or not statuses[0].startswith("SUCCESS::")):
        raise ValueError("卖家统计接口未返回成功回执")
    payload = response.get("data")
    if (not isinstance(platform_account_id, str) or not platform_account_id
            or not isinstance(payload, dict) or payload.get("accountId") != platform_account_id):
        raise ValueError("卖家统计账号与预期账号不一致")
    rows = payload.get("dailyStats")
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError("卖家统计至少需要两天的日数据")
    return rows


def fetch_seller_exposure_stats(cookie_str: str, days: int = 2) -> List[Dict[str, Any]]:
    account_id = _split_cookies(cookie_str).get("unb")
    if not account_id:
        raise ValueError("Cookie 中缺少 unb")
    if type(days) is not int or days < 2:
        raise ValueError("卖家统计至少需要两天的日数据")
    response = _call(EXPOSURE_API, {
        "days": days, "statType": "EXPOSURE_PV_UV", "pageNumber": 1, "pageSize": 20,
    }, cookie_str, EXPOSURE_VERSION, retry_expired_token=False)
    return parse_seller_exposure_stats(response, account_id)


def _num(value: Any) -> Optional[float]:
    try:
        return float(str(value).replace("¥", "").replace("￥", "").strip())
    except Exception:
        return None


def _parse_sub_orders(value):
    if not isinstance(value, list):
        return None
    sub_order_ids = set()
    rows = []
    for row in value:
        if not isinstance(row, dict):
            return None
        sub_order_id, sku_id, quantity = row.get("subOrderId"), row.get("skuId"), row.get("buyNum")
        if not all(isinstance(part, str) and part and part == part.strip()
                   for part in (sub_order_id, sku_id)):
            return None
        if sub_order_id in sub_order_ids or type(quantity) is not int or quantity <= 0:
            return None
        sub_order_ids.add(sub_order_id)
        rows.append((sub_order_id, sku_id, quantity))
    return tuple(rows)


def parse_sold_order(item: Dict[str, Any]) -> Dict[str, Any]:
    """把 sold.get 的一行展开成与浏览器抓取同构的扁平字段。"""
    from order_status_rules import normalize_status

    common = item.get("commonData") or {}
    is_flat = "commonData" not in item
    identity = item if is_flat else common
    buyer = item.get("buyerInfoVO") or {}
    price = item.get("priceVO") or {}
    item_vo = item.get("itemVO") or {}
    right = item.get("rightVO") or {}

    actions = [b.get("tradeAction") for b in (right.get("btnList") or []) if b.get("tradeAction")]

    def s(v: Any) -> str:
        return "" if v is None else str(v)

    order_id = s(identity.get("orderId"))
    status_text = s(identity.get("orderStatus"))
    order_status = "pending_ship" if is_flat and status_text == "PAID" else normalize_status("", status_text)
    amount = s(price.get("totalPrice"))
    buy_num = price.get("buyNum") or 1
    sub_orders = _parse_sub_orders(item["subOrders"]) if "subOrders" in item else ()
    if sub_orders:
        buy_num = sum(quantity for _, _, quantity in sub_orders)
    elif is_flat and not price.get("buyNum"):
        buy_num = None
    if not amount and buy_num is not None:
        # totalPrice 缺失（历史归档单）时用单价×件数兜底，并在字段里标出来
        unit = _num(price.get("auctionPrice"))
        if unit is not None:
            amount = f"{unit * int(buy_num):.2f}"

    return {
        "order_id": order_id,
        "url": f"https://www.goofish.com/order-detail?orderId={order_id}&role=seller",
        "title": f"订单详情 - {order_id}",
        "order_status": order_status,
        "status_text": status_text,
        "item_id": s(identity.get("itemId")),
        "item_title": s(item.get("itemTitle") if is_flat else item_vo.get("title")),
        "spec_name": "",
        "spec_value": "",
        "quantity": s(buy_num),
        "amount": amount,
        "auction_price": s(price.get("auctionPrice")),
        "buy_num": buy_num,
        "post_fee": s(price.get("postFee")),
        "discount_fee": s(price.get("discountFee")),
        "confirm_fee": s(price.get("confirmFee")),
        "refund_fee": s(price.get("refundFee")),
        "order_time": s(common.get("createTime")),
        "pay_time": s(common.get("paySuccessTime")),
        "consign_time": s(common.get("consignTime")),
        "finish_time": s(common.get("finishTime")),
        "buyer_id": s(buyer.get("buyerId")),
        "buyer_nick": s(buyer.get("userNick")),
        "receiver_name": s(buyer.get("name")),
        "receiver_phone": s(buyer.get("phone")),
        "receiver_address": s(buyer.get("address")),
        "receiver_city": "",
        "is_privacy": bool(buyer.get("isPrivacy")),
        "in_refund": bool(common.get("inRefund")),
        "refund_status_desc": s(common.get("refundStatusDesc")),
        "seller_rate_status": common.get("sellerRateStatus"),
        "can_rate": "RATE" in actions,
        "trade_actions": actions,
        "api_status": "sold.get",
        "dom_status": "",
        "from_seller_api": True,
        **({"sub_orders": sub_orders} if "subOrders" in item else {}),
    }


def fetch_orders_by_ids(cookie_str: str, cookie_id: str, order_ids: List[str],
                        chunk_size: int = 20, timeout: int = 20) -> Dict[str, Dict[str, Any]]:
    """按订单号精确查（一次最多 20 单）。返回 `{order_id: 扁平字段}`。

    永不抛异常：拿到的部分返回，拿不到的由调用方回退浏览器。
    """
    out: Dict[str, Dict[str, Any]] = {}
    ids = [str(x) for x in (order_ids or []) if str(x).strip()]
    if not ids or not cookie_str:
        return out
    if not enabled():
        return out
    if _blocked(cookie_id):
        return out

    for start in range(0, len(ids), max(1, int(chunk_size))):
        chunk = ids[start:start + max(1, int(chunk_size))]
        try:
            batch = get_sold_orders(cookie_str, page_number=1,
                                    rows_per_page=max(len(chunk), 10),
                                    order_ids=chunk, timeout=timeout)
        except Exception as e:
            _note_fail(cookie_id, str(e))
            logger.warning(f"【{cookie_id}】卖家端接口查询 {len(chunk)} 单失败，回退浏览器: {e}")
            continue
        for item in batch["items"]:
            try:
                row = parse_sold_order(item)
            except Exception as e:
                logger.debug(f"【{cookie_id}】解析 sold.get 行失败: {e}")
                continue
            if row["order_id"]:
                out[row["order_id"]] = row
        _note_ok(cookie_id)

    if out:
        logger.info(f"【{cookie_id}】卖家端接口直取 {len(out)}/{len(ids)} 个订单（未启动浏览器）")
    return out


def iter_sold_orders(cookie_str: str, days: Optional[int] = None, rows_per_page: int = 50,
                     query_code: str = "ALL", max_pages: int = 100,
                     page_interval: float = 0.3) -> List[Dict[str, Any]]:
    """翻页拉卖出订单，返回成功读取的订单列表。"""
    return fetch_sold_order_pages(cookie_str, days, rows_per_page, query_code, max_pages, page_interval)["items"]


def fetch_sold_order_pages(cookie_str: str, days: Optional[int] = None, rows_per_page: int = 50,
                           query_code: str = "ALL", max_pages: int = 100,
                           page_interval: float = 0.3, cookie_id: str = "") -> Dict[str, Any]:
    """按平台的 next_page 翻页，保留成功的订单并记录未能读到的页。"""
    if not enabled():
        raise RuntimeError("卖家端订单接口已关闭")
    if cookie_id and _blocked(cookie_id):
        raise RuntimeError("卖家端订单接口处于失败冷却中")
    if not cookie_str:
        raise RuntimeError("账号无可用登录状态")

    collected: List[Dict[str, Any]] = []
    failed_pages: List[int] = []
    pages_read = 0
    has_more = False
    seen: set = set()
    search = {}
    if days:
        start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        search = {"createStart": start}

    for page in range(1, max(1, int(max_pages)) + 1):
        batch = None
        for attempt in range(3):
            try:
                batch = get_sold_orders(cookie_str, page_number=page,
                                        rows_per_page=rows_per_page,
                                        query_code=query_code,
                                        search_param=search or None)
                break
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"第 {page} 页拉取失败（已重试 3 次）: {type(e).__name__}")
                else:
                    time.sleep(1.0 * (attempt + 1))
        if batch is None:
            failed_pages.append(page)
            if cookie_id:
                _note_fail(cookie_id, "订单分页失败")
                if _blocked(cookie_id):
                    break
            if page_interval:
                time.sleep(page_interval)
            continue
        pages_read += 1
        if cookie_id:
            _note_ok(cookie_id)
        for item in batch["items"]:
            common = item.get("commonData") if isinstance(item, dict) else None
            oid = str(common.get("orderId") or "") if isinstance(common, dict) else ""
            if oid and oid in seen:
                continue
            if oid:
                seen.add(oid)
            collected.append(item)
        has_more = batch["next_page"]
        if not has_more:
            break
        if page_interval:
            time.sleep(page_interval)
    return {"items": collected, "failed_pages": failed_pages, "has_more": has_more, "pages_read": pages_read}


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """给 B2 复盘用的真成交口径（原先只能靠商品卡上的累计销量猜）。"""
    total = 0.0
    paid = refund = pending_ship = 0
    for r in rows:
        st = r.get("order_status") or ""
        if st in ("cancelled",):
            continue
        paid += 1
        try:
            total += float(str(r.get("amount") or 0).replace("¥", "").strip() or 0)
        except Exception:
            pass
        if r.get("in_refund") or st == "refunding":
            refund += 1
        if st in ("pending_ship", "pending"):
            pending_ship += 1
    return {
        "orders": paid,
        "amount": round(total, 2),
        "refund_orders": refund,
        "pending_ship": pending_ship,
    }


def sales_summary(cookie_str: str, cookie_id: str = "", days: int = 7,
                  max_pages: int = 5, rows_per_page: int = 50) -> Dict[str, Any]:
    """
    店铺复盘（B2）的**真成交口径**——订单数、成交额、退款单、待发货。

    原来 B2 只能用商品卡上的「累计销量」猜：那是商品的**历史**累计，
    反映不了"这周卖了几单、卖了多少、有没有退款"。`sold.get` 直接给原值。

    三次克制：
    - **只翻 max_pages 页**（默认 5×50=250 单封顶）。复盘是人工触发的一次性动作，
      不是全量回填；店铺大到翻不完时宁可只算最近的，也别让一次点击打上百个请求。
    - **永不抛异常**，拿不到就 `{"ok": False, "reason": ...}`，复盘报告照常出。
    - 接口被关 / 已熔断时直接返回不可用，不去撞墙。

    返回 `{ok, reason, days, orders, amount, refund_orders, pending_ship, pages, cancelled}`。
    """
    out: Dict[str, Any] = {"ok": False, "reason": "", "days": int(days),
                           "orders": 0, "amount": 0.0, "refund_orders": 0,
                           "pending_ship": 0, "pages": 0, "cancelled": 0}
    if not enabled():
        out["reason"] = "卖家端接口已关闭（SELLER_API_ENABLED=0）"
        return out
    if not cookie_str:
        out["reason"] = "没有可用 Cookie"
        return out
    if cookie_id and _blocked(cookie_id):
        out["reason"] = "卖家端接口处于失败冷却中"
        return out

    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - int(days) * 86400))
    parsed: List[Dict[str, Any]] = []
    seen: set = set()
    pages = 0
    try:
        for page in range(1, max(1, int(max_pages)) + 1):
            batch = get_sold_orders(cookie_str, page_number=page,
                                    rows_per_page=rows_per_page, query_code="ALL",
                                    search_param={"createStart": start})
            pages += 1
            for item in batch["items"]:
                try:
                    row = parse_sold_order(item)
                except Exception as e:
                    logger.debug(f"复盘：解析 sold.get 行失败: {e}")
                    continue
                oid = row.get("order_id")
                if oid and oid in seen:
                    continue
                if oid:
                    seen.add(oid)
                parsed.append(row)
            if not batch["next_page"]:
                break
            if not batch["items"]:
                break
            time.sleep(0.3)
        _note_ok(cookie_id)
    except Exception as e:
        _note_fail(cookie_id, str(e))
        if not parsed:
            out["reason"] = f"拉取失败：{e}"
            return out
        out["reason"] = f"部分页拉取失败：{e}"

    s = summarize(parsed)
    cancelled = len([r for r in parsed if (r.get("order_status") or "") == "cancelled"])
    out.update(s)
    out.update({"ok": True, "pages": pages, "cancelled": cancelled})
    if not out["reason"]:
        out["reason"] = "ok"
    return out


__all__ = ["SellerApiError", "enabled", "get_sold_orders", "parse_sold_order",
           "fetch_orders_by_ids", "iter_sold_orders", "summarize", "sales_summary",
           "SOLD_API"]
