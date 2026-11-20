"""
Voucher Channel Router & Charge Account Extractor
==================================================
REA 落地自《拆解 52：商业闭源动态充值卡密多渠道分流与超时异步回调对账引擎》
用于从买家留言中智能提取充值账号（手机号/QQ/邮箱），并提供多供应商渠道的
优先级故障转移（Failover）与熔断降级选择器。
"""

import re
import time
from typing import Dict, List, Optional, Tuple


def extract_charge_account(text: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    从买家留言、备注或聊天文本中智能提取充值目标账号。
    :return: (account_str, account_type) 或 (None, None)
             account_type 可选值: 'phone', 'qq', 'email', 'generic'
    """
    if not text:
        return None, None

    raw = str(text).strip()

    # 1. 优先匹配中国大陆 11 位手机号 (支持 +86、空格与连字符)
    # 过滤掉 +86 前缀
    phone_search = re.search(r"(?:\+?86[- ]?)?(1[3-9]\d[- ]?\d{4}[- ]?\d{4})\b", raw)
    if phone_search:
        clean_phone = re.sub(r"\D", "", phone_search.group(1))
        if len(clean_phone) == 11 and clean_phone.startswith("1"):
            return clean_phone, "phone"

    # 2. 匹配标准电子邮箱
    email_search = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", raw)
    if email_search:
        return email_search.group(0).strip(), "email"

    # 3. 匹配 QQ 号 (5-11 位非 0 开头纯数字)
    qq_search = re.search(r"(?:qq|扣扣|Q号)?[:：\s]*([1-9]\d{4,10})\b", raw, flags=re.I)
    if qq_search:
        return qq_search.group(1).strip(), "qq"

    # 4. 纯数字账号保底 (5 位以上)
    digits = re.findall(r"\b\d{5,12}\b", raw)
    if digits:
        return digits[0], "generic"

    return None, None


class VoucherChannelRouter:
    """
    多渠道优先级与故障转移路由器 (Failover Router)。
    支持按商品配置渠道优先级，并在主渠道断货时自动顺延至备选渠道。
    """

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: int = 300):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        # channel_code -> consecutive_failures
        self._consecutive_failures: Dict[str, int] = {}
        # channel_code -> cooldown_until_timestamp
        self._cooldown_until: Dict[str, float] = {}

    def is_channel_available(self, channel_code: str) -> bool:
        """检查渠道是否在熔断冷却期内。"""
        now = time.time()
        until = self._cooldown_until.get(channel_code, 0.0)
        return now >= until

    def record_channel_failure(self, channel_code: str, reason: str = "") -> None:
        """记录渠道调用失败，达到阈值时触发熔断冷却。"""
        streak = self._consecutive_failures.get(channel_code, 0) + 1
        self._consecutive_failures[channel_code] = streak
        if streak >= self.failure_threshold:
            self._cooldown_until[channel_code] = time.time() + self.cooldown_seconds

    def record_channel_success(self, channel_code: str) -> None:
        """调用成功后重置该渠道失败计数与熔断状态。"""
        self._consecutive_failures[channel_code] = 0
        self._cooldown_until.pop(channel_code, None)

    def select_next_channel(
        self,
        candidate_channels: List[str],
        attempted_channels: Optional[List[str]] = None,
        enforce_health_check: bool = True,
    ) -> Optional[str]:
        """
        在有序候选渠道列表中，按优先级选取下一个未曾尝试且健康的渠道。
        :param candidate_channels: 配置的渠道列表（优先级从左至右）
        :param attempted_channels: 该订单已尝试失败的渠道列表
        :param enforce_health_check: 是否过滤处于熔断冷却期的渠道
        :return: 选中的渠道 code，若全无可用则返回 None
        """
        attempted_set = set(attempted_channels or [])

        # 第一轮尝试：挑选未尝试且处于健康状态的渠道
        for channel in candidate_channels:
            if channel in attempted_set:
                continue
            if enforce_health_check and not self.is_channel_available(channel):
                continue
            return channel

        # 若健康渠道均已耗尽，且还有未尝试渠道，降级允许尝试冷却中渠道保底
        for channel in candidate_channels:
            if channel not in attempted_set:
                return channel

        return None


# 全局默认单例
router = VoucherChannelRouter()
