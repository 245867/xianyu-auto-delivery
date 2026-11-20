"""
Geo Feed Simulator & Account Health Evaluator
==============================================
REA 落地自《拆解 49：商业闭源多账号「矩阵流量养号与同城拟态权重系统」》
提供 LBS 经纬度高斯微扰、拟人对数正态长尾停留时长生成、以及 48 小时曝光断崖隐形降权评估。
"""

import math
import random
from typing import Any, Dict, List, Optional, Tuple


def jitter_coordinates(
    base_lat: float,
    base_lon: float,
    radius_meters: float = 500.0,
    seed: Optional[int] = None,
) -> Tuple[float, float]:
    """
    对商圈基准经纬度注入二维高斯随机微扰（模拟基站切换与移动），保留同城商圈权重同时打散固定坐标。
    :param base_lat: 基础纬度 (例如 39.9042)
    :param base_lon: 基础经度 (例如 116.4074)
    :param radius_meters: 扰动半径标准差（默认 500 米）
    :param seed: 可选随机种子（用于测试确定性）
    :return: (new_lat, new_lon) 精确到 6 位小数
    """
    if seed is not None:
        random.seed(seed)

    # 纬度 1 度约 111,320 米；经度 1 度约 111,320 * cos(lat) 米
    meters_per_lat = 111320.0
    meters_per_lon = 111320.0 * math.cos(math.radians(base_lat))
    if abs(meters_per_lon) < 1.0:
        meters_per_lon = 111320.0

    # 二维高斯微扰
    dx = random.gauss(0, radius_meters / 2.0)
    dy = random.gauss(0, radius_meters / 2.0)

    # 截断最大微扰范围在 2.5 倍半径内，防止偏离原商圈
    max_dist = radius_meters * 2.5
    dist = math.hypot(dx, dy)
    if dist > max_dist:
        scale = max_dist / dist
        dx *= scale
        dy *= scale

    new_lat = base_lat + (dy / meters_per_lat)
    new_lon = base_lon + (dx / meters_per_lon)
    return round(new_lat, 6), round(new_lon, 6)


def generate_lognormal_dwell_time(
    mean_seconds: float = 8.0,
    sigma: float = 0.45,
    min_s: float = 3.0,
    max_s: float = 25.0,
    seed: Optional[int] = None,
) -> float:
    """
    生成服从对数正态分布 (Log-normal) 的拟人停留时长，符合人类长尾浏览特征。
    """
    if seed is not None:
        random.seed(seed)

    mu = math.log(max(1.0, mean_seconds))
    raw_time = random.lognormvariate(mu, sigma)
    clamped_time = max(min_s, min(max_s, raw_time))
    return round(clamped_time, 2)


def should_trigger_feed_interaction(probability: float = 0.08, seed: Optional[int] = None) -> bool:
    """按指定概率触发点赞/超赞/收藏等买家正向互动。"""
    if seed is not None:
        random.seed(seed)
    return random.random() < probability


def evaluate_exposure_health(
    exposures_48h: List[Dict[str, Any]],
    current_score: float = 100.0,
    recent_risk_blocks: int = 0,
) -> Tuple[float, bool, str]:
    """
    评估账号健康度得分并判断是否触发隐形降权（小黑屋 / Silent-Warming）。
    :param exposures_48h: 包含至少最近两日曝光量的列表 [{'date': '...', 'exposure': 1200}, ...]
    :param current_score: 当前健康分 (0.0 ~ 100.0)
    :param recent_risk_blocks: 近期平台安全拦截次数
    :return: (new_score, is_silent_mode, reason)
    """
    score = current_score
    reasons = []

    # 1. 平台安全拦截扣分：每次拦截扣 25 分
    if recent_risk_blocks > 0:
        penalty = recent_risk_blocks * 25.0
        score = max(0.0, score - penalty)
        reasons.append(f"平台安全拦截{recent_risk_blocks}次(-{penalty}分)")

    # 2. 48 小时曝光断崖斜率检测
    if len(exposures_48h) >= 2:
        prev_exp = float(exposures_48h[-2].get("exposure", 0))
        cur_exp = float(exposures_48h[-1].get("exposure", 0))
        if prev_exp > 50:  # 具有统计基数
            drop_rate = (prev_exp - cur_exp) / prev_exp
            if drop_rate >= 0.60:
                score = max(0.0, score - 35.0)
                reasons.append(f"48h曝光断崖下跌{drop_rate*100:.1f}%(-35分)")
            elif drop_rate <= -0.10 and recent_risk_blocks == 0:
                # 曝光回升且无拦截，触发自愈回血 (+10分)
                score = min(100.0, score + 10.0)
                reasons.append("曝光回升自愈(+10分)")
    elif recent_risk_blocks == 0 and score < 100.0:
        # 静默自愈回血
        score = min(100.0, score + 15.0)
        reasons.append("静默养号自愈(+15分)")

    # 3. 熔断与恢复阈值判定
    # 低于 60 分切入静默模式；回升至 80 分以上方可恢复
    is_silent = score < 60.0
    if not is_silent and current_score < 60.0 and score < 80.0:
        # 处于恢复期但未达到 80 分警戒线，保持静默观察
        is_silent = True
        reasons.append("静默恢复期观察中(需>=80分)")

    reason_str = "; ".join(reasons) if reasons else "状态健康"
    return round(score, 1), is_silent, reason_str
