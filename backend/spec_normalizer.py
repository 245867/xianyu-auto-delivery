"""
Spec Normalizer & Multi-SKU Hash Engine
========================================
REA 落地自《拆解 48：鱼小铺多规格 SKU 拆单与库存动态同步熔断》
用于清洗鱼小铺复杂多规格口语文本、生成稳定的 SKU 规格哈希、
并提供与上游供应商候选 SKU 的模糊匹配与归一化能力。
"""

import hashlib
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple


def clean_spec_text(text: Optional[str]) -> str:
    """清洗规格口语化文本：去除修饰词括号、表情符号、特殊符号与多余空格。"""
    if not text:
        return ""

    # 1. 规范化 Unicode 字符（半角转全角/兼容字符归一）
    normalized = unicodedata.normalize("NFKC", str(text))

    # 2. 剥离营销标签方括号及其内部修饰词，例如 【爆款推荐】、【热卖】、[秒杀]、(特惠)
    normalized = re.sub(r"[【\[(（][^】\])）]*?(爆款|热卖|推荐|秒杀|特惠|特价|自用|全新|精选|促销)[^】\])）]*?[】\])）]", "", normalized, flags=re.I)

    # 3. 剥离无意义前后括号
    normalized = re.sub(r"^[【\[(（]|[】\])）]$", "", normalized)

    # 4. 去除特殊标点与 Emoji（保留中文字符、字母、数字、加号与减号）
    cleaned = re.sub(r"[^\w\u4e00-\u9fa5\+\-\.\/]", " ", normalized)

    # 5. 压缩多余空格
    return re.sub(r"\s+", " ", cleaned).strip()


def compute_spec_hash(
    item_id: str,
    spec_name_1: str = "",
    spec_value_1: str = "",
    spec_name_2: str = "",
    spec_value_2: str = "",
) -> str:
    """
    计算稳定唯一的规格特征哈希 (MD5 前 16 位)。
    对规格键值对进行排序，确保无论入参顺序如何，相同规格哈希完全一致。
    """
    c_n1 = clean_spec_text(spec_name_1)
    c_v1 = clean_spec_text(spec_value_1)
    c_n2 = clean_spec_text(spec_name_2)
    c_v2 = clean_spec_text(spec_value_2)

    specs = []
    if c_n1 or c_v1:
        specs.append(f"{c_n1}:{c_v1}")
    if c_n2 or c_v2:
        specs.append(f"{c_n2}:{c_v2}")

    # 字典排序保证确定性
    specs.sort()
    canonical_spec_str = "|".join(specs)
    raw_payload = f"{item_id.strip()}#{canonical_spec_str}"
    return hashlib.md5(raw_payload.encode("utf-8")).hexdigest()[:16]


def calculate_token_similarity(text_a: str, text_b: str) -> float:
    """计算两段文本的字符级与词元交并比 (Jaccard Similarity)。"""
    cleaned_a = clean_spec_text(text_a).lower()
    cleaned_b = clean_spec_text(text_b).lower()

    if not cleaned_a or not cleaned_b:
        return 0.0
    if cleaned_a == cleaned_b:
        return 1.0

    # 字符集合交并比
    set_a = set(cleaned_a.replace(" ", ""))
    set_b = set(cleaned_b.replace(" ", ""))
    intersection = set_a & set_b
    union = set_a | set_b
    if not union:
        return 0.0

    char_sim = len(intersection) / len(union)

    # 关键子串包含加分
    sub_bonus = 0.2 if (cleaned_a in cleaned_b or cleaned_b in cleaned_a) else 0.0
    return min(1.0, char_sim + sub_bonus)


def match_best_upstream_sku(
    order_spec_text: str,
    candidate_skus: List[Dict[str, Any]],
    threshold: float = 0.45,
) -> Tuple[Optional[Dict[str, Any]], float]:
    """
    在上游候选 SKU 列表中通过模糊比对找到最相符的规格项。
    candidate_skus 需包含 'upstream_sku_name' 或 'sku_name'。
    """
    if not candidate_skus:
        return None, 0.0

    best_match = None
    best_score = 0.0

    for candidate in candidate_skus:
        cand_name = candidate.get("upstream_sku_name") or candidate.get("sku_name") or ""
        score = calculate_token_similarity(order_spec_text, cand_name)
        if score > best_score:
            best_score = score
            best_match = candidate

    if best_score >= threshold:
        return best_match, round(best_score, 3)
    return None, round(best_score, 3)
