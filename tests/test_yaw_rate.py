"""Tests for YawRateConsistencyDetector.

The detector flags when |reported_yaw − gps_yaw_rate| > 90 °/s.

Common setup: both positions ~22 m apart (> MIN_DISTANCE_M = 5 m),
100 ms gap, speed 72 km/h (> SPEED_GATE_KMH = 20).

GPS yaw rate = heading_change / elapsed.

Flagged case: heading changes 18° in 100 ms → GPS yaw = 180 °/s; reported = 0 °/s → diff = 180 >> 90.
Clean case:   heading changes 1° in 100 ms  → GPS yaw =  10 °/s; reported = 10 °/s → diff = 0.

Multi-message confirmation requires 2 consecutive violations before flagging.
"""

import pytest
from detectors.yaw_rate_consistency import YawRateConsistencyDetector
from conftest import make_bsm

LAT1, LON1 = 41.0,    -81.0
LAT2, LON2 = 41.0002, -81.0   # ~22 m north
LAT3, LON3 = 41.0004, -81.0   # ~44 m north


@pytest.fixture
def det(det_config):
    return YawRateConsistencyDetector(
        det_config.section("yaw_rate"), det_config.confirm_n
    )


def test_first_message_returns_none(det):
    assert det.check(make_bsm(lat_deg=LAT1, secmark=0)) is None


def test_consistent_yaw_returns_none(det):
    # heading Δ = 1° in 100 ms → GPS yaw = 10 °/s; reported yaw = 10 °/s → diff = 0
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0,
                        yaw_deg_s=10.0, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=1.0,
                                 yaw_deg_s=10.0, secmark=100))
    assert result is None


def test_inconsistent_yaw_requires_two_consecutive(det):
    # heading Δ = 18° in 100 ms → GPS yaw = 180 °/s; reported = 0 °/s → diff = 180 >> 90
    # First violation → streak = 1, None; second (heading swings back) → confirmed, fires.
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0,
                        yaw_deg_s=0.0, secmark=0))
    result1 = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=18.0,
                                   yaw_deg_s=0.0, secmark=100))
    assert result1 is None   # streak = 1

    # heading swings back to 0°: Δ = -18° → GPS yaw = -180 °/s; reported = 0 → diff = 180
    result2 = det.check(make_bsm(lat_deg=LAT3, lon_deg=LON3, heading_deg=0.0,
                                   yaw_deg_s=0.0, secmark=200))
    assert result2 is not None
    assert result2["misbehavior"] == "yaw_rate_inconsistency"
    assert result2["yaw_diff_deg_s"] > 90.0


def test_streak_resets_on_clean_observation(det):
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0,
                        yaw_deg_s=0.0, secmark=0))
    det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=18.0,
                        yaw_deg_s=0.0, secmark=100))    # streak = 1

    # Clean: 1° heading change in 100 ms → GPS yaw = 10 °/s; reported = 10 °/s → diff = 0
    det.check(make_bsm(lat_deg=LAT3, lon_deg=LON3, heading_deg=19.0,
                        yaw_deg_s=10.0, secmark=200))   # clean → streak = 0

    # Next violation starts a fresh streak — should not fire
    result = det.check(make_bsm(lat_deg=41.0006, lon_deg=LON1, heading_deg=1.0,
                                  yaw_deg_s=0.0, secmark=300))
    assert result is None   # fresh streak = 1, not yet confirmed


def test_below_speed_gate_current_returns_none(det):
    # Current speed = 5 km/h < SPEED_GATE_KMH (20)
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=18.0,
                                 speed_kmh=5.0, yaw_deg_s=0.0, secmark=100))
    assert result is None


def test_below_speed_gate_previous_returns_none(det):
    # Previous speed = 5 km/h < SPEED_GATE_KMH (20) — gate applies to both ends
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0,
                        speed_kmh=5.0, yaw_deg_s=0.0, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=18.0,
                                 speed_kmh=72.0, yaw_deg_s=0.0, secmark=100))
    assert result is None


def test_poor_gps_accuracy_returns_none(det):
    # accuracy_m = 10 m > MAX_GPS_ACCURACY_M (5 m) — skip even large yaw diff
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0,
                        yaw_deg_s=0.0, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=18.0,
                                 yaw_deg_s=0.0, secmark=100, accuracy_m=10.0))
    assert result is None


def test_good_gps_accuracy_does_not_suppress(det):
    # accuracy_m = 2 m ≤ MAX_GPS_ACCURACY_M (5 m) — should not suppress
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0,
                        yaw_deg_s=0.0, secmark=0, accuracy_m=2.0))
    result1 = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=18.0,
                                   yaw_deg_s=0.0, secmark=100, accuracy_m=2.0))
    assert result1 is None   # streak = 1

    result2 = det.check(make_bsm(lat_deg=LAT3, lon_deg=LON3, heading_deg=0.0,
                                   yaw_deg_s=0.0, secmark=200, accuracy_m=2.0))
    assert result2 is not None


def test_too_little_displacement_returns_none(det):
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0, secmark=0))
    result = det.check(make_bsm(lat_deg=41.000001, lon_deg=LON1, heading_deg=18.0,
                                 yaw_deg_s=0.0, secmark=100))
    assert result is None


def test_missing_secmark_returns_none(det):
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=18.0,
                                 yaw_deg_s=0.0, secmark=None))
    assert result is None
