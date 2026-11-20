import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from spec_normalizer import (
    clean_spec_text,
    compute_spec_hash,
    calculate_token_similarity,
    match_best_upstream_sku,
)


def test_clean_spec_text():
    raw1 = "【爆款热销】深邃黑（官方标配+2枚香芯）"
    assert clean_spec_text(raw1) == "深邃黑 官方标配+2枚香芯"

    raw2 = "[限时秒杀] 经典款-8G内存 "
    assert clean_spec_text(raw2) == "经典款-8G内存"

    assert clean_spec_text("") == ""
    assert clean_spec_text(None) == ""


def test_compute_spec_hash_determinism():
    item_id = "1100839041022"
    h1 = compute_spec_hash(item_id, "颜色", "深邃黑", "套餐", "标配")
    h2 = compute_spec_hash(item_id, "套餐", "标配", "颜色", "深邃黑")

    # 排序一致性：顺序颠倒哈希必须相同
    assert h1 == h2
    assert len(h1) == 16

    # 包含修饰词但核心规格相同的文本，哈希应当相同
    h3 = compute_spec_hash(item_id, "颜色", "【爆款推荐】深邃黑", "套餐", "标配")
    assert h1 == h3

    # 不同规格哈希必须不同
    h_other = compute_spec_hash(item_id, "颜色", "极光银", "套餐", "标配")
    assert h1 != h_other


def test_calculate_token_similarity():
    s1 = "深邃黑+2芯"
    s2 = "深邃黑（配2枚香芯）"
    sim = calculate_token_similarity(s1, s2)
    assert sim > 0.5

    # 完全无关的规格相似度低
    sim_low = calculate_token_similarity("极光银", "深邃黑")
    assert sim_low < 0.4


def test_match_best_upstream_sku():
    candidates = [
        {"upstream_sku_id": "sku_001", "upstream_sku_name": "经典极光银+2芯"},
        {"upstream_sku_id": "sku_002", "upstream_sku_name": "深邃黑+2原装香芯"},
        {"upstream_sku_id": "sku_003", "upstream_sku_name": "中国红豪华版+5芯"},
    ]

    target = "【爆款】深邃黑+2香芯"
    match, score = match_best_upstream_sku(target, candidates)
    assert match is not None
    assert match["upstream_sku_id"] == "sku_002"
    assert score >= 0.5

    # 无法匹配无关联项
    match_none, score_none = match_best_upstream_sku("未知规格绿色", candidates, threshold=0.7)
    assert match_none is None
