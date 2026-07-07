"""
Detector: Speed-Acceleration Consistency

The change in reported speed between two consecutive messages from the same
vehicle should match the reported longitudinal acceleration integrated over
the elapsed time:

    expected_Δspeed = accelSet.long × Δt

A large deviation suggests that one of the three fields (speed, acceleration,
or timestamp) has been spoofed or is severely corrupted.

Thresholds
----------
MAX_DELTA_ERROR_MS  : 5.0  — allowed |observed_Δspeed − expected_Δspeed| in m/s;
                             set above the p99 (0.40 m/s) of clean consecutive pairs
MAX_GAP_SECONDS     : 0.15 — gaps longer than this are skipped; the linear
                             integration assumption breaks down over long intervals
MIN_DELTA_SPEED_KMH : 20.0 — require a meaningful speed change before flagging;
                             filters noise when the vehicle is nearly constant-speed
CONFIRM_N           :  2   — consecutive violations required before flagging
                             (inherited from BaseDetector)
"""

from typing import Optional

from .utils import (
    _parse_secmark, _secmark_elapsed_s, BaseDetector, get_core,
    SPEED_UNIT_MS, SPEED_UNAVAILABLE, ACCEL_UNIT_MS2, ACCEL_UNAVAILABLE, MS_TO_KMH,
)

class SpeedAccelConsistencyDetector(BaseDetector):
    """Stateful detector — tracks last speed/accel/time per vehicle."""

    def __init__(self, cfg: dict, confirm_n: int):
        super().__init__(confirm_n)
        self.max_delta_error_ms  = float(cfg["max_delta_error_ms"])
        self.min_gap_seconds     = float(cfg["min_gap_seconds"])
        self.max_gap_seconds     = float(cfg["max_gap_seconds"])
        self.min_delta_speed_ms  = float(cfg["min_delta_speed_kmh"]) / MS_TO_KMH

    def check(self, bsm: dict) -> Optional[dict]:
        core = get_core(bsm)

        vehicle_id = core.get("id")
        spd_raw    = core.get("speed")
        acc_raw    = core.get("accelSet", {}).get("long")

        if any(v is None for v in [vehicle_id, spd_raw, acc_raw]):
            return None

        try:
            spd_raw = int(spd_raw)
            acc_raw = int(acc_raw)
        except (ValueError, TypeError):
            return None

        if spd_raw == SPEED_UNAVAILABLE or acc_raw == ACCEL_UNAVAILABLE:
            return None

        speed_ms  = spd_raw * SPEED_UNIT_MS
        accel_ms2 = acc_raw * ACCEL_UNIT_MS2
        secmark   = _parse_secmark(core)

        prev = self._last.get(vehicle_id)
        self._last[vehicle_id] = (speed_ms, accel_ms2, secmark)

        if prev is None:
            return None

        prev_speed_ms, prev_accel_ms2, prev_secmark = prev

        if secmark is None or prev_secmark is None:
            return None

        elapsed_s = _secmark_elapsed_s(prev_secmark, secmark)
        if elapsed_s < self.min_gap_seconds or elapsed_s > self.max_gap_seconds:
            return None

        observed_delta  = speed_ms - prev_speed_ms
        expected_delta  = prev_accel_ms2 * elapsed_s
        error_ms        = abs(observed_delta - expected_delta)

        if abs(observed_delta) < self.min_delta_speed_ms:
            return None

        if error_ms <= self.max_delta_error_ms:
            self._reset_streak(vehicle_id)
            return None

        if self._increment_streak(vehicle_id) < self.CONFIRM_N:
            return None

        return {
            "misbehavior":       "speed_accel_inconsistency",
            "observed_delta_ms": round(observed_delta, 4),
            "expected_delta_ms": round(expected_delta, 4),
            "error_ms":          round(error_ms, 4),
            "error_kmh":         round(error_ms * MS_TO_KMH, 2),
            "threshold_ms":      self.max_delta_error_ms,
            "accel_ms2":         round(prev_accel_ms2, 4),
            "elapsed_s":         round(elapsed_s, 3),
        }
