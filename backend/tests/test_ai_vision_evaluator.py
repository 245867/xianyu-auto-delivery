# -*- coding: utf-8 -*-
"""
多模态商品价值评估与 Craft-Anything 转售引擎单元测试
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_vision_evaluator import (
    AIVisionEvaluator,
    ItemEvaluationResult,
    RelistingAdvice,
)


class TestAIVisionEvaluator(unittest.TestCase):

    def setUp(self):
        self.evaluator = AIVisionEvaluator()

    def test_json_payload_extraction(self):
        """测试对各种非标准大模型输出的 JSON 容错提取能力"""
        # 情况 1: 标准 ```json 代码块
        raw1 = '```json\n{"condition": "99新", "estimated_market_price": 300.0, "verdict": "buy"}\n```'
        data1 = self.evaluator._extract_json_payload(raw1)
        self.assertIsNotNone(data1)
        self.assertEqual(data1.get("condition"), "99新")

        # 情况 2: 前后夹杂废话文本
        raw2 = '你好，根据我的分析，结果如下：\n{"condition": "全新", "estimated_market_price": 500.0, "verdict": "strong_buy"}\n希望对你有用！'
        data2 = self.evaluator._extract_json_payload(raw2)
        self.assertIsNotNone(data2)
        self.assertEqual(data2.get("verdict"), "strong_buy")

        # 情况 3: 纯非法文本
        raw3 = '抱歉，无法分析该商品。'
        data3 = self.evaluator._extract_json_payload(raw3)
        self.assertIsNone(data3)

    def test_fallback_rule_evaluation(self):
        """测试当未配置 AI API 时的规则保底表现"""
        # 正常商品
        res = self.evaluator.evaluate_item(
            item_id="10001",
            title="罗技 MX Master 3S 无线鼠标 黑色",
            price=300.0,
            description="自用一手，箱说全",
        )
        self.assertEqual(res.item_id, "10001")
        self.assertEqual(res.current_price, 300.0)
        self.assertFalse(res.has_defects)
        self.assertIn(res.verdict, ("strong_buy", "buy", "neutral"))

        # 风险/负向商品
        res_bad = self.evaluator.evaluate_item(
            item_id="10002",
            title="iPhone 13 屏幕碎 配件机 不能开机",
            price=200.0,
            description="暗病多，售出不退",
        )
        self.assertEqual(res_bad.verdict, "avoid")
        self.assertTrue(res_bad.has_defects)

    def test_mock_llm_evaluation_metrics(self):
        """测试 Mock 大模型返回并验证利润和衍生决策计算"""
        mock_data = {
            "condition": "99新/近全新",
            "has_defects": False,
            "defect_details": [],
            "estimated_market_price": 600.0,
            "verdict": "strong_buy",
            "confidence": 0.95,
            "summary": "标价极低，低于行情200元以上，极具捡漏价值",
            "relisting_advice": {
                "recommended_title": "【99新带票】罗技 Master 3S 高级办公鼠标",
                "recommended_price": 580.0,
                "recommended_tags": ["成色极新", "顺丰包邮", "正品保真"],
                "selling_points": ["无任何划痕", "全套配件齐备", "现货即发"],
            },
        }

        res = self.evaluator.evaluate_item(
            item_id="20001",
            title="罗技 Master 3S",
            price=350.0,
            description="买来没怎么用，急出回血",
            image_urls=["https://img.alicdn.com/mock.jpg"],
            mock_response=mock_data,
        )

        self.assertEqual(res.item_id, "20001")
        self.assertEqual(res.verdict, "strong_buy")
        self.assertEqual(res.estimated_market_price, 600.0)
        # 预估毛利 = 600 - 350 - 10 = 240
        self.assertEqual(res.estimated_profit, 240.0)
        # 利润率 = 240 / 350 ≈ 0.6857
        self.assertAlmostEqual(res.profit_rate, 240.0 / 350.0, places=3)
        self.assertTrue(res.has_visual_inspection)
        self.assertIsNotNone(res.relisting_advice)
        self.assertEqual(res.relisting_advice.recommended_price, 580.0)

    @patch("ai_vision_evaluator.generate_product_images")
    def test_craft_anything_relist_posters(self, mock_generate):
        """测试与 Craft-Anything 电商海报引擎联动生成全套转售图"""
        mock_generate.return_value = ["static/posters/1.png", "static/posters/2.png", "static/posters/3.png"]

        eval_res = ItemEvaluationResult(
            item_id="30001",
            title="任天堂 Switch OLED 续航版",
            current_price=1200.0,
            condition="99新",
            has_defects=False,
            defect_details=[],
            estimated_market_price=1600.0,
            estimated_profit=390.0,
            profit_rate=0.325,
            verdict="strong_buy",
            confidence=0.92,
            summary="价格远低于市场价",
            relisting_advice=RelistingAdvice(
                recommended_title="【国行在保】任天堂 Switch OLED 白色版 全套箱说全",
                recommended_price=1550.0,
                recommended_tags=["极速发货", "官方正品", "验货实拍"],
                selling_points=["99新无划痕", "带原装充电器", "即发即玩"],
            ),
        )

        posters = self.evaluator.create_relist_posters_with_craft_anything(eval_res)
        self.assertEqual(len(posters), 3)
        self.assertEqual(eval_res.generated_posters, posters)
        mock_generate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
