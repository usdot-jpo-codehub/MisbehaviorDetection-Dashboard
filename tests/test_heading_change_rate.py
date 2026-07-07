"""Tests for HeadingChangeRateDetector.

The detector flags when |heading_diff| / elapsed_s > 90 °/s.

Common setup: messages ~22 m apart (distance > MIN_DISTANCE_M = 5 m),
100 ms gaps (within [50, 150] ms), speed 72 km/h (> SPEED_GATE_KMH = 20).

Multi-message confirmation requires 2 consecutive violations before flagging.
"""

import pytest
from detectors.heading_change_rate import HeadingChangeRateDetector
from conftest import make_bsm

LAT1, LON1 = 41.0,    -81.0
LAT2, LON2 = 41.0002, -81.0   # ~22 m north
LAT3, LON3 = 41.0004, -81.0   # ~44 m north


@pytest.fixture
def det(det_config):
    return HeadingChangeRateDetector(
        det_config.section("heading_change_rate"), det_config.confirm_n
    )


def test_first_message_returns_none(det):
    assert det.check(make_bsm(lat_deg=LAT1, secmark=0)) is None


def test_small_heading_change_returns_none(det):
    # 1° change in 100 ms → 10 °/s ≤ 90 °/s threshold
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=1.0, secmark=100))
    assert result is None


def test_implausible_heading_rate_requires_two_consecutive(det):
    # 180° change in 100 ms → 1 800 °/s >> 90 °/s
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0, secmark=0))
    result1 = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=180.0, secmark=100))
    assert result1 is None   # streak = 1

    result2 = det.check(make_bsm(lat_deg=LAT3, lon_deg=LON3, heading_deg=0.0, secmark=200))
    assert result2 is not None
    assert result2["misbehavior"] == "implausible_heading_change_rate"
    assert result2["heading_rate_deg_s"] > 90.0


def test_streak_resets_on_clean_observation(det):
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0, secmark=0))
    det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=180.0, secmark=100))  # streak=1
    det.check(make_bsm(lat_deg=LAT3, lon_deg=LON3, heading_deg=181.0, secmark=200))  # clean, resets
    result = det.check(make_bsm(lat_deg=41.0006, lon_deg=LON1, heading_deg=0.0, secmark=300))
    assert result is None   # fresh streak = 1, not yet confirmed


def test_below_speed_gate_current_returns_none(det):
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=180.0,
                                 speed_kmh=5.0, secmark=100))
    assert result is None


def test_below_speed_gate_previous_returns_none(det):
    # Previous message had low speed — gate applies to both ends
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0,
                        speed_kmh=5.0, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=180.0,
                                 speed_kmh=72.0, secmark=100))
    assert result is None


def test_poor_gps_accuracy_returns_none(det):
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0, secmark=0))
    result = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=180.0,
                                 secmark=100, accuracy_m=10.0))
    assert result is None


def test_good_gps_accuracy_does_not_suppress(det):
    # accuracy_m = 2 m ≤ MAX_GPS_ACCURACY_M (5 m) — should not suppress
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0,
                        secmark=0, accuracy_m=2.0))
    result1 = det.check(make_bsm(lat_deg=LAT2, lon_deg=LON2, heading_deg=180.0,
                                   secmark=100, accuracy_m=2.0))
    assert result1 is None   # streak = 1

    result2 = det.check(make_bsm(lat_deg=LAT3, lon_deg=LON3, heading_deg=0.0,
                                   secmark=200, accuracy_m=2.0))
    assert result2 is not None


def test_too_little_displacement_returns_none(det):
    det.check(make_bsm(lat_deg=LAT1, lon_deg=LON1, heading_deg=0.0, secmark=0))
    result = det.check(make_bsm(lat_deg=41.000001, lon_deg=LON1, heading_deg=180.0, secmark=100))
    assert result is None
