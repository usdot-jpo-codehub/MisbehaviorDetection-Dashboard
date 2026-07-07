"""Tests for HeadingInconsistencyDetector.

The detector compares the reported heading against the GPS-derived bearing
between consecutive positions.  MAX_HEADING_DIFF_DEG = 90°.

Setup for each stateful test:
  - Message 1: anchor position (41.0°, -81.0°), heading = test value
  - Message 2: moved ~22 m north (lat += 0.0002°), heading = test value
    → GPS bearing ≈ 0° (due north)

All tests use speed = 72 km/h (> SPEED_GATE_KMH = 20) and
secmark 0 → 100 → 200 (100 ms gaps, within [50, 150] ms window).

Multi-message confirmation requires 2 consecutive violations before flagging.
"""

import pytest
from detectors.heading_inconsistency import HeadingInconsistencyDetector
from conftest import make_bsm

LAT1, LON1 = 41.0,    -81.0
LAT2, LON2 = 41.0002, -81.0   # ~22 m north; GPS bearing ≈ 0°
LAT3, LON3 = 41.0004, -81.0   # ~44 m north; GPS bearing ≈ 0°


@pytest.fixture
def det(det_config):
    return HeadingInconsistencyDetector(
        det_config.section("heading_inconsistency"), det_config.confirm_n
    )


def test_first_message_returns_none(det):
    assert det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, secmark=0)) is None


def test_consistent_heading_returns_none(det):
    # Reported 0° (north), moving north → diff ≈ 0° ≤ 90°
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=0.0, secmark=100))
    assert result is None


def test_inconsistent_heading_requires_two_consecutive(det):
    # First violation returns None (streak = 1, not yet confirmed)
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=135.0, secmark=0))
    result1 = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=135.0, secmark=100))
    assert result1 is None   # streak = 1

    # Second consecutive violation fires
    result2 = det.check(make_bsm(lat_deg=LAT3, lon_deg=LON3, heading_deg=135.0, secmark=200))
    assert result2 is not None
    assert result2["misbehavior"] == "heading_inconsistency"
    assert result2["heading_diff"] > 90.0


def test_streak_resets_on_clean_observation(det):
    # First violation — streak = 1
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=135.0, secmark=0))
    det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=135.0, secmark=100))

    # Clean observation resets streak
    det.check(make_bsm(lat_deg=LAT3, lon_deg=LON3, heading_deg=0.0, secmark=200))

    # Next violation starts a fresh streak — should not fire
    result = det.check(make_bsm(lat_deg=41.0006, lon_deg=LON1, heading_deg=135.0, secmark=300))
    assert result is None


def test_below_speed_gate_current_returns_none(det):
    # Current speed = 5 km/h < SPEED_GATE_KMH (20)
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=135.0, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, speed_kmh=5.0,
                                 heading_deg=135.0, secmark=100))
    assert result is None


def test_below_speed_gate_previous_returns_none(det):
    # Previous speed = 5 km/h < SPEED_GATE_KMH (20) — gate applies to both ends
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, speed_kmh=5.0,
                        heading_deg=135.0, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, speed_kmh=72.0,
                                 heading_deg=135.0, secmark=100))
    assert result is None


def test_poor_gps_accuracy_returns_none(det):
    # accuracy_m = 10 m > MAX_GPS_ACCURACY_M (5 m)
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=135.0, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=135.0,
                                 secmark=100, accuracy_m=10.0))
    assert result is None


def test_good_gps_accuracy_does_not_suppress(det):
    # accuracy_m = 2 m ≤ MAX_GPS_ACCURACY_M (5 m) — should not suppress
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=135.0, secmark=0,
                        accuracy_m=2.0))
    result1 = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=135.0,
                                   secmark=100, accuracy_m=2.0))
    assert result1 is None   # streak = 1
    result2 = det.check(make_bsm(lat_deg=LAT3, lon_deg=LON3, heading_deg=135.0,
                                   secmark=200, accuracy_m=2.0))
    assert result2 is not None


def test_too_little_displacement_returns_none(det):
    # ~0.1 m movement — below MIN_DISTANCE_M (5 m)
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, secmark=0))
    result = det.check(make_bsm(lat_deg=41.000001, lon_deg=LON1,
                                 heading_deg=90.0, secmark=100))
    assert result is None
