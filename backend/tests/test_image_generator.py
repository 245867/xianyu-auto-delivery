"""
测试智能商品配图与生图引擎 (Image Generator)
============================================
验证:
1. 品牌/品类配色智能匹配 (_match_palette)
2. 800x800 高清电商海报本地渲染 (generate_ecommerce_poster)
3. 3 张全套图生成 (主图 + 发货流程图 + 售后质保图)
4. AI 接口降级与安全边界 (generate_ai_image / generate_product_images)
5. 文件存在性、RGB 模式与分辨率校验
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import image_generator as ig


class TestImageGenerator(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_img_gen_")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_palette_matching(self):
        """测试不同品类关键词的配色识别"""
        pal_quark = ig._match_palette("夸克网盘 SVIP 年卡")
        self.assertIn("夸克", pal_quark["brand_name"])

        pal_baidu = ig._match_palette("百度网盘 SVIP 空间卡")
        self.assertIn("百度", pal_baidu["brand_name"])

        pal_luckin = ig._match_palette("瑞幸咖啡 生椰拿铁 通兑券")
        self.assertIn("瑞幸", pal_luckin["brand_name"])

        pal_default = ig._match_palette("未知商品测试品类")
        self.assertIn("官方正规", pal_default["brand_name"])

    def test_ecommerce_poster_main_render(self):
        """测试主图渲染 (800x800, RGB, 文件非空)"""
        out_path = os.path.join(self.temp_dir, "test_main.png")
        res = ig.generate_ecommerce_poster(
            title="腾讯视频 VIP 月卡 官方直充",
            price=12.5,
            tags=["自动发货", "官方正品"],
            kind="main",
            output_path=out_path,
        )
        self.assertTrue(os.path.exists(res))
        self.assertGreater(os.path.getsize(res), 5000)

        im = Image.open(res)
        self.assertEqual(im.size, (800, 800))
        self.assertEqual(im.mode, "RGB")

    def test_generate_product_image_set_3_pack(self):
        """测试生成 3 张成套营销素材图 (主图+流程+质保)"""
        results = ig.generate_product_images(
            title="哔哩哔哩 大会员 年卡",
            price=98.0,
            tags=["B站", "秒发"],
            mode="canvas",
            count=3,
        )
        self.assertEqual(len(results), 3)
        kinds = [r["kind"] for r in results]
        self.assertEqual(kinds, ["main", "guide", "guarantee"])

        for item in results:
            self.assertTrue(os.path.exists(item["path"]))
            self.assertTrue(item["url"].startswith("/static/uploads/images/generated/"))
            self.assertEqual(item["mode_used"], "canvas")

    def test_ai_fallback_to_canvas(self):
        """测试未配置 AI Key 时自动安全降级至 canvas 模式"""
        results = ig.generate_product_images(
            title="爱奇艺 黄金 VIP 会员",
            price=15.0,
            mode="auto",
            count=1,
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(os.path.exists(results[0]["path"]))
        self.assertEqual(results[0]["mode_used"], "canvas")


if __name__ == "__main__":
    unittest.main()
