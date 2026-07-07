"""
Detector: Yaw Rate vs. Heading Change Rate

Compares the yaw rate reported in accelSet.yaw against the rate of heading
change derived from consecutive GPS positions.  A large discrepancy indicates
that the yaw sensor reading and the position/heading fields are inconsistent
— a strong signal of spoofing or sensor injection.

Sign convention (SAE J2735 / ISO 8855)
---------------------------------------
accelSet.yaw  positive → turning right (clockwise, increasing heading)
Heading       0° = North, increases clockwise

Reported yaw rate and GPS-derived heading change rate should therefore carry
the same sign.  The check uses the signed difference to catch both magnitude
and direction errors.

Thresholds
----------
MAX_YAW_DIFF_DEG_S : 90.0  — |reported_yaw − gps_heading_rate| above this is
                             flagged; set above the p99 (19.0 °/s) of clean pairs
SPEED_GATE_KMH     : 20.0  — skip if EITHER the current or previous message
                             speed is below this; yaw is noisy at low speed
MAX_GPS_ACCURACY_M :  5.0  — skip if positional accuracy (semiMajor) exceeds
                             this; poor GPS inflates the derived heading rate
MIN_GAP_SECONDS    : 0.05  — pairs closer than this are timing artifacts
MAX_GAP_SECONDS    : 0.15  — gaps longer than this are skipped
MIN_DISTANCE_M     :  5.0  — minimum displacement for a reliable heading rate
CONFIRM_N          :  2    — consecutive violations required before flagging
                             (inherited from BaseDetector)
"""

from typing import Optional

from .utils import (
    _haversine_m, _parse_secmark, _secmark_elapsed_s, _parse_accuracy_m,
    BaseDetector, get_core,
    LAT_SCALE, LON_SCALE, HEADING_UNIT, HEADING_UNAVAILABLE,
    SPEED_UNIT_MS, SPEED_UNAVAILABLE, YAW_UNIT, YAW_UNAVAILABLE, MS_TO_KMH,
)


def _signed_heading_delta(h_prev: float, h_curr: float) -> float:
    """
    Signed shortest-path heading change from h_prev to h_curr (degrees).
    Positive = clockwise (right turn), negative = counter-clockwise.
    """
    delta = (h_curr - h_prev) % 360
    if delta > 180:
        delta -= 360
    return delta


class YawRateConsistencyDetector(BaseDetector):
    """Stateful detector — tracks last heading/position/speed/time per vehicle."""

    def __init__(self, cfg: dict, confirm_n: int):
        super().__init__(confirm_n)
        self.max_yaw_diff_deg_s = float(cfg["max_yaw_diff_deg_s"])
        self.speed_gate_kmh     = float(cfg["speed_gate_kmh"])
        self.max_gps_accuracy_m = float(cfg["max_gps_accuracy_m"])
        self.min_gap_seconds    = float(cfg["min_gap_seconds"])
        self.max_gap_seconds    = float(cfg["max_gap_seconds"])
        self.min_distance_m     = float(cfg["min_distance_m"])

    def check(self, bsm: dict) -> Optional[dict]:
        core = get_core(bsm)

        vehicle_id = core.get("id")
        hdg_raw    = core.get("heading")
        spd_raw    = core.get("speed")
        yaw_raw    = core.get("accelSet", {}).get("yaw")
        lat_raw    = core.get("lat")
        lon_raw    = core.get("long")

        if any(v is None for v in [vehicle_id, hdg_raw, spd_raw, yaw_raw, lat_raw, lon_raw]):
            return None

        try:
            hdg_raw = int(hdg_raw)
            spd_raw = int(spd_raw)
            yaw_raw = int(yaw_raw)
            lat     = round(int(lat_raw) * LAT_SCALE, 7)
            lon     = round(int(lon_raw) * LON_SCALE, 7)
        except (ValueError, TypeError):
            return None

        if hdg_raw == HEADING_UNAVAILABLE or spd_raw == SPEED_UNAVAILABLE or yaw_raw == YAW_UNAVAILABLE:
            return None

        heading_deg    = hdg_raw * HEADING_UNIT
        speed_kmh      = spd_raw * SPEED_UNIT_MS * MS_TO_KMH
        yaw_rate_deg_s = yaw_raw * YAW_UNIT        # signed: + = right turn
        secmark        = _parse_secmark(core)

        prev = self._last.get(vehicle_id)
        self._last[vehicle_id] = (heading_deg, lat, lon, secmark, speed_kmh)

        if prev is None:
            return None

        prev_heading, prev_lat, prev_lon, prev_secmark, prev_speed_kmh = prev

        # Speed gate — skip if either end of the interval was slow
        if speed_kmh < self.speed_gate_kmh or prev_speed_kmh < self.speed_gate_kmh:
            return None

        # GPS accuracy gate
        accuracy_m = _parse_accuracy_m(core)
        if accuracy_m is not None and accuracy_m > self.max_gps_accuracy_m:
            return None

        if secmark is None or prev_secmark is None:
            return None

        elapsed_s = _secmark_elapsed_s(prev_secmark, secmark)
        if elapsed_s < self.min_gap_seconds or elapsed_s > self.max_gap_seconds:
            return None

        distance_m = _haversine_m(prev_lat, prev_lon, lat, lon)
        if distance_m < self.min_distance_m:
            return None

        signed_delta       = _signed_heading_delta(prev_heading, heading_deg)
        gps_yaw_rate_deg_s = signed_delta / elapsed_s   # signed °/s from GPS

        yaw_diff = abs(yaw_rate_deg_s - gps_yaw_rate_deg_s)

        if yaw_diff <= self.max_yaw_diff_deg_s:
            self._reset_streak(vehicle_id)
            return None

        if self._increment_streak(vehicle_id) < self.CONFIRM_N:
            return None

        return {
            "misbehavior":           "yaw_rate_inconsistency",
            "reported_yaw_deg_s":    round(yaw_rate_deg_s, 3),
            "gps_yaw_rate_deg_s":    round(gps_yaw_rate_deg_s, 3),
            "yaw_diff_deg_s":        round(yaw_diff, 3),
            "threshold_deg_s":       self.max_yaw_diff_deg_s,
            "elapsed_s":             round(elapsed_s, 3),
            "speed_kmh":             round(speed_kmh, 2),
        }
