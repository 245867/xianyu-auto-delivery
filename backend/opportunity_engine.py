"""
选品机会评分引擎 —— 复刻自 dameng2026/xianyu-pilot-pro
apps/automation-service/app/api/v1/routes/opportunity.py（Apache-2.0）。

输入：utils/item_search.py::search_xianyu_items 的搜索结果（Playwright 真实数据，
已按想要数倒序），字段为本地格式：item_id/title/price/seller_name/item_url/
main_image/area/want_count/raw_data。
输出：按 opportunity_score 倒序的榜单 + 概要（价格水位、热度等级、推荐意见）。

评分公式与上游一致（见 00-项目规划/GitHub复刻方案.md 第三节）。

许可说明：评分逻辑与敏感词表复刻自上游 Apache-2.0 项目；本地合并应用
（AGPL 底座 + Apache 引擎），来源与许可已在 LICENSE/README 标注。
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

# ---- 与上游一致的敏感词（命中即扣分） ----
SENSITIVE_WORDS = [
    "高仿", "复刻", "A货", "精仿", "违禁",
    "烟", "电子烟", "药", "处方", "枪", "刀",
]

# ---- 推荐意见阈值（上游 summary 语义：推荐/观察/暂不推荐） ----
RECOMMEND_THRESHOLD = 60.0
OBSERVE_THRESHOLD = 30.0


def _to_int(value: Any, default: int = 0) -> int:
    """把字符串/浮点宽松转 int：以任何数字片段为准。"""
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
        if not match:
            return default
        return int(float(match.group()))
    except Exception:
        return default


def _deep_find(obj: Any, keys: Tuple[str, ...], max_nodes: int = 5000,
               max_depth: int = 40) -> Any:
    """在 dict/list 树里找第一个命中键的**非空**值。

    原来是纯递归、无上限、不防环。真实接口返回体偶尔会带上千层嵌套的东西
    （或同一个 dict 被多处引用形成环），递归版本会直接 RecursionError，
    而这个函数的调用方在解析搜索结果里——一条畸形数据就能让整次搜索报错。

    改成显式栈 + 三重闸：节点数上限、深度上限、visited 去重。
    上限口径借用 AutoJs6 `UiNodes.walk` 的 maxNodes=2000 思路，这里放宽到 5000，
    因为 JSON 树一般比 UI 树浅得多，够用；超了就当没找到（返回 None），不报错。
    """
    stack: List[Tuple[Any, int]] = [(obj, 0)]
    visited: set = set()
    seen = 0

    while stack:
        cur, depth = stack.pop()
        seen += 1
        if seen > max_nodes or depth > max_depth:
            continue
        if isinstance(cur, dict):
            for k in keys:
                if k in cur and cur[k] not in (None, ""):
                    return cur[k]
            if id(cur) in visited:
                continue
            visited.add(id(cur))
            # 反着压栈，弹出时才是原来的先后顺序——语义必须和递归版一致，
            # 否则同一个返回体会从「取第一个命中」变成「取最后一个命中」。
            for v in reversed(list(cur.values())):
                stack.append((v, depth + 1))
        elif isinstance(cur, list):
            if id(cur) in visited:
                continue
            visited.add(id(cur))
            for v in reversed(cur):
                stack.append((v, depth + 1))
    return None


def extract_price(value: Any) -> Optional[float]:
    """
    兼容多种价格格式：
    - 数字（int/float/str"160"）
    - 数组式数组 [{"text":"¥160","type":"sign"},{"text":"160","type":"integer"}]
    - 带货币前缀 "¥160 原价 299"
    """
    try:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, list):
            # goofish 价格数组：取 integer 片段；退化则拼接 text
            integers = [p.get("text", "") for p in value
                        if isinstance(p, dict) and p.get("type") == "integer"]
            if integers:
                num = _to_int("".join(integers), default=-1)
                return float(num) if num >= 0 else None
            text = "".join(p.get("text", "") for p in value if isinstance(p, dict))
            value = text
        if isinstance(value, str):
            text = value.strip()
            num = _to_int(text, default=-1)
            if num < 0:
                return None
            return float(num)
    except Exception:
        return None
    return None


def normalize_item(raw: Dict[str, Any], keyword: str = "") -> Dict[str, Any]:
    """
    本地搜索字段 → 上游评分字段的归一化。
    兼容 key 命名：item_id/title/price/seller_name/item_url/main_image/
    area/want_count/raw_data。
    """
    raw = raw or {}
    if not isinstance(raw, dict):
        raw = {}
    raw_data = raw.get("raw_data") or {}

    title = str(raw.get("title") or "").strip()
    description = str(raw.get("description") or "").strip()
    if not description:
        # 本地搜索没有单独正文字段时，用商品短语（含想看数的标签）略
        tags = raw.get("tags") or []
        description = " ".join(str(t) for t in tags[:3] if t)

    price = extract_price(raw.get("price") or raw_data.get("price"))
    image = str(raw.get("main_image") or raw_data.get("picUrl") or "")
    seller = str(raw.get("seller_name") or raw_data.get("userNickName") or "")
    item_url = str(raw.get("item_url") or raw_data.get("itemUrl") or "")
    area = str(raw.get("area") or raw_data.get("area") or "")
    # 想要/浏览/成交三个数：**顶层字段优先，再去 raw_data 里深挖**。
    # 原来浏览与成交只查 raw_data，顶层给的值全被丢掉——而详情补数
    # 恰恰是把 browse_count/sold_count 写在顶层，于是补了数也进不了漏斗率。
    want_count = _to_int(raw.get("want_count") or raw.get("wantCnt")
                         or _deep_find(raw_data, ("wantCount", "wantCountMtop", "want_count")))
    view_count = _to_int(raw.get("view_count") or raw.get("browse_count")
                         or _deep_find(raw_data, ("viewCount", "showCount", "browseCnt", "view_count")))
    sold_count = _to_int(raw.get("sold_count") or raw.get("soldCnt")
                         or _deep_find(raw_data, ("soldCount", "saleCount", "sold_count")))
    # 搜索卡片解析器已经把“N人想要”的准确来源和详情补数的可信状态写在顶层。
    # 归一化如果丢掉这两个字段，opportunity_detail 会把所有搜索值误降级成 list，
    # 页面随即把真实想要数当成不可用。其余详情指标也一并透传，避免二次丢数。
    want_source = raw.get("want_source")
    want_stale = raw.get("want_stale")

    normalized = {
        "itemId": str(raw.get("item_id") or _deep_find(raw_data, ("itemId", "item_id")) or ""),
        "title": title,
        "description": description,
        "price": price,
        "imageUrl": image,
        "itemUrl": item_url or (
            f"https://www.goofish.com/item?itemId={str(raw.get('item_id') or '')}"
            if raw.get("item_id") else ""
        ),
        "seller": seller,
        "area": area,
        "want_count": want_count,
        "want_source": want_source,
        "want_stale": want_stale,
        "view_count": view_count,
        "browse_count": raw.get("browse_count"),
        "sold_count": sold_count,
        "detail_sold_count": raw.get("detail_sold_count"),
        "detail_stock": raw.get("detail_stock"),
        "sold_out": raw.get("sold_out"),
        "keyword": keyword,
    }
    return normalized


def score_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    上游同款评分（逐项对齐）：
    heat_score  = clamp[0,100] (20 + want*2 + view/20 + sold*6)
    price_score = 无价格 45；否则 clamp[0,60] (60 - log10(max(price,1))*8)
    keyword_score = min(15, hits*8)
    risk_penalty = hits(SENSITIVE_WORDS)*18
    opinion     = 推荐关注 ≥60 / 可观察 30-59 / 暂不推荐 <30
    """
    want_count = int(item.get("want_count") or 0)
    view_count = int(item.get("view_count") or 0)
    sold_count = int(item.get("sold_count") or 0)

    heat_score = min(100.0, 20 + want_count * 2 + view_count // 20 + sold_count * 6)

    price_num = item.get("price")
    if price_num is None:
        price_score = 45.0
    else:
        price_score = max(0.0, min(60.0, 60 - int(math.log10(max(price_num, 1)) * 8)))

    keyword = str(item.get("keyword") or "") or str(item.get("title") or "")
    text_blob = f"{item.get('title','')} {item.get('description','')}"
    keyword_hits = 0
    if keyword:
        keyword_hits = sum(1 for kw in [keyword] if kw in text_blob)
    keyword_score = min(15.0, keyword_hits * 8)

    risk_hits = [kw for kw in SENSITIVE_WORDS if kw in text_blob]
    risk_penalty = len(risk_hits) * 18

    opportunity_score = max(0.0, min(100.0,
                                      heat_score + price_score + keyword_score - risk_penalty))

    if opportunity_score >= RECOMMEND_THRESHOLD:
        opinion = "推荐关注"
    elif opportunity_score >= OBSERVE_THRESHOLD:
        opinion = "可观察"
    else:
        opinion = "暂不推荐"

    scored = dict(item)
    scored.update({
        "heat_score": round(heat_score, 1),
        "price_score": round(price_score, 1),
        "keyword_score": round(keyword_score, 1),
        "risk_penalty": risk_penalty,
        "opportunity_score": round(opportunity_score, 1),
        "opinion": opinion,
        "risk_hits": risk_hits,
    })
    return scored


def summarize(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """概要：价格水位/热度等级/高分标题。"""
    scores = [float(x.get("opportunity_score") or 0) for x in items]
    prices = [x["price"] for x in items if x.get("price") is not None]
    # heat_score 是 v1 的字段；v2 打分的条目没有它，用 .get 兜底，
    # 否则这里会 KeyError，把"评分成功但概要以"变成整页 500。
    heats = [float(x.get("heat_score") or 0) for x in items]
    if scores:
        avg_score = round(sum(scores) / len(scores), 1)
        max_score = round(max(scores), 1)
    else:
        avg_score = max_score = 0
    return {
        "count": len(items),
        "avg_score": avg_score,
        "max_score": max_score,
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "price_avg": round(sum(prices) / len(prices), 1) if prices else None,
        "heat_level": "高" if avg_score >= RECOMMEND_THRESHOLD else
                      ("中" if avg_score >= OBSERVE_THRESHOLD else "低"),
        "top_titles": [x.get("title", "")[:40] for x in items[:5]],
    }


def rank_items(raw_items: List[Dict[str, Any]], keyword: str,
               top_n: int = 50) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    入口：原始搜索结果 → 评分榜 + 概要。

    v2 必须在这里就接手，**不能等详情补数之后再切**：
    原来只有"补数成功"那条分支才走 merge_inplace，于是补数失败时整页仍是 v1 ——
    而 v1 在热门词上给所有条目同一个 100 分，用户看到的就是一屏并列第一。
    评分口径从第一眼起就该是同一套。
    """
    normalized = []
    seen_ids = set()
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        norm = normalize_item(raw, keyword)
        item_id = str(norm.get("itemId") or "")
        if item_id and item_id in seen_ids:
            continue
        if item_id:
            seen_ids.add(item_id)
        normalized.append(norm)

    limit = max(1, min(top_n, 100))

    try:
        from opportunity_score_v2 import v2_enabled, score_batch
        use_v2 = v2_enabled()
    except Exception:
        use_v2 = False

    if use_v2:
        # score_batch 在**整批**上算分位数和分档，所以先全量打分再截断，
        # 不能反过来——截断后再归一化，等于拿前 50 名当参照集。
        res = score_batch(normalized, keyword=keyword)
        scored_all = res["items"]
        summary = summarize(scored_all)
        summary["score_meta"] = res.get("meta") or {}
        return scored_all[:limit], summary

    scored = sorted(map(score_item, normalized),
                    key=lambda x: x["opportunity_score"], reverse=True)
    return scored[:limit], summarize(scored)



__all__ = [
    "SENSITIVE_WORDS", "normalize_item", "score_item", "rank_items", "summarize",
    "extract_price", "RECOMMEND_THRESHOLD", "OBSERVE_THRESHOLD",
]
