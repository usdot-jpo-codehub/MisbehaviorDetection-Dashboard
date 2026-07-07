"""Tests for PositionJumpDetector.

Geometry note (lat=41°):
  1° lat ≈ 111 319 m
  0.001° lat ≈ 111 m   (used for the jump test)
  0.00001° lat ≈ 1.1 m (used for the tiny-move test)

secMark pairs are 100 ms apart (0 → 100 → 200), well within [50, 150] ms window.

Multi-message confirmation requires 2 consecutive violations before flagging.
"""

import pytest
from detectors.position_jump import PositionJumpDetector
from conftest import make_bsm

LAT1, LON1 = 41.0,   -81.0
LAT2, LON2 = 41.001, -81.0   # ~111 m north — triggers jump
LAT3, LON3 = 41.002, -81.0   # another ~111 m north


@pytest.fixture
def det(det_config):
    return PositionJumpDetector(det_config.section("position_jump"), det_config.confirm_n)


def test_first_message_returns_none(det):
    assert det.check(make_bsm(secmark=0)) is None


def test_tiny_movement_returns_none(det):
    # ~1 m displacement — well below MIN_JUMP_METERS (100 m)
    det.check(make_bsm(lat_deg=LAT1, secmark=0))
    result = det.check(make_bsm(lat_deg=41.00001, secmark=100))
    assert result is None


def test_large_jump_requires_two_consecutive(det):
    # ~111 m north in 100 ms → implied ≈ 4 000 km/h >> 10 km/h threshold
    det.check(make_bsm(lat_deg=LAT1, secmark=0))
    result1 = det.check(make_bsm(lat_deg=LAT2, secmark=100))
    assert result1 is None   # streak = 1

    result2 = det.check(make_bsm(lat_deg=LAT3, secmark=200))
    assert result2 is not None
    assert result2["misbehavior"] == "position_jump"
    assert result2["jump_m"] > 100.0
    assert result2["implied_speed_kmh"] > 10.0


def test_streak_resets_on_clean_observation(det):
    det.check(make_bsm(lat_deg=LAT1, secmark=0))
    det.check(make_bsm(lat_deg=LAT2, secmark=100))          # streak = 1
    det.check(make_bsm(lat_deg=41.0011, secmark=200))       # clean → reset
    result = det.check(make_bsm(lat_deg=41.0021, secmark=300))
    assert result is None   # fresh streak = 1


def test_poor_gps_accuracy_returns_none(det):
    # accuracy_m = 10 m > MAX_GPS_ACCURACY_M (5 m) — skip even large jumps
    det.check(make_bsm(lat_deg=LAT1, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, secmark=100, accuracy_m=10.0))
    assert result is None


def test_good_gps_accuracy_does_not_suppress(det):
    # accuracy_m = 2 m ≤ MAX_GPS_ACCURACY_M (5 m) — should not suppress
    det.check(make_bsm(lat_deg=LAT1, secmark=0, accuracy_m=2.0))
    result1 = det.check(make_bsm(lat_deg=LAT2, secmark=100, accuracy_m=2.0))
    assert result1 is None   # streak = 1
    result2 = det.check(make_bsm(lat_deg=LAT3, secmark=200, accuracy_m=2.0))
    assert result2 is not None


def test_gap_too_small_returns_none(det):
    # 10 ms gap < MIN_GAP_SECONDS (50 ms)
    det.check(make_bsm(lat_deg=LAT1, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, secmark=10))
    assert result is None


def test_gap_too_large_returns_none(det):
    # 500 ms gap > MAX_GAP_SECONDS (150 ms)
    det.check(make_bsm(lat_deg=LAT1, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, secmark=500))
    assert result is None


def test_missing_secmark_returns_none(det):
    det.check(make_bsm(lat_deg=LAT1, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, secmark=None))
    assert result is None


def test_each_instance_is_independent(det_config):
    cfg = det_config.section("position_jump")
    n   = det_config.confirm_n

    det1 = PositionJumpDetector(cfg, n)
    det2 = PositionJumpDetector(cfg, n)

    det1.check(make_bsm(lat_deg=LAT1, secmark=0))
    det1.check(make_bsm(lat_deg=LAT2, secmark=100))           # streak=1
    assert det1.check(make_bsm(lat_deg=LAT3, secmark=200)) is not None  # confirmed

    # det2 only saw the third message — no state
    assert det2.check(make_bsm(lat_deg=LAT3, secmark=200)) is None
