"""
Detector: Rapid Position Jump

Compares each BSM against the last known position of the same vehicle.
Flags when the implied speed (distance ÷ elapsed_time) exceeds a plausible
maximum, indicating a position spoof or severe data error.

Thresholds
----------
MAX_JUMP_SPEED_KMH :  10   — implied speed above this is flagged
MIN_JUMP_METERS    : 100   — jump must be at least this large;
                             filters out GPS noise on tiny Δt
MAX_GPS_ACCURACY_M :   5   — skip if positional accuracy (semiMajor) exceeds
                             this; a poor GPS fix can look like a position jump
MIN_GAP_SECONDS    : 0.05  — pairs closer than this are timing artifacts
MAX_GAP_SECONDS    : 0.15  — gaps longer than this are skipped; the vehicle
                             may have legitimately reappeared elsewhere
CONFIRM_N          :   2   — consecutive violations required before flagging
                             (inherited from BaseDetector)
"""

from typing import Optional

from .utils import (
    _haversine_m, _parse_secmark, _secmark_elapsed_s, _parse_accuracy_m,
    BaseDetector, LAT_SCALE, LON_SCALE, MS_TO_KMH, get_core,
)


class PositionJumpDetector(BaseDetector):
    """Stateful detector — tracks the last known position per vehicle."""

    def __init__(self, cfg: dict, confirm_n: int):
        super().__init__(confirm_n)
        self.max_jump_speed_kmh = float(cfg["max_jump_speed_kmh"])
        self.min_jump_meters    = float(cfg["min_jump_meters"])
        self.max_gps_accuracy_m = float(cfg["max_gps_accuracy_m"])
        self.min_gap_seconds    = float(cfg["min_gap_seconds"])
        self.max_gap_seconds    = float(cfg["max_gap_seconds"])

    def check(self, bsm: dict) -> Optional[dict]:
        core = get_core(bsm)

        vehicle_id = core.get("id")
        lat_raw    = core.get("lat")
        lon_raw    = core.get("long")

        if vehicle_id is None or lat_raw is None or lon_raw is None:
            return None

        try:
            lat = round(int(lat_raw) * LAT_SCALE, 7)
            lon = round(int(lon_raw) * LON_SCALE, 7)
        except (ValueError, TypeError):
            return None

        secmark = _parse_secmark(core)

        prev = self._last.get(vehicle_id)
        self._last[vehicle_id] = (lat, lon, secmark)

        if prev is None:
            return None

        prev_lat, prev_lon, prev_secmark = prev

        if secmark is None or prev_secmark is None:
            return None

        elapsed_s = _secmark_elapsed_s(prev_secmark, secmark)
        if elapsed_s < self.min_gap_seconds or elapsed_s > self.max_gap_seconds:
            return None

        # GPS accuracy gate — poor fix can masquerade as a position jump
        accuracy_m = _parse_accuracy_m(core)
        if accuracy_m is not None and accuracy_m > self.max_gps_accuracy_m:
            return None

        distance_m  = _haversine_m(prev_lat, prev_lon, lat, lon)
        implied_kmh = (distance_m / elapsed_s) * MS_TO_KMH

        if distance_m < self.min_jump_meters or implied_kmh <= self.max_jump_speed_kmh:
            self._reset_streak(vehicle_id)
            return None

        if self._increment_streak(vehicle_id) < self.CONFIRM_N:
            return None

        return {
            "misbehavior":       "position_jump",
            "jump_m":            round(distance_m, 1),
            "elapsed_s":         round(elapsed_s, 3),
            "implied_speed_kmh": round(implied_kmh, 2),
            "threshold_kmh":     self.max_jump_speed_kmh,
            "prev_lat":          prev_lat,
            "prev_lon":          prev_lon,
        }
