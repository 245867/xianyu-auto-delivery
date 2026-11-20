"""
AI 文案改写（选品复刻）—— 移植自 dameng2026/xianyu-pilot-pro
`app/services/ai_provider.py` 的润色限制 + `routes/opportunity.py` 的改写路由
（Apache-2.0）。

安全设计（上游硬约束，必须保留）：
1. 系统提示注入「润色关键词/禁止关键词」限制（本机内置默认集，后台配置不可绕过）；
2. 输出经 enforce_polish_restriction 过滤后落库（命中禁止词则脱敏替换并标记）；
3. 与原文相似度 < 80% 标记「需人工复核」——产出仍走人工确认，不做无人值守直发。

调用协议：读取 global_config.yml 的 AI_PROVIDER 节（model/api_url/api_key/wire_api），
兼容 OpenAI chat-completions 与 wire_api=responses 两种协议（与简易回复引擎一致的
调用约定，便于用户只填一处）。
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

from opportunity_engine import SENSITIVE_WORDS

# 平台违规导流/夸大宣传词：与 SENSITIVE_WORDS 一起构成润色禁止集
POLISH_FORBIDDEN_EXTRA = [
    "微信", "VX", "vx", "QQ", "qq", "站外", "私下交易", "代充",
    "包成功", "稳赚", "100%",
]

DEFAULT_FORBIDDEN = list(sorted(set(SENSITIVE_WORDS) | set(POLISH_FORBIDDEN_EXTRA)))


def _load_ai_settings() -> Dict[str, Any]:
    """从 global_config.yml 读取 AI 服务配置（唯一来源，不许 DB 存 Key）。"""
    section = {}
    try:
        from config import config  # backend/config.py
        section = config.get("AI_PROVIDER", {}) or {}
    except Exception:
        section = {}
    settings = {
        "api_url": str(section.get("api_url") or "").rstrip("/"),
        "api_key": str(section.get("api_key") or ""),
        "model": str(section.get("model") or section.get("provider_model") or ""),
        "provider_model": str(section.get("provider_model") or ""),
        "wire_api": str(section.get("wire_api") or "chat").lower(),
        "timeout": int(section.get("request_timeout_seconds") or 60),
    }
    return settings


def generate_text(scene: str,
                  system_prompt: str,
                  user_prompt: str,
                  temperature: float = 0.7,
                  max_tokens: int = 600) -> Dict[str, Any]:
    """
    通用文本生成（本地复刻版，无计费）：返回
    {"ok": bool, "content": str, "error": str, "provider": str, "model": str}
    """
    settings = _load_ai_settings()
    if not settings["api_url"] or not settings["api_key"] or not settings["model"]:
        return {"ok": False, "content": "", "error": "AI 服务未配置：请在 global_config.yml 的 AI_PROVIDER 节填写 api_url/api_key/model",
                "provider": "", "model": ""}

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    headers = {"Authorization": f"Bearer {settings['api_key']}",
               "Content-Type": "application/json"}
    url = settings["api_url"]

    try:
        if settings["wire_api"] == "responses":
            resp = requests.post(
                url,
                headers=headers,
                json={
                    "model": settings["provider_model"] or settings["model"],
                    "input": messages,
                    "instructions": system_prompt,
                    "max_output_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=settings["timeout"],
            )
            data = resp.json()
            output_text = data.get("output_text", "")
            if not output_text:
                parts = []
                for out in data.get("output", []) or []:
                    for content in out.get("content", []) or []:
                        if content.get("text"):
                            parts.append(content["text"])
                output_text = "\n".join(parts)
            content = output_text.strip()
        else:
            resp = requests.post(
                url,
                headers=headers,
                json={
                    "model": settings["model"],
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                timeout=settings["timeout"],
            )
            data = resp.json()
            choices = data.get("choices") or []
            content = ""
            if choices:
                content = (choices[0].get("message", {}) or {}).get("content", "").strip()

        if resp.status_code != 200 or not content:
            return {"ok": False, "content": "",
                    "error": f"AI 返回异常 status={resp.status_code} resp={str(data)[:300]}",
                    "provider": "", "model": settings["model"]}
        return {"ok": True, "content": content, "error": "",
                "provider": "default", "model": settings["model"]}
    except Exception as e:
        return {"ok": False, "content": "", "error": f"AI 调用失败: {e}",
                "provider": "", "model": ""}


def mask_forbidden_keywords(text: str, keywords: List[str]) -> str:
    """把命中的禁止词逐字脱敏（保留长度，不泄露原词），与上游 mask 行为一致。"""
    if not text:
        return ""
    for kw in keywords:
        if kw and kw in text:
            text = text.replace(kw, "*" * len(kw))
    return text


def enforce_polish_restriction(title: str, body: str) -> Tuple[str, str, List[str]]:
    """
    上游同款润色强限制：返回 (脱敏后标题, 脱敏后正文, 命中的禁止词列表)。
    """
    hits = [kw for kw in DEFAULT_FORBIDDEN if kw and (kw in (title or "") or kw in (body or ""))]
    clean_title = mask_forbidden_keywords(title or "", DEFAULT_FORBIDDEN)
    clean_body = mask_forbidden_keywords(body or "", DEFAULT_FORBIDDEN)
    return clean_title.strip(), clean_body.strip(), hits


def _parse_ai_output(content: str, source_title: str,
                     source_description: str) -> Dict[str, Any]:
    """
    解析 AI 输出：
    - 第一行视为标题（≤30 字，超长截断）
    - 其余行视为描述；若含 #  标签行，拆为 tags
    """
    lines = [line.strip() for line in (content or "").splitlines() if line.strip()]
    title = ""
    desc_lines: List[str] = []
    tags: List[str] = []
    if lines:
        title = lines[0].lstrip("# ").strip()[:30]
    if not title:
        title = source_title[:30]
    for line in lines[1:]:
        tag_match = re.match(r"^#\s*(.{1,20})$", line)
        if tag_match and len(tags) < 6:
            tags.append(tag_match.group(1).strip())
        elif not line.startswith("#"):
            desc_lines.append(line)
    description = "\n".join(desc_lines).strip()
    if not description:
        description = source_description

    # 相似度检查（字符级，与原文比不应差太多）
    a = source_title + source_description
    b = title + description
    ratio = difflib.SequenceMatcher(None, a, b).ratio() if (a and b) else 0.0

    return {
        "title": title,
        "description": description,
        "tags": tags,
        "similarity": round(ratio, 2),
    }


def rewrite_item(title: str, description: str,
                 style: str = "friendly",
                 custom_prompt: str = "",
                 keyword: str = "") -> Dict[str, Any]:
    """
    复刻 opportunity/rewrite 路由：
    - 默认 instruction：标题单独第一行（≤30字）+ 第二行起改写描述 + 保留大核心卖点
    - system prompt 注入润色限制
    - 输出 enforce_polish_restriction + 相似度 < 0.8 标记需人工复核
    """
    source_title = (title or "").strip()
    source_description = (description or "").strip()
    if not source_title:
        return {"ok": False, "error": "请选择需要改写的商品"}

    if custom_prompt:
        instruction = custom_prompt
    else:
        instruction = (
            "请根据原标题和正文，为闲鱼二手商品改写一版可发布标题、商品描述和3-5个标签。"
            "要求：1⃣ 标题单独放在第一行，不超过30个字，简洁吸引人；"
            "2⃣ 正文从第二行开始，保留核心商品信息和卖点但重新表述；"
            "3⃣ 与原内容相似度在80%以上；除 # 开头外不输出多余内容；"
            "4⃣ 真实、不夸大、不承诺站外交易、不包含违禁词；发布前仍需人工复核。"
        )

    system_prompt = (
        "你是二手电商商品文案助手，只输出合法合规、可人工编辑的中文文案。\n"
        "禁止出现以下词汇：{forbidden}"
    ).format(forbidden="、".join(DEFAULT_FORBIDDEN))

    prompt = (
        "{instruction}\n风格={style}；\n原标题={title}；\n原文案={desc}\n商品来源关键词={kw}"
    ).format(instruction=instruction, style=style, title=source_title,
             desc=source_description[:2000], kw=keyword or "-")

    ai = generate_text("opportunity_rewrite", system_prompt, prompt, 0.7)
    if not ai.get("ok"):
        return {"ok": False, "error": ai.get("error") or "AI 调用失败"}

    parsed = _parse_ai_output(ai.get("content", ""), source_title, source_description)
    clean_title, clean_body, hits = enforce_polish_restriction(
        parsed["title"], parsed["description"])

    return {
        "ok": True,
        "title": clean_title,
        "description": clean_body,
        "tags": parsed["tags"],
        "similarity": parsed["similarity"],
        "needs_review": parsed["similarity"] < 0.8 or bool(hits),
        "forbidden_hits": hits,
        "raw": ai.get("content", ""),
        "model": ai.get("model", ""),
    }


__all__ = ["rewrite_item", "generate_text", "enforce_polish_restriction",
           "mask_forbidden_keywords", "DEFAULT_FORBIDDEN"]
