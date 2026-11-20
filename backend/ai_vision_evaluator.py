# -*- coding: utf-8 -*-
"""
多模态 AI 捡漏与商品价值评估决策引擎 (AI Vision Evaluator)

借鉴项目:
1. Usagi-org/ai-goofish-monitor: 多任务并发监控与视觉大模型审图决策链 (成色/瑕疵/真实转手利润)
2. Craft-Anything (电商视觉生成): 检出高价值捡漏商品后，一键生成去水印全套 800x800 高清转售海报与质保图
3. 严格遵循 REA (Reverse Engineering Analysis) 规范: 契约自洽、JSON 鲁棒解析、纯文本/多模态无缝降级
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import requests
from loguru import logger

# 引入已有的 Craft-Anything 电商海报生成引擎
try:
    from image_generator import generate_product_images
except ImportError:
    try:
        from backend.image_generator import generate_product_images
    except ImportError:
        generate_product_images = None


@dataclass
class RelistingAdvice:
    """转售建议（基于 Craft-Anything 与爆款文案方法论）"""
    recommended_title: str
    recommended_price: float
    recommended_tags: List[str] = field(default_factory=list)
    selling_points: List[str] = field(default_factory=list)


@dataclass
class ItemEvaluationResult:
    """多模态商品价值与捡漏评估结果"""
    item_id: str
    title: str
    current_price: float
    condition: str  # 全新/未拆封, 99新, 95新, 9成新以下, 故障/配件机
    has_defects: bool
    defect_details: List[str]
    estimated_market_price: float
    estimated_profit: float
    profit_rate: float
    verdict: Literal["strong_buy", "buy", "neutral", "avoid"]  # 强烈推荐捡漏, 建议捡漏, 观望, 避坑
    confidence: float  # 0.0 ~ 1.0
    summary: str
    has_visual_inspection: bool = False
    relisting_advice: Optional[RelistingAdvice] = None
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    generated_posters: List[str] = field(default_factory=list)  # 生成的转售图路径

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


PROMPT_VISION_EVALUATE = """你是一名资深的闲鱼二手电商专家与数码/奢侈品/消费品专业鉴定师。
请仔细阅读商品信息并观察图片（如有），对该商品进行专业的「捡漏价值评估」与「转手利润测算」。

【商品信息】
- 商品ID: {item_id}
- 商品标题: {title}
- 当前标价: {price} 元
- 卖家描述: {description}

【鉴定与评估要求】
1. 成色判断: 仔细辨别商品是全新/99新/95新/伊拉克成色/故障机。注意是否有暗病描述、发票配件是否齐全。
2. 市场二手行情: 结合当前闲鱼/转转市场的真实正常成交行情，给出该成色下的合理市场均价（单位: 元）。
3. 利润测算: 预估毛利润 = 预估市场均价 - 卖家标价 - 10元预估运费与损耗。利润率 = 预估毛利润 / 卖家标价。
4. 捡漏决策 (verdict):
   - strong_buy (强烈推荐捡漏): 真实利润率 >= 35% 且成色好、无明显暗病风险；
   - buy (建议捡漏): 真实利润率在 15% ~ 35% 之间；
   - neutral (观望): 价格处于正常市场区间，无明显利润空间；
   - avoid (避坑/翻车): 疑似山寨假货、严重暗病、钓鱼引流贴、商家批量刷单贴或标价虚低实则到付等。
5. 转售建议 (relisting_advice): 如果决定捡漏转售，给出一个更具吸引力、符合闲鱼 SEO 推荐算法的转售标题、建议定价与 3 个提炼卖点。

【重要】请严格以 JSON 格式输出，不要附加任何前缀或寒暄说明，JSON 结构如下:
{{
  "condition": "95新/轻微使用",
  "has_defects": false,
  "defect_details": [],
  "estimated_market_price": 500.0,
  "verdict": "strong_buy",
  "confidence": 0.9,
  "summary": "成色极佳且配件齐全，卖家急售，低于二手行情150元，具备极高转手利润空间。",
  "relisting_advice": {{
    "recommended_title": "转手爆款标题...",
    "recommended_price": 480.0,
    "recommended_tags": ["正品包邮", "带发票", "成色极新"],
    "selling_points": ["官方在保", "全套箱说全", "无任何划痕暗病"]
  }}
}}
"""


class AIVisionEvaluator:
    """多模态商品价值评估与转售生成器"""

    def __init__(self, config_path: Optional[str] = None):
        self.config = self._load_config(config_path)

    def _load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        root = Path(__file__).resolve().parent.parent
        target_path = Path(config_path) if config_path else root / "global_config.yml"
        if not target_path.exists():
            return {}
        try:
            import yaml
            with open(target_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"读取配置失败 ({target_path}): {e}")
            return {}

    def _extract_json_payload(self, text: str) -> Optional[Dict[str, Any]]:
        """从大模型原始文本中提取标准的 JSON 字典，防御各种 Markdown 标记和冗余文本"""
        if not text:
            return None
        text = text.strip()
        # 优先提取 ```json ... ``` 代码块
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            candidate = match.group(1)
            try:
                return json.loads(candidate)
            except Exception:
                pass

        # 其次尝试直接提取第一个花括号对
        brace_match = re.search(r"(\{.*\})", text, re.DOTALL)
        if brace_match:
            candidate = brace_match.group(1)
            try:
                return json.loads(candidate)
            except Exception:
                pass

        # 最后直接解析
        try:
            return json.loads(text)
        except Exception:
            return None

    def evaluate_item(
        self,
        item_id: str,
        title: str,
        price: float,
        description: str = "",
        image_urls: Optional[List[str]] = None,
        mock_response: Optional[Dict[str, Any]] = None,
    ) -> ItemEvaluationResult:
        """
        对指定商品执行多模态/文本捡漏价值评估。
        支持 mock_response 用于单元测试和脱机自测。
        """
        image_urls = image_urls or []
        has_images = len(image_urls) > 0

        # 测试模式直接使用 mock
        if mock_response is not None:
            raw_eval = mock_response
        else:
            raw_eval = self._call_llm_evaluation(
                item_id=item_id,
                title=title,
                price=price,
                description=description,
                image_urls=image_urls,
            )

        # 降级防御保障
        if not raw_eval:
            logger.warning(f"AI 评估未返回有效数据，触发规则保底评估: item_id={item_id}")
            raw_eval = self._fallback_rule_evaluation(title, price, description)

        # 解析与计算衍生指标
        condition = raw_eval.get("condition", "成色未明")
        has_defects = bool(raw_eval.get("has_defects", False))
        defect_details = raw_eval.get("defect_details", [])
        if not isinstance(defect_details, list):
            defect_details = [str(defect_details)]

        try:
            market_price = float(raw_eval.get("estimated_market_price", price))
        except (ValueError, TypeError):
            market_price = float(price)

        current_price = float(price)
        estimated_profit = round(market_price - current_price - 10.0, 2)
        profit_rate = round(estimated_profit / current_price, 4) if current_price > 0 else 0.0

        verdict = raw_eval.get("verdict", "neutral")
        if verdict not in ("strong_buy", "buy", "neutral", "avoid"):
            if profit_rate >= 0.35 and not has_defects:
                verdict = "strong_buy"
            elif profit_rate >= 0.15:
                verdict = "buy"
            else:
                verdict = "neutral"

        try:
            confidence = float(raw_eval.get("confidence", 0.8))
        except (ValueError, TypeError):
            confidence = 0.8

        summary = raw_eval.get("summary", f"标价 {current_price} 元，预估行情 {market_price} 元。")

        # 解析转售建议
        relist_raw = raw_eval.get("relisting_advice")
        relisting_advice = None
        if isinstance(relist_raw, dict):
            relisting_advice = RelistingAdvice(
                recommended_title=relist_raw.get("recommended_title", title),
                recommended_price=float(relist_raw.get("recommended_price", market_price)),
                recommended_tags=relist_raw.get("recommended_tags", ["成色好", "极速发货"]),
                selling_points=relist_raw.get("selling_points", []),
            )

        return ItemEvaluationResult(
            item_id=str(item_id),
            title=title,
            current_price=current_price,
            condition=condition,
            has_defects=has_defects,
            defect_details=defect_details,
            estimated_market_price=market_price,
            estimated_profit=estimated_profit,
            profit_rate=profit_rate,
            verdict=verdict,
            confidence=confidence,
            summary=summary,
            has_visual_inspection=has_images,
            relisting_advice=relisting_advice,
        )

    def _fallback_rule_evaluation(self, title: str, price: float, description: str) -> Dict[str, Any]:
        """纯规则兜底评估（当大模型超时或未配置 Key 时激活）"""
        negative_words = ["坏", "配件", "不能开机", "暗病", "报废", "高仿", "山寨", "到付", "自提"]
        has_negative = any(w in (title + description) for w in negative_words)

        if has_negative:
            return {
                "condition": "成色偏旧或有瑕疵",
                "has_defects": True,
                "defect_details": ["描述中包含负向风险词汇"],
                "estimated_market_price": price,
                "verdict": "avoid",
                "confidence": 0.6,
                "summary": "商品可能存在暗病或瑕疵，建议规避。",
            }

        # 默认温和评估
        estimated_market = round(price * 1.15, 2)
        return {
            "condition": "95新/轻微使用",
            "has_defects": False,
            "defect_details": [],
            "estimated_market_price": estimated_market,
            "verdict": "neutral",
            "confidence": 0.5,
            "summary": "基于价格基准的常规评估，市场行情稳定。",
            "relisting_advice": {
                "recommended_title": f"【正品优选】{title}",
                "recommended_price": estimated_market,
                "recommended_tags": ["正品保证", "现货秒发"],
                "selling_points": ["成色良好", "功能全通", "顺丰包邮"],
            },
        }

    def _call_llm_evaluation(
        self,
        item_id: str,
        title: str,
        price: float,
        description: str,
        image_urls: List[str],
    ) -> Optional[Dict[str, Any]]:
        """调用多模态或文本大模型进行专业评估"""
        ai_cfg = self.config.get("AI_PROVIDER", {})
        api_key = ai_cfg.get("api_key", "")
        api_url = ai_cfg.get("api_url", "https://api.openai.com/v1")
        model = ai_cfg.get("model", "gpt-4o-mini")

        if not api_key or "replace-with" in api_key:
            logger.info("未配置有效的 AI API Key，直接使用规则兜底评估")
            return None

        prompt = PROMPT_VISION_EVALUATE.format(
            item_id=item_id,
            title=title,
            price=price,
            description=description or "暂无详细描述",
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # 构建支持 Vision 的消息体
        user_content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        # 最多截取前 3 张图片输入，防止超 token 限制
        for url in image_urls[:3]:
            if url and (url.startswith("http://") or url.startswith("https://")):
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": url, "detail": "low"},
                })

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个严谨客观的二手电商捡漏与价值评估机器人。必须且仅返回标准 JSON。"},
                {"role": "user", "content": user_content if len(image_urls) > 0 else prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 1200,
        }

        endpoint = f"{api_url.rstrip('/')}/chat/completions"
        try:
            resp = requests.post(endpoint, headers=headers, json=payload, timeout=25)
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return self._extract_json_payload(content)
            else:
                logger.warning(f"大模型 API 响应状态异常: {resp.status_code} - {resp.text[:200]}")
                return None
        except Exception as e:
            logger.warning(f"调用大模型价值评估异常: {e}")
            return None

    def create_relist_posters_with_craft_anything(
        self,
        eval_result: ItemEvaluationResult,
        custom_price: Optional[float] = None,
        custom_tags: Optional[List[str]] = None,
    ) -> List[str]:
        """
        深度结合 Craft-Anything 电商海报引擎:
        为评估出具备捡漏价值的商品，一键自动生成 3 张高转化闲鱼去水印海报图（主图、流程图、质保图）
        """
        if generate_product_images is None:
            logger.error("未找到 image_generator 模块，无法生成电商海报")
            return []

        title = eval_result.title
        price = custom_price or eval_result.current_price
        tags = custom_tags or ["正品保真", "极速发货", "验货实拍"]

        if eval_result.relisting_advice:
            title = eval_result.relisting_advice.recommended_title or title
            price = custom_price or eval_result.relisting_advice.recommended_price
            if eval_result.relisting_advice.recommended_tags:
                tags = eval_result.relisting_advice.recommended_tags

        logger.info(f"正在运用 Craft-Anything 视觉引擎为捡漏商品生成全套转售图: {title}")
        try:
            generated = generate_product_images(title=title, price=price, tags=tags)
            eval_result.generated_posters = generated
            return generated
        except Exception as e:
            logger.error(f"Craft-Anything 电商生图失败: {e}")
            return []
