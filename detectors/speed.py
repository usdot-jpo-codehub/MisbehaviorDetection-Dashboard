"""
Detector: Speed Exceeds Threshold

BSM speed field (coreData.speed) is encoded per SAE J2735:
  - Unit: 0.02 m/s per LSB
  - Range: 0–8190 (8191 = unavailable)

200 km/h = 55.556 m/s → 2777.78 units → flag when speed_kmh > 200
"""

from typing import Optional

from .utils import MS_TO_KMH, SPEED_UNAVAILABLE, SPEED_UNIT_MS, get_core


class SpeedDetector:
    """Stateless detector — flags BSMs where reported speed exceeds the threshold."""

    def __init__(self, cfg: dict):
        self.threshold_kmh = float(cfg["threshold_kmh"])

    def check(self, bsm: dict) -> Optional[dict]:
        core = get_core(bsm)

        raw = core.get("speed")
        if raw is None:
            return None

        try:
            raw = int(raw)
        except (ValueError, TypeError):
            return None

        if raw == SPEED_UNAVAILABLE:
            return None

        speed_kmh = raw * SPEED_UNIT_MS * MS_TO_KMH

        if speed_kmh <= self.threshold_kmh:
            return None

        return {
            "misbehavior":   "speed_exceeded",
            "speed_kmh":     round(speed_kmh, 2),
            "threshold_kmh": self.threshold_kmh,
            "speed_raw":     raw,
        }
