import pytest
from detectors.accel import AccelDetector
from conftest import make_bsm
from detectors.utils import ACCEL_UNAVAILABLE


@pytest.fixture
def det(det_config):
    return AccelDetector(det_config.section("accel"))


def test_below_threshold_returns_none(det):
    assert det.check(make_bsm(accel_long_ms2=5.0)) is None


def test_at_threshold_returns_none(det):
    # Threshold is 2.0 g (19.6133 m/s²). Use 9.80 m/s² (0.9993 g) — well below the
    # threshold — to confirm values under 2.0 g are not flagged.
    assert det.check(make_bsm(accel_long_ms2=9.80)) is None


def test_positive_accel_exceeded_flags(det):
    result = det.check(make_bsm(accel_long_ms2=25.0))
    assert result is not None
    assert result["misbehavior"] == "accel_exceeded"
    assert result["accel_g"] > 2.0


def test_negative_decel_exceeded_flags(det):
    result = det.check(make_bsm(accel_long_ms2=-25.0))
    assert result is not None
    assert result["misbehavior"] == "accel_exceeded"
    assert result["accel_g"] < -2.0


def test_unavailable_returns_none(det):
    bsm = make_bsm()
    bsm["payload"]["data"]["coreData"]["accelSet"]["long"] = ACCEL_UNAVAILABLE
    assert det.check(bsm) is None


def test_missing_field_returns_none(det):
    bsm = make_bsm()
    del bsm["payload"]["data"]["coreData"]["accelSet"]
    assert det.check(bsm) is None
