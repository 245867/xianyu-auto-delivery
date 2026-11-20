import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from geo_feed_simulator import (
    jitter_coordinates,
    generate_lognormal_dwell_time,
    should_trigger_feed_interaction,
    evaluate_exposure_health,
)


def test_jitter_coordinates():
    base_lat = 39.9042
    base_lon = 116.4074
    j_lat, j_lon = jitter_coordinates(base_lat, base_lon, radius_meters=500, seed=42)

    # 坐标必须产生微扰但不得偏离过远 (0.0001度约11米，500米约0.0045度)
    assert (j_lat, j_lon) != (base_lat, base_lon)
    assert abs(j_lat - base_lat) < 0.015
    assert abs(j_lon - base_lon) < 0.02


def test_generate_lognormal_dwell_time():
    times = [generate_lognormal_dwell_time(mean_seconds=8.0, seed=i) for i in range(20)]
    assert all(3.0 <= t <= 25.0 for t in times)
    # 平均值应在合理区间
    avg = sum(times) / len(times)
    assert 5.0 <= avg <= 15.0


def test_should_trigger_feed_interaction():
    # 概率 1.0 应必触发，0.0 应不触发
    assert should_trigger_feed_interaction(1.0) is True
    assert should_trigger_feed_interaction(0.0) is False


def test_evaluate_exposure_health_normal():
    # 正常曝光无拦截
    exposures = [{"date": "2026-11-18", "exposure": 1000}, {"date": "2026-11-19", "exposure": 1050}]
    score, is_silent, reason = evaluate_exposure_health(exposures, current_score=100.0, recent_risk_blocks=0)
    assert score == 100.0
    assert is_silent is False


def test_evaluate_exposure_health_drop_triggers_silent():
    # 曝光断崖下跌 70% (1000 -> 300)
    exposures = [{"date": "2026-11-18", "exposure": 1000}, {"date": "2026-11-19", "exposure": 300}]
    score, is_silent, reason = evaluate_exposure_health(exposures, current_score=70.0, recent_risk_blocks=0)
    # 70 - 35 = 35 < 60 -> 触发静默
    assert score == 35.0
    assert is_silent is True
    assert "断崖下跌" in reason


def test_evaluate_exposure_health_risk_blocks():
    # 连续 2 次安全拦截扣 50 分
    exposures = [{"date": "2026-11-18", "exposure": 1000}, {"date": "2026-11-19", "exposure": 1000}]
    score, is_silent, reason = evaluate_exposure_health(exposures, current_score=100.0, recent_risk_blocks=2)
    assert score == 50.0
    assert is_silent is True
    assert "平台安全拦截2次" in reason
