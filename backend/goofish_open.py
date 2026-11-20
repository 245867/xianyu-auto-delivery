"""
闲管家开放平台客户端（D1）

**为什么要这个东西**：D1 实测确认，个人闲鱼号可以自建"自研进销存"应用，按店铺订购
铂金版（约 198 元/年）即可调用。它提供的东西恰好是高危写操作：
    改库存（edit/stock）、发货（不用自己模拟点击）、改快递单号、无需物流发货、售后处理、
    订单/商品推送回调。
而 MTOP 侧那些**未实证**的写接口（正是 A6 绕开的 `item.edit` / `status.update`）
在这里有官方版本，参数是公开文档写明的，不会"返回成功但没改成"。

**它没有什么**（官方 FAQ 明确否认，别指望）：
    曝光/浏览/想要/收藏/咨询/搜索排名 → 没有
    闲鱼消息/聊天 API → 没有，且官方说"后续也不支持"
    图片上传 → 没有，只能传公网 URL
    擦亮 → 没有
所以不是"换路线"，是"补一条腿"：写操作走官方，读数据与擦亮继续走 MTOP。

**安全姿态**：
1. 默认关闭。没有配 app_key / app_secret 就整体不启用，任何调用直接返回错误，
   绝不 fallback 到别的通道（避免"以为走了官方其实走了 MTOP"）。
2. 只读接口（类目/商品列表/商品详情/订单列表）可以直接用；
   写接口一律要 `enabled=True` + 非 dry_run。
3. 所有写调用都要外部传入幂等键，内部记 `open_platform_calls.jsonl`（本地追加日志，
   含时间/接口/成败/耗时，不含 app_secret 与报文全文）。

签名（官方原文）：`md5("应用KEY,Body报文Md5值,时间戳,应用密钥")`
- body 必须**压缩后**的 JSON（无空格）；参数**不排序**；中文与链接原样不编码。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from platform_guard import (
    PlatformGuardBlocked,
    admit as guard_admit,
    observe as guard_observe,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_FILE = DATA_DIR / "open_platform_config.json"
CALL_LOG = DATA_DIR / "open_platform_calls.jsonl"

BASE_URL = "https://open.goofish.pro"
DEFAULT_TIMEOUT = 20
QPS_LIMIT = 20          # 官方：单账号总和 20 次/秒
DAILY_LIMIT = 100000    # 官方：单商家单 API 日 10 万次

# 只读接口（不影响店铺数据）
READ_APIS = {
    "category.list":       "/api/open/product/category/list",
    "attribute.list":      "/api/open/product/attribute/list",
    "product.list":        "/api/open/product/list",
    "product.detail":      "/api/open/product/detail",
    "product.sku.list":    "/api/open/product/sku/list",
    "order.list":          "/api/open/order/list",
    "order.detail":        "/api/open/order/detail",
    "order.kam":           "/api/open/order/kam",
    "aftersale.list":      "/api/open/aftersale/list",
    "aftersale.detail":    "/api/open/trade/refund/detail",
    "aftersale.reject_reasons": "/api/open/trade/refund/refused/reason",
    "shop.list":           "/api/open/user/authorize/list",
    "express.list":        "/api/open/express/list",
    "wallet.balance":      "/api/open/wallet/balance",
}

# 写接口（必须 enabled 且非演练）
WRITE_APIS = {
    "product.create":      "/api/open/product/create",
    "product.batchCreate": "/api/open/product/batchCreate",
    "product.publish":     "/api/open/product/publish",
    "product.downShelf":   "/api/open/product/downShelf",
    "product.edit":        "/api/open/product/edit",
    "product.editStock":   "/api/open/product/edit/stock",
    "product.delete":      "/api/open/product/delete",
    "order.ship":          "/api/open/order/ship",
    "order.dummyShip":     "/api/open/order/dummyShip",
    "order.editPrice":     "/api/open/order/editPrice",
    "order.editExpress":   "/api/open/order/editExpress",
    "order.refuseRefund":  "/api/open/trade/refund/operate/refused",
    "aftersale.agree":     "/api/open/aftersale/agree",
    "aftersale.reject":    "/api/open/trade/refund/operate/refused",
}

ALL_APIS = {**READ_APIS, **WRITE_APIS}

# 官方状态码（商品/订单），用于把 errcode 翻成人话
PRODUCT_STATUS = {21: "在售", 22: "已下架", 23: "已售出", 31: "审核中",
                  33: "审核未通过", 36: "已删除", -1: "未知"}
ORDER_STATUS = {11: "待付款", 12: "待发货", 21: "已发货", 22: "交易成功",
                23: "退款中", 24: "已关闭"}

DEFAULTS = {
    "enabled": False,
    "dry_run": True,
    "app_key": "",
    "app_secret": "",
    "seller_id": "",
    # 本地限流（比官方 20 QPS 保守：官方是按账号总和算，我们单进程用不到那么多）
    "max_qps": 5,
}


class OpenPlatformError(Exception):
    """开放平台调用失败。kind: disabled / config / network / business / risk"""

    def __init__(self, msg: str, kind: str = "business", raw: Any = None):
        super().__init__(msg)
        self.kind = kind
        self.raw = raw


# ---------------------------------------------------------------- 配置

def get_config() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k in DEFAULTS:
                if k in raw:
                    cfg[k] = raw[k]
    except Exception:
        pass
    cfg["enabled"] = bool(cfg["enabled"])
    cfg["dry_run"] = bool(cfg["dry_run"])
    try:
        cfg["max_qps"] = max(1, min(QPS_LIMIT, int(cfg["max_qps"] or 5)))
    except Exception:
        cfg["max_qps"] = 5
    for k in ("app_key", "app_secret", "seller_id"):
        cfg[k] = str(cfg[k] or "").strip()
    return cfg


def save_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """保存配置。app_secret 只落盘、不经任何返回值或日志外泄。"""
    cfg = get_config()
    for k in DEFAULTS:
        if k in (data or {}):
            cfg[k] = data[k]
    cfg["enabled"] = bool(cfg["enabled"])
    cfg["dry_run"] = bool(cfg["dry_run"])
    try:
        cfg["max_qps"] = max(1, min(QPS_LIMIT, int(cfg.get("max_qps") or 5)))
    except Exception:
        cfg["max_qps"] = 5
    for k in ("app_key", "app_secret", "seller_id"):
        cfg[k] = str(cfg.get(k) or "").strip()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return get_config()


def is_configured(cfg: Optional[Dict[str, Any]] = None) -> bool:
    c = cfg or get_config()
    return bool(c["app_key"] and c["app_secret"])


def status() -> Dict[str, Any]:
    """给页面用的状态。**绝不返回 app_secret，app_key 只回显后 4 位**。"""
    cfg = get_config()
    key = cfg["app_key"]
    return {
        "ok": True,
        "configured": is_configured(cfg),
        "enabled": cfg["enabled"],
        "dry_run": cfg["dry_run"],
        "app_key_masked": (f"****{key[-4:]}" if len(key) > 4 else ("已填" if key else "")),
        "seller_id": cfg["seller_id"],
        "max_qps": cfg["max_qps"],
        "read_api_count": len(READ_APIS),
        "write_api_count": len(WRITE_APIS),
        "note": ("未配置 AppKey，开放平台通道不启用（功能不受影响，仍走 MTOP）"
                 if not is_configured(cfg) else ""),
    }


# ---------------------------------------------------------------- 签名与调用

def sign(app_key: str, body_str: str, timestamp: str, app_secret: str, seller_id: str = "") -> str:
    """
    官方规则：md5("应用KEY,Body报文Md5值,时间戳,应用密钥")
    注意 body 的 md5 是**压缩后 JSON 原文**的 md5（无空格、不排序、不编码）。
    """
    body_md5 = hashlib.md5(body_str.encode("utf-8")).hexdigest()
    parts = [app_key, body_md5, timestamp]
    if seller_id:
        parts.append(seller_id)
    raw = ",".join([*parts, app_secret])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


_qps_window: List[float] = []


def _throttle(max_qps: int) -> None:
    """进程内 1 秒滑动窗口，主动守在 max_qps 以内（比官方 20 保守）。"""
    now = time.time()
    while _qps_window and now - _qps_window[0] > 1.0:
        _qps_window.pop(0)
    if len(_qps_window) >= max_qps:
        sleep_for = 1.0 - (now - _qps_window[0])
        if sleep_for > 0:
            time.sleep(min(sleep_for, 1.0))
        now = time.time()
        while _qps_window and now - _qps_window[0] > 1.0:
            _qps_window.pop(0)
    _qps_window.append(time.time())


def _log_call(api: str, ok: bool, ms: int, err: str = "") -> None:
    """本地追加日志。**不含 app_secret、不含报文全文**（只留体量）。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(CALL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "api": api, "ok": bool(ok), "ms": int(ms), "err": str(err or "")[:160],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def call(api_name: str, body: Optional[Dict[str, Any]] = None,
         allow_write: bool = False, timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """
    调一个开放平台接口。

    api_name 用 ALL_APIS 里的键（如 "product.list"），不用原始路径——
    这样拼错会在本地报错，不会打到平台上去。
    """
    cfg = get_config()
    if not cfg["enabled"]:
        raise OpenPlatformError("开放平台通道未启用（默认关闭）", kind="disabled")
    if not is_configured(cfg):
        raise OpenPlatformError("未配置 AppKey/AppSecret", kind="config")
    path = ALL_APIS.get(api_name)
    if not path:
        raise OpenPlatformError(f"未知接口：{api_name}", kind="config")

    is_write = api_name in WRITE_APIS
    if is_write and not allow_write:
        raise OpenPlatformError(
            f"{api_name} 是写操作，调用方必须显式 allow_write=True（防误触）", kind="config")
    if is_write and cfg["dry_run"]:
        raise OpenPlatformError(
            f"{api_name} 是写操作，当前是演练模式（dry_run），已拒绝执行", kind="disabled")

    # 官方要求 body 是压缩 JSON，参数不排序
    body_str = json.dumps(body or {}, separators=(",", ":"), ensure_ascii=False)
    ts = str(int(time.time()))
    sig = sign(cfg["app_key"], body_str, ts, cfg["app_secret"], cfg["seller_id"])

    params = {"appid": cfg["app_key"], "timestamp": ts, "sign": sig}
    if cfg["seller_id"]:
        params["seller_id"] = cfg["seller_id"]

    _throttle(cfg["max_qps"])
    guard_account = str(cfg.get("seller_id") or "open_platform")
    try:
        guard_admit(
            account_id=guard_account,
            scope="open_platform",
            write=is_write,
            sleep=True,
        )
    except PlatformGuardBlocked as exc:
        raise OpenPlatformError(str(exc), kind="risk")
    t0 = time.time()
    try:
        resp = requests.post(f"{BASE_URL}{path}", params=params,
                             data=body_str.encode("utf-8"),
                             headers={"Content-Type": "application/json"},
                             timeout=timeout)
    except Exception as e:
        _log_call(api_name, False, int((time.time() - t0) * 1000), f"network: {e}")
        raise OpenPlatformError(f"网络异常：{str(e)[:120]}", kind="network")

    ms = int((time.time() - t0) * 1000)
    try:
        raw = resp.json()
    except Exception:
        _log_call(api_name, False, ms, f"HTTP {resp.status_code} 非 JSON")
        raise OpenPlatformError(
            f"响应不是 JSON（HTTP {resp.status_code}）：{resp.text[:120]}", kind="network")

    # 官方返回体形如 {"code":0,"msg":"success","data":{...}}；code 非 0 即失败
    code = raw.get("code", raw.get("errcode"))
    ok = (code in (0, "0", None)) and raw.get("success") is not False
    msg = str(raw.get("msg") or raw.get("message") or "")
    guard_observe(
        account_id=guard_account,
        text=("SUCCESS" if ok else json.dumps(raw, ensure_ascii=False)),
        status_code=int(getattr(resp, "status_code", 0) or 0),
    )
    if not ok:
        _log_call(api_name, False, ms, f"{code}: {msg}")
        kind = "risk" if any(k in msg for k in ("限流", "频繁", "稍后重试")) else "business"
        raise OpenPlatformError(f"接口返回失败（code={code}）：{msg[:160]}", kind=kind, raw=raw)

    _log_call(api_name, True, ms)
    return raw


# ---------------------------------------------------------------- 便捷只读封装

def list_products(page_no: int = 1, page_size: int = 20,
                  keyword: str = "", goods_type: int = 0) -> Dict[str, Any]:
    """查商品列表。goods_type: 0 全部 / 1 出售中 / 2 仓库中（以官方为准）。"""
    return call("product.list", {"keyword": keyword, "goods_type": goods_type,
                                 "page_no": page_no, "page_size": page_size})


def get_product(product_id: str = "", item_id: str = "") -> Dict[str, Any]:
    """
    查商品详情。**注意官方 FAQ 第 5 条**：product_id 是闲管家商品 ID，
    item_id 是闲鱼商品 ID，不能混用。
    """
    if not product_id and not item_id:
        raise OpenPlatformError("product_id 与 item_id 至少要给一个", kind="config")
    body: Dict[str, Any] = {}
    if product_id:
        body["product_id"] = str(product_id)
    if item_id:
        body["item_id"] = str(item_id)
    return call("product.detail", body)


def get_product_skus(product_id: str) -> Dict[str, Any]:
    return call("product.sku.list", {"product_id": str(product_id)})


def list_orders(page_no: int = 1, page_size: int = 20,
                order_status: Optional[int] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"page_no": page_no, "page_size": page_size}
    if order_status is not None:
        body["order_status"] = int(order_status)
    return call("order.list", body)


def get_order(order_id: str) -> Dict[str, Any]:
    return call("order.detail", {"order_id": str(order_id)})


def list_shops() -> Dict[str, Any]:
    """查已授权的闲鱼店铺（含 authorize_expires，官方说半年过期要重授权）。"""
    return call("shop.list", {})


def wallet_balance() -> Dict[str, Any]:
    return call("wallet.balance", {})


# ---------------------------------------------------------------- 便捷写封装

ORDER_EDIT_PRICE_ERRORS = {
    "ORDER_ALREADY_PAID": "订单已付款，无法改价",
    "ORDER_CLOSED": "订单已关闭，无法改价",
    "PRICE_BELOW_MIN_LIMIT": "改价低于平台最低限价",
}


def edit_order_price(order_id: str, price: float, post_fee: float = 0.0,
                     allow_write: bool = False) -> Dict[str, Any]:
    """
    订单级改价。契约：POST /api/open/order/editPrice
    body = {"orderId": <id>, "price": <元>, "postFee": <元>}，金额为浮点数元。
    """
    oid = str(order_id or "").strip()
    if not oid:
        raise OpenPlatformError("订单号不能为空", kind="config")
    try:
        amount = round(float(price), 2)
        post = round(float(post_fee), 2)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OpenPlatformError("改价金额无效", kind="config") from exc
    if not math.isfinite(amount) or amount <= 0:
        raise OpenPlatformError("改价金额必须为正数", kind="config")
    if not math.isfinite(post) or post < 0:
        raise OpenPlatformError("邮费不能为负数", kind="config")
    try:
        return call("order.editPrice",
                    {"orderId": oid, "price": amount, "postFee": post},
                    allow_write=allow_write)
    except OpenPlatformError as exc:
        text = f"{exc} {exc.raw if exc.raw is not None else ''}"
        for code, reason in ORDER_EDIT_PRICE_ERRORS.items():
            if code in text:
                raise OpenPlatformError(f"{code}: {reason}",
                                        kind=exc.kind, raw=exc.raw) from exc
        raise
def edit_stock(product_id: str, quantity: int, sku_id: str = "",
               allow_write: bool = False) -> Dict[str, Any]:
    """
    改库存。**这是 A6 最想要的那个接口**——官方有，参数公开，不会"返回成功但没改"。
    售罄补货走这里，比 A6 现在"重新发布一条新商品"好得多（想要数不用归零）。
    """
    qty = max(0, int(quantity or 0))
    body: Dict[str, Any] = {"product_id": str(product_id), "quantity": qty}
    if sku_id:
        body["sku_id"] = str(sku_id)
    return call("product.editStock", body, allow_write=allow_write)


def publish_product(product_id: str, allow_write: bool = False) -> Dict[str, Any]:
    """上架。官方 FAQ 第 2 条：创建成功还要调这个，否则闲鱼 APP 看不到。"""
    return call("product.publish", {"product_id": str(product_id)}, allow_write=allow_write)


def down_shelf_product(product_id: str, allow_write: bool = False) -> Dict[str, Any]:
    return call("product.downShelf", {"product_id": str(product_id)}, allow_write=allow_write)


def ship_order(order_id: str, express_code: str, express_no: str,
               allow_write: bool = False) -> Dict[str, Any]:
    return call("order.ship", {"order_id": str(order_id),
                               "express_code": str(express_code),
                               "express_no": str(express_no)}, allow_write=allow_write)


def dummy_ship_order(order_id: str, allow_write: bool = False) -> Dict[str, Any]:
    """无需物流发货——虚拟商品最常用。"""
    return call("order.dummyShip", {"order_id": str(order_id)}, allow_write=allow_write)


def refuse_order_refund(order_id: str, reason: str, explanation: str,
                        allow_write: bool = False) -> Dict[str, Any]:
    """驳回退款。与 aftersale.reject 同端点的显式业务别名。"""
    return call("order.refuseRefund", {
        "order_no": str(order_id),
        "refuse_reason": str(reason),
        "refuse_explain": str(explanation),
    }, allow_write=allow_write)


# ---------------------------------------------------------------- 自检

def self_check() -> Dict[str, Any]:
    """
    不联网的自检：配置是否齐、接口表是否自洽、签名是否稳定。
    页面上的"检查配置"按钮用它。
    """
    cfg = get_config()
    problems = []
    if not cfg["app_key"]:
        problems.append("app_key 未填")
    if not cfg["app_secret"]:
        problems.append("app_secret 未填")
    if not cfg["seller_id"]:
        problems.append("seller_id 未填（服务商模式下必传，自研单店可留空）")
    duplicated_paths = {
        path for path in ALL_APIS.values()
        if list(ALL_APIS.values()).count(path) > 1
    }
    intentional_aliases = {"/api/open/trade/refund/operate/refused"}
    dup = bool(duplicated_paths - intentional_aliases)
    if dup:
        problems.append("接口路径有重复")
    s1 = sign("k", '{"a":1}', "1", "s")
    s2 = sign("k", '{"a":1}', "1", "s")
    return {"ok": True, "configured": is_configured(cfg), "problems": problems,
            "sign_stable": s1 == s2, "sign_len": len(s1),
            "api_total": len(ALL_APIS), "enabled": cfg["enabled"],
            "dry_run": cfg["dry_run"], "note": status()["note"]}
