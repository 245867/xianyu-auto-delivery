"""
Order Chaser - 待付款订单三阶智能催付与防骚扰决策引擎
《拆解 57：商业闭源「全天候待付款订单三阶智能催付逼单与库存自愈系统」》

核心目标：
1. 三阶阶梯式温和催付状态机 (关怀提醒 -> 紧迫感释放名额 -> 超时结案释放库存)
2. 四层刚性合规防骚扰安全闸门 (夜间免打扰 / 买家会话熔断 / 2次频次硬顶 / 终态秒级拦截)
3. 催付文案智能插槽生成
"""

from datetime import datetime
from typing import Tuple, Optional, Any


STAGE_1_TEXT = "亲亲，看到您已经拍下宝贝啦~ 库房已为您预留好名额，确认无误支付后，仓库稍后将为您优先排单打包发出哦！"
STAGE_2_TEXT = "亲，这款宝贝非常抢手，由于当前库存紧张，系统将在 5 分钟后自动释放订单名额给其他排队的买家哦，喜欢的亲请尽快完成支付，避免错失优惠呢~"

UNPAID_STATUSES = frozenset({
    "processing",
    "waiting",
    "wait_buyer_pay",
    "unpaid",
    "waitbuyerpay",
    "pending",
})


def is_paid_or_closed(status: Any) -> bool:
    """判定订单是否已付款或已终结。"""
    normalized = str(status or "").strip().lower().replace("-", "_")
    if not normalized or normalized == "unknown":
        return False
    return normalized not in UNPAID_STATUSES


def is_night_quiet_hours(
    dt: Optional[datetime] = None,
    start_hour: int = 23,
    end_hour: int = 8,
) -> bool:
    """
    检查是否处于夜间免打扰静默期（默认 23:00 ~ 次日 08:00）。
    夜间强制暂停所有私聊催促消息，防止被买家投诉骚扰或平台打标。
    """
    if dt is None:
        dt = datetime.now()
    hour = dt.hour
    if start_hour > end_hour:
        # 跨天区间：例如 23点 至 次日8点
        return hour >= start_hour or hour < end_hour
    else:
        return start_hour <= hour < end_hour


def decide_chase_action(
    order_age_minutes: float,
    chase_count: int,
    buyer_has_replied: bool = False,
    is_terminal_status: bool = False,
    is_night_time: bool = False,
    stage_1_delay_min: float = 3.0,
    stage_2_delay_min: float = 10.0,
    timeout_expire_min: float = 30.0,
    max_chase_attempts: int = 2,
    *,
    buyer_replied: Optional[bool] = None,
    order_status: Optional[str] = None,
) -> Tuple[str, str]:
    """
    催付状态机决策引擎（纯函数，无副作用）：
    输入当前订单待付时长、已催付次数及安全闸门信号，输出下一步动作与判决理由。

    返回动作枚举：
    - SKIP_TERMINATED: 订单已付款、已完成或已关闭，秒级阻断
    - SKIP_BUYER_TALKING: 买家在会话中已有发言，挂起催付防打扰
    - SKIP_NIGHT_SILENT: 处于夜间静默期，推迟催付
    - SKIP_MAX_ATTEMPTS: 已达单单催付次数上限（默认 2 次）
    - EXPIRE_RELEASE: 拍下超时未付款，触发库存预占释放并优雅关单
    - CHASE_STAGE_1: 触发第 1 阶柔和关怀催付
    - CHASE_STAGE_2: 触发第 2 阶紧迫感逼单催付
    - WAIT_NEXT_WINDOW: 处于正常等待窗口，暂不动作
    """
    has_replied = buyer_has_replied or bool(buyer_replied)
    terminal = is_terminal_status or (order_status is not None and is_paid_or_closed(order_status))

    # 闸门 1：终态秒级拦截
    if terminal:
        return "SKIP_TERMINATED", "订单已付款或已终结，终止催付"

    # 闸门 2：买家回复沟通熔断（极高优先级，防机器人死板复读惹恼买家）
    if has_replied:
        return "SKIP_BUYER_TALKING", "买家在会话中已有最新回复，挂起催付交由人工/意图接管"

    # 闸门 3：超时未付释放自愈（超期放弃，释放库存锁）
    if order_age_minutes >= timeout_expire_min:
        return "EXPIRE_RELEASE", f"订单待付款超过 {timeout_expire_min:.0f} 分钟，执行超时结案并释放预占库存"

    # 闸门 4：频次硬顶限制
    if chase_count >= max_chase_attempts:
        return "SKIP_MAX_ATTEMPTS", f"已达单单最大催付次数 ({max_chase_attempts}次)，停止催付"

    # 闸门 5：夜间免打扰静默
    if is_night_time:
        return "SKIP_NIGHT_SILENT", "处于夜间免打扰静默时段，推迟发送"

    # 阶梯 2：紧迫感倒计时逼单
    if order_age_minutes >= stage_2_delay_min and chase_count == 1:
        return "CHASE_STAGE_2", f"订单待付款满 {stage_2_delay_min:.0f} 分钟，触发第 2 阶紧迫感逼单催付"

    # 阶梯 1：关怀型柔和提醒
    if order_age_minutes >= stage_1_delay_min and chase_count == 0:
        return "CHASE_STAGE_1", f"订单待付款满 {stage_1_delay_min:.0f} 分钟，触发第 1 阶柔和排单关怀"

    return "WAIT_NEXT_WINDOW", "未到催付时间窗口，保持等待"


def get_chase_message(stage: int, custom_text: Optional[str] = None) -> str:
    """获取催付标准文案。"""
    if custom_text:
        return custom_text
    if stage == 1:
        return STAGE_1_TEXT
    if stage == 2:
        return STAGE_2_TEXT
    return ""
