"""Shared test helpers for MBD detector unit tests.

make_bsm() builds a minimal syntactically-correct BSM dict using the same
SAE J2735 encoding constants the detectors use, so tests stay DRY and
don't need to hand-roll raw integer values.
"""

from pathlib import Path

import pytest

from detectors.config import DetectorConfig
from detectors.utils import (
    LAT_SCALE, LON_SCALE,
    SPEED_UNIT_MS, MS_TO_KMH,
    HEADING_UNIT,
    ACCEL_UNIT_MS2,
    YAW_UNIT,
    ACCURACY_UNIT_M,
)

_THRESHOLD_FILE = Path(__file__).parent.parent / "ode_config.json"


@pytest.fixture(scope="session")
def det_config() -> DetectorConfig:
    """Load the project threshold config once per test session."""
    return DetectorConfig.from_file(_THRESHOLD_FILE)


def make_bsm(
    vehicle_id: str = "veh-001",
    secmark: int = 500,
    lat_deg: float = 41.0,
    lon_deg: float = -81.0,
    speed_kmh: float = 72.0,
    heading_deg: float = 0.0,
    accel_long_ms2: float = 0.0,
    yaw_deg_s: float = 0.0,
    wheel_brakes: str = "00000",
    record_generated_at: str = "2020-01-01 00:00:00.000",
    accuracy_m: float = None,   # positional accuracy semiMajor in metres; None = omit field
) -> dict:
    """Return a BSM dict with all common coreData fields populated.

    All physical values are accepted in human-readable units and converted to
    the J2735 integer encoding automatically.  Pass secmark=None to omit the
    field (simulates a vehicle that doesn't include it).
    """
    core: dict = {
        "id":      vehicle_id,
        "lat":     round(lat_deg  / LAT_SCALE),
        "long":    round(lon_deg  / LON_SCALE),
        "speed":   round(speed_kmh / (SPEED_UNIT_MS * MS_TO_KMH)),
        "heading": round(heading_deg / HEADING_UNIT),
        "accelSet": {
            "long": round(accel_long_ms2 / ACCEL_UNIT_MS2),
            "yaw":  round(yaw_deg_s      / YAW_UNIT),
        },
        "brakes": {
            "wheelBrakes": wheel_brakes,
        },
    }
    if secmark is not None:
        core["secMark"] = secmark
    if accuracy_m is not None:
        core["accuracy"] = {"semiMajor": round(accuracy_m / ACCURACY_UNIT_M)}

    return {
        "metadata": {
            "recordGeneratedAt": record_generated_at,
            "RSUID":    "rsu-test",
            "bsmSource": "EV",
        },
        "payload": {
            "data": {
                "coreData": core,
            }
        },
    }
