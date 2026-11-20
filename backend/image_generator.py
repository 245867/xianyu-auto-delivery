"""
智能商品配图生成引擎（Image Generator）
=========================================
解决痛点：
闲鱼发布虚拟商品/卡券/会员时，无图或侵权图必被降权/下架。
本模块提供两级生图能力：
1. 外部 AI 生图 API 接口（OpenAI / Gemini Imagen / 自定义 AI 接口）；
2. 内置高转化电商营销主图渲染引擎（基于 PIL 原生合成 800x800 高清主图，
   支持品牌智能调色、卖点胶囊、质保徽章、微噪点防算法查重降权）。
3. 支持一键生成整套素材（主图、发货说明图、售后质保图）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "static" / "uploads" / "images" / "generated"

# Windows 常见高质量中文字体查找路径
FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc",   # 微软雅黑粗体
    "C:/Windows/Fonts/msyh.ttc",     # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",   # 黑体
    "C:/Windows/Fonts/simsun.ttc",   # 宋体
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",  # Linux 备选
    "/System/Library/Fonts/PingFang.ttc",            # macOS 备选
]


def _get_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """安全获取中文字体，找不到时降级默认字体。"""
    candidates = FONT_CANDIDATES if not bold else [FONT_CANDIDATES[0]] + FONT_CANDIDATES
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


# 品牌与品类配色库（深渐变底色 + 品牌强调色）
THEME_PALETTES = {
    "quark": {
        "keywords": ["夸克", "夸克网盘", "夸克会员"],
        "bg_start": (11, 27, 61),      # 深邃科技蓝
        "bg_end": (29, 78, 216),       # 亮钴蓝
        "accent": (255, 232, 21),      # 闲鱼明黄
        "sub_accent": (6, 182, 212),   # 青碧色
        "card_bg": (15, 23, 42, 210),
        "brand_name": "夸克网盘 · 极速云存",
    },
    "baidu": {
        "keywords": ["百度", "百度网盘", "文心一言", "度加"],
        "bg_start": (15, 23, 42),      # 暗夜黑蓝
        "bg_end": (30, 64, 175),       # 百度科技蓝
        "accent": (254, 240, 138),     # 柔金色
        "sub_accent": (239, 68, 68),   # 醒目红
        "card_bg": (17, 24, 39, 210),
        "brand_name": "百度网盘 · 超大空间",
    },
    "tencent": {
        "keywords": ["腾讯", "腾讯视频", "qq", "qq会员", "微信读书"],
        "bg_start": (6, 78, 59),       # 祖母绿暗调
        "bg_end": (16, 185, 129),      # 翡翠绿
        "accent": (253, 224, 71),      # 鲜柠檬黄
        "sub_accent": (56, 189, 248),  # 天空蓝
        "card_bg": (6, 78, 59, 210),
        "brand_name": "腾讯视频 · 臻彩视界",
    },
    "iqiyi": {
        "keywords": ["爱奇艺", "奇艺"],
        "bg_start": (20, 83, 45),
        "bg_end": (34, 197, 94),
        "accent": (255, 232, 21),
        "sub_accent": (163, 230, 53),
        "card_bg": (15, 23, 42, 210),
        "brand_name": "爱奇艺 · 帧绮画质",
    },
    "bilibili": {
        "keywords": ["b站", "哔哩哔哩", "bilibili", "大会员"],
        "bg_start": (157, 23, 77),     # 玫红暗调
        "bg_end": (236, 72, 153),      # B站粉
        "accent": (254, 240, 138),
        "sub_accent": (56, 189, 248),
        "card_bg": (30, 27, 75, 210),
        "brand_name": "哔哩哔哩 · 追番畅看",
    },
    "luckin": {
        "keywords": ["瑞幸", "咖啡", "生椰", "酱香"],
        "bg_start": (15, 23, 42),
        "bg_end": (30, 58, 138),
        "accent": (251, 191, 36),      # 琥珀金
        "sub_accent": (248, 250, 252), # 纯白
        "card_bg": (15, 23, 42, 215),
        "brand_name": "瑞幸咖啡 · 门店通用",
    },
    "starbucks": {
        "keywords": ["星巴克", "星礼卡", "星享卡"],
        "bg_start": (6, 78, 59),
        "bg_end": (4, 120, 87),
        "accent": (253, 224, 71),
        "sub_accent": (254, 243, 199),
        "card_bg": (6, 78, 59, 215),
        "brand_name": "星巴克 · 咖啡特饮",
    },
    "fastfood": {
        "keywords": ["麦当劳", "肯德基", "必胜客", "汉堡王", "美团", "饿了么"],
        "bg_start": (153, 27, 27),     # 暖深红
        "bg_end": (220, 38, 38),       # 鲜红
        "accent": (250, 204, 21),      # 活力明黄
        "sub_accent": (255, 255, 255),
        "card_bg": (69, 10, 10, 215),
        "brand_name": "美食礼券 · 立减直充",
    },
    "music": {
        "keywords": ["网易云", "网易云音乐", "酷狗", "酷我"],
        "bg_start": (153, 27, 27),
        "bg_end": (225, 29, 72),
        "accent": (254, 240, 138),
        "sub_accent": (251, 113, 133),
        "card_bg": (24, 24, 27, 210),
        "brand_name": "音乐畅听 · 黑胶VIP",
    },
    "game": {
        "keywords": ["游戏", "点券", "steam", "暴雪", "雷神", "加速器", "网易UU"],
        "bg_start": (24, 24, 27),
        "bg_end": (67, 56, 202),       # 电竞紫蓝
        "accent": (250, 204, 21),      # 暴击金
        "sub_accent": (168, 85, 247),  # 霓虹紫
        "card_bg": (24, 24, 27, 220),
        "brand_name": "极速竞技 · 官方卡密",
    },
}

DEFAULT_PALETTE = {
    "keywords": [],
    "bg_start": (15, 23, 42),          # 深石板灰
    "bg_end": (30, 41, 59),            # 商务深蓝
    "accent": (255, 232, 21),          # 闲鱼标志黄
    "sub_accent": (56, 189, 248),      # 清爽天蓝
    "card_bg": (15, 23, 42, 220),
    "brand_name": "官方正规 · 自动发货",
}


def _match_palette(title: str, tags: Optional[List[str]] = None) -> Dict[str, Any]:
    """根据标题和标签自动匹配最佳品牌调色板。"""
    text = (title + " " + " ".join(tags or [])).lower()
    for key, pal in THEME_PALETTES.items():
        for kw in pal["keywords"]:
            if kw.lower() in text:
                return pal
    return DEFAULT_PALETTE


def _create_gradient_background(width: int, height: int, start_color: Tuple[int, int, int],
                                end_color: Tuple[int, int, int]) -> Image.Image:
    """生成平滑对角渐变背景 + 微噪点纹理（防算法查重）。"""
    base = Image.new("RGB", (width, height), start_color)
    draw = ImageDraw.Draw(base)

    for y in range(height):
        ratio = y / float(height)
        r = int(start_color[0] * (1 - ratio) + end_color[0] * ratio)
        g = int(start_color[1] * (1 - ratio) + end_color[1] * ratio)
        b = int(start_color[2] * (1 - ratio) + end_color[2] * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # 添加轻微科技光晕（大径透明圆环，营造景深）
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    
    # 左上角光晕
    overlay_draw.ellipse([(-100, -100), (350, 350)], fill=(end_color[0], end_color[1], end_color[2], 40))
    # 右下角光晕
    overlay_draw.ellipse([(width - 250, height - 250), (width + 100, height + 100)],
                         fill=(start_color[0], start_color[1], start_color[2], 50))

    # 细微噪点扰动（闲鱼图像防重复算法抗体）
    random.seed(int(time.time() * 1000) % 100000)
    for _ in range(120):
        nx = random.randint(0, width - 1)
        ny = random.randint(0, height - 1)
        overlay_draw.point((nx, ny), fill=(255, 255, 255, random.randint(15, 45)))

    base.paste(overlay, (0, 0), overlay)
    return base


def _draw_rounded_rectangle(draw: ImageDraw.ImageDraw, xy: Tuple[int, int, int, int],
                           radius: int, fill: Any, outline: Any = None, width: int = 1):
    """绘制高兼容性圆角矩形。"""
    try:
        draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)
    except AttributeError:
        draw.rectangle(xy, fill=fill, outline=outline, width=width)


def _wrap_text(text: str, font: ImageFont.ImageFont, max_width: int, max_lines: int = 2) -> List[str]:
    """智能中文分行，保证不超出卡片范围。"""
    lines = []
    current_line = ""
    for char in text:
        test_line = current_line + char
        # 获取当前行像素宽度
        bbox = font.getbbox(test_line) if hasattr(font, "getbbox") else (0, 0, font.getsize(test_line)[0], 0)
        line_w = bbox[2] - bbox[0]
        if line_w <= max_width:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            current_line = char
            if len(lines) >= max_lines:
                break
    if current_line and len(lines) < max_lines:
        lines.append(current_line)
    elif len(lines) >= max_lines and current_line:
        # 超出部分加省略号
        if lines:
            lines[-1] = lines[-1][:-1] + "..."
    return lines


# ==============================================================================
# 本地高清电商海报生成器 (Canvas Engine)
# ==============================================================================

def generate_ecommerce_poster(title: str, price: float = 0.0, tags: Optional[List[str]] = None,
                              kind: str = "main", output_path: Optional[str] = None) -> str:
    """
    生成 800x800 高转化率电商营销主图或售后图。
    kind: "main" (主图) | "guide" (发货说明图) | "guarantee" (售后质保图)
    """
    width, height = 800, 800
    palette = _match_palette(title, tags)

    # 1. 生成渐变背景
    img = _create_gradient_background(width, height, palette["bg_start"], palette["bg_end"])
    draw = ImageDraw.Draw(img)

    # 2. 顶部微章栏（Header Capsule）
    top_badge_text = palette["brand_name"]
    font_top = _get_font(22, bold=True)
    tb_bbox = font_top.getbbox(top_badge_text) if hasattr(font_top, "getbbox") else (0, 0, len(top_badge_text)*22, 30)
    tb_w = tb_bbox[2] - tb_bbox[0]
    badge_x = (width - (tb_w + 50)) // 2
    _draw_rounded_rectangle(draw, (badge_x, 42, badge_x + tb_w + 50, 84), radius=21,
                            fill=(0, 0, 0, 100), outline=(255, 255, 255, 120), width=2)
    # 黄色小光点
    draw.ellipse([(badge_x + 16, 58), (badge_x + 26, 68)], fill=palette["accent"])
    draw.text((badge_x + 36, 50), top_badge_text, font=font_top, fill=(255, 255, 255))

    if kind == "main":
        # ==================== 主图模式 ====================
        # 3. 中央主体玻璃态悬浮卡片
        card_margin_x = 45
        card_y1 = 115
        card_y2 = 680
        _draw_rounded_rectangle(draw, (card_margin_x, card_y1, width - card_margin_x, card_y2),
                                radius=24, fill=(15, 23, 42), outline=(255, 255, 255, 45), width=2)

        # 4. 品牌闪电/钻石角标
        tag_text = "正品直发 · 极速卡密"
        font_pill = _get_font(20, bold=True)
        _draw_rounded_rectangle(draw, (card_margin_x + 30, card_y1 + 35, card_margin_x + 230, card_y1 + 75),
                                radius=12, fill=(30, 41, 59), outline=palette["sub_accent"], width=2)
        draw.text((card_margin_x + 48, card_y1 + 43), tag_text, font=font_pill, fill=palette["sub_accent"])

        # 5. 大字标题（醒目、居中分行）
        font_title = _get_font(44, bold=True)
        title_clean = title.strip()
        lines = _wrap_text(title_clean, font_title, max_width=620, max_lines=2)
        title_start_y = card_y1 + 105
        for idx, line in enumerate(lines):
            t_bbox = font_title.getbbox(line) if hasattr(font_title, "getbbox") else (0, 0, len(line)*44, 50)
            lw = t_bbox[2] - t_bbox[0]
            lx = (width - lw) // 2
            # 阴影层
            draw.text((lx + 2, title_start_y + idx * 62 + 2), line, font=font_title, fill=(0, 0, 0))
            # 主文字层
            text_color = palette["accent"] if idx == 0 else (255, 255, 255)
            draw.text((lx, title_start_y + idx * 62), line, font=font_title, fill=text_color)

        # 6. 规格亮点胶囊区（3 个金刚卖点）
        feature_y = card_y1 + 250
        features = [
            ("⚡ 秒发卡密", "拍下自动出单 无需等待"),
            ("🛡️ 正规官方", "独享正品 售后无忧质保"),
            ("🔒 一客一密", "全新独立卡密 绝非共享"),
        ]
        feat_font_title = _get_font(22, bold=True)
        feat_font_desc = _get_font(18, bold=False)

        for i, (f_title, f_desc) in enumerate(features):
            fy = feature_y + i * 85
            _draw_rounded_rectangle(draw, (card_margin_x + 25, fy, width - card_margin_x - 25, fy + 72),
                                    radius=16, fill=(30, 41, 59), outline=(51, 65, 85), width=1)
            # 左侧高亮装饰条
            _draw_rounded_rectangle(draw, (card_margin_x + 25, fy, card_margin_x + 32, fy + 72),
                                    radius=3, fill=palette["accent"])
            draw.text((card_margin_x + 50, fy + 12), f_title, font=feat_font_title, fill=palette["accent"])
            draw.text((card_margin_x + 50, fy + 42), f_desc, font=feat_font_desc, fill=(203, 213, 225))

        # 7. 底部价格保障栏
        footer_y = card_y1 + 475
        _draw_rounded_rectangle(draw, (card_margin_x + 25, footer_y, width - card_margin_x - 25, footer_y + 75),
                                radius=18, fill=(15, 23, 42), outline=(255, 232, 21, 180), width=2)
        font_foot_label = _get_font(22, bold=True)
        draw.text((card_margin_x + 50, footer_y + 24), "🔥 闲鱼专享特惠", font=font_foot_label, fill=(255, 255, 255))

        if price > 0:
            price_str = f"¥ {price:.2f}"
            font_price = _get_font(34, bold=True)
            p_bbox = font_price.getbbox(price_str) if hasattr(font_price, "getbbox") else (0, 0, len(price_str)*34, 40)
            pw = p_bbox[2] - p_bbox[0]
            draw.text((width - card_margin_x - 50 - pw, footer_y + 18), price_str, font=font_price, fill=palette["accent"])
        else:
            font_price = _get_font(24, bold=True)
            draw.text((width - card_margin_x - 220, footer_y + 24), "超值底价直出", font=font_price, fill=palette["accent"])

    elif kind == "guide":
        # ==================== 发货流程说明图 ====================
        card_margin_x = 45
        _draw_rounded_rectangle(draw, (card_margin_x, 115, width - card_margin_x, 680),
                                radius=24, fill=(15, 23, 42), outline=(56, 189, 248, 120), width=2)
        font_h = _get_font(36, bold=True)
        draw.text((80, 150), "📦 自动发货流程说明", font=font_h, fill=palette["accent"])

        steps = [
            ("步骤 1：直接拍下付款", "系统 24 小时在线守候，无需提前咨询是否有货。"),
            ("步骤 2：自动推送卡密", "付款后 5-10 秒内，系统通过闲鱼私聊窗口秒发。"),
            ("步骤 3：官方渠道兑换", "复制卡密或网盘链接，按说明在官方入口完成兑换。"),
            ("步骤 4：确认收货与好评", "使用满意请帮忙点亮 5 星好评，享受质保服务！"),
        ]
        font_s_t = _get_font(22, bold=True)
        font_s_d = _get_font(18, bold=False)
        for i, (st, sd) in enumerate(steps):
            sy = 225 + i * 105
            _draw_rounded_rectangle(draw, (75, sy, width - 75, sy + 85),
                                    radius=14, fill=(30, 41, 59), outline=(71, 85, 105), width=1)
            draw.text((95, sy + 15), st, font=font_s_t, fill=(255, 255, 255))
            draw.text((95, sy + 48), sd, font=font_s_d, fill=(148, 163, 184))

    elif kind == "guarantee":
        # ==================== 售后质保说明图 ====================
        card_margin_x = 45
        _draw_rounded_rectangle(draw, (card_margin_x, 115, width - card_margin_x, 680),
                                radius=24, fill=(15, 23, 42), outline=(244, 63, 94, 120), width=2)
        font_h = _get_font(36, bold=True)
        draw.text((80, 150), "🛡️ 平台安心购·服务承诺", font=font_h, fill=palette["accent"])

        rules = [
            ("✅ 官方正品货源", "所有卡券均采购自正规供应商/官方云仓，安全可靠。"),
            ("✅ 独享密匙质保", "一客一密，绝非共享翻车车位，卡密售出即绑定。"),
            ("✅ 极速售后响应", "兑换若遇任何异常，人工客服全天候跟进，包退包换。"),
            ("⚠️ 防骗安全警示", "请认准平台内置聊天发货，切勿添加任何站外微信或QQ！"),
        ]
        font_s_t = _get_font(22, bold=True)
        font_s_d = _get_font(18, bold=False)
        for i, (st, sd) in enumerate(rules):
            sy = 225 + i * 105
            _draw_rounded_rectangle(draw, (75, sy, width - 75, sy + 85),
                                    radius=14, fill=(30, 41, 59), outline=(71, 85, 105), width=1)
            draw.text((95, sy + 15), st, font=font_s_t, fill=(255, 255, 255) if i < 3 else (251, 113, 133))
            draw.text((95, sy + 48), sd, font=font_s_d, fill=(148, 163, 184))

    # 8. 底部安全水印与合规标识
    font_bottom = _get_font(16, bold=False)
    draw.text((width // 2 - 130, height - 60), "⚡ 智控 Pro 自动化保障系统", font=font_bottom, fill=(148, 163, 184))

    # 9. 保存到指定路径
    if not output_path:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        rand = random.randint(100, 999)
        filename = f"poster_{kind}_{ts}_{rand}.png"
        output_path = str(DEFAULT_OUTPUT_DIR / filename)
    else:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    img.save(output_path, "PNG", quality=95)
    return output_path


# ==============================================================================
# 外部 AI 生图 API 接口 (OpenAI / Gemini / 自定义 API)
# ==============================================================================

def generate_ai_image(title: str, prompt_override: Optional[str] = None,
                      output_path: Optional[str] = None) -> Optional[str]:
    """
    通过外部 AI 生图接口生成商品主图。
    优先读取 global_config.yml 中的 IMAGE_GENERATION 或 AI_PROVIDER 配置。
    若未配置 API Key 或请求失败，返回 None 以触发降级到本地海报引擎。
    """
    img_cfg = config.get("IMAGE_GENERATION") or {}
    ai_cfg = config.get("AI_PROVIDER") or {}

    api_key = img_cfg.get("api_key") or ai_cfg.get("api_key")
    if not api_key or "sk-replace" in api_key or "your-api-key" in api_key:
        return None

    api_url = img_cfg.get("api_url")
    if not api_url:
        base_url = ai_cfg.get("api_url", "https://api.openai.com/v1").rstrip("/")
        api_url = f"{base_url}/images/generations"

    model = img_cfg.get("model") or "dall-e-3"

    # 构建针对闲鱼虚拟商品/卡券的高清质感 Prompt
    prompt = prompt_override or (
        f"Professional minimalist 3D e-commerce commercial product shot for digital card: '{title}', "
        f"floating sleek futuristic glass card, studio lighting, volumetric glow, vibrant clean background, "
        f"8k resolution, photorealistic, cinematic octane render, 1:1 aspect ratio, no blurry elements."
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024",
        "response_format": "b64_json",
    }

    try:
        resp = requests.post(api_url, headers=headers, json=payload, timeout=45)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("data") or []
            if items:
                b64_data = items[0].get("b64_json")
                if b64_data:
                    raw_bytes = base64.b64decode(b64_data)
                elif items[0].get("url"):
                    img_resp = requests.get(items[0]["url"], timeout=30)
                    raw_bytes = img_resp.content
                else:
                    return None

                # 保存
                if not output_path:
                    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"ai_gen_{ts}_{random.randint(100, 999)}.png"
                    output_path = str(DEFAULT_OUTPUT_DIR / filename)
                else:
                    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

                with open(output_path, "wb") as f:
                    f.write(raw_bytes)
                return output_path
    except Exception:
        # 网络或额度异常时静默降级，不阻断发布流程
        pass
    return None


# ==============================================================================
# 统一综合生图入口 (Smart Orchestrator)
# ==============================================================================

def generate_product_images(title: str, price: float = 0.0, tags: Optional[List[str]] = None,
                            mode: str = "auto", count: int = 1) -> List[Dict[str, Any]]:
    """
    统一智能生图服务入口。
    mode: "auto" (优先尝试 AI 生图，失败自动降级到电商卡片) | "canvas" (纯本地电商海报) | "ai" (仅 AI)
    count: 1 = 仅生成主图; 3 = 生成 3 张完整套图 (主图 + 流程图 + 质保图)
    
    返回: [
      {
        "path": "绝对本地路径",
        "url": "/static/uploads/images/generated/...",
        "filename": "...",
        "mode_used": "ai" | "canvas",
        "kind": "main" | "guide" | "guarantee"
      }
    ]
    """
    results: List[Dict[str, Any]] = []
    kinds = ["main"] if count <= 1 else ["main", "guide", "guarantee"]

    for idx, kind in enumerate(kinds):
        image_path: Optional[str] = None
        mode_used = "canvas"

        # 若是主图且允许使用 AI
        if kind == "main" and mode in ("auto", "ai"):
            try:
                image_path = generate_ai_image(title)
                if image_path:
                    mode_used = "ai"
            except Exception:
                image_path = None

        # 降级或指定使用本地电商合成
        if not image_path and mode != "ai":
            image_path = generate_ecommerce_poster(title, price=price, tags=tags, kind=kind)
            mode_used = "canvas"

        if image_path and os.path.exists(image_path):
            p = Path(image_path)
            # 转为相对于 static 的 URL
            url = f"/static/uploads/images/generated/{p.name}"
            results.append({
                "path": str(p.resolve()).replace("\\", "/"),
                "url": url,
                "filename": p.name,
                "mode_used": mode_used,
                "kind": kind,
            })

    return results
