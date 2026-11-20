"""
商品详情补数（A3：想要数来源修正）

背景（2026 年平台行为变化）：
- 搜索列表接口里返回的 `wantNum` 已经恒为 0，继续读列表字段会让
  「想要数」「热度」「价格历史」「热度成长值」全部失真；
- 真实想要数只在详情接口 `mtop.taobao.idle.pc.detail` 里：
  `data.itemDO.wantCnt / browseCnt / collectCnt / soldCnt / itemStatus`。

设计原则：
1. **只做只读补数**，不改商品、不写平台任何东西；
2. **失败不改写可信搜索值**：搜索卡片已有的「N人想要」继续保留；没有可信
   来源的项标成 unknown，绝不用 0 冒充；
3. **风控熔断**：详情一旦返回 RGV587 / FAIL_SYS_USER_VALIDATE，本轮剩余详情
   请求立即停止，不再逐个硬撞同一接口；
4. **限量 + 随机间隔**：默认只补前 10 个，间隔 1.0–2.0s，并走统一限流；
4. Cookie 刷新照 polish_service 的做法回写（服务端可能下发新 token）。
"""

from __future__ import annotations

import random
import re
import time
import unicodedata
from typing import Any, Dict, List, Optional

import requests

from opportunity_publisher import (
    PublishAuthError, PublishError, PublishRiskError,
    TOKEN_COOKIE_NAME, extract_token_from_cookie, _classify_ret, _mtop_request,
)
import polish_service
import rate_limiter

DETAIL_API = "mtop.taobao.idle.pc.detail"
DETAIL_VERSION = "1.0"
DETAIL_SPM = "a21ybx.im.0.0"  # 与底座 get_item_info 用的 spm_cnt 一致

# 补数预算：默认 10 个 × (1.0~2.0s 间隔 + 单次超时 12s)
DEFAULT_LIMIT = 10
MIN_DELAY = 1.0
MAX_DELAY = 2.0
TIMEOUT = 12


def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _find_key(obj: Any, keys: tuple, depth: int = 0) -> Any:
    """在嵌套 JSON 里按 key 找值（详情接口层级随版本变化，不写死路径）。"""
    if depth > 6 or obj is None:
        return None
    if isinstance(obj, dict):
        for key in keys:
            if key in obj and obj[key] not in (None, ""):
                return obj[key]
        for value in obj.values():
            found = _find_key(value, keys, depth + 1)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for value in obj[:20]:
            found = _find_key(value, keys, depth + 1)
            if found not in (None, ""):
                return found
    return None


def _normalized_focus_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"【[^】]*】", "", text)


def _focus_relevance(title: Any, focus_keyword: Any) -> int:
    title_text = _normalized_focus_text(title)
    focus_text = _normalized_focus_text(focus_keyword).strip()
    if not title_text or not focus_text:
        return 0

    compact_title = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", title_text)
    compact_focus = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", focus_text)
    if compact_focus and compact_focus in compact_title:
        return 1000 + len(compact_focus)

    terms = [
        term for term in re.split(r"[\s+＋/／、,，·•|｜]+", focus_text)
        if term
    ]
    if not terms:
        return 0

    matched = [term for term in terms if term in title_text]
    score = len(matched) * 100
    if len(matched) == len(terms):
        score += 500
    score += sum(len(term) for term in matched)
    return score


def select_detail_targets(
    items: List[Dict[str, Any]],
    limit: int,
    focus_keyword: str = "",
) -> List[Dict[str, Any]]:
    """Select detail-enrichment targets by product relevance, then search order."""
    ranked = sorted(
        enumerate(items),
        key=lambda pair: (-_focus_relevance(pair[1].get("title"), focus_keyword), pair[0]),
    )
    return [
        item
        for _, item in ranked
        if item.get("itemId")
    ][:max(0, int(limit))]


def fetch_item_detail(cookie_str: str, item_id: str, app_key: str,
                      cookie_id: str = "", timeout: int = TIMEOUT) -> Dict[str, Any]:
    """读单个商品详情。返回 {ok, want_count, browse_count, ...}；失败带 error。"""
    item_id = str(item_id or "").strip()
    if not item_id:
        return {"ok": False, "error": "商品 ID 为空"}
    token = extract_token_from_cookie(cookie_str)
    if not token:
        return {"ok": False, "error": f"Cookie 中缺少 {TOKEN_COOKIE_NAME}，无法读取详情"}

    try:
        raw, resp = _mtop_request(cookie_str, DETAIL_API, {"itemId": item_id},
                                  DETAIL_VERSION, DETAIL_SPM, app_key,
                                  timeout=timeout)
    except requests.RequestException as exc:
        return {"ok": False, "error": f"详情请求失败：{type(exc).__name__}", "cookie_str": cookie_str}
    except PublishRiskError as exc:
        return {
            "ok": False,
            "error": str(exc)[:160],
            "error_kind": "risk",
            "cookie_str": cookie_str,
        }
    except PublishAuthError as exc:
        return {
            "ok": False,
            "error": str(exc)[:160],
            "error_kind": "auth",
            "cookie_str": cookie_str,
        }
    except PublishError as exc:
        return {
            "ok": False,
            "error": str(exc)[:160],
            "error_kind": "business",
            "cookie_str": cookie_str,
        }
    except Exception as exc:  # 缺 token 等
        return {"ok": False, "error": str(exc)[:160], "cookie_str": cookie_str}

    if resp.headers.get("set-cookie"):
        new_cookie = polish_service._merge_response_cookies(resp, cookie_str)
        if new_cookie != cookie_str:
            cookie_str = new_cookie
            if cookie_id:
                polish_service._persist_cookie(cookie_id, new_cookie)

    ret = raw.get("ret") or []
    ret_str = " | ".join(str(r) for r in ret) if isinstance(ret, list) else str(ret)
    try:
        _classify_ret(DETAIL_API, ret_str)
    except PublishRiskError as exc:
        return {
            "ok": False,
            "error": str(exc)[:200],
            "error_kind": "risk",
            "cookie_str": cookie_str,
        }
    except PublishAuthError as exc:
        return {
            "ok": False,
            "error": str(exc)[:200],
            "error_kind": "auth",
            "cookie_str": cookie_str,
        }
    except PublishError as exc:
        return {
            "ok": False,
            "error": str(exc)[:200],
            "error_kind": "business",
            "cookie_str": cookie_str,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "cookie_str": cookie_str}

    data = raw.get("data") or {}
    want = _to_int(_find_key(data, ("wantCnt", "wantCount", "wantNum")))
    if want is None:
        return {"ok": False, "error": "详情返回里没有想要数字段", "cookie_str": cookie_str}

    skus = _find_key(data, ("skuList",))
    stock: Optional[int] = None
    if isinstance(skus, list) and skus:
        qty = [_to_int(s.get("quantity")) for s in skus if isinstance(s, dict)]
        qty = [q for q in qty if q is not None]
        if qty:
            stock = sum(qty)

    return {
        "ok": True,
        "item_id": item_id,
        "want_count": want,
        "browse_count": _to_int(_find_key(data, ("browseCnt", "browseCount", "viewCnt", "viewCount"))),
        "collect_count": _to_int(_find_key(data, ("collectCnt", "collectCount"))),
        "sold_count": _to_int(_find_key(data, ("soldCnt", "soldCount"))),
        "item_status": _find_key(data, ("itemStatus", "status")),
        "stock": stock,
        "sold_out": (stock == 0) if stock is not None else None,
        "cookie_str": cookie_str,
    }


def enrich_items(items: List[Dict[str, Any]], cookie_str: str, app_key: str,
                 cookie_id: str = "", limit: int = DEFAULT_LIMIT,
                 focus_keyword: str = "") -> Dict[str, Any]:
    """
    给榜单前 N 个补真实想要数（原地修改 items）。

    写回字段：
      want_count / browse_count / collect_count / sold_count / detail_stock
      want_source = "search" | "detail" | "unknown"
      want_stale  = True 表示这个 want_count 来自过期或不可验证快照，
                    不可参与排名、漏斗率和成长值计算
    """
    stats = {
        "requested": 0,
        "success": 0,
        "failed": 0,
        "error": "",
        "search_fallback": 0,
        "unknown": 0,
        "stopped_by_risk": False,
        "stop_reason": "",
    }
    # 搜索卡片自带的 "N人想要" 标签是平台直接展示的真实值。详情补数失败时
    # 保留它；没有任何已确认来源的数字则标为 unknown，不能借列表的 0 冒充。
    for it in items:
        if (
            it.get("want_source") in ("search", "detail")
            and it.get("want_stale") is not True
            and it.get("want_count") is not None
        ):
            if it.get("want_source") == "search":
                stats["search_fallback"] += 1
            continue
        it["want_source"] = "unknown"
        it["want_stale"] = True
        it["want_count"] = None
        stats["unknown"] += 1

    if not cookie_str or not extract_token_from_cookie(cookie_str):
        stats["error"] = "没有可用账号 Cookie；已保留搜索卡片值，其余想要数保持未知"
        return stats

    try:
        import opportunity_guard
        allowed, guard_reason = opportunity_guard.check_allowed()
        if not allowed:
            stats["error"] = guard_reason
            return stats
    except Exception:
        pass

    targets = select_detail_targets(items, limit, focus_keyword)
    for idx, it in enumerate(targets):
        if idx:
            rate_limiter.jitter(MIN_DELAY, MAX_DELAY)
        # A2：详情补数也走统一限流（默认 12 次/分钟），排不上就等一轮
        ok, retry = rate_limiter.acquire("detail", "global")
        if not ok:
            time.sleep(min(int(retry), 5))
            ok, retry = rate_limiter.acquire("detail", "global")
        if not ok:
            stats["failed"] += 1
            stats["error"] = f"详情补数触发限流（{retry}s 后可继续）"
            continue
        previous_source = str(it.get("want_source") or "unknown")
        stats["requested"] += 1
        try:
            d = fetch_item_detail(cookie_str, str(it.get("itemId")), app_key,
                                  cookie_id=cookie_id)
        except Exception as exc:  # 兜底：详情失败绝不影响主流程
            stats["failed"] += 1
            stats["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            continue
        cookie_str = d.get("cookie_str") or cookie_str
        if not d.get("ok"):
            stats["failed"] += 1
            stats["error"] = str(d.get("error") or "")[:160]
            if d.get("error_kind") in ("risk", "auth"):
                stats["stopped_by_risk"] = d.get("error_kind") == "risk"
                stats["stop_reason"] = (
                    "详情接口触发平台风控，本轮停止继续补数"
                    if d.get("error_kind") == "risk"
                    else "详情接口登录态失效，本轮停止继续补数"
                )
                try:
                    import opportunity_guard
                    opportunity_guard.record_failure(stats["error"])
                except Exception:
                    pass
                break
            continue
        if previous_source == "search":
            stats["search_fallback"] = max(0, stats["search_fallback"] - 1)
        elif previous_source == "unknown":
            stats["unknown"] = max(0, stats["unknown"] - 1)
        stats["success"] += 1
        it["want_count"] = d.get("want_count")
        it["want_source"] = "detail"
        it["want_stale"] = False
        if d.get("sold_count") is not None:
            it["detail_sold_count"] = d["sold_count"]
        for src, dst in (("browse_count", "browse_count"),
                         ("collect_count", "collect_count"),
                         ("sold_count", "sold_count"),
                         ("stock", "detail_stock"),
                         ("item_status", "detail_status"),
                         ("sold_out", "sold_out")):
            if d.get(src) is not None:
                it[dst] = d[src]
    return stats
    return stats


def resolve_cookie(cookie_id: str = "", fallback_first: bool = True) -> tuple:
    """取一个可用 Cookie：(cookie_id, cookie_str)。没有就返回 ("", "")。"""
    try:
        import cookie_manager as cm
        cookies = getattr(getattr(cm, "manager", None), "cookies", None) or {}
    except Exception:
        cookies = {}
    if not cookies:
        try:
            from db_manager import db_manager
            cookies = db_manager.get_all_cookies() or {}
        except Exception:
            cookies = {}
    if cookie_id and cookies.get(cookie_id):
        return cookie_id, cookies[cookie_id]
    if fallback_first:
        for cid, value in cookies.items():
            if value:
                return str(cid), value
    return "", ""


__all__ = ["fetch_item_detail", "enrich_items", "resolve_cookie",
           "select_detail_targets",
           "DETAIL_API", "DEFAULT_LIMIT"]
