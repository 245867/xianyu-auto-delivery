"""Aggregate quotes from anonymous public card-market pages."""

from __future__ import annotations

import base64
import html as html_lib
import json
import math
import os
import re
import subprocess
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import unescape
from threading import Lock
from time import monotonic
from urllib.parse import urlencode, urljoin, urlsplit

import httpx


REQUEST_TIMEOUT_SECONDS = 15
SNAPSHOT_TTL_SECONDS = 300
FAILURE_TTL_SECONDS = 30
HOT_KEYWORD_LIMIT = 10
CARD_API_LIMIT = 20

QUANSHANGBI_ORIGIN = "https://quanshangbi.com"
KAJIAGE_ORIGIN = "https://kajiage.com"
WISDOMSTAR_ORIGIN = "https://www.wisdomstar.site"
KAQUANJISHI_ORIGIN = "https://kaquanjishi.com"

# These terms were present in Kajiage's public homepage HotKeywords payload on
# 2026-11-17. They are used only when that dynamically sourced list is unavailable.
FALLBACK_HOT_KEYWORDS = (
    "星巴克",
    "麦当劳",
    "美团",
    "瑞幸",
    "肯德基",
    "滴滴",
    "艾起亿会员",
    "腾讯视频会员",
    "QQ会员",
    "哔哩哔哩会员",
)

# Every anchor below returned matching public offers from Kajiage on
# 2026-11-17. Broad titles such as "会员" and "视频" produce a substantial
# cross-category sample; they are category discovery anchors, not claims that
# every SKU in the category has been checked.
CATEGORY_KEYWORD_ANCHORS = (
    ("会员", "综合权益"),
    ("音乐", "音频音乐"),
    ("视频", "影视娱乐"),
    ("电影票", "娱乐票务"),
    ("软件", "工具软件"),
    ("办公", "工具软件"),
    ("教程", "教育知识"),
    ("资料", "教育知识"),
    ("网盘", "网盘资料"),
    ("游戏", "游戏虚拟"),
    ("点券", "游戏虚拟"),
    ("话费", "生活服务"),
    ("流量", "生活服务"),
    ("外卖", "美食餐饮"),
    ("打车", "生活服务"),
    ("京东E卡", "电商购物"),
)

HOT_KEYWORD_CATEGORY = {
    "星巴克": "美食餐饮",
    "麦当劳": "美食餐饮",
    "美团": "美食餐饮",
    "瑞幸": "美食餐饮",
    "肯德基": "美食餐饮",
    "滴滴": "生活服务",
    "艾起亿会员": "综合权益",
    "腾讯视频会员": "影视娱乐",
    "QQ会员": "社交会员",
    "哔哩哔哩会员": "影视娱乐",
}

PROVIDER_META = {
    "kajiage": {"name": "卡券雷达"},
    "kaquanjishi": {"name": "卡券集市"},
    "quanshangbi": {"name": "券上比"},
    "wisdomstar": {"name": "智多星"},
}
PROVIDER_ORDER = ("kajiage", "kaquanjishi", "quanshangbi", "wisdomstar")

_lock = Lock()
_snapshot = None
_expires_at = 0.0
_last_success = {}


def _clean_text(value):
    text = unescape(str(value or ""))
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalized_title(value):
    # Normalization only removes presentation differences. It deliberately keeps
    # duration, quantity, delivery, tier and other specification text intact.
    return _clean_text(value).casefold()


def _match_key(value):
    return re.sub(r"\s+", "", _normalized_title(value))


def _utc_now():
    return datetime.now(timezone.utc)


def _utc_iso(value=None):
    return (value or _utc_now()).astimezone(timezone.utc).isoformat()


def _strip_html(value):
    return _clean_text(re.sub(r"<[^>]+>", " ", value or ""))


def _parse_price(value):
    match = re.search(r"(\d+(?:\.\d+)?)", _clean_text(value))
    if not match:
        return None
    price = float(match.group(1))
    return price if math.isfinite(price) and price >= 0 else None


def _make_quote(
    *,
    title,
    price,
    supplier,
    url,
    source,
    source_url,
    updated_at,
    link_type="source",
):
    clean_title = _clean_text(title)
    clean_supplier = _clean_text(supplier)
    clean_url = str(url or source_url).strip()
    try:
        address = urlsplit(clean_url)
        if address.scheme not in ("https", "http") or not address.hostname:
            clean_url = str(source_url).strip()
            link_type = "source"
        elif (
            link_type == "product"
            and not address.path.strip("/")
            and not address.query
        ):
            link_type = "source"
    except ValueError:
        clean_url = str(source_url).strip()
        link_type = "source"
    try:
        clean_price = float(price)
    except (TypeError, ValueError):
        return None
    if (
        not clean_title
        or not clean_url
        or not math.isfinite(clean_price)
        or clean_price < 0
    ):
        return None
    return {
        "title": clean_title,
        "price": clean_price,
        "supplier": clean_supplier,
        "url": clean_url,
        "source": source,
        "source_label": PROVIDER_META[source]["name"],
        "source_url": source_url,
        "updated_at": updated_at,
        "link_type": link_type,
    }


def _dedupe_quotes(quotes):
    merged = {}
    for quote in quotes:
        if not quote:
            continue
        identity = (
            quote["source"],
            _normalized_title(quote["title"]),
            _normalized_title(quote["supplier"]),
            quote["url"] if quote.get("link_type") == "product" else None,
        )
        current = merged.get(identity)
        # A fresh observation takes precedence over a cached value for the same
        # product (or the same published summary when only a directory is known).
        rank = (
            not quote.get("stale", False),
            quote.get("fetched_at") or "",
            -quote["price"],
        )
        if current is None or rank > (
            not current.get("stale", False),
            current.get("fetched_at") or "",
            -current["price"],
        ):
            merged[identity] = quote
    return sorted(
        merged.values(),
        key=lambda quote: (
            quote["source"],
            _normalized_title(quote["title"]),
            quote["price"],
            _normalized_title(quote["supplier"]),
        ),
    )


def parse_quotes(html):
    """Retain the original homepage-card parser as a public-page fallback."""
    quotes = []
    seen = set()
    for card in re.finditer(
        r'<a\b([^>]*\bdata-offer-card="true"[^>]*)>(.*?)</a>',
        html,
        re.S,
    ):
        attrs, body = card.groups()
        title = re.search(r'\btitle="([^"]+)"', attrs)
        path = re.search(r'\bhref="(/goods/\d+)"', attrs)
        price = re.search(
            r'¥</span>\s*<span[^>]*>\s*(\d+(?:\.\d+)?)\s*</span>',
            body,
        )
        if not title or not path or not price or path[1] in seen:
            continue
        name = _clean_text(title[1])
        if not re.search(
            r"会员|月卡|季卡|年卡|瑞幸|星巴克|肯德基|麦当劳|礼品卡|京东E卡",
            name,
        ):
            continue
        supplier = re.search(
            r'<span class="truncate">([^<]+)</span>',
            body,
        )
        seen.add(path[1])
        quotes.append(
            {
                "title": name,
                "url": QUANSHANGBI_ORIGIN + path[1],
                "price": float(price[1]),
                "supplier": _clean_text(supplier[1]) if supplier else "",
            }
        )
        if len(quotes) == 12:
            break
    return quotes


def _powershell_get(url):
    safe_url = url.replace("'", "''")
    script = (
        "$ProgressPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        f"(Invoke-WebRequest -Uri '{safe_url}' -UseBasicParsing "
        f"-TimeoutSec {REQUEST_TIMEOUT_SECONDS} -MaximumRedirection 0 "
        "-ErrorAction Stop).Content"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=REQUEST_TIMEOUT_SECONDS + 3,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if not result.stdout:
        raise ValueError("empty PowerShell response")
    return result.stdout


def _http_get_text(url):
    with httpx.Client(
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def _extract_card_radar_payload(html):
    marker = "window.__CARD_RADAR__"
    marker_at = html.find(marker)
    if marker_at < 0:
        raise ValueError("Card Radar payload not found")
    equals_at = html.find("=", marker_at + len(marker))
    if equals_at < 0:
        raise ValueError("Card Radar assignment not found")
    payload, _ = json.JSONDecoder().raw_decode(
        html_lib.unescape(html[equals_at + 1 :]).lstrip()
    )
    if not isinstance(payload, dict):
        raise ValueError("Card Radar payload is not an object")
    return payload


def _load_hot_catalog():
    try:
        payload = _extract_card_radar_payload(
            _http_get_text(f"{KAJIAGE_ORIGIN}/")
        )
        values = payload.get("HotKeywords")
        if not isinstance(values, list):
            raise ValueError("HotKeywords is not a list")
        keywords = []
        seen = set()
        for item in values:
            if not isinstance(item, dict):
                continue
            keyword = _clean_text(item.get("keyword"))
            key = _match_key(keyword)
            if keyword and key not in seen:
                keywords.append(keyword)
                seen.add(key)
            if len(keywords) == HOT_KEYWORD_LIMIT:
                break
        if keywords:
            return keywords, {
                "source": "卡券雷达公开首页",
                "source_url": f"{KAJIAGE_ORIGIN}/",
                "site_count": payload.get("TotalSites"),
                "product_count": payload.get("TotalProducts"),
                "offer_count": payload.get("TotalOffers"),
            }
    except Exception:
        pass
    return list(FALLBACK_HOT_KEYWORDS[:HOT_KEYWORD_LIMIT]), {}


def _fetch_kajiage(keyword):
    query_url = (
        f"{KAJIAGE_ORIGIN}/api/price-check?"
        + urlencode(
            {
                "keyword": keyword,
                "page": 1,
                "limit": CARD_API_LIMIT,
                "sort": "price-asc",
            }
        )
    )
    payload = json.loads(_http_get_text(query_url))
    offers = payload.get("offers")
    if not isinstance(offers, list):
        raise ValueError("Kajiage offers is not a list")
    fetched_at = _utc_iso()
    query_key = _match_key(keyword)
    quotes = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        title = _clean_text(offer.get("productName"))
        if query_key and query_key not in _match_key(title):
            continue
        quote = _make_quote(
            title=title,
            price=offer.get("price"),
            supplier=offer.get("siteName"),
            url=offer.get("productUrl") or f"{KAJIAGE_ORIGIN}/",
            source="kajiage",
            source_url=f"{KAJIAGE_ORIGIN}/",
            updated_at=offer.get("updatedAt") or fetched_at,
            link_type="product" if offer.get("productUrl") else "source",
        )
        if quote:
            quotes.append(quote)
    return _dedupe_quotes(quotes), fetched_at


def _fetch_kaquanjishi(keyword):
    page_url = f"{KAQUANJISHI_ORIGIN}/price-check"
    query_url = f"{KAQUANJISHI_ORIGIN}/price-check/query"
    with httpx.Client(
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as client:
        page = client.get(page_url)
        page.raise_for_status()
        csrf = re.search(
            r'<meta\s+name="csrf-token"\s+content="([^"]+)"',
            page.text,
        )
        if not csrf:
            raise ValueError("csrf-token meta field not found")
        response = client.post(
            query_url,
            data={
                "keyword": keyword,
                "offset": 0,
                "limit": CARD_API_LIMIT,
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-TOKEN": csrf.group(1),
            },
        )
        response.raise_for_status()
        payload = response.json()

    if payload.get("code") != 0:
        raise ValueError(
            f"price-check code {payload.get('code')}: {payload.get('msg', '')}"
        )
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        raise ValueError("price-check data.list is not a list")

    fetched_at = _utc_iso()
    query_key = _match_key(keyword)
    quotes = []
    for item in data["list"]:
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("name"))
        if query_key and query_key not in _match_key(title):
            continue
        source_url = f"{page_url}?{urlencode({'keyword': keyword})}"
        quote = _make_quote(
            title=title,
            price=item.get("price"),
            supplier=item.get("site_name"),
            url=(
                f"{KAQUANJISHI_ORIGIN}/site/{item['site_id']}"
                if item.get("site_id") is not None
                else source_url
            ),
            source="kaquanjishi",
            source_url=source_url,
            updated_at=fetched_at,
        )
        if quote:
            quotes.append(quote)
    return _dedupe_quotes(quotes), fetched_at


def _parse_quanshangbi_search(html, keyword, source_url):
    fetched_at = _utc_iso()
    query_key = _match_key(keyword)
    quotes = []
    for card in re.finditer(
        r'<a\b([^>]*\bdata-offer-card="true"[^>]*)>(.*?)</a>',
        html,
        re.S,
    ):
        attrs, body = card.groups()
        title = re.search(r'\btitle="([^"]+)"', attrs)
        path = re.search(r'\bhref="(/goods/\d+)"', attrs)
        price = re.search(
            r'¥</span>\s*<span[^>]*>\s*(\d+(?:\.\d+)?)\s*</span>',
            body,
        )
        if not title or not path or not price:
            continue
        clean_title = _clean_text(title[1])
        if query_key and query_key not in _match_key(clean_title):
            continue
        supplier = re.search(
            r'<span class="truncate">([^<]+)</span>',
            body,
        )
        quote = _make_quote(
            title=clean_title,
            price=price[1],
            supplier=supplier[1] if supplier else "",
            url=QUANSHANGBI_ORIGIN + path[1],
            source="quanshangbi",
            source_url=source_url,
            updated_at=fetched_at,
            link_type="product",
        )
        if quote:
            quotes.append(quote)
    return _dedupe_quotes(quotes), fetched_at


def _fetch_quanshangbi(keyword):
    source_url = (
        f"{QUANSHANGBI_ORIGIN}/search?"
        + urlencode({"q": keyword})
    )
    try:
        text = (
            _powershell_get(source_url)
            if os.name == "nt"
            else _http_get_text(source_url)
        )
        quotes, fetched_at = _parse_quanshangbi_search(
            text,
            keyword,
            source_url,
        )
        if quotes:
            return quotes, fetched_at
    except Exception:
        pass

    # Existing-home card fallback. This only uses the same public DOM marker and
    # never assumes a private endpoint.
    home_url = f"{QUANSHANGBI_ORIGIN}/"
    text = (
        _powershell_get(home_url)
        if os.name == "nt"
        else _http_get_text(home_url)
    )
    fetched_at = _utc_iso()
    query_key = _match_key(keyword)
    quotes = []
    for item in parse_quotes(text):
        if query_key and query_key not in _match_key(item["title"]):
            continue
        quote = _make_quote(
            title=item["title"],
            price=item["price"],
            supplier=item["supplier"],
            url=item["url"],
            source="quanshangbi",
            source_url=home_url,
            updated_at=fetched_at,
            link_type="product",
        )
        if quote:
            quotes.append(quote)
    return _dedupe_quotes(quotes), fetched_at


def _parse_wisdomstar(html, keyword, source_url):
    fetched_at = _utc_iso()
    query_key = _match_key(keyword)
    quotes = []
    for row in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row.group(1), re.S | re.I)
        if len(cells) < 4:
            continue
        supplier = _strip_html(cells[0])
        title = _strip_html(cells[1])
        price = _parse_price(_strip_html(cells[2]))
        link = re.search(
            r'href="([^"]+)"',
            cells[3] or cells[0],
            re.I,
        )
        if not link:
            continue
        if query_key and query_key not in _match_key(title):
            continue
        quote = _make_quote(
            title=title,
            price=price,
            supplier=supplier,
            url=urljoin(WISDOMSTAR_ORIGIN, html_lib.unescape(link.group(1))),
            source="wisdomstar",
            source_url=source_url,
            updated_at=fetched_at,
        )
        if quote:
            quotes.append(quote)
    return _dedupe_quotes(quotes), fetched_at


def _fetch_wisdomstar(keyword):
    source_url = (
        f"{WISDOMSTAR_ORIGIN}/compare?"
        + urlencode({"kw": keyword})
    )
    return _parse_wisdomstar(
        _http_get_text(source_url),
        keyword,
        source_url,
    )


def _fetch_for_source(source, keyword):
    if source == "kajiage":
        return _fetch_kajiage(keyword)
    if source == "kaquanjishi":
        return _fetch_kaquanjishi(keyword)
    if source == "quanshangbi":
        return _fetch_quanshangbi(keyword)
    if source == "wisdomstar":
        return _fetch_wisdomstar(keyword)
    raise ValueError(f"unknown provider {source}")


def _provider_outcome(source, keywords, fresh_by_keyword, errors):
    previous = _last_success.get(source, {})
    remembered = {}
    quotes = []
    for keyword in keywords:
        observation = fresh_by_keyword.get(keyword) or previous.get(keyword)
        if observation is None:
            continue
        remembered[keyword] = observation
        quotes.extend(
            {
                **quote,
                "fetched_at": observation["fetched_at"],
                "stale": keyword not in fresh_by_keyword,
            }
            for quote in observation["quotes"]
        )
    _last_success[source] = remembered
    merged = _dedupe_quotes(quotes)
    if fresh_by_keyword:
        status = "partial" if errors else "ok"
    else:
        status = "stale" if merged else "error"
    relevant_observations = fresh_by_keyword or remembered
    return {
        "id": source,
        "name": PROVIDER_META[source]["name"],
        "ok": status == "ok",
        "status": status,
        "quote_count": len(merged),
        "fetched_at": max(
            (observation["fetched_at"] for observation in relevant_observations.values()),
            default=None,
        ),
        "error": "; ".join(errors[:3]) or None,
    }, merged


def current_quotes(force_refresh=False):
    global _snapshot, _expires_at
    observed_snapshot = _snapshot
    with _lock:
        if _snapshot is not None and (
            _snapshot is not observed_snapshot
            or (not force_refresh and monotonic() < _expires_at)
        ):
            return _snapshot

        keywords, catalog_summary = _load_hot_catalog()
        keyword_catalog = [
            {
                "keyword": keyword,
                "category": HOT_KEYWORD_CATEGORY.get(keyword, "公开热词"),
                "origin": "hot",
            }
            for keyword in keywords
        ]
        known_keywords = {_match_key(keyword) for keyword in keywords}
        for keyword, category in CATEGORY_KEYWORD_ANCHORS:
            key = _match_key(keyword)
            if key in known_keywords:
                continue
            keyword_catalog.append({
                "keyword": keyword,
                "category": category,
                "origin": "category",
            })
            known_keywords.add(key)
        category_keywords = [
            item["keyword"]
            for item in keyword_catalog
            if item["origin"] == "category"
        ]
        state = {
            source: {
                "fresh_by_keyword": {},
                "errors": [],
                "keywords": [*keywords, *category_keywords],
            }
            for source in PROVIDER_ORDER
        }
        tasks = [
            (source, keyword)
            for source in PROVIDER_ORDER
            for keyword in state[source]["keywords"]
        ]
        with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as executor:
            futures = {
                executor.submit(_fetch_for_source, source, keyword): (
                    source,
                    keyword,
                )
                for source, keyword in tasks
            }
            for future in as_completed(futures):
                source, keyword = futures[future]
                try:
                    quotes, fetched_at = future.result()
                except Exception as exc:
                    state[source]["errors"].append(
                        f"{keyword}: {type(exc).__name__}: {exc}"
                    )
                    continue
                state[source]["fresh_by_keyword"][keyword] = {
                    "quotes": quotes,
                    "fetched_at": fetched_at,
                }

        providers = []
        all_quotes = []
        for source in PROVIDER_ORDER:
            provider, quotes = _provider_outcome(
                source,
                state[source]["keywords"],
                state[source]["fresh_by_keyword"],
                state[source]["errors"],
            )
            providers.append(provider)
            all_quotes.extend(quotes)

        snapshot = {
            "quotes": _dedupe_quotes(all_quotes),
            "fetched_at": _utc_iso(),
            "stale": any(not provider["ok"] for provider in providers),
            "providers": providers,
            "hot_keywords": keywords,
            "keyword_catalog": keyword_catalog,
            "catalog_summary": catalog_summary,
        }
        _snapshot = snapshot
        _expires_at = monotonic() + (
            SNAPSHOT_TTL_SECONDS
            if not snapshot["stale"]
            else FAILURE_TTL_SECONDS
        )
        return _snapshot
