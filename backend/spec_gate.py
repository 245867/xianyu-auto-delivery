"""规格闸的纯判定逻辑（可单测，不碰数据库、不碰浏览器）。

背景（拆解 26）：订单侧没有 skuId，只有文本 `规格名:规格值`。所以"买家到底选了哪一档"
只能靠三层证据叠出来：

1. 商品级声明：发布成功那一刻写死的 `spec_group_count`（0/1/2）；
2. 订单侧解析：`utils/order_detail_fetcher.py` 解析出的组数；
3. 卡券侧匹配：`cards` 表里那条卡券自己的规格（一维或两维）。

这个模块只负责第 1、2 层的一致性判定。核心口径是"对不上就停"：
**发不出货是看得见的失败，发错货（买家收到别人的卡密）是看不见的失败。**
所以任何一处拿不准都返回 False，绝不退回到"普通关键词卡"。
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

# 判定结果的原因码，直接写进日志，方便事后按原因统计
REASON_OK = "ok"
REASON_PARSE_FAILED = "parse_failed"      # 声明有规格，订单里读不出来
REASON_GROUP_MISMATCH = "group_mismatch"  # 声明 2 组，订单只解析出 1 组（或反过来）


def needs_detail_fetch(is_multi_spec: bool, declared_groups: Any) -> bool:
    """要不要为这个订单去拉订单详情（拉一次要起浏览器，不是免费动作）。

    只有"预期有规格"才值得拉：商品被标记为多规格，或发布时声明了 >=1 组。
    declared_groups 为 None 表示未声明（老商品），不能当成 0，也不能当成 1。
    """
    if is_multi_spec:
        return True
    try:
        return int(declared_groups) > 0
    except (TypeError, ValueError):
        return False


def check_spec_shape(is_multi_spec: bool, declared_groups: Any, parsed_groups: Any) -> Tuple[bool, str]:
    """第 1、2 层的一致性判定。

    参数：
        is_multi_spec: item_info.is_multi_spec（商品级标记）
        declared_groups: item_info.spec_group_count；None = 未声明
        parsed_groups: 订单解析出的规格组数（0/1/2）

    返回 (通过?, 原因码)。False 时调用方必须停止自动发货。
    """
    expects_spec = needs_detail_fetch(is_multi_spec, declared_groups)

    try:
        parsed = int(parsed_groups or 0)
    except (TypeError, ValueError):
        parsed = 0

    if expects_spec and parsed <= 0:
        return False, REASON_PARSE_FAILED

    if declared_groups is not None and expects_spec:
        try:
            declared = int(declared_groups)
        except (TypeError, ValueError):
            return False, REASON_GROUP_MISMATCH
        if parsed != declared:
            return False, REASON_GROUP_MISMATCH

    return True, REASON_OK


def spec_desc(spec_name: Any, spec_value: Any, spec_name_2: Any = None, spec_value_2: Any = None) -> str:
    """统一的规格展示：`颜色:黑` 或 `颜色:黑;尺码:L`（与平台订单页的拼法一致）。"""
    desc = f"{spec_name or ''}:{spec_value or ''}"
    if spec_name_2:
        desc += f";{spec_name_2}:{spec_value_2 or ''}"
    return desc


def card_spec_key(card: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """把卡券的规格压成可比较的四元组；空值统一成 ''，避免 None/'' 被判成不同档。"""
    return (
        (card.get("spec_name") or "").strip(),
        (card.get("spec_value") or "").strip(),
        (card.get("spec_name_2") or "").strip(),
        (card.get("spec_value_2") or "").strip(),
    )


def order_spec_key(spec_name: Any, spec_value: Any, spec_name_2: Any = None, spec_value_2: Any = None) -> Tuple[str, str, str, str]:
    """订单解析结果压成同样的四元组，供与卡券逐字比较。"""
    return (
        (spec_name or "").strip(),
        (spec_value or "").strip(),
        (spec_name_2 or "").strip(),
        (spec_value_2 or "").strip(),
    )