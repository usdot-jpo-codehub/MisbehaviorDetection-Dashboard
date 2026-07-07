"""
Detector: Speed vs. Position Consistency

Compares the speed reported in each BSM against the speed implied by the
vehicle's GPS displacement between consecutive messages.  A significant
disagreement in either direction suggests a spoofed speed or position field.

  reported >> implied  — speed field inflated (e.g. ghost-vehicle attack)
  implied >> reported  — position jumps faster than the vehicle claims to move

Thresholds
----------
MAX_SPEED_DIFF_KMH    : 500.0 — absolute difference above which a misbehavior is
                                flagged; set above the p95 (14.6 km/h) of clean data
SPEED_GATE_KMH        :  10.0 — both reported and implied speed must exceed this;
                                near-standstill GPS noise dominates below this value;
                                10 km/h (not 20) because magnitude is far less sensitive
                                to GPS noise than direction (heading/yaw detectors)
MAX_HEADING_CHANGE_DEG:  30.0 — skip if the reported heading changed by more than
                                this between messages; a turn makes the straight-line
                                haversine underestimate actual travel distance,
                                causing false implied-speed underestimates
MAX_GPS_ACCURACY_M    :   5.0 — skip if positional accuracy (semiMajor) exceeds
                                this; poor GPS fixes produce unreliable implied speeds
MIN_GAP_SECONDS       :  0.05 — pairs closer than this are timing artifacts;
                                microsecond Δt inflates implied speed astronomically
MAX_GAP_SECONDS       :  0.15 — gaps longer than this are skipped; large Δt makes
                                the haversine average unreliable
MIN_DISTANCE_M        :   5.0 — minimum displacement to produce a meaningful
                                implied-speed estimate
CONFIRM_N             :   2   — consecutive violations required before flagging
                                (inherited from BaseDetector)
"""

from typing import Optional

from .utils import (
    _haversine_m, _angular_diff, _parse_secmark, _secmark_elapsed_s,
    _parse_accuracy_m, BaseDetector, get_core,
    LAT_SCALE, LON_SCALE, SPEED_UNIT_MS, SPEED_UNAVAILABLE, MS_TO_KMH,
    HEADING_UNIT, HEADING_UNAVAILABLE,
)

class SpeedPositionConsistencyDetector(BaseDetector):
    """Stateful detector — tracks last known position/speed/heading/time per vehicle."""

    def __init__(self, cfg: dict, confirm_n: int):
        super().__init__(confirm_n)
        self.max_speed_diff_kmh    = float(cfg["max_speed_diff_kmh"])
        self.speed_gate_kmh        = float(cfg["speed_gate_kmh"])
        self.max_heading_change_deg = float(cfg["max_heading_change_deg"])
        self.max_gps_accuracy_m    = float(cfg["max_gps_accuracy_m"])
        self.min_gap_seconds       = float(cfg["min_gap_seconds"])
        self.max_gap_seconds       = float(cfg["max_gap_seconds"])
        self.min_distance_m        = float(cfg["min_distance_m"])

    def check(self, bsm: dict) -> Optional[dict]:
        core = get_core(bsm)

        vehicle_id = core.get("id")
        lat_raw    = core.get("lat")
        lon_raw    = core.get("long")
        spd_raw    = core.get("speed")

        if any(v is None for v in [vehicle_id, lat_raw, lon_raw, spd_raw]):
            return None

        try:
            lat     = round(int(lat_raw) * LAT_SCALE, 7)
            lon     = round(int(lon_raw) * LON_SCALE, 7)
            spd_raw = int(spd_raw)
        except (ValueError, TypeError):
            return None

        if spd_raw == SPEED_UNAVAILABLE:
            return None

        speed_ms  = spd_raw * SPEED_UNIT_MS
        speed_kmh = speed_ms * MS_TO_KMH
        secmark   = _parse_secmark(core)

        # Parse heading optionally — used for turn detection only
        hdg_raw = core.get("heading")
        heading_deg: Optional[float] = None
        if hdg_raw is not None:
            try:
                h = int(hdg_raw)
                if h != HEADING_UNAVAILABLE:
                    heading_deg = h * HEADING_UNIT
            except (ValueError, TypeError):
                pass

        prev = self._last.get(vehicle_id)
        self._last[vehicle_id] = (lat, lon, secmark, speed_ms, heading_deg)

        if prev is None:
            return None

        prev_lat, prev_lon, prev_secmark, prev_speed_ms, prev_heading_deg = prev

        if secmark is None or prev_secmark is None:
            return None

        elapsed_s = _secmark_elapsed_s(prev_secmark, secmark)
        if elapsed_s < self.min_gap_seconds or elapsed_s > self.max_gap_seconds:
            return None

        distance_m   = _haversine_m(prev_lat, prev_lon, lat, lon)
        if distance_m < self.min_distance_m:
            return None

        implied_ms  = distance_m / elapsed_s
        implied_kmh = implied_ms * MS_TO_KMH

        # Skip if either speed is below the noise floor
        if speed_kmh < self.speed_gate_kmh or implied_kmh < self.speed_gate_kmh:
            return None

        # Heading correction — haversine underestimates distance during turns;
        # skip the interval when a significant heading change is observed
        if heading_deg is not None and prev_heading_deg is not None:
            if _angular_diff(prev_heading_deg, heading_deg) > self.max_heading_change_deg:
                return None

        # GPS accuracy gate
        accuracy_m = _parse_accuracy_m(core)
        if accuracy_m is not None and accuracy_m > self.max_gps_accuracy_m:
            return None

        diff_kmh = speed_kmh - implied_kmh   # positive = reported faster than GPS

        if abs(diff_kmh) <= self.max_speed_diff_kmh:
            self._reset_streak(vehicle_id)
            return None

        direction = "reported_exceeds_implied" if diff_kmh > 0 else "implied_exceeds_reported"

        if self._increment_streak(vehicle_id) < self.CONFIRM_N:
            return None

        return {
            "misbehavior":        "speed_position_inconsistency",
            "direction":          direction,
            "reported_speed_kmh": round(speed_kmh, 2),
            "implied_speed_kmh":  round(implied_kmh, 2),
            "diff_kmh":           round(diff_kmh, 2),
            "diff_abs_kmh":       round(abs(diff_kmh), 2),
            "threshold_kmh":      self.max_speed_diff_kmh,
            "distance_m":         round(distance_m, 1),
            "elapsed_s":          round(elapsed_s, 3),
        }
