"""
选品工作台路由 —— /api/opportunity 系列（合并复刻自 dameng2026/xianyu-pilot-pro）

- POST /api/opportunity/analyze   关键词搜索 → 机会评分排名
- POST /api/opportunity/rewrite   AI 改写（强润色限制 + 相似度校验）
- POST /api/opportunity/publish   一键发布（MTOP 直发，60s 限速 + 夜间禁发）
- GET  /api/opportunity/history   最近选品记录
- GET  /opportunity              选品工作台页面
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import FileResponse

from config import config
from opportunity_engine import (
    rank_items, score_item, summarize, RECOMMEND_THRESHOLD, OBSERVE_THRESHOLD,
)
from opportunity_rewrite import rewrite_item
from opportunity_price import (
    record_snapshots, annotate_items, keyword_summary,
)
from opportunity_detail import enrich_items, resolve_cookie
from opportunity_vision import analyze_images
from market_listings import (
    cached_market_items,
    market_cache_snapshot,
    remember_market_error,
    remember_market_items,
    search_market_listings,
)
import opportunity_score_v2 as _v2

# B4：评分 v2（漏斗率 + 分层加权 + 样本自适应 + 风险分档）。
# 回滚：环境变量 OPPORTUNITY_SCORE_V2=0 即回到 v1 绝对分公式。
def _use_v2() -> bool:
    try:
        return _v2.v2_enabled()
    except Exception:
        return False


def _opinion_of(score: float, item: dict | None = None) -> str:
    """加成长值/AI 看图之后重算意见。v2 下用本批排名，v1 下用固定阈值。"""
    if item is not None and item.get("blocked"):
        return "不推荐"
    if _use_v2():
        return _v2.opinion_for_score(score)
    return ("推荐关注" if score >= RECOMMEND_THRESHOLD
            else "可观察" if score >= OBSERVE_THRESHOLD
            else "暂不推荐")
from opportunity_guard import (
    check_allowed, record_success, record_failure, reset_guard, get_guard_status,
)
from opportunity_publisher import (
    publish_item, extract_token_from_cookie, TOKEN_COOKIE_NAME, PUBLISH_API,
    PUBLISH_VERSION, PublishRiskError, PublishAuthError,
)
from publish_guard import check_publish
import rate_limiter
import idempotency
from opportunity_auth import require_authenticated_user

router = APIRouter()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
HISTORY_FILE = DATA_DIR / "opportunity_history.json"

# 限速：同一账号 60 秒内不允许二次发布（防风控；实现在 rate_limiter）
PUBLISH_MIN_INTERVAL_SECONDS = 60


def _load_history() -> list:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_history(items: list) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(items[:20], f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _resolve_cookie(cookie_id: str, user_id: int | None = None) -> str:
    """优先从内存 cookie_manager 取，回退 db_manager（值为 Cookie 字符串）。"""
    import cookie_manager as cm
    from db_manager import db_manager

    if user_id is not None:
        owned = db_manager.get_all_cookies(int(user_id)) or {}
        if cookie_id not in owned:
            return ""
    cookies = {}
    if getattr(cm, "manager", None) is not None:
        cookies = getattr(cm.manager, "cookies", {}) or {}
    cookie = cookies.get(cookie_id)
    if cookie:
        return cookie
    all_cookies = db_manager.get_all_cookies() or {}
    return all_cookies.get(cookie_id, "")


def _night_ban_ok() -> (bool, str):
    """夜间禁发：23:00-08:00 禁止自动发布。"""
    now = datetime.now()
    if 23 <= now.hour or now.hour < 8:
        return False, "当前处于夜间时段（23:00-08:00），已禁止自动发布；请白天操作"
    return True, ""


@router.get("/opportunity", include_in_schema=False)
async def opportunity_page():
    """选品工作台页面。"""
    return FileResponse(PROJECT_ROOT / "static" / "opportunity.html")


@router.get("/pickup", include_in_schema=False)
async def pickup_page():
    """买家端独立网页自提中心页面 (吸收嗷嗷鱼防封交付闭环)。"""
    return FileResponse(PROJECT_ROOT / "static" / "pickup.html")


@router.get("/automation-hub", include_in_schema=False)
async def automation_hub_page():
    """商业闭源高阶策略中枢页面 (自提防封 + 阶梯降价 + 拟态养号)。"""
    return FileResponse(PROJECT_ROOT / "static" / "automation_hub.html")


@router.get("/api/opportunity/sourcing-quotes")
def sourcing_quotes(refresh: bool = False):
    from sourcing_quotes import current_quotes

    return {**current_quotes(force_refresh=refresh), "market_cache": market_cache_snapshot()}


@router.get("/api/opportunity/market-listings")
def market_listings(keyword: str = ""):
    """Read the locally cached marketplace listings without contacting Goofish."""
    if not keyword.strip():
        return {"ok": True, "records": market_cache_snapshot()}
    return {"ok": True, **cached_market_items(keyword)}


@router.post("/api/opportunity/analyze")
async def opportunity_analyze(body: Dict[str, Any] = Body(...)):
    """
    关键词搜索 → 机会评分排名。
    请求: {keyword: "壁纸", top_n: 50}
    """
    keyword = str(body.get("keyword") or "").strip()
    top_n = int(body.get("top_n") or 50)
    if not keyword:
        return {"ok": False, "error": "搜索关键词不能为空"}

    # 风控守卫：冷却期内拒绝搜索（照 ai-goofish-monitor 失败保护）
    ok, reason = check_allowed()
    if not ok:
        return {"ok": False, "error": reason, "guard": get_guard_status()}

    rate_ok, retry_after = rate_limiter.acquire("search", "global")
    if not rate_ok:
        return {
            "ok": False,
            "error": f"搜索请求过于频繁，请 {retry_after} 秒后再试",
            "retry_after": retry_after,
            "guard": get_guard_status(),
        }

    cookie_id, cookie_str = resolve_cookie(
        str(body.get("cookie_id") or "").strip(),
        fallback_first=True,
    )
    if not cookie_str:
        return {
            "ok": False,
            "error": "没有可用的闲鱼登录会话，请先在 Cookie 管理中更新账号",
            "guard": get_guard_status(),
        }

    result = await asyncio.to_thread(
        search_market_listings,
        cookie_str,
        keyword,
        app_key=str(config.get("APP_CONFIG.app_key", "")),
        page=1,
        rows=30,
    )
    raw_items = result.get("items", [])
    if not result.get("ok"):
        error = str(result.get("error") or "闲鱼搜索失败")
        remember_market_error(keyword, error)
        failure = record_failure(result.get("error"))
        return {"ok": False, "error": result.get("error"),
                "guard": {**get_guard_status(), "risk_signal": failure.get("risk_signal")}}
    record_success()

    scored, summary = rank_items(raw_items, keyword=keyword, top_n=top_n)

    # A3：详情补数（只读）。搜索列表的 wantNum 自 2026 起恒为 0，
    # 真实想要数只在详情接口 data.itemDO.wantCnt。补到的项重算分数并重排，
    # 补不到的项标 want_stale，不参与成长值、也不写进价格历史。
    detail_stats: Dict[str, Any] = {"requested": 0, "success": 0, "failed": 0, "error": ""}
    if body.get("detail", True):
        try:
            cid, cstr = resolve_cookie(str(body.get("cookie_id") or "").strip())
            detail_stats = await asyncio.to_thread(
                enrich_items,
                scored, cstr, str(config.get("APP_CONFIG.app_key", "")),
                cookie_id=cid,
                limit=max(0, min(30, int(body.get("detail_limit") or 10))),
                focus_keyword=keyword,
            )
            if detail_stats.get("success"):
                # 补到真实想要数后必须重算：v1 的绝对公式对同一个 want 给同一个分，
                # v2 用本批分位数，所以要整批重算而不是逐条 rescore。
                if _use_v2():
                    scored = _v2.merge_inplace(scored, keyword)
                    # 补数会提高覆盖率：meta 必须跟着重算，否则页面还显示补数前的
                    # "想要覆盖 0%"，与实际已补到的条目对不上。
                    summary.setdefault("score_meta", {}).update(_v2.coverage_meta(scored))
                else:
                    rescored = [score_item(it) for it in scored]
                    rescored.sort(key=lambda x: x["opportunity_score"], reverse=True)
                    scored = rescored
                summary = summarize(scored)
        except Exception as e:
            detail_stats["error"] = f"详情补数异常：{str(e)[:120]}"
    summary["detail_enrich"] = detail_stats

    # 价格历史（补齐④）：先按历史标注（不含本次），再把本次写入快照表
    try:
        annotate_items(keyword, scored)
        record_snapshots(keyword, scored)
        market = keyword_summary(keyword)
        if market.get("ok"):
            summary["price_market"] = market.get("market")
            summary["price_suggestion"] = market.get("suggestion")
    except Exception as e:
        summary["price_history_error"] = f"价格历史不可用：{str(e)[:120]}"

    # 热度成长加权（借鉴芹菜工具箱"爆品潜力值"：想要数涨得快=需求真旺）：
    # 只有跨天历史才生效，首次搜索不会加分，避免把存量热度当成长。
    for it in scored:
        wpd = it.get("want_per_day")
        if not wpd:
            continue
        bonus = round(min(8.0, float(wpd) * 1.5), 1)
        if bonus <= 0:
            continue
        it["score_base"] = it.get("opportunity_score")
        it["momentum_bonus"] = bonus
        adj = max(0.0, min(100.0, float(it["opportunity_score"]) + bonus))
        it["opportunity_score"] = round(adj, 1)
        it["opinion"] = _opinion_of(adj, it)
    if _use_v2():
        _v2.refresh_rank(scored)
    else:
        scored.sort(key=lambda x: x.get("opportunity_score", 0), reverse=True)

    # 可选：AI 看图（body.vision=true 时对前 3 个有图商品分析并修正评分）
    if body.get("vision"):
        for it in scored[:3]:
            if not it.get("imageUrl"):
                continue
            try:
                v = analyze_images(str(it.get("itemId") or ""), it.get("title", ""),
                                   it.get("price"), [it["imageUrl"]])
            except Exception as e:
                v = {"ok": False, "error": str(e)[:120]}
            if v.get("ok"):
                it["vision"] = v.get("analysis")
                it["vision_cached"] = v.get("cached", False)
                it["score_base"] = it.get("opportunity_score")
                adjusted = max(0.0, min(100.0, float(it["opportunity_score"])
                                        + float(v["analysis"].get("bonus") or 0)))
                it["opportunity_score"] = round(adjusted, 1)
                it["opinion"] = _opinion_of(adjusted, it)
            else:
                it["vision_error"] = v.get("error", "")
        if _use_v2():
            _v2.refresh_rank(scored)
        else:
            scored.sort(key=lambda x: x.get("opportunity_score", 0), reverse=True)

    _save_history([{
        "time": datetime.now().isoformat(),
        "keyword": keyword,
        "total": len(raw_items),
        "top_titles": summary["top_titles"],
    }])
    fetched_at = str(result.get("fetched_at") or datetime.now().isoformat())
    remember_market_items(keyword, scored, fetched_at)
    return {
        "ok": True,
        "keyword": keyword,
        "items": scored,
        "summary": summary,
        "fetched_at": fetched_at,
        "cookie_id": cookie_id,
    }


@router.post("/api/opportunity/rewrite")
async def opportunity_rewrite(body: Dict[str, Any] = Body(...)):
    """
    AI 改写商品文案。
    请求: {title, description, keyword, style, customPrompt}
    """
    result = rewrite_item(
        title=str(body.get("title") or ""),
        description=str(body.get("description") or ""),
        keyword=str(body.get("keyword") or ""),
        style=str(body.get("style") or "friendly"),
        custom_prompt=str(body.get("customPrompt") or ""),
    )
    return {"ok": result.get("ok", False),
            "error": result.get("error", ""),
            "data": {
                "title": result.get("title", ""),
                "description": result.get("description", ""),
                "tags": result.get("tags", []),
                "similarity": result.get("similarity", 0),
                "needs_review": result.get("needs_review", False),
                "forbidden_hits": result.get("forbidden_hits", []),
            }}


async def _publish_conflict(rec: Dict[str, Any], cookie_str: str, cookie_id: str,
                            title: str, price: float) -> Dict[str, Any]:
    """「这条内容已经提交过」的统一响应（只读预检与并发兜底共用）。只读，不再提交。"""
    status = str(rec.get("status") or "")
    if status == "submission_unknown":
        from starlette.concurrency import run_in_threadpool
        from shop_diagnosis import fetch_my_items

        try:
            listed = await run_in_threadpool(
                fetch_my_items, cookie_str, str(config.get("APP_CONFIG.app_key", "")),
                cookie_id=cookie_id, max_pages=2)
            matches = [
                {"item_id": item["item_id"], "title": item["title"]}
                for item in listed["items"]
                if item["title"].strip() == title and item["price"] == price
            ] if listed["ok"] else []
            readback = {"ok": listed["ok"], "matches": matches}
        except Exception:
            readback = {"ok": False, "matches": []}
        return {"ok": False,
                "error": "上一次相同内容的发布结果未知（可能已提交成功）。请先到闲鱼「我发布的」核对；"
                         "确认没有重复上架后再重新发布。",
                "status": "submission_unknown",
                "requires_readback": True, "readback": readback}
    return {"ok": False,
            "error": f"这条内容已经提交过（状态：{status}）。如需重复上架请先修改标题或图片。",
            "status": status,
            "requires_readback": False}


@router.post("/api/opportunity/publish")
async def opportunity_publish(
    body: Dict[str, Any] = Body(...),
    current_user: Dict[str, Any] = Depends(require_authenticated_user),
):
    """
    一键发布到闲鱼（goofish-cli 验证链路：图片上传 CDN → 类目推荐 → 默认地址 → 提交）。
    请求: {
      title, desc/description, imageUrls: [本地路径 | http URL | 回执dict], price, quantity,
      delivery: "无需邮寄"|"包邮"|"按距离计费"|"一口价", postPrice,
      cookie_id: "", category: {catId,catName,channelCatId,tbCatId}(可选，留空走AI推荐)
    }
    """
    cookie_id = str(body.get("cookie_id") or "").strip()
    if not cookie_id:
        return {"ok": False, "error": "发布账号未指定"}

    ok, reason = _night_ban_ok()
    if not ok:
        return {"ok": False, "error": reason}

    cookie_str = _resolve_cookie(cookie_id, current_user["user_id"])
    if not cookie_str or not extract_token_from_cookie(cookie_str):
        return {"ok": False,
                "error": f"账号 {cookie_id} 的 Cookie 中缺少 {TOKEN_COOKIE_NAME}，请重新登录闲鱼"}

    title = str(body.get("title") or "").strip()
    desc = str(body.get("desc") or body.get("description") or "").strip()
    images = body.get("imageUrls") or []
    if not title:
        return {"ok": False, "error": "发布标题不能为空"}

    # 若未提供配图，自动调用智能生图引擎补齐主图（解决无图无法发布的痛点）
    if not images:
        try:
            from image_generator import generate_product_images
            auto_imgs = generate_product_images(
                title=title,
                price=float(body.get("price") or 0),
                tags=body.get("tags") or [],
                count=1,
            )
            if auto_imgs:
                images = [auto_imgs[0]["path"]]
        except Exception as _gen_err:
            pass

    if not images:
        return {"ok": False, "error": "发布至少需要一张主图（本地路径或图片 URL）"}

    # 发布前合规闸（P0）：BLOCK 级词命中直接拒绝，WARN/CATEGORY 级随响应返回给前端提示。
    # 之前这条路径上是零校验——AI 改写脱敏只作用于 /rewrite，用户手工改回来或从模板库
    # 直接发布都能绕过。词表口径来自 148 份闲鱼实操文档的共识（微信=导流违禁、100%=极限词、
    # 高仿/A货=售假），详见 publish_guard.py 顶部说明。
    guard = check_publish(title, desc, body.get("tags") or [], images=len(images))
    if not guard["ok"]:
        return {"ok": False,
                "error": "内容含平台违禁/导流词，已阻止发布：" + "、".join(guard["block"]),
                "forbidden_hits": guard["block"]}

    item_data = {
        "title": title,
        "desc": desc,
        "imageUrls": images,
        "price": float(body.get("price") or 1),
        "quantity": int(body.get("quantity") or 1),
        "delivery": str(body.get("delivery") or "无需邮寄"),
        "postPrice": float(body.get("postPrice") or 0),
    }
    if body.get("category"):
        item_data["category"] = body["category"]

    # B5 顺序：**只读预检 → 取配额 → 原子 claim**。
    # 幂等键先算：同一账号 + 同标题/价格/描述视为同一次提交。
    # 若上一次是 submission_unknown（断网/超时，平台可能已收），禁止自动重发。
    idem_key = idempotency.make_key("publish", cookie_id, title, item_data["price"], desc)
    pre = idempotency.precheck(idem_key)
    if pre.get("exists"):
        # 重复点击不该白吃 60 秒配额，也不该把"已经提交过"报成"限流"。
        return await _publish_conflict(pre.get("record") or {}, cookie_str, cookie_id,
                                       title, item_data["price"])

    # A2 统一限流（落盘，重启不清零）：发布 1 次/60s/账号 + 日上限 200
    ok_rate, retry = rate_limiter.acquire("publish", cookie_id, limit=1,
                                          window=PUBLISH_MIN_INTERVAL_SECONDS)
    if not ok_rate:
        # 并发双提交：两个请求同时过了只读预检，赢家是在这之后才 claim 成功的。
        # 失败方若只报"限流"，用户会以为是手太快、等一分钟再点——而这正是 B5
        # 要消灭的错误原因。只读再看一眼：这一行现在有了就报"已经提交过"。
        again = idempotency.precheck(idem_key)
        if again.get("exists"):
            return await _publish_conflict(again.get("record") or {"status": "pending"},
                                           cookie_str, cookie_id, title, item_data["price"])
        return {"ok": False,
                "error": f"限流：该账号发布过于频繁，请 {retry} 秒后再试（发布 1 次/分钟、每日 200 次上限）"}

    # 原子 claim：并发下第二个请求在这里被挡住（预检只是提前说清楚，不替代它）。
    claim = idempotency.claim(idem_key, "publish", note=title[:80])
    if claim.get("blocked") or not claim.get("claimed"):
        return await _publish_conflict(claim.get("record") or {"status": "pending"},
                                       cookie_str, cookie_id, title, item_data["price"])

    try:
        app_key = str(config.get('APP_CONFIG.app_key', '{{SECRET_jm434om}}'))
        result = publish_item(cookie_str, item_data, app_key, cookie_id=cookie_id)
        idempotency.finish(idem_key, "done", note=str(result.get("itemId", ""))[:80])
        _save_history([{
            "time": datetime.now().isoformat(),
            "keyword": f"publish:{cookie_id}",
            "total": 1,
            "top_titles": [title[:40]],
            "pub_itemId": result.get("itemId", ""),
        }])
        return {"ok": True,
                "itemId": result.get("itemId", ""),
                "status": result.get("status", "submitted"),
                "requires_readback": True,
                "warnings": guard.get("warnings") or [],
                "message": "发布已受理，请到闲鱼「我发布的」核对商品是否上架"}
    except Exception as e:
        # 风控/登录失效 → 同步写入搜索守卫（同一账号冻结，防止连续硬撞）
        err_text = str(e)
        guard_extra: Dict[str, Any] = {}
        if isinstance(e, (PublishRiskError, PublishAuthError)):
            failure = record_failure(err_text)
            guard_extra = {"guard": {**get_guard_status(),
                                     "risk_signal": failure.get("risk_signal")}}
        status = getattr(e, "submission_status", "") or (
            "submission_unknown" if "结果未知" in err_text else "")
        # 幂等表记录结局：未知 → 永久禁重发（人工回读后 resolve）；其余失败 → 释放可重试
        try:
            if status == "submission_unknown":
                idempotency.finish(idem_key, "submission_unknown", note=err_text[:200])
            else:
                idempotency.release(idem_key)
        except Exception:
            pass
        if status == "submission_unknown":
            _save_history([{
                "time": datetime.now().isoformat(),
                "keyword": f"publish-unknown:{cookie_id}",
                "total": 1,
                "top_titles": [title[:40]],
                "pub_itemId": "",
            }])
        # 发布失败/未知 → 推送告警（风控与"结果未知"必须人工介入，其余失败也让用户知道）
        try:
            from notify_service import send_event
            if isinstance(e, (PublishRiskError, PublishAuthError)) or status == "submission_unknown":
                send_event("publish", "闲鱼发布告警",
                           f"账号：{cookie_id}\n标题：{title[:40]}\n"
                           f"阶段：{getattr(e, 'stage', '')}\n状态：{status or 'rejected'}\n"
                           f"详情：{err_text[:300]}",
                           level="error" if status == "submission_unknown" else "warn")
        except Exception:
            pass
        return {"ok": False, "error": err_text[:400],
                "stage": getattr(e, "stage", ""),
                "status": status,
                "requires_readback": status == "submission_unknown", **guard_extra}



@router.get("/api/opportunity/history")
async def opportunity_history():
    """最近 20 条选品记录。"""
    try:
        records = _load_history()
        return {"ok": True, "records": records[-20:]}
    except Exception as e:
        return {"ok": False, "error": str(e), "records": []}


@router.get("/api/opportunity/price-history")
async def opportunity_price_history(keyword: str = "", days: int = 30):
    """关键词价格历史：市场水位（最低/P25/中位/均价）+ 发布定价建议。"""
    try:
        return keyword_summary(keyword, days)
    except Exception as e:
        return {"ok": False, "error": f"价格历史读取失败：{str(e)[:160]}"}


@router.post("/api/opportunity/vision")
async def opportunity_vision(body: Dict[str, Any] = Body(...)):
    """
    AI 看图：分析单个商品图片（成色/图文相符/仿货风险），返回结构化结论 + 评分修正。
    请求: {item_id, title, price, imageUrl | imageUrls: [], use_cache?}
    """
    image_urls = body.get("imageUrls") or ([body.get("imageUrl")] if body.get("imageUrl") else [])
    try:
        result = analyze_images(
            str(body.get("item_id") or ""),
            str(body.get("title") or ""),
            body.get("price"),
            [str(u) for u in image_urls if u],
            use_cache=bool(body.get("use_cache", True)),
        )
    except Exception as e:
        result = {"ok": False, "analysis": None, "cached": False,
                  "images_used": 0, "error": f"看图分析异常：{str(e)[:160]}"}
    return result


@router.get("/api/opportunity/idempotency")
async def opportunity_idempotency(action: str = "", limit: int = 30):
    """最近的操作幂等记录（发布/发货等），用于排查"为什么没让我重发"。"""
    try:
        records = idempotency.recent(limit=limit, action=action)
        unknown = [r for r in records if r.get("status") == "submission_unknown"]
        return {"ok": True, "records": records,
                "unknown": unknown,
                "verify_log": idempotency.verify_log(limit=30) if unknown else [],
                "stats": idempotency.stats()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], "records": []}


@router.post("/api/opportunity/idempotency/verify")
async def opportunity_idempotency_verify(body: Dict[str, Any] = Body(...)):
    """
    未知发布的人工核对出口（单条）。操作员必须明确选择核对结果并填理由：
      confirmed / not_received / still_unknown
    原发布记录、核对结果、理由与时间都会写入 op_verify_log 留痕。
    同标题同价格在页面上只作辅助，不在这里自动判定。
    """
    key = str(body.get("key") or "").strip()
    outcome = str(body.get("outcome") or "").strip()
    reason = str(body.get("reason") or "").strip()
    matched_item_id = str(body.get("matched_item_id") or "").strip()
    if not key:
        return {"ok": False, "error": "缺少 key"}
    return idempotency.verify_unknown(key, outcome, reason=reason,
                                      matched_item_id=matched_item_id)


@router.get("/api/opportunity/idempotency/verify-log")
async def opportunity_idempotency_verify_log(key: str = "", limit: int = 30):
    """人工核对留痕（可按 key 查单条）。"""
    try:
        return {"ok": True, "records": idempotency.verify_log(key=key, limit=limit)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], "records": []}


@router.post("/api/opportunity/idempotency/release")
async def opportunity_idempotency_release(body: Dict[str, Any] = Body(...)):
    """人工核对平台确实没收到后，释放幂等键允许重发。"""
    key = str(body.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "缺少 key"}
    return {"ok": idempotency.release(key), "key": key}


@router.get("/api/opportunity/guard-status")
async def opportunity_guard_status():
    """风控守卫状态（冷却/连续失败/最近错误）+ 限流占用。"""
    return {"ok": True, **get_guard_status(), "rate_limit": rate_limiter.status()}


@router.post("/api/opportunity/guard-reset")
async def opportunity_guard_reset():
    """手动解除搜索冷却。"""
    reset_guard()
    return {"ok": True, "message": "冷却已解除，可以继续搜索"}


@router.post("/api/opportunity/generate-image")
async def opportunity_generate_image(body: Dict[str, Any] = Body(...)):
    """
    智能商品配图接口。
    输入:
      - title: 商品标题 (必填)
      - price: 价格 (可选，用于卡片价格标识)
      - tags: 卖点标签 (可选)
      - mode: "auto" | "canvas" | "ai" (默认 "auto")
      - count: 1 (仅主图) 或 3 (主图+流程图+质保图)
    返回:
      - ok: True/False
      - images: 详细图片对象列表
      - paths: 本地绝对路径列表（可直接传给 publish 接口）
      - urls: 静态预览 URL 列表
    """
    title = str(body.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "请提供商品标题以生成对应配图"}

    price = float(body.get("price") or 0)
    tags = body.get("tags") or []
    mode = str(body.get("mode") or "auto").strip().lower()
    count = int(body.get("count") or 1)

    try:
        from image_generator import generate_product_images
        imgs = generate_product_images(title=title, price=price, tags=tags, mode=mode, count=count)
        if not imgs:
            return {"ok": False, "error": "生成配图失败，请稍后重试"}

        paths = [img["path"] for img in imgs]
        urls = [img["url"] for img in imgs]

        return {
            "ok": True,
            "images": imgs,
            "paths": paths,
            "urls": urls,
            "primary_path": paths[0] if paths else "",
            "primary_url": urls[0] if urls else "",
            "message": f"成功生成 {len(imgs)} 张商品配图",
        }
    except Exception as e:
        return {"ok": False, "error": f"生成配图异常: {str(e)[:160]}"}


@router.post("/api/monitor/evaluate")
async def monitor_evaluate_item(body: Dict[str, Any] = Body(...)):
    """
    多模态 AI 捡漏与商品价值评估接口 (吸收 Usagi-org/ai-goofish-monitor 决策链)。
    输入:
      - item_id: 商品ID (必填)
      - title: 标题 (必填)
      - price: 当前标价 (必填)
      - description: 描述 (可选)
      - image_urls: 闲鱼实物图 URL 列表 (可选，传入即激活 Vision 多模态视觉审图)
    """
    item_id = str(body.get("item_id") or "").strip()
    title = str(body.get("title") or "").strip()
    if not item_id or not title:
        return {"ok": False, "error": "缺少 item_id 或 title"}

    price = float(body.get("price") or 0.0)
    description = str(body.get("description") or "").strip()
    image_urls = body.get("image_urls") or []

    try:
        from ai_vision_evaluator import AIVisionEvaluator
        evaluator = AIVisionEvaluator()
        result = evaluator.evaluate_item(
            item_id=item_id,
            title=title,
            price=price,
            description=description,
            image_urls=image_urls,
        )
        return {"ok": True, "data": result.to_dict()}
    except Exception as e:
        return {"ok": False, "error": f"评估异常: {str(e)[:160]}"}


@router.post("/api/monitor/relist-posters")
async def monitor_relist_posters(body: Dict[str, Any] = Body(...)):
    """
    结合 Craft-Anything 电商视觉海报引擎:
    为已检出的捡漏商品一键生成 3 张闲鱼高清转售海报 (主图 + 流程图 + 质保图)。
    """
    title = str(body.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "缺少商品标题"}

    price = float(body.get("price") or 0.0)
    tags = body.get("tags") or ["官方正品", "极速发货", "实拍验货"]

    try:
        from image_generator import generate_product_images
        imgs = generate_product_images(title=title, price=price, tags=tags, mode="auto", count=3)
        if not imgs:
            return {"ok": False, "error": "生成海报失败"}

        return {
            "ok": True,
            "images": imgs,
            "paths": [img["path"] for img in imgs],
            "urls": [img["url"] for img in imgs],
            "message": f"成功基于 Craft-Anything 为商品生成 {len(imgs)} 张转售视觉图",
        }
    except Exception as e:
        return {"ok": False, "error": f"转售生图异常: {str(e)[:160]}"}
