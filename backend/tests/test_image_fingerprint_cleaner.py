import io
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
import pytest
from image_fingerprint_cleaner import (
    compute_dhash,
    hamming_distance,
    strip_exif,
    apply_perceptual_perturbation,
    format_to_xianyu_ratio,
    clean_and_reconstruct,
)


def _make_dummy_image(width=200, height=200, seed=1):
    random.seed(seed)
    img = Image.new("RGB", (width, height))
    pixels = []
    # 生成多区块色彩与复杂纹理
    for y in range(height):
        for x in range(width):
            r = (x * 7 + y * 13 + random.randint(0, 30)) % 256
            g = (x * 11 + y * 3 + random.randint(0, 30)) % 256
            b = ((x + y) * 5 + random.randint(0, 50)) % 256
            pixels.append((r, g, b))
    img.putdata(pixels)
    return img


def test_compute_dhash_and_hamming():
    img1 = _make_dummy_image()
    hash1 = compute_dhash(img1)
    assert len(hash1) == 64
    assert set(hash1).issubset({"0", "1"})

    # 同一张图汉明距离应为 0
    assert hamming_distance(hash1, hash1) == 0


def test_perceptual_perturbation_changes_hash():
    img = _make_dummy_image(300, 300, seed=42)
    hash_orig = compute_dhash(img)

    perturbed = apply_perceptual_perturbation(img, noise_intensity=0.08, crop_margin=0.03, seed=99)
    hash_new = compute_dhash(perturbed)

    # 经过微扰动和裁切后，感知哈希必须产生差异以避开同图碰撞检测
    dist = hamming_distance(hash_orig, hash_new)
    assert dist > 0, f"Expected non-zero hamming distance, got {dist}"
    assert perturbed.size == img.size


def test_strip_exif():
    img = Image.new("RGBA", (100, 100), (255, 0, 0, 128))
    clean = strip_exif(img)
    assert clean.mode == "RGB"
    assert clean.size == (100, 100)


def test_format_to_xianyu_3_4_ratio():
    # 1:1 的正方形图转 3:4 黄金画幅
    square_img = _make_dummy_image(300, 300)
    formatted = format_to_xianyu_ratio(square_img, target_ratio=(3, 4))
    w, h = formatted.size
    assert math.isclose(w / h, 3 / 4, rel_tol=0.01)


def test_clean_and_reconstruct_pipeline():
    img = _make_dummy_image(250, 250)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    out_bytes = clean_and_reconstruct(png_bytes, target_ratio=(3, 4), seed=123)
    assert len(out_bytes) > 0

    # 验证输出为合法的 JPEG 并且符合 3:4 比例
    out_img = Image.open(io.BytesIO(out_bytes))
    assert out_img.format == "JPEG"
    w, h = out_img.size
    assert math.isclose(w / h, 3 / 4, rel_tol=0.01)
