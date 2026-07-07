"""
Detector: Brakes-Deceleration Inconsistency

Cross-checks the wheelBrakes bitmap against the reported longitudinal
acceleration to catch spoofed or corrupted brake/accel fields.

Two misbehavior sub-types are flagged:

  brakes_on_no_decel
    Wheel brakes reported as applied, yet the vehicle is accelerating
    (accelSet.long > +ACCEL_BRAKING_THRESHOLD).  Physically, applied wheel
    brakes cannot produce net forward acceleration beyond sensor noise.

  decel_no_brakes
    Strong deceleration reported (accelSet.long < -DECEL_NO_BRAKES_THRESHOLD)
    with no wheel brakes active.  Engine braking tops out around 1.5 m/s²;
    anything beyond that without wheel brakes is implausible.

SAE J2735 field encoding
------------------------
wheelBrakes : 5-character binary string (BrakeAppliedStatus BITSTRING)
    bit 0  – unavailable  (if '1', entire field is invalid → skip)
    bit 1  – leftFront
    bit 2  – rightFront
    bit 3  – leftRear
    bit 4  – rightRear

accelSet.long : integer, unit 0.01 m/s² per LSB, range -2000 to 2000
    2001 = unavailable → skip
"""

from typing import Optional

from .utils import ACCEL_UNAVAILABLE, ACCEL_UNIT_MS2, G_MS2, get_core


def _parse_accel(raw) -> Optional[float]:
    """Return accel in m/s², or None if missing/unavailable."""
    if raw is None:
        return None
    try:
        raw = int(raw)
    except (ValueError, TypeError):
        return None
    if raw == ACCEL_UNAVAILABLE:
        return None
    return raw * ACCEL_UNIT_MS2


def _parse_wheel_brakes(wb) -> Optional[bool]:
    """
    Return True if any wheel brake is active, False if none are active,
    or None if the field is missing/unavailable/malformed.
    """
    if not isinstance(wb, str) or len(wb) != 5:
        return None
    if wb[0] == '1':          # unavailable bit set
        return None
    if any(c not in ('0', '1') for c in wb):
        return None
    return any(c == '1' for c in wb[1:])


class BrakesInconsistencyDetector:
    """Stateless detector — flags inconsistencies between brake state and acceleration."""

    def __init__(self, cfg: dict):
        self.accel_braking_threshold_ms2   = float(cfg["accel_braking_threshold_g"])   * G_MS2
        self.decel_no_brakes_threshold_ms2 = float(cfg["decel_no_brakes_threshold_g"]) * G_MS2

    def check(self, bsm: dict) -> Optional[dict]:
        core = get_core(bsm)

        wheel_brakes_raw = core.get("brakes", {}).get("wheelBrakes")
        accel_ms2        = _parse_accel(core.get("accelSet", {}).get("long"))
        any_braking      = _parse_wheel_brakes(wheel_brakes_raw)

        if accel_ms2 is None or any_braking is None:
            return None

        # Case 1 — brakes applied but vehicle is accelerating
        if any_braking and accel_ms2 > self.accel_braking_threshold_ms2:
            return {
                "misbehavior":   "brakes_on_no_decel",
                "accel_ms2":     round(accel_ms2, 4),
                "accel_g":       round(accel_ms2 / G_MS2, 4),
                "threshold_ms2": self.accel_braking_threshold_ms2,
                "wheel_brakes":  wheel_brakes_raw,
            }

        # Case 2 — heavy deceleration with no wheel brakes active
        if not any_braking and accel_ms2 < -self.decel_no_brakes_threshold_ms2:
            return {
                "misbehavior":   "decel_no_brakes",
                "accel_ms2":     round(accel_ms2, 4),
                "accel_g":       round(accel_ms2 / G_MS2, 4),
                "threshold_ms2": -self.decel_no_brakes_threshold_ms2,
                "wheel_brakes":  wheel_brakes_raw,
            }

        return None
