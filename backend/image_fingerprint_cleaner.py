"""
Image Fingerprint Cleaner & Anti-Duplicate Processor
=====================================================
REA 落地自《拆解 47：商业闭源跨平台货源搬家与 Craft-Anything 主图防重去指纹引擎》
用于清除电商上游搬运图片的 EXIF 元数据指纹、微扰动破坏感知哈希（pHash/dHash），
并适配闲鱼 3:4 / 1:1 最佳视觉展示，防止平台判定同图搬运与限流。
"""

import io
import math
import random
from typing import Optional, Tuple, Union
from PIL import Image, ImageEnhance, ImageFilter


def _get_pixel_list(img: Image.Image):
    """兼容 Pillow 13+ 与旧版获取像素列表。"""
    if hasattr(img, "get_flattened_data"):
        try:
            return list(img.get_flattened_data())
        except Exception:
            pass
    return list(img.getdata())


def compute_dhash(img: Image.Image, hash_size: int = 8) -> str:
    """计算图片的差分感知哈希 (dHash, 64-bit)。"""
    resized = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    pixels = _get_pixel_list(resized)
    diff = []
    for row in range(hash_size):
        for col in range(hash_size):
            pixel_left = pixels[row * (hash_size + 1) + col]
            pixel_right = pixels[row * (hash_size + 1) + col + 1]
            diff.append("1" if pixel_left > pixel_right else "0")
    return "".join(diff)


def hamming_distance(hash1: str, hash2: str) -> int:
    """计算两个二进制哈希串的汉明距离。"""
    return sum(c1 != c2 for c1, c2 in zip(hash1, hash2))


def strip_exif(img: Image.Image) -> Image.Image:
    """物理剥离所有 EXIF 元数据与设备特征，重建纯净 RGB 画布。"""
    clean_img = Image.new(img.mode, img.size)
    pixels = _get_pixel_list(img)
    clean_img.putdata(pixels)
    if clean_img.mode in ("RGBA", "P"):
        clean_img = clean_img.convert("RGB")
    return clean_img


def apply_perceptual_perturbation(
    img: Image.Image,
    noise_intensity: float = 0.04,
    crop_margin: float = 0.03,
    seed: Optional[int] = None,
) -> Image.Image:
    """
    通过微裁切、微扰动与微量色彩微调颠覆感知哈希，人眼无感但汉明距离显著偏移。
    """
    if seed is not None:
        random.seed(seed)

    w, h = img.size
    # 1. 边缘微量随机裁切 (1% ~ 3%)
    margin_x = max(2, int(w * crop_margin))
    margin_y = max(2, int(h * crop_margin))
    left = random.randint(1, margin_x)
    top = random.randint(1, margin_y)
    right = w - random.randint(1, margin_x)
    bottom = h - random.randint(1, margin_y)
    cropped = img.crop((left, top, right, bottom)).resize((w, h), Image.Resampling.LANCZOS)

    # 2. 轻微对比度与亮度微扰 (±2%)
    brightness_factor = 1.0 + random.uniform(-0.025, 0.025)
    contrast_factor = 1.0 + random.uniform(-0.025, 0.025)
    enhancer_b = ImageEnhance.Brightness(cropped)
    cropped = enhancer_b.enhance(brightness_factor)
    enhancer_c = ImageEnhance.Contrast(cropped)
    cropped = enhancer_c.enhance(contrast_factor)

    # 3. 注入微量不可见像素噪点与高频抖动
    pixels = _get_pixel_list(cropped)
    noisy_pixels = []
    max_delta = max(3, int(255 * noise_intensity))
    for p in pixels:
        if isinstance(p, tuple):
            delta = random.randint(-max_delta, max_delta)
            noisy_p = tuple(max(0, min(255, c + delta)) for c in p[:3])
            noisy_pixels.append(noisy_p)
        else:
            delta = random.randint(-max_delta, max_delta)
            noisy_pixels.append(max(0, min(255, p + delta)))

    perturbed = Image.new("RGB", (w, h))
    perturbed.putdata(noisy_pixels)
    return perturbed


def format_to_xianyu_ratio(
    img: Image.Image,
    target_ratio: Tuple[int, int] = (3, 4),
    bg_color: Tuple[int, int, int] = (250, 250, 250),
) -> Image.Image:
    """
    将任意比例图片等比居中自适应缩放至闲鱼推荐画幅（默认 3:4 黄金画幅）。
    """
    target_w_ratio, target_h_ratio = target_ratio
    orig_w, orig_h = img.size

    # 计算目标画布尺寸（以原图较长一边为基准保证清晰度）
    if orig_w / orig_h > target_w_ratio / target_h_ratio:
        canvas_w = orig_w
        canvas_h = int(orig_w * target_h_ratio / target_w_ratio)
    else:
        canvas_h = orig_h
        canvas_w = int(orig_h * target_w_ratio / target_w_ratio)

    canvas = Image.new("RGB", (canvas_w, canvas_h), bg_color)
    offset_x = (canvas_w - orig_w) // 2
    offset_y = (canvas_h - orig_h) // 2
    canvas.paste(img, (offset_x, offset_y))
    return canvas


def clean_and_reconstruct(
    input_data: Union[str, bytes],
    target_ratio: Optional[Tuple[int, int]] = (3, 4),
    seed: Optional[int] = None,
) -> bytes:
    """
    一站式处理入口：EXIF剥离 -> 感知哈希微扰 -> 3:4 比例画幅重构 -> 输出 JPEG 字节流。
    """
    if isinstance(input_data, str):
        img = Image.open(input_data)
    else:
        img = Image.open(io.BytesIO(input_data))

    clean = strip_exif(img)
    perturbed = apply_perceptual_perturbation(clean, seed=seed)
    if target_ratio:
        formatted = format_to_xianyu_ratio(perturbed, target_ratio=target_ratio)
    else:
        formatted = perturbed

    buf = io.BytesIO()
    formatted.save(buf, format="JPEG", quality=92, optimize=True)
    return buf.getvalue()
