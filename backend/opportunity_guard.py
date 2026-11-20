"""
选品搜索风控守卫（补齐③）—— 思路移植自 Usagi-org/ai-goofish-monitor（MIT）
`src/scraper.py` 的 RiskControlError/LoginRequiredError + failure_guard 失败保护。

风控信号（与上游一致，在错误文本/页面特征里识别）：
- baxia-dialog / J_MIDDLEWARE_FRAME_WIDGET（阿里风控弹层/滑块容器）
- FAIL_SYS_USER_VALIDATE（接口侧人机校验）
- 登录跳转 / login（登录态失效）
- 滑块 / 验证 / 风控 / punish（中文特征）

保护策略：
- 连续失败 ≥3 次或命中任一风控信号 → 冷却（风控 30 分钟 / 普通失败 10 分钟）；
- 冷却期内 /api/opportunity/analyze 直接拒绝并提示剩余时间与原因；
- 任意一次成功搜索自动清零；提供手动解除接口。
状态落 data/search_guard.json，重启不丢。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / "data" / "search_guard.json"

RISK_SIGNALS = [
    "baxia-dialog", "J_MIDDLEWARE_FRAME_WIDGET", "FAIL_SYS_USER_VALIDATE",
    "punish", "风控", "滑块", "滑动验证", "验证码",
    "登录", "login", "Login required",
]
COOLDOWN_RISK_SECONDS = 30 * 60    # 命中风控信号：30 分钟
COOLDOWN_PLAIN_SECONDS = 10 * 60   # 普通连续失败：10 分钟
FAIL_THRESHOLD = 3


def _load() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(state: Dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _find_risk_signal(text: str) -> Optional[str]:
    low = str(text or "").lower()
    for sig in RISK_SIGNALS:
        if sig.lower() in low:
            return sig
    return None


def check_allowed() -> Tuple[bool, str]:
    """冷却期内拒绝搜索。返回 (ok, 提示)。"""
    state = _load()
    until = float(state.get("cooldown_until", 0) or 0)
    if until <= 0:
        return True, ""
    remain = int(until - time.time())
    if remain <= 0:
        return True, ""
    mins = remain // 60 + 1
    return False, (f"搜索已进入风控冷却（{mins} 分钟后自动恢复）。"
                   f"原因：{state.get('last_reason', '连续搜索失败')}。"
                   f"建议：打开闲鱼网页确认登录态，或稍后再试；紧急可在管理页手动解除。")


def record_success() -> None:
    """任意一次成功搜索自动清零并解除冷却。"""
    _save({"consecutive_fail": 0, "cooldown_until": 0, "last_reason": ""})


def record_failure(error_text: str) -> Dict[str, Any]:
    """记录一次失败；命中风控信号或连续失败达阈值则进入冷却。"""
    state = _load()
    fails = int(state.get("consecutive_fail", 0) or 0) + 1
    signal = _find_risk_signal(error_text)
    cooldown = 0
    reason = ""
    if signal:
        cooldown = int(time.time()) + COOLDOWN_RISK_SECONDS
        reason = f"检测到风控信号「{signal}」"
    elif fails >= FAIL_THRESHOLD:
        cooldown = int(time.time()) + COOLDOWN_PLAIN_SECONDS
        reason = f"连续 {fails} 次搜索失败"
    state.update({
        "consecutive_fail": fails,
        "last_error": str(error_text or "")[:300],
        "last_reason": reason or state.get("last_reason", ""),
        "cooldown_until": cooldown or int(state.get("cooldown_until", 0) or 0),
    })
    _save(state)
    if cooldown:
        # 进入冷却 = 需要人介入的事件，立即推送
        try:
            from notify_service import send_event
            send_event("risk", "闲鱼风控告警：搜索已冷却",
                       f"原因：{reason or '连续失败'}\n"
                       f"连续失败：{fails} 次\n错误：{str(error_text or '')[:200]}",
                       level="error")
        except Exception:
            pass
    return {"risk_signal": signal, "consecutive_fail": fails,
            "cooldown_active": bool(cooldown)}


def reset_guard() -> None:
    _save({"consecutive_fail": 0, "cooldown_until": 0, "last_reason": ""})


def get_guard_status() -> Dict[str, Any]:
    state = _load()
    until = float(state.get("cooldown_until", 0) or 0)
    remain = max(0, int(until - time.time())) if until > 0 else 0
    return {
        "consecutive_fail": int(state.get("consecutive_fail", 0) or 0),
        "last_error": state.get("last_error", ""),
        "last_reason": state.get("last_reason", ""),
        "cooldown_active": remain > 0,
        "cooldown_remain_seconds": remain,
        "fail_threshold": FAIL_THRESHOLD,
    }


__all__ = ["check_allowed", "record_success", "record_failure",
           "reset_guard", "get_guard_status"]
