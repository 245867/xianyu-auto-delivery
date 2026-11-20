"""Read-only Goofish search results and a local comparison snapshot.

The public search endpoint fields used here are the ones verified in the
reference connector: item identity and display values come from
``main.clickParam.args`` with ``main.exContent`` as the display fallback.

This module never changes a listing. It makes one MTOP search request and
stores successful results locally so the comparison page can render the last
known marketplace prices without sending a burst of searches on page load.
"""

from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from opportunity_publisher import (
    SEARCH_API,
    SEARCH_VERSION,
    _classify_ret,
    _mtop_request,
    extract_token_from_cookie,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = Path(
    os.environ.get("MARKET_LISTINGS_CACHE")
    or (PROJECT_ROOT / "data" / "market_listings.json")
)
_lock = threading.Lock()


class MarketSearchError(RuntimeError):
    """The marketplace did not return a trustworthy search result."""


def _keyword_key(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().lower()


def _number(value: Any) -> Optional[float]:
    try:
        parsed = float(str(value).replace(",", "").replace("¥", "").replace("￥", ""))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _card_price(main: Dict[str, Any], args: Dict[str, Any]) -> Optional[float]:
    raw_price = args.get("displayPrice") or args.get("price")
    price = _number(raw_price)
    if price is not None:
        return price
    parts = main.get("exContent", {}).get("price")
    if not isinstance(parts, list):
        return None
    text = "".join(
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict)
    )
    return _number(text)


_WANT_LABEL = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(万)?人想要\s*$")


def _want_count_from_card(args: Dict[str, Any]) -> Optional[int]:
    """Read the marketplace's own "N人想要" label from the search card.

    2026-11 实测：`clickParam.args.wantNum` 恒为字符串 "0"，真实想要数出现在
    `serviceUtParams[].args.content`（例如 "1087人想要"）。只采用平台上直接
    展示的标签值；没有标签就返回 None，绝不用 0 冒充未知。
    """
    raw = args.get("serviceUtParams")
    entries: List[Any] = []
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                entries = decoded
        except (TypeError, ValueError):
            entries = []
    elif isinstance(raw, list):
        entries = raw

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        label_args = entry.get("args")
        content = label_args.get("content") if isinstance(label_args, dict) else None
        match = _WANT_LABEL.match(str(content or ""))
        if match:
            value = float(match.group(1))
            if match.group(2):
                value *= 10000
            return int(value)

    try:
        fallback = int(float(str(args.get("wantNum"))))
    except (TypeError, ValueError):
        fallback = 0
    return fallback if fallback > 0 else None


def parse_search_card(card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert one verified MTOP search card to the local item shape."""
    if not isinstance(card, dict):
        return None
    main = ((card.get("data") or {}).get("item") or {}).get("main") or {}
    if not isinstance(main, dict):
        return None
    ex = main.get("exContent") or {}
    if not isinstance(ex, dict):
        ex = {}
    args = (main.get("clickParam") or {}).get("args") or {}
    if not isinstance(args, dict):
        args = {}

    item_id = str(args.get("item_id") or args.get("id") or ex.get("itemId") or "")
    title = str(ex.get("title") or "").strip()
    price = _card_price(main, args)
    if not item_id.isdigit() or not title or price is None:
        return None

    item_url = f"https://www.goofish.com/item?id={item_id}"
    image = str(ex.get("picUrl") or "")
    want_count = _want_count_from_card(args)
    return {
        "item_id": item_id,
        "itemId": item_id,
        "title": title,
        "price": price,
        "item_url": item_url,
        "itemUrl": item_url,
        "main_image": image,
        "imageUrl": image,
        "seller_name": str(ex.get("userNickName") or ""),
        "seller": str(ex.get("userNickName") or ""),
        "area": str(ex.get("area") or ""),
        "want_count": want_count,
        "want_source": "search" if want_count is not None else None,
        "want_stale": want_count is None,
        "raw_data": card,
    }


def _read_cache_unlocked() -> Dict[str, Any]:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("keywords"), dict):
            return data
    except Exception:
        pass
    return {"keywords": {}}


def _write_cache_unlocked(data: Dict[str, Any]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, CACHE_FILE)


def market_cache_snapshot() -> List[Dict[str, Any]]:
    """Return local snapshots only. This function never contacts Goofish."""
    with _lock:
        data = _read_cache_unlocked()
    records: List[Dict[str, Any]] = []
    for key, record in (data.get("keywords") or {}).items():
        if not isinstance(record, dict):
            continue
        records.append(
            {
                "keyword_key": key,
                "keyword": str(record.get("keyword") or ""),
                "fetched_at": record.get("fetched_at"),
                "items": record.get("items") if isinstance(record.get("items"), list) else [],
                "error": str(record.get("error") or ""),
            }
        )
    records.sort(key=lambda row: str(row.get("fetched_at") or ""), reverse=True)
    return records


def cached_market_items(keyword: str) -> Dict[str, Any]:
    key = _keyword_key(keyword)
    with _lock:
        record = (_read_cache_unlocked().get("keywords") or {}).get(key) or {}
    return {
        "keyword": keyword,
        "fetched_at": record.get("fetched_at"),
        "items": record.get("items") if isinstance(record.get("items"), list) else [],
        "error": str(record.get("error") or ""),
    }


def remember_market_items(
    keyword: str,
    items: List[Dict[str, Any]],
    fetched_at: Optional[str] = None,
) -> None:
    """Replace a keyword snapshot only after a trustworthy search succeeded."""
    key = _keyword_key(keyword)
    if not key:
        return
    stamp = fetched_at or datetime.now(timezone.utc).isoformat()
    with _lock:
        data = _read_cache_unlocked()
        keywords = data.setdefault("keywords", {})
        keywords[key] = {
            "keyword": str(keyword or "").strip(),
            "fetched_at": stamp,
            "items": items,
            "error": "",
        }
        _write_cache_unlocked(data)


def remember_market_error(keyword: str, error: str) -> None:
    """Keep the last successful items while exposing the latest failure."""
    key = _keyword_key(keyword)
    if not key:
        return
    with _lock:
        data = _read_cache_unlocked()
        keywords = data.setdefault("keywords", {})
        existing = keywords.get(key) if isinstance(keywords.get(key), dict) else {}
        keywords[key] = {
            "keyword": str(keyword or "").strip(),
            "fetched_at": existing.get("fetched_at"),
            "items": existing.get("items") if isinstance(existing.get("items"), list) else [],
            "error": str(error or "")[:240],
        }
        _write_cache_unlocked(data)


def search_market_listings(
    cookie_str: str,
    keyword: str,
    *,
    app_key: str,
    page: int = 1,
    rows: int = 30,
    newest_first: bool = False,
) -> Dict[str, Any]:
    """Make exactly one read-only marketplace search request."""
    keyword = str(keyword or "").strip()
    if not keyword:
        return {"ok": False, "error": "搜索关键词不能为空", "items": []}
    if not extract_token_from_cookie(cookie_str):
        return {
            "ok": False,
            "error": "Cookie 中缺少 MTOP token，请先更新闲鱼登录会话",
            "items": [],
        }

    payload = {
        "pageNumber": max(1, int(page)),
        "keyword": keyword,
        "fromFilter": False,
        "rowsPerPage": max(1, min(int(rows), 100)),
        "sortValue": "DESC" if newest_first else "",
        "sortField": "pub_time" if newest_first else "",
        "customDistance": "",
        "gps": "",
        "propValueStr": {},
        "customGps": "",
        "searchReqFromPage": "pcSearch",
        "extraFilterValue": "{}",
        "userPositionJson": "{}",
    }
    fetched_at = datetime.now(timezone.utc).isoformat()
    try:
        raw, _response = _mtop_request(
            cookie_str,
            SEARCH_API,
            payload,
            SEARCH_VERSION,
            "",
            app_key,
            timeout=20,
        )
        ret = raw.get("ret") or []
        ret_text = " | ".join(str(value) for value in ret) if isinstance(ret, list) else str(ret)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc)[:240],
            "items": [],
            "fetched_at": fetched_at,
        }

    try:
        _classify_ret(SEARCH_API, ret_text)
        data = raw.get("data")
        if not isinstance(data, dict):
            raise MarketSearchError("闲鱼搜索返回缺少 data")
        result_list = data.get("resultList")
        if not isinstance(result_list, list):
            raise MarketSearchError("闲鱼搜索返回缺少 data.resultList")
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc)[:240],
            "items": [],
            "fetched_at": fetched_at,
        }

    items = [
        item
        for card in result_list
        if (item := parse_search_card(card)) is not None
    ]
    return {
        "ok": True,
        "items": items,
        "raw_count": len(result_list),
        "fetched_at": fetched_at,
        "error": "",
    }


__all__ = [
    "MarketSearchError",
    "cached_market_items",
    "market_cache_snapshot",
    "parse_search_card",
    "remember_market_error",
    "remember_market_items",
    "search_market_listings",
]
