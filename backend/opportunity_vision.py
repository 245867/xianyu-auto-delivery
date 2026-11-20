"""
AI 看图分析（补齐④）—— 思路移植自 Usagi-org/ai-goofish-monitor
`src/ai_handler.py::get_ai_analysis`（MIT）：下载商品图 → base64 → 多模态模型 →
严格 JSON 结论。本地按 OpenAI chat-completions 协议实现，复用 AI_PROVIDER 配置
（唯一来源 global_config.yml，与 AI 改写一致的填写方式）。

输出约定（模型必须返回 JSON）：
{
  "condition_score": 0-10  （成色/品相，10 最好）,
  "match_score": 0-10      （图片与标题描述相符度）,
  "authenticity_risk": "low"|"medium"|"high"（图文夸大/货不对板/仿货风险）,
  "comment": "一句话结论（≤60字）",
  "resale_advice": "转售/选品建议（≤60字）"
}

风控与成本控制：
- 最多 3 张图、单图 ≤8MB、总下载超时 20s；
- 结果按 (item_id + 图片) 缓存 24h（data/vision_cache.json），重复查看不重复计费；
- 任一图片下载失败不影响其余图片；全部失败则明确报错，不臆造结论。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

MAX_IMAGES = 3
MAX_IMAGE_BYTES = 8 * 1024 * 1024
CACHE_TTL_SECONDS = 24 * 3600

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = PROJECT_ROOT / "data" / "vision_cache.json"

SYSTEM_PROMPT = (
    "你是闲鱼二手商品图片审核助手。只根据图片和文字信息做客观判断，不得编造图中不存在的信息；"
    "无法判断的字段给中间值（5 分 / medium）。只输出一个 JSON 对象，不要输出其他内容。"
)

USER_PROMPT = (
    "商品标题：{title}\n标价：{price}\n请分析这些商品图片，并按以下 JSON 结构输出：\n"
    '{{"condition_score": <0-10整数>, "match_score": <0-10整数>, '
    '"authenticity_risk": "<low|medium|high>", '
    '"comment": "<一句话结论，不超过60字>", '
    '"resale_advice": "<对选品/转售的建议，不超过60字>"}}\n'
    "评分标准：condition_score 看划痕/磨损/使用痕迹；match_score 看图与标题描述是否一致；"
    "authenticity_risk 看是否存在盗图、夸大、疑似仿品或货不对板的迹象。"
)


def _load_ai_settings() -> Dict[str, Any]:
    """复用改写的配置读取（同一 AI_PROVIDER 节）。"""
    from opportunity_rewrite import _load_ai_settings as _load
    return _load()


def _cache_load() -> Dict[str, Any]:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _cache_save(cache: Dict[str, Any]) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        cache = {k: v for k, v in cache.items()
                 if isinstance(v, dict) and now - float(v.get("_ts", 0)) < CACHE_TTL_SECONDS}
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


def _fetch_image_data_url(url: str) -> Optional[str]:
    """下载图片 → data URL。失败返回 None（不抛，逐图容错）。"""
    try:
        resp = requests.get(url, timeout=20, stream=True,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        resp.raise_for_status()
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            return None
        buf = b""
        for chunk in resp.iter_content(65536):
            buf += chunk
            if len(buf) > MAX_IMAGE_BYTES:
                return None
        if not buf:
            return None
        return f"data:{ctype};base64,{base64.b64encode(buf).decode('ascii')}"
    except Exception:
        return None


def _parse_json_content(content: str) -> Optional[Dict[str, Any]]:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def _normalize_analysis(data: Dict[str, Any]) -> Dict[str, Any]:
    def _clamp10(v: Any, default: float = 5.0) -> float:
        try:
            n = float(v)
        except (TypeError, ValueError):
            return default
        return max(0.0, min(10.0, n))

    risk = str(data.get("authenticity_risk") or "medium").lower()
    if risk not in ("low", "medium", "high"):
        risk = "medium"
    return {
        "condition_score": round(_clamp10(data.get("condition_score")), 1),
        "match_score": round(_clamp10(data.get("match_score")), 1),
        "authenticity_risk": risk,
        "comment": str(data.get("comment") or "")[:120],
        "resale_advice": str(data.get("resale_advice") or "")[:120],
    }


def vision_bonus_score(analysis: Dict[str, Any]) -> float:
    """看图结论 → 评分修正（-10 ~ +10，供分析页合并）。"""
    cond = float(analysis.get("condition_score") or 5)
    match = float(analysis.get("match_score") or 5)
    risk = analysis.get("authenticity_risk") or "medium"
    risk_penalty = {"low": 0.0, "medium": 3.0, "high": 8.0}.get(risk, 3.0)
    return round((cond - 5) * 2 + (match - 5) * 1 - risk_penalty, 1)


def analyze_images(item_id: str, title: str, price: Any,
                   image_urls: List[str],
                   use_cache: bool = True) -> Dict[str, Any]:
    """
    看图入口。返回 {"ok", "analysis", "cached", "error", "images_used"}。
    """
    urls = [str(u).strip() for u in (image_urls or []) if str(u).strip()][:MAX_IMAGES]
    if not urls:
        return {"ok": False, "error": "该商品没有可用图片", "analysis": None,
                "cached": False, "images_used": 0}

    settings = _load_ai_settings()
    if not settings.get("api_url") or not settings.get("api_key") or not settings.get("model"):
        return {"ok": False, "analysis": None, "cached": False, "images_used": 0,
                "error": "AI 服务未配置：请在 global_config.yml 的 AI_PROVIDER 节填写 api_url/api_key/model"}
    if settings.get("wire_api") == "responses":
        return {"ok": False, "analysis": None, "cached": False, "images_used": 0,
                "error": "当前 AI_PROVIDER.wire_api=responses，看图分析需 chat 协议（OpenAI 兼容 /v1/chat/completions）"}

    cache_key = hashlib.sha256(
        (str(item_id) + "|" + "|".join(urls)).encode("utf-8")).hexdigest()[:32]
    cache = _cache_load()
    if use_cache and cache_key in cache:
        entry = cache[cache_key]
        if time.time() - float(entry.get("_ts", 0)) < CACHE_TTL_SECONDS:
            return {"ok": True, "analysis": entry.get("analysis"),
                    "cached": True, "images_used": entry.get("images_used", len(urls)),
                    "error": ""}

    data_urls = []
    for u in urls:
        d = _fetch_image_data_url(u)
        if d:
            data_urls.append(d)
    if not data_urls:
        return {"ok": False, "analysis": None, "cached": False, "images_used": 0,
                "error": "图片全部下载失败（可能已下架或需要登录），无法看图"}

    user_content: List[Dict[str, Any]] = [{
        "type": "text",
        "text": USER_PROMPT.format(title=str(title)[:200],
                                   price=price if price not in (None, "") else "未知"),
    }]
    for d in data_urls:
        user_content.append({"type": "image_url", "image_url": {"url": d}})

    headers = {"Authorization": f"Bearer {settings['api_key']}",
               "Content-Type": "application/json"}
    try:
        resp = requests.post(settings["api_url"], headers=headers, json={
            "model": settings["model"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.2,
            "max_tokens": 500,
        }, timeout=settings.get("timeout") or 90)
        payload = resp.json()
    except Exception as e:
        return {"ok": False, "analysis": None, "cached": False,
                "images_used": len(data_urls), "error": f"AI 调用失败：{e}"}

    content = ""
    choices = payload.get("choices") or []
    if choices:
        content = ((choices[0].get("message") or {}).get("content") or "").strip()
    if resp.status_code != 200 or not content:
        return {"ok": False, "analysis": None, "cached": False,
                "images_used": len(data_urls),
                "error": f"AI 返回异常 status={resp.status_code} resp={str(payload)[:200]}"}

    parsed = _parse_json_content(content)
    if not parsed:
        return {"ok": False, "analysis": None, "cached": False,
                "images_used": len(data_urls),
                "error": f"AI 输出不是合法 JSON：{content[:200]}"}

    analysis = _normalize_analysis(parsed)
    analysis["bonus"] = vision_bonus_score(analysis)

    cache[cache_key] = {"_ts": time.time(), "analysis": analysis,
                        "images_used": len(data_urls)}
    _cache_save(cache)
    return {"ok": True, "analysis": analysis, "cached": False,
            "images_used": len(data_urls), "error": ""}


__all__ = ["analyze_images", "vision_bonus_score", "MAX_IMAGES"]
