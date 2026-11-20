"""
MTOP 一键发布引擎 —— 双版本融合（补齐②）

血统：
- 初版移植自 dameng2026/xianyu-pilot-pro（双层 inputJson / 旧发布 API）；
- 2026-11-09 按 fancyboi999/goofish-cli（Apache-2.0，真实账号端到端验证）重写核心链路：
  旧版把外部图片 URL 直接填进 imageInfoDOList 且 widthSize/heightSize=0 —— 平台会拒绝。
  goofish-cli 验证过的路线是：

  1. 图片先上传闲鱼 CDN（stream-upload.goofish.com/api/upload.api）
     拿回执 {url, width(pix), height, size}；
  2. 类目推荐 mtop.taobao.idle.kgraph.property.recommend v2.0
     返回 catId/catName/channelCatId/tbCatId + uniqueCode；
  3. 默认发布地址 mtop.taobao.idle.local.poi.get v1.0
     返回 prov/city/area/poi/divisionId 等；
  4. 提交 mtop.idle.pc.idleitem.publish v1.0（单层紧凑 JSON，
     imageInfoDOList 用 CDN 回执图 + 真实尺寸）。

安全与幂等（对齐 goofish-cli 护栏设计）：
- 任何确定性输入错误在上传/提交前拒绝；
- 提交阶段网络异常 → submission_status="submission_unknown"，
  绝不自动重发，提示先核对商品列表（requires_readback）；
- 风控关键字（RGV587 / FAIL_SYS_USER_VALIDATE / 哎哟喂 / punish）
  抛 PublishRiskError，登录失效抛 PublishAuthError，由路由层分流处理；
- APP_KEY 读 global_config.yml（签名固定值，禁改）。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import tempfile
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import requests

from platform_guard import (
    PlatformGuardBlocked,
    admit as guard_admit,
    observe as guard_observe,
)

TOKEN_COOKIE_NAME = "_m_h5_tk"
MTOP_HOST = "https://h5api.m.goofish.com"
UPLOAD_URL = "https://stream-upload.goofish.com/api/upload.api"

PUBLISH_API = "mtop.idle.pc.idleitem.publish"
PUBLISH_VERSION = "1.0"
CATEGORY_API = "mtop.taobao.idle.kgraph.property.recommend"
CATEGORY_VERSION = "2.0"
LOCATION_API = "mtop.taobao.idle.local.poi.get"
LOCATION_VERSION = "1.0"
SEARCH_API = "mtop.taobao.idlemtopsearch.pc.search"
SEARCH_VERSION = "1.0"
PUBLISH_SPM = "a21ybx.publish.0.0"
WEB_APP_KEY = "34839810"
WEB_ITEM_APIS = frozenset({
    PUBLISH_API, CATEGORY_API, LOCATION_API, "mtop.idle.web.xyh.item.list",
    SEARCH_API,
})

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

RISK_KEYWORDS = ("RGV587_ERROR", "RGV587", "FAIL_SYS_USER_VALIDATE", "哎哟喂", "/punish")
AUTH_KEYWORDS = ("FAIL_SYS_SESSION_EXPIRED", "FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EXPIRED",
                 "FAIL_SYS_TOKEN_EMPTY", "令牌过期", "FAIL_SYS_ILLEGAL_ACCESS")

DEFAULT_CATEGORY = {
    "catId": "50025461",
    "catName": "软件安装包/序列号/激活码",
    "channelCatId": "201449620",
    "tbCatId": "50003316",
}


class PublishError(Exception):
    """发布链路一般错误（输入/结构/普通接口失败）。"""


class PublishRiskError(PublishError):
    """命中风控信号。"""


class PublishPausedError(PublishError):
    """账号已暂停，本次没有提交。"""

    submission_status = "not_submitted"


class PublishAuthError(PublishError):
    """登录态 / token 失效。"""


class PublishTransportError(PublishError):
    """网络/网关层失败（5xx、429、非 JSON 错误页）。

    单独一类的原因：这类失败**不代表接口不兼容或参数错**，应当退避重试；
    和它混在一起的后果是运维去查接口版本，而实际只是 502 抖了一下。
    """


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def extract_token_from_cookie(cookie_str: str) -> Optional[str]:
    """从 Cookie 字符串解析 MTOP token（_m_h5_tk，兼容 'xxx_yyy' 取首段）。"""
    if not cookie_str:
        return None
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        if name.strip() == TOKEN_COOKIE_NAME:
            token = value.strip()
            if token and "_" in token:
                return token.split("_")[0]
            return token or None
    return None


def build_sign(app_key: str, token: str, t_ms: str, data_str: str) -> str:
    """MD5(token & t & APP_KEY & data)"""
    raw = f"{token}&{t_ms}&{app_key}&{data_str}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _money_to_cent(value: Any) -> str:
    """元 → 分（字符串）。校验非负且最多两位小数。"""
    from decimal import Decimal, InvalidOperation
    try:
        amount = Decimal(str(value if value not in (None, "") else 0))
        if not amount.is_finite() or amount < 0:
            raise PublishError("金额必须为非负数")
        if amount != amount.quantize(Decimal("0.01")):
            raise PublishError("金额最多两位小数")
    except InvalidOperation:
        raise PublishError("金额必须是有效数字") from None
    return str(int(amount * 100))


def _mtop_headers(cookie_str: str) -> Dict[str, str]:
    return {
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "cache-control": "no-cache",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.goofish.com",
        "pragma": "no-cache",
        "referer": "https://www.goofish.com/",
        "sec-ch-ua": '"Chromium";v="120", "Not-A.Brand";v="24", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": UA,
        "cookie": cookie_str.strip(),
    }


def _classify_ret(api: str, ret_str: str) -> None:
    """ret 不含 SUCCESS 时按关键字分类抛异常。"""
    if not ret_str or "SUCCESS" in ret_str:
        return
    for kw in RISK_KEYWORDS:
        if kw in ret_str:
            raise PublishRiskError(f"[{api}] 触发平台风控：{ret_str}")
    for kw in AUTH_KEYWORDS:
        if kw in ret_str:
            raise PublishAuthError(f"[{api}] 登录态失效：{ret_str}")
    raise PublishError(f"[{api}] 调用失败：{ret_str}")


def _mtop_request(cookie_str: str, api: str, data: Any, version: str,
                  spm_cnt: str, app_key: str, timeout: int = 30):
    """底层 MTOP POST：签名 + 发请求，返回 (raw_json, response)。

    不做 ret 分类 —— 调用方按自己的语义处理（发布要区分风控/登录失效，
    保活只关心"能不能通"）。response 留给调用方合并 Set-Cookie。
    """
    token = extract_token_from_cookie(cookie_str)
    if not token:
        raise PublishAuthError(f"Cookie 中缺少 MTOP token（{TOKEN_COOKIE_NAME}），请重新登录闲鱼")

    # Web 商品接口使用独立的应用键，签名和请求参数必须一致。
    if api in WEB_ITEM_APIS:
        app_key = WEB_APP_KEY

    data_val = data if isinstance(data, str) else json.dumps(
        data, ensure_ascii=False, separators=(",", ":"))
    t_ms = str(int(time.time() * 1000))
    sign = build_sign(app_key, token, t_ms, data_val)

    params = {
        "jsv": "2.7.2",
        "appKey": app_key,
        "t": t_ms,
        "sign": sign,
        "v": version,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": api,
        "sessionOption": "AutoLoginOnly",
        "spm_cnt": spm_cnt,
    }
    url = f"{MTOP_HOST}/h5/{api}/{version}/"
    try:
        guard_admit(cookie_str, scope="opportunity_publish", write=True, sleep=True)
    except PlatformGuardBlocked as exc:
        raise PublishRiskError(str(exc))
    resp = requests.post(url, params=params, headers=_mtop_headers(cookie_str),
                         data={"data": data_val}, timeout=timeout)
    # 传输层失败要单独报：502 返 HTML 页不是"接口不兼容"，是网络抖动。
    try:
        from mtop_response import parse_mtop_response
        kind, raw, text = parse_mtop_response(resp)
    except Exception:
        kind, raw = "", {}
    guard_observe(
        cookie_str,
        text=text if kind else str(getattr(resp, "text", "") or ""),
        status_code=int(getattr(resp, "status_code", 0) or 0),
    )
    if kind == "transport":
        raise PublishTransportError(f"[{api}] {text[:160]}")
    if kind == "risk":
        raise PublishRiskError(f"[{api}] 触发平台风控：{text[:160]}")
    if kind == "auth":
        raise PublishAuthError(f"[{api}] 登录态失效：{text[:160]}")
    if kind == "ok":
        return raw, resp
    resp.raise_for_status()
    return resp.json(), resp


def _mtop_call(cookie_str: str, api: str, data: Any, version: str,
               spm_cnt: str, app_key: str, timeout: int = 30) -> Dict[str, Any]:
    """统一 MTOP 调用（goofish-cli core/mtop.py 对齐版）。"""
    raw, _resp = _mtop_request(cookie_str, api, data, version, spm_cnt,
                              app_key, timeout=timeout)
    ret = raw.get("ret") or []
    ret_str = " | ".join(str(r) for r in ret) if isinstance(ret, list) else str(ret)
    _classify_ret(api, ret_str)
    return raw


# ---------------------------------------------------------------------------
# 图片：本地文件 / 远程 URL → 闲鱼 CDN 回执
# ---------------------------------------------------------------------------

def _validate_receipt(value: Any) -> Dict[str, Any]:
    """校验图片回执：必须是闲鱼/alicdn HTTPS CDN 地址 + 正整数宽高。"""
    if not isinstance(value, dict):
        raise PublishError("图片回执必须包含 url、width、height")
    url = value.get("url")
    parsed = urlsplit(url) if isinstance(url, str) else None
    host = (parsed.hostname or "").lower() if parsed else ""
    if (not parsed or parsed.scheme != "https" or parsed.username
            or not host or not (host == "alicdn.com" or host.endswith(".alicdn.com")
                                or host == "goofish.com" or host.endswith(".goofish.com"))):
        raise PublishError(f"图片 URL 必须是闲鱼上传返回的 HTTPS CDN 地址：{str(url)[:80]}")
    dims = []
    for key in ("width", "height"):
        v = value.get(key)
        if isinstance(v, bool) or not str(v).isdigit() or int(v) <= 0:
            raise PublishError("图片宽高必须是正整数")
        dims.append(int(v))
    return {"url": url, "width": dims[0], "height": dims[1]}


def upload_image(cookie_str: str, file_path: str) -> Dict[str, Any]:
    """上传一张本地图片到闲鱼 CDN，返回回执 {url,width,height,size}。"""
    from pathlib import Path

    source = Path(file_path).expanduser()
    if not source.is_file() or source.stat().st_size == 0:
        raise PublishError(f"图片文件不存在或为空：{file_path}")
    mime = mimetypes.guess_type(str(source))[0]
    if not mime or not mime.startswith("image/"):
        raise PublishError(f"无法识别图片格式：{source.name}")

    headers = {
        "accept": "*/*",
        "origin": "https://www.goofish.com",
        "referer": "https://www.goofish.com/",
        "user-agent": UA,
        "cookie": cookie_str.strip(),
    }
    params = {"floderId": "0", "appkey": "xy_chat", "_input_charset": "utf-8"}
    try:
        with source.open("rb") as stream:
            resp = requests.post(UPLOAD_URL, headers=headers, params=params,
                                 files={"file": (source.name, stream, mime)}, timeout=60)
        resp.raise_for_status()
        raw = resp.json()
    except (OSError, ValueError) as e:
        raise PublishError(f"图片上传连接或响应失败（未确认成功，不自动重传）：{e}") from None

    if not isinstance(raw, dict):
        raise PublishError("图片上传响应结构无效；未确认成功")
    if raw.get("success") is not True:
        marker = str(raw.get("status", ""))
        if any(k in marker for k in ("RGV587", "USER_VALIDATE", "ILLEGAL_ACCESS")):
            raise PublishRiskError("图片上传被平台风控拒绝")
        raise PublishError(f"图片上传未被服务端确认成功：{marker[:120]}")

    obj = raw.get("object") or {}
    try:
        width, height = map(int, str(obj.get("pix", "")).split("x"))
    except (ValueError, TypeError):
        raise PublishError("图片上传响应缺少有效尺寸（pix）") from None
    receipt = _validate_receipt({"url": obj.get("url"), "width": width, "height": height})
    receipt["size"] = obj.get("size")
    return receipt


def _download_to_temp(url: str) -> str:
    """下载远程图片到临时文件（仅 image/*，上限 20MB）。返回路径，调用方负责删除。"""
    resp = requests.get(url, timeout=30, stream=True,
                        headers={"user-agent": UA, "accept": "image/*,*/*;q=0.8"})
    resp.raise_for_status()
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not ctype.startswith("image/"):
        raise PublishError(f"远程地址不是图片（content-type={ctype or '未知'}）：{url[:100]}")
    suffix = mimetypes.guess_extension(ctype) or ".jpg"
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="xyimg_")
    os.close(fd)
    total = 0
    try:
        with open(path, "wb") as f:
            for chunk in resp.iter_content(65536):
                total += len(chunk)
                if total > 20 * 1024 * 1024:
                    raise PublishError("远程图片超过 20MB 上限")
                f.write(chunk)
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path


def resolve_images(cookie_str: str, images: List[Any]) -> List[Dict[str, Any]]:
    """
    任意图片输入 → 闲鱼 CDN 回执列表（1–9 张）。
    支持三种输入：
    - dict 回执（已上传，含 url/width/height）→ 直接校验；
    - http(s) URL → 下载临时文件后上传 CDN（外部图直填必被平台拒绝）；
    - 本地文件路径 → 直接上传 CDN。
    """
    receipts: List[Dict[str, Any]] = []
    temps: List[str] = []
    try:
        for item in images:
            if isinstance(item, dict) and item.get("url"):
                receipts.append(_validate_receipt(item))
            elif isinstance(item, str) and item.strip():
                s = item.strip()
                if s.lower().startswith(("http://", "https://")):
                    tmp = _download_to_temp(s)
                    temps.append(tmp)
                    receipts.append(upload_image(cookie_str, tmp))
                else:
                    receipts.append(upload_image(cookie_str, s))
    finally:
        for tmp in temps:
            try:
                os.remove(tmp)
            except OSError:
                pass
    if not 1 <= len(receipts) <= 9:
        raise PublishError(f"发布需要 1–9 张图片，当前有效图片 {len(receipts)} 张")
    return receipts


def _image_do_list(receipts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{
        "extraInfo": {"isH": "false", "isT": "false", "raw": "false"},
        "isQrCode": False,
        "url": img["url"],
        "heightSize": img["height"],
        "widthSize": img["width"],
        "major": idx == 0,
        "type": 0,
        "status": "done",
    } for idx, img in enumerate(receipts)]


# ---------------------------------------------------------------------------
# 类目推荐 / 默认地址
# ---------------------------------------------------------------------------

def recommend_category(cookie_str: str, title: str, desc: str,
                       receipts: List[Dict[str, Any]], app_key: str) -> Dict[str, Any]:
    """AI 类目推荐。返回 {cat_id,cat_name,channel_cat_id,tb_cat_id,unique_code}。"""
    unique_code = str(time.time_ns() // 1000)
    raw = _mtop_call(cookie_str, CATEGORY_API, {
        "title": title,
        "lockCpv": False,
        "multiSKU": False,
        "publishScene": "mainPublish",
        "scene": "newPublishChoice",
        "description": desc or title,
        "imageInfos": _image_do_list(receipts),
        "uniqueCode": unique_code,
    }, CATEGORY_VERSION, PUBLISH_SPM, app_key)

    data = raw.get("data") or {}
    candidates: List[Dict[str, Any]] = []
    for card in data.get("cardList") or []:
        card_data = card.get("cardData") or {}
        if str(card_data.get("propertyId")) != "-10000":
            continue
        for value in card_data.get("valuesList") or []:
            transport = value.get("transportData") or {}
            cat_id = str(value.get("catId") or "").strip()
            channel_cat_id = str(value.get("channelCatId") or "").strip()
            tb_cat_id = str(transport.get("tbCatId") or "").strip()
            if cat_id and channel_cat_id and tb_cat_id:
                candidates.append({
                    "cat_id": cat_id,
                    "cat_name": str(value.get("catName") or ""),
                    "channel_cat_id": channel_cat_id,
                    "tb_cat_id": tb_cat_id,
                    "score": float(value.get("score") or 0),
                })
    if not candidates:
        raise PublishError(
            "类目推荐候选中没有同时具备 catId/channelCatId/tbCatId 的选项，"
            "为避免发错类目，已停止自动发布；可在发布参数里手动指定 category")
    best = max(candidates, key=lambda c: c["score"])
    return {
        "cat_id": best["cat_id"],
        "cat_name": best["cat_name"],
        "channel_cat_id": best["channel_cat_id"],
        "tb_cat_id": best["tb_cat_id"],
        "unique_code": unique_code,
    }


def get_default_location(cookie_str: str, app_key: str,
                         longitude: float = 121.4737, latitude: float = 31.2304) -> Dict[str, Any]:
    """账号默认发布地址。返回选址 DTO（含 divisionId/gps/poiId 等）。"""
    raw = _mtop_call(cookie_str, LOCATION_API,
                     {"longitude": longitude, "latitude": latitude},
                     LOCATION_VERSION, PUBLISH_SPM, app_key)
    data = raw.get("data") or {}
    addrs = data.get("commonAddresses") or []
    selected = data.get("selectedPoi") or (addrs[0] if addrs else None)
    if not isinstance(selected, dict) or not selected.get("divisionId"):
        raise PublishError(
            "账号未返回可用发布地址（divisionId 为空）。请先在闲鱼 App/网页端设置默认发布地址后重试")
    return {
        "prov": selected.get("prov", ""),
        "city": selected.get("city", ""),
        "area": selected.get("area", ""),
        "poi": selected.get("poi", ""),
        "division_id": str(selected.get("divisionId", "")),
        "selected": selected,
        "all": addrs or [selected],
    }


# ---------------------------------------------------------------------------
# 发布数据组装与提交
# ---------------------------------------------------------------------------

def _build_publish_data(*, title: str, desc: str, receipts: List[Dict[str, Any]],
                        price: Any, quantity: int, delivery: str, post_price: Any,
                        cat: Dict[str, Any], location: Dict[str, Any]) -> Dict[str, Any]:
    """按 goofish-cli _build_publish_data 对齐的发布 payload。"""
    post_fee: Dict[str, Any] = {
        "canFreeShipping": False,
        "supportFreight": False,
        "onlyTakeSelf": False,
    }
    if delivery == "包邮":
        post_fee["canFreeShipping"] = True
        post_fee["supportFreight"] = True
        post_fee["templateId"] = "0"
    elif delivery == "按距离计费":
        post_fee["supportFreight"] = True
        post_fee["templateId"] = "-100"
    elif delivery == "一口价":
        post_fee["supportFreight"] = True
        post_fee["postPriceInCent"] = _money_to_cent(post_price)
        post_fee["templateId"] = "0"
    else:  # 无需邮寄（虚拟商品默认）
        post_fee["templateId"] = "0"

    price_dto: Dict[str, str] = {}
    default_price = float(price or 0) <= 0
    if not default_price:
        price_dto["priceInCent"] = _money_to_cent(price)

    sel = location.get("selected") or {}
    item_addr = {
        "area": sel.get("area", ""),
        "city": sel.get("city", ""),
        "divisionId": str(sel.get("divisionId", "")),
        "gps": sel.get("gps") or f"{sel.get('longitude', '')},{sel.get('latitude', '')}",
        "poiId": sel.get("poiId", ""),
        "poiName": sel.get("poi", ""),
        "prov": sel.get("prov", ""),
    }

    return {
        "freebies": False,
        "itemTypeStr": "b",
        "quantity": str(int(quantity or 1)),
        "simpleItem": "true",
        "imageInfoDOList": _image_do_list(receipts),
        "itemTextDTO": {"desc": desc, "title": title, "titleDescSeparate": True},
        "itemLabelExtList": [],
        "itemPriceDTO": price_dto,
        # 参考实现（统一项目工作副本 listing_publisher）必须有 itemSkuList
        "itemSkuList": [{"priceInCent": _money_to_cent(price),
                         "quantity": str(int(quantity or 1)), "propertyList": []}],
        "userRightsProtocols": [{"enable": False, "serviceCode": code} for code in (
            "FAST_DELIVERY_48_HOUR", "FAST_DELIVERY_24_HOUR",
            "VIRTUAL_NONCONFORMITY_FREE_REFUND_SERVICE", "SKILL_PLAY_NO_MIND")],
        "itemPostFeeDTO": post_fee,
        "itemAddrDTO": item_addr,
        "defaultPrice": default_price,
        "itemCatDTO": {
            "catId": cat["cat_id"],
            "catName": cat["cat_name"],
            "channelCatId": cat["channel_cat_id"],
            "tbCatId": cat["tb_cat_id"],
        },
        "onlyTakeSelf": True,
        "uniqueCode": cat.get("unique_code") or str(time.time_ns() // 1000),
        "sourceId": "pcMainPublish",
        "bizcode": "pcMainPublish",
        "publishScene": "pcMainPublish",
    }


def _category_from_item(item_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """请求里显式指定类目时（{"catId","catName","channelCatId","tbCatId"}）转内部结构。"""
    cat = item_data.get("category")
    if isinstance(cat, dict) and all(cat.get(k) for k in
                                     ("catId", "catName", "channelCatId", "tbCatId")):
        return {
            "cat_id": str(cat["catId"]),
            "cat_name": str(cat["catName"]),
            "channel_cat_id": str(cat["channelCatId"]),
            "tb_cat_id": str(cat["tbCatId"]),
            "unique_code": str(cat.get("uniqueCode") or ""),
        }
    return None


def publish_item(cookie_str: str, item_data: Dict[str, Any],
                 app_key: str, *, cookie_id: str = "") -> Dict[str, Any]:
    """
    一键发布入口。返回:
    {
      success, itemId, error, status, stage,
      requires_readback, images: [回执], cat: {..}
    }
    status: submitted / submission_unknown / submission_rejected / not_submitted
    """
    import rate_limiter

    if cookie_id:
        blocked, remaining = rate_limiter.circuit_blocked(cookie_id)
        if blocked:
            raise PublishPausedError(f"账号发布已暂停，请 {remaining} 秒后再检查")

    title = str(item_data.get("title") or "").strip()
    desc = str(item_data.get("desc") or item_data.get("description") or "").strip()
    images = item_data.get("imageUrls") or item_data.get("images") or []
    if not title:
        raise PublishError("发布标题不能为空")
    if not desc:
        raise PublishError("发布描述不能为空（闲鱼要求标题与描述都填写）")
    price = item_data.get("price", 0)
    _money_to_cent(price)  # 提前校验
    delivery = str(item_data.get("delivery") or "无需邮寄")
    if delivery not in ("包邮", "按距离计费", "一口价", "无需邮寄"):
        raise PublishError(f"不支持的邮寄方式：{delivery}")

    progress: Dict[str, Any] = {"images": [], "category": None,
                                "location": None, "status": "not_submitted",
                                "requires_readback": False}
    stage = "upload"
    attempted = False
    try:
        receipts = resolve_images(cookie_str, images)
        progress["images"] = receipts

        stage = "category"
        cat = _category_from_item(item_data)
        if cat is None:
            cat = recommend_category(cookie_str, title, desc, receipts, app_key)
        progress["category"] = {k: v for k, v in cat.items() if k != "unique_code"}

        stage = "location"
        loc = get_default_location(cookie_str, app_key)
        progress["location"] = {k: v for k, v in loc.items() if k != "all"}

        stage = "submit"
        data = _build_publish_data(
            title=title, desc=desc, receipts=receipts,
            price=price, quantity=int(item_data.get("quantity") or 1),
            delivery=delivery, post_price=item_data.get("postPrice") or 0,
            cat=cat, location=loc)
        attempted = True
        raw = _mtop_call(cookie_str, PUBLISH_API, data, PUBLISH_VERSION,
                         PUBLISH_SPM, app_key)
        body = raw.get("data") if isinstance(raw, dict) else None
        item_id = None
        if isinstance(body, dict):
            item_id = body.get("itemId")
            if not item_id and isinstance(body.get("data"), dict):
                item_id = body["data"].get("itemId")
        if item_id is None or not str(item_id).strip():
            raise _UnknownSubmission("发布结果未知：响应缺少 itemId，请先核对商品列表")
    except _UnknownSubmission:
        raise
    except PublishTransportError as e:
        if attempted:
            raise _UnknownSubmission("发布结果未知：提交时连接或网关异常，请先核对商品列表") from e
        raise _with_progress(stage, attempted)
    except PublishRiskError as e:
        secs = 0
        if cookie_id:
            secs = rate_limiter.trip_circuit(cookie_id, reason="发布流程收到平台风控反馈")
            try:
                from notify_service import notify_risk_trip
                notify_risk_trip(cookie_id, "发布商品", str(e)[:120], secs)
            except Exception:
                pass
        raise _with_progress(stage, attempted)
    except PublishError:
        progress["status"] = "submission_rejected" if attempted else "not_submitted"
        progress["stage"] = stage
        raise _with_progress(stage, attempted)
    except Exception as e:
        # 提交阶段网络/解析异常 → 结果未知，绝不自动重发
        unknown = attempted
        raise _UnknownSubmission(f"发布结果未知（{stage} 阶段异常：{str(e)[:160]}）。"
                                 f"请先到闲鱼「我发布的」核对该商品，确认未上架再重试；请勿自动重复发布") \
            if unknown else PublishError(f"{stage} 阶段失败：{str(e)[:160]}") from e

    return {
        "success": True,
        "itemId": str(item_id),
        "error": "",
        "status": "submitted",
        "stage": "done",
        "requires_readback": True,
        "images": receipts,
        "cat": {"catId": cat["cat_id"], "catName": cat["cat_name"]},
    }


def _current_exc() -> Exception:
    import sys
    return sys.exc_info()[1]  # type: ignore[return-value]


def _with_progress(stage: str, attempted: bool) -> Exception:
    """保留原异常类型与信息，仅附加进度信息（由上层 API 捕获）。"""
    import sys
    exc = sys.exc_info()[1]
    try:
        setattr(exc, "stage", stage)
        setattr(exc, "submission_status",
                "submission_rejected" if attempted else "not_submitted")
    except Exception:
        pass
    return exc


class _UnknownSubmission(PublishError):
    """提交已发出但结果未知（网络异常/响应解析失败）。必须人工回读，禁止自动重发。"""
    stage = "submit"
    submission_status = "submission_unknown"


__all__ = [
    "TOKEN_COOKIE_NAME", "PUBLISH_API", "PUBLISH_VERSION",
    "PublishError", "PublishRiskError", "PublishAuthError",
    "extract_token_from_cookie", "build_sign",
    "upload_image", "resolve_images", "recommend_category",
    "get_default_location", "publish_item",
]
