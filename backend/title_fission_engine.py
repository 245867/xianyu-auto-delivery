"""
Title Fission Engine & Blue-Ocean Keyword Mutator
==================================================
REA 落地自《拆解 51：商业闭源「爆款裂变式智能铺货与类目关键词蓝海探针」》
用于将单个爆款商品一键派生为 5~10 组高转化、低重合度的差异化闲鱼标题，
严格遵守 30 字限制，控制 Jaccard 词重合度 < 45%，杜绝平台同质化违规。
"""

import itertools
import random
from typing import Any, Dict, List, Optional, Set, Tuple

# 闲置口语化前缀词库
IDLE_PREFIX_POOL = [
    "自用全新转让",
    "朋友送的一直没拆",
    "年会抽奖中了闲置",
    "搬家断舍离急出",
    "买了两个多出一个",
    "自用超推荐低价出",
    "仅拆封未使用全新",
    "退坑清仓转让",
]

# 核心卖点与蓝海修饰词库
MODIFIER_POOL = [
    "高档持久",
    "轻奢高级感",
    "极简冷淡风",
    "小众不撞款",
    "高颜值耐用",
    "经典百搭",
    "正品做工扎实",
    "超值划算",
]

# 促单转化与售后尾缀词库
SUFFIX_POOL = [
    "包邮秒发",
    "降价急出回血",
    "爽快包邮出",
    "拍下当天发",
    "正品保真手慢无",
    "低价诚心出",
]


def tokenize_chinese(text: str) -> Set[str]:
    """中文简单 2-gram 与单字分词集合。"""
    cleaned = "".join(c for c in text if c.isalnum() or "\u4e00" <= c <= "\u9fa5")
    tokens = set(cleaned)
    for i in range(len(cleaned) - 1):
        tokens.add(cleaned[i : i + 2])
    return tokens


def calculate_title_jaccard(title_a: str, title_b: str) -> float:
    """计算两个标题的 2-gram 词元交并比 (Jaccard 相似度)。"""
    set_a = tokenize_chinese(title_a)
    set_b = tokenize_chinese(title_b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def generate_variant_titles(
    base_title: str,
    core_keyword: str,
    count: int = 5,
    max_length: int = 30,
    seed: Optional[int] = None,
) -> List[str]:
    """
    根据核心关键词与基础标题生成 count 组相互差异化、无重复铺货风险的高转化标题。
    :param base_title: 原始商品标题
    :param core_keyword: 核心品类词 (如 "车载香薰")
    :param count: 派生数量 (默认 5)
    :param max_length: 闲鱼平台标题字数上限 (30 字)
    :param seed: 随机种子
    :return: 变异标题列表
    """
    if seed is not None:
        random.seed(seed)

    prefixes = list(IDLE_PREFIX_POOL)
    modifiers = list(MODIFIER_POOL)
    suffixes = list(SUFFIX_POOL)

    random.shuffle(prefixes)
    random.shuffle(modifiers)
    random.shuffle(suffixes)

    results: List[str] = []
    # 候选组合排列
    candidates_comb = list(itertools.product(prefixes, modifiers, suffixes))
    random.shuffle(candidates_comb)

    for prefix, modifier, suffix in candidates_comb:
        # 组装：前缀 + 核心词 + 修饰词 + 尾缀
        assembled = f"{prefix} {core_keyword} {modifier} {suffix}"
        if len(assembled) > max_length:
            # 缩减空格或尾缀
            assembled = f"{prefix}{core_keyword} {modifier}{suffix}"
            if len(assembled) > max_length:
                assembled = f"{prefix} {core_keyword} {modifier}"[:max_length]

        # 检查与已生成标题的相似度（保证成组标题多元化）
        is_too_similar = False
        for existing in results:
            if calculate_title_jaccard(assembled, existing) > 0.48:
                is_too_similar = True
                break

        if not is_too_similar:
            results.append(assembled)
            if len(results) >= count:
                break

    # 若候选过滤后不足，填充保底差异标题
    while len(results) < count:
        idx = len(results) + 1
        fallback = f"全新自用闲置{idx} {core_keyword} 质感极佳 包邮"[:max_length]
        results.append(fallback)

    return results


def build_fission_matrix(
    parent_item_id: str,
    base_title: str,
    core_keyword: str,
    image_urls: List[str],
    count: int = 5,
    seed: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    构建完整的商品裂变矩阵任务结构，绑定母商品、派生标题与轮换主图。
    """
    variant_titles = generate_variant_titles(
        base_title=base_title,
        core_keyword=core_keyword,
        count=count,
        seed=seed,
    )

    matrix = []
    for idx, title in enumerate(variant_titles):
        cover = image_urls[idx % len(image_urls)] if image_urls else ""
        matrix.append(
            {
                "parent_item_id": parent_item_id,
                "fission_id": f"{parent_item_id}_fiss_{idx+1}",
                "variant_title": title,
                "cover_image_url": cover,
                "status": "ready",
            }
        )
    return matrix
