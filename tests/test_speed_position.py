"""Tests for SpeedPositionConsistencyDetector.

Geometry: at lat=41°, 1° lat ≈ 111 319 m.

Flagged test: 0.002° north ≈ 222 m in 100 ms → implied ≈ 8 000 km/h,
reported 72 km/h → diff ≈ 7 928 km/h >> MAX_SPEED_DIFF_KMH (500).

Multi-message confirmation requires 2 consecutive violations before flagging.
"""

import pytest
from detectors.speed_position_consistency import SpeedPositionConsistencyDetector
from conftest import make_bsm

LAT1, LON1 = 41.0,   -81.0
LAT2, LON2 = 41.002, -81.0   # ~222 m north — triggers SPC violation
LAT3, LON3 = 41.004, -81.0   # another ~222 m north


@pytest.fixture
def det(det_config):
    return SpeedPositionConsistencyDetector(
        det_config.section("speed_position"), det_config.confirm_n
    )


def test_first_message_returns_none(det):
    assert det.check(make_bsm(lat_deg=LAT1, secmark=0)) is None


def test_tiny_displacement_returns_none(det):
    # ~0.1 m — below MIN_DISTANCE_M (5 m), skipped
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, secmark=0))
    result = det.check(make_bsm(lat_deg=41.000001, lon_deg=LON1, secmark=100))
    assert result is None


def test_large_position_jump_requires_two_consecutive(det):
    # First violation returns None (streak = 1)
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, speed_kmh=72.0, secmark=0))
    result1 = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, speed_kmh=72.0, secmark=100))
    assert result1 is None

    # Second consecutive violation fires
    result2 = det.check(make_bsm(lat_deg=LAT3, lon_deg=LON3, speed_kmh=72.0, secmark=200))
    assert result2 is not None
    assert result2["misbehavior"] == "speed_position_inconsistency"
    assert abs(result2["diff_kmh"]) > 500.0


def test_streak_resets_on_clean_observation(det):
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, speed_kmh=72.0, secmark=0))
    det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, speed_kmh=72.0, secmark=100))  # streak=1
    # Clean observation — tiny move, below distance threshold (skipped, no reset)
    # Use a consistent speed move that passes guards but doesn't violate
    det.check(make_bsm(lat_deg=41.0021, lon_deg=LON1, speed_kmh=72.0, secmark=200))  # clean → reset
    result = det.check(make_bsm(lat_deg=41.0041, lon_deg=LON1, speed_kmh=72.0, secmark=300))
    assert result is None   # fresh streak = 1


def test_heading_correction_suppresses_turn(det):
    # Vehicle reports heading change > MAX_HEADING_CHANGE_DEG (30°) — skip interval
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, speed_kmh=72.0,
                        heading_deg=0.0, secmark=0))
    # 45° heading change — vehicle was turning; skip regardless of distance
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, speed_kmh=72.0,
                                 heading_deg=45.0, secmark=100))
    assert result is None


def test_small_heading_change_does_not_suppress(det):
    # 10° heading change ≤ MAX_HEADING_CHANGE_DEG (30°) — should not suppress
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, speed_kmh=72.0,
                        heading_deg=0.0, secmark=0))
    result1 = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, speed_kmh=72.0,
                                   heading_deg=10.0, secmark=100))
    assert result1 is None   # streak = 1, not yet confirmed

    result2 = det.check(make_bsm(lat_deg=LAT3, lon_deg=LON3, speed_kmh=72.0,
                                   heading_deg=10.0, secmark=200))
    assert result2 is not None


def test_poor_gps_accuracy_returns_none(det):
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, speed_kmh=72.0, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, speed_kmh=72.0,
                                 secmark=100, accuracy_m=10.0))
    assert result is None


def test_good_gps_accuracy_does_not_suppress(det):
    # accuracy_m = 2 m ≤ MAX_GPS_ACCURACY_M (5 m) — should not suppress
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, speed_kmh=72.0,
                        secmark=0, accuracy_m=2.0))
    result1 = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, speed_kmh=72.0,
                                   secmark=100, accuracy_m=2.0))
    assert result1 is None   # streak = 1

    result2 = det.check(make_bsm(lat_deg=LAT3, lon_deg=LON3, speed_kmh=72.0,
                                   secmark=200, accuracy_m=2.0))
    assert result2 is not None


def test_both_speeds_below_floor_returns_none(det):
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, speed_kmh=5.0, secmark=0))
    result = det.check(make_bsm(lat_deg=41.000001, lon_deg=LON1, speed_kmh=5.0, secmark=100))
    assert result is None


def test_missing_secmark_returns_none(det):
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, secmark=None))
    assert result is None
