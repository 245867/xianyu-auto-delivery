"""
Bargain Concession Engine & Urgency Sales Closer
=================================================
REA 落地自《拆解 55：商业闭源「IM 智能促单话术流与限时议价倒计时逼单引擎」》
提供三阶阶梯让步曲线计算、底价刚性守门、以及 15 分钟限时逼单倒计时话术生成。
"""

from typing import Optional, Tuple


def calculate_concession_price(
    original_price: float,
    price_floor: float,
    buyer_offer: Optional[float] = None,
    round_number: int = 1,
    step_delta: float = 1.0,
) -> Tuple[float, str, str]:
    """
    三阶阶梯让步决策引擎：
    - 轮次 1：礼貌婉拒，突出商品品质与性价比优势（不降价）；
    - 轮次 2：微额让步（降 step_delta 元），但绝不跌破底价；
    - 轮次 3+：刚性锁定在底价，不再额外折价；若买家乱砍跌破底价，坚决拒绝。

    :return: (final_price, decision_code, script_template)
             decision_code: 'REJECT_POLITE', 'AGREE_STEP_DOWN', 'LOCK_FLOOR', 'REJECT_EXTREME'
    """
    orig_p = round(float(original_price), 2)
    p_floor = round(float(price_floor), 2)

    # 1. 极端恶意大刀砍价拦截 (买家出价低于底价的 85% 或砍幅 > 50%)
    if buyer_offer is not None and float(buyer_offer) > 0:
        offer = round(float(buyer_offer), 2)
        if offer < p_floor * 0.85:
            return (
                orig_p,
                "REJECT_EXTREME",
                "亲亲，大刀真的砍不动呀！小店商品均为精选高品质，标价已经接近成本，实在没办法让这么多，请您理解呢~",
            )

    # 2. 第 1 轮：守住原价，礼貌婉拒
    if round_number <= 1:
        return (
            orig_p,
            "REJECT_POLITE",
            "亲，这个是全新正品/自用精选，成色非常好且包邮，目前标价已经非常划算了，建议您直接拍下，当天就能安排发出哦~",
        )

    # 3. 第 2 轮：小额折中让步 (减 step_delta 元)
    if round_number == 2:
        proposed = round(orig_p - step_delta, 2)
        final_p = max(proposed, p_floor)
        discount = round(orig_p - final_p, 2)
        if discount > 0:
            return (
                final_p,
                "AGREE_STEP_DOWN",
                f"看您诚心想要，我也爽快点，跟您交个朋友帮您减 ¥{discount:.2f}！您拍下我帮您改价，请尽快完成支付哦~",
            )
        else:
            return (
                p_floor,
                "LOCK_FLOOR",
                f"亲，标价 ¥{orig_p:.2f} 已经到底价了，实在没法再少啦，拍下马上为您打包发出！",
            )

    # 4. 第 3 轮及以上：守死底价红线
    final_p = p_floor
    discount = round(orig_p - final_p, 2)
    if discount > 0:
        return (
            p_floor,
            "LOCK_FLOOR",
            f"亲，真的已经是赔本赚吆喝的底价 ¥{p_floor:.2f} 啦，一分钱都少不了啦！诚心要的话直接拍下，给您附送赠品哦~",
        )
    return (
        orig_p,
        "REJECT_POLITE",
        "亲亲，实在少不了啦，已经是全网最低价啦，感谢您的支持！",
    )


def generate_urgency_countdown_message(
    concession_price: float,
    original_price: float,
    expiry_minutes: int = 15,
) -> str:
    """
    生成高转化率的 15 分钟限时逼单倒计时话术，植入紧迫感。
    """
    con_p = round(float(concession_price), 2)
    orig_p = round(float(original_price), 2)
    discount = round(orig_p - con_p, 2)

    return (
        f"【限时专属优惠】已为您成功保留优惠 ¥{discount:.2f}，折后价仅需 ¥{con_p:.2f}！"
        f"拍下系统将自动减价。优惠名额仅锁定 {expiry_minutes} 分钟，"
        f"超时未付款系统将自动恢复原价 ¥{orig_p:.2f} 并释放库存给其他买家哦，手慢无~"
    )
