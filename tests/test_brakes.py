import pytest
from detectors.brakes_inconsistency import BrakesInconsistencyDetector
from conftest import make_bsm


@pytest.fixture
def det(det_config):
    return BrakesInconsistencyDetector(det_config.section("brakes"))


# ── helpers ───────────────────────────────────────────────────────────────────

def _bsm(wheel_brakes: str, accel_long_ms2: float) -> dict:
    return make_bsm(wheel_brakes=wheel_brakes, accel_long_ms2=accel_long_ms2)


# ── clean cases ───────────────────────────────────────────────────────────────

def test_brakes_on_with_decel_is_clean(det):
    # Brakes applied + normal deceleration — physically consistent
    assert det.check(_bsm("01000", -5.0)) is None


def test_no_brakes_mild_decel_is_clean(det):
    # Engine braking (< 1 g) with no wheel brakes — acceptable
    assert det.check(_bsm("00000", -1.0)) is None


def test_no_brakes_mild_accel_is_clean(det):
    assert det.check(_bsm("00000", 3.0)) is None


# ── brakes_on_no_decel ────────────────────────────────────────────────────────

def test_brakes_on_but_accelerating_flags(det):
    # Wheel brake active AND longitudinal accel > 1 g
    result = det.check(_bsm("01000", 10.5))   # 10.5 m/s² > 9.81 m/s²
    assert result is not None
    assert result["misbehavior"] == "brakes_on_no_decel"
    assert result["accel_ms2"] > 0


# ── decel_no_brakes ───────────────────────────────────────────────────────────

def test_strong_decel_without_brakes_flags(det):
    # Heavy deceleration > 1 g with no wheel brakes
    result = det.check(_bsm("00000", -10.5))
    assert result is not None
    assert result["misbehavior"] == "decel_no_brakes"
    assert result["accel_ms2"] < 0


# ── unavailable / malformed ───────────────────────────────────────────────────

def test_unavailable_brake_bit_returns_none(det):
    # bit 0 set = entire field unavailable
    result = det.check(_bsm("10000", -10.5))
    assert result is None


def test_missing_brakes_field_returns_none(det):
    bsm = make_bsm()
    del bsm["payload"]["data"]["coreData"]["brakes"]
    assert det.check(bsm) is None
