"""
DetectorConfig — loads and validates ode_config.json.

Fails hard (sys.exit) if the file is missing, not valid JSON, or is
missing any required key.  This ensures the process never runs with an
ambiguous or partial configuration.
"""

import json
import sys
from pathlib import Path


# Every key that must be present in the config file.
# Top-level scalars map to None; sections map to their required sub-keys.
_REQUIRED: dict = {
    "logstash":             ["url"],
    "kafka":                ["bootstrap_servers", "topic", "group_id"],
    "confirm_n":            None,
    "cooldown":             ["meters", "seconds"],
    "speed":                ["threshold_kmh"],
    "accel":                ["threshold_g"],
    "brakes":               ["accel_braking_threshold_g", "decel_no_brakes_threshold_g"],
    "position_jump":        ["max_jump_speed_kmh", "min_jump_meters", "max_gps_accuracy_m",
                             "min_gap_seconds", "max_gap_seconds"],
    "heading_inconsistency":["max_heading_diff_deg", "speed_gate_kmh", "max_gps_accuracy_m",
                             "min_distance_m", "min_gap_seconds", "max_gap_seconds"],
    "speed_position":       ["max_speed_diff_kmh", "speed_gate_kmh", "max_heading_change_deg",
                             "max_gps_accuracy_m", "min_gap_seconds", "max_gap_seconds",
                             "min_distance_m"],
    "speed_accel":          ["max_delta_error_ms", "min_gap_seconds", "max_gap_seconds",
                             "min_delta_speed_kmh"],
    "heading_change_rate":  ["max_heading_rate_deg_s", "speed_gate_kmh", "max_gps_accuracy_m",
                             "min_gap_seconds", "max_gap_seconds", "min_distance_m"],
    "yaw_rate":             ["max_yaw_diff_deg_s", "speed_gate_kmh", "max_gps_accuracy_m",
                             "min_gap_seconds", "max_gap_seconds", "min_distance_m"],
}


class DetectorConfig:
    """Immutable view of a validated ode_config.json file."""

    def __init__(self, data: dict, source: str = "<dict>"):
        self._data = data
        self._source = source
        self._validate()

    @classmethod
    def from_file(cls, path: Path) -> "DetectorConfig":
        """Load and validate a threshold config file.  Calls sys.exit on any error."""
        try:
            text = path.read_text()
        except FileNotFoundError:
            sys.exit(f"Error: threshold config not found: {path}")
        except OSError as exc:
            sys.exit(f"Error reading threshold config {path}: {exc}")

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            sys.exit(f"Error: threshold config {path} is not valid JSON: {exc}")

        return cls(data, source=str(path))

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self) -> None:
        missing = []
        for key, subkeys in _REQUIRED.items():
            if key not in self._data:
                missing.append(key)
            elif subkeys is not None:
                for subkey in subkeys:
                    if subkey not in self._data[key]:
                        missing.append(f"{key}.{subkey}")
        if missing:
            sys.exit(
                f"Error: threshold config {self._source} is missing required keys:\n"
                + "\n".join(f"  {k}" for k in missing)
            )

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def logstash_url(self) -> str:
        return str(self._data["logstash"]["url"])

    @property
    def confirm_n(self) -> int:
        return int(self._data["confirm_n"])

    @property
    def cooldown_meters(self) -> float:
        return float(self._data["cooldown"]["meters"])

    @property
    def cooldown_seconds(self) -> float:
        return float(self._data["cooldown"]["seconds"])

    def section(self, name: str) -> dict:
        """Return the sub-dict for a named detector section."""
        return self._data[name]
