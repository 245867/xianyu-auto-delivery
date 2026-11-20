import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from title_fission_engine import (
    calculate_title_jaccard,
    generate_variant_titles,
    build_fission_matrix,
)


def test_calculate_title_jaccard():
    t1 = "自用全新转让 车载香薰 高档持久 包邮秒发"
    t2 = "自用全新转让 车载香薰 高档持久 降价急出回血"
    t3 = "搬家断舍离 汽车挂件 极简冷淡风 拍下当天发"

    sim_high = calculate_title_jaccard(t1, t2)
    sim_low = calculate_title_jaccard(t1, t3)

    assert sim_high > 0.40
    assert sim_low < 0.20
    assert calculate_title_jaccard(t1, t1) == 1.0


def test_generate_variant_titles():
    base_title = "车载出风口香水持久散香高档汽车香薰"
    core_keyword = "车载香薰"

    variants = generate_variant_titles(base_title, core_keyword, count=5, seed=42)

    assert len(variants) == 5
    # 严格检验闲鱼 30 字限制
    for t in variants:
        assert len(t) <= 30
        assert core_keyword in t

    # 检验成组标题间两两相似度不得过高（防重复铺货违规）
    for i in range(len(variants)):
        for j in range(i + 1, len(variants)):
            jaccard = calculate_title_jaccard(variants[i], variants[j])
            assert jaccard <= 0.50, f"Titles too similar ({jaccard}): '{variants[i]}' vs '{variants[j]}'"


def test_build_fission_matrix():
    parent_id = "1100839041022"
    base_title = "车载出风口香水"
    core_kw = "车载香薰"
    images = ["https://img.alicdn.com/1.jpg", "https://img.alicdn.com/2.jpg"]

    matrix = build_fission_matrix(parent_id, base_title, core_kw, images, count=4, seed=123)

    assert len(matrix) == 4
    for idx, item in enumerate(matrix):
        assert item["parent_item_id"] == parent_id
        assert item["fission_id"] == f"{parent_id}_fiss_{idx+1}"
        assert item["status"] == "ready"
        assert item["cover_image_url"] in images
        assert len(item["variant_title"]) <= 30
