"""
Detector: Heading Inconsistency

Compares the reported heading in each BSM against the bearing implied by
the vehicle's movement between two consecutive messages.  A large
discrepancy suggests the heading field has been spoofed or corrupted.

Thresholds
----------
MAX_HEADING_DIFF_DEG : 90    — allowed angular difference between reported
                               heading and GPS-derived bearing
SPEED_GATE_KMH       : 20    — skip if EITHER the current or previous message
                               speed is below this; heading noise dominates at
                               low speed and legitimate turns are frequent
MAX_GPS_ACCURACY_M   :  5    — skip if positional accuracy (semiMajor) exceeds
                               this; poor GPS fixes produce unreliable bearings
MIN_DISTANCE_M       :  5    — minimum displacement needed for a reliable
                               bearing calculation
MIN_GAP_SECONDS      : 0.05  — pairs closer than this are timing artifacts
MAX_GAP_SECONDS      : 0.15  — gaps longer than this are skipped (vehicle may
                               have made a legitimate turn during the gap)
CONFIRM_N            :  2    — consecutive violations required before flagging
                               (inherited from BaseDetector)
"""

import math
from typing import Optional

from .utils import (
    _haversine_m, _angular_diff, _parse_secmark, _secmark_elapsed_s,
    _parse_accuracy_m, BaseDetector, get_core,
    LAT_SCALE, LON_SCALE, SPEED_UNIT_MS, SPEED_UNAVAILABLE, MS_TO_KMH,
    HEADING_UNIT, HEADING_UNAVAILABLE,
)


def _bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Forward azimuth from point-1 to point-2, returned as 0–360°."""
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(lat2r)
    y = (math.cos(lat1r) * math.sin(lat2r)
         - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


class HeadingInconsistencyDetector(BaseDetector):
    """Stateful detector — tracks last position/speed per vehicle to derive bearing."""

    def __init__(self, cfg: dict, confirm_n: int):
        super().__init__(confirm_n)
        self.max_heading_diff_deg = float(cfg["max_heading_diff_deg"])
        self.speed_gate_kmh       = float(cfg["speed_gate_kmh"])
        self.max_gps_accuracy_m   = float(cfg["max_gps_accuracy_m"])
        self.min_distance_m       = float(cfg["min_distance_m"])
        self.min_gap_seconds      = float(cfg["min_gap_seconds"])
        self.max_gap_seconds      = float(cfg["max_gap_seconds"])

    def check(self, bsm: dict) -> Optional[dict]:
        core = get_core(bsm)

        vehicle_id = core.get("id")
        lat_raw    = core.get("lat")
        lon_raw    = core.get("long")
        h_raw      = core.get("heading")
        spd_raw    = core.get("speed")

        if any(v is None for v in [vehicle_id, lat_raw, lon_raw, h_raw, spd_raw]):
            return None

        try:
            h_raw   = int(h_raw)
            spd_raw = int(spd_raw)
            lat = round(int(lat_raw) * LAT_SCALE, 7)
            lon = round(int(lon_raw) * LON_SCALE, 7)
        except (ValueError, TypeError):
            return None

        if h_raw == HEADING_UNAVAILABLE or spd_raw == SPEED_UNAVAILABLE:
            return None

        reported_deg = h_raw * HEADING_UNIT
        speed_kmh    = spd_raw * SPEED_UNIT_MS * MS_TO_KMH
        secmark      = _parse_secmark(core)

        prev = self._last.get(vehicle_id)
        self._last[vehicle_id] = (lat, lon, secmark, speed_kmh)

        if prev is None:
            return None

        prev_lat, prev_lon, prev_secmark, prev_speed_kmh = prev

        # Speed gate — skip if either end of the interval was slow (turning/parking)
        if speed_kmh < self.speed_gate_kmh or prev_speed_kmh < self.speed_gate_kmh:
            return None

        # GPS accuracy gate — skip if positional fix is too poor for bearing calc
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

        gps_bearing  = _bearing_deg(prev_lat, prev_lon, lat, lon)
        heading_diff = _angular_diff(reported_deg, gps_bearing)

        if heading_diff <= self.max_heading_diff_deg:
            self._reset_streak(vehicle_id)
            return None

        if self._increment_streak(vehicle_id) < self.CONFIRM_N:
            return None

        return {
            "misbehavior":      "heading_inconsistency",
            "reported_heading": round(reported_deg, 2),
            "gps_bearing":      round(gps_bearing, 2),
            "heading_diff":     round(heading_diff, 2),
            "threshold_deg":    self.max_heading_diff_deg,
            "speed_kmh":        round(speed_kmh, 2),
            "distance_m":       round(distance_m, 1),
        }
