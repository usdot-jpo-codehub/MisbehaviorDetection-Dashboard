"""Tests for SpeedAccelConsistencyDetector.

The detector flags when |observed_Δspeed − expected_Δspeed| > MAX_DELTA_ERROR_MS (5 m/s)
AND |observed_Δspeed| > MIN_DELTA_SPEED_MS (~5.56 m/s = 20 km/h).

Flagged case (2-message setup):
  prev speed = 20 m/s (72 km/h), prev accel = 5 m/s²
  curr speed = 40 m/s (144 km/h) → observed_Δ = 20 m/s
  expected_Δ = 5 × 0.1 = 0.5 m/s → error = 19.5 m/s >> 5 m/s

Multi-message confirmation requires 2 consecutive violations before flagging.

Streak-reset case uses accel = 100 m/s²:
  observed_Δ = 10 m/s, expected_Δ = 100 × 0.1 = 10 m/s → error = 0 → clean.

Clean case: observed_Δ = 0.4 m/s < MIN_DELTA_SPEED_MS → skipped (no streak reset).
"""

import pytest
from detectors.speed_accel_consistency import SpeedAccelConsistencyDetector
from detectors.utils import SPEED_UNIT_MS, ACCEL_UNIT_MS2
from conftest import make_bsm


def _spd_raw(ms: float) -> int:
    return round(ms / SPEED_UNIT_MS)


def _acc_raw(ms2: float) -> int:
    return round(ms2 / ACCEL_UNIT_MS2)


@pytest.fixture
def det(det_config):
    return SpeedAccelConsistencyDetector(
        det_config.section("speed_accel"), det_config.confirm_n
    )


def test_first_message_returns_none(det):
    assert det.check(make_bsm(secmark=0)) is None


def test_small_delta_skipped(det):
    # Δspeed = 0.4 m/s = 1.44 km/h < MIN_DELTA_SPEED_KMH (20) → not evaluated
    bsm1 = make_bsm(secmark=0)
    bsm1["payload"]["data"]["coreData"]["speed"]            = _spd_raw(20.0)
    bsm1["payload"]["data"]["coreData"]["accelSet"]["long"] = _acc_raw(5.0)

    bsm2 = make_bsm(secmark=100)
    bsm2["payload"]["data"]["coreData"]["speed"]            = _spd_raw(20.4)
    bsm2["payload"]["data"]["coreData"]["accelSet"]["long"] = _acc_raw(5.0)

    det.check(bsm1)
    assert det.check(bsm2) is None


def test_inconsistent_speed_accel_requires_two_consecutive(det):
    # Speed jumps 20 m/s in 100 ms but accel only predicts 0.5 m/s → error = 19.5 >> 5
    # First violation → streak = 1, returns None; second → confirmed, fires.
    bsm1 = make_bsm(speed_kmh=72.0,  accel_long_ms2=5.0, secmark=0)    # 20 m/s
    bsm2 = make_bsm(speed_kmh=144.0, accel_long_ms2=5.0, secmark=100)  # 40 m/s
    bsm3 = make_bsm(speed_kmh=216.0, accel_long_ms2=5.0, secmark=200)  # 60 m/s

    det.check(bsm1)
    result1 = det.check(bsm2)
    assert result1 is None   # streak = 1

    result2 = det.check(bsm3)
    assert result2 is not None
    assert result2["misbehavior"] == "speed_accel_inconsistency"
    assert result2["error_ms"] > 5.0


def test_streak_resets_on_clean_observation(det):
    # accel = 100 m/s² → expected_Δ = 10 m/s in 100 ms.
    # bsm2: speed +20 m/s → error = 10 > 5 (violation, streak=1)
    # bsm3: speed +10 m/s → error = 0 ≤ 5 and |Δ| > 5.56 → clean, streak reset
    # bsm4: speed +20 m/s → error = 10 > 5 (violation, fresh streak=1) → None
    bsm1 = make_bsm(speed_kmh=72.0,  accel_long_ms2=100.0, secmark=0)    # 20 m/s
    bsm2 = make_bsm(speed_kmh=144.0, accel_long_ms2=100.0, secmark=100)  # 40 m/s, violation
    bsm3 = make_bsm(speed_kmh=180.0, accel_long_ms2=100.0, secmark=200)  # 50 m/s, clean
    bsm4 = make_bsm(speed_kmh=252.0, accel_long_ms2=100.0, secmark=300)  # 70 m/s, violation

    det.check(bsm1)
    det.check(bsm2)   # streak = 1
    det.check(bsm3)   # clean → streak = 0
    result = det.check(bsm4)
    assert result is None   # fresh streak = 1, not yet confirmed


def test_gap_too_small_returns_none(det):
    bsm1 = make_bsm(secmark=0)
    bsm1["payload"]["data"]["coreData"]["speed"]            = _spd_raw(20.0)
    bsm1["payload"]["data"]["coreData"]["accelSet"]["long"] = _acc_raw(5.0)

    bsm2 = make_bsm(secmark=10)   # 10 ms < MIN_GAP_SECONDS (50 ms)
    bsm2["payload"]["data"]["coreData"]["speed"]            = _spd_raw(40.0)
    bsm2["payload"]["data"]["coreData"]["accelSet"]["long"] = _acc_raw(5.0)

    det.check(bsm1)
    assert det.check(bsm2) is None


def test_missing_secmark_returns_none(det):
    bsm1 = make_bsm(secmark=0)
    bsm1["payload"]["data"]["coreData"]["accelSet"]["long"] = _acc_raw(5.0)

    bsm2 = make_bsm(secmark=None)
    bsm2["payload"]["data"]["coreData"]["accelSet"]["long"] = _acc_raw(5.0)

    det.check(bsm1)
    assert det.check(bsm2) is None
