"""
V2X BSM Misbehavior Detector

Reads BSM data and runs all registered detectors, writing a JSON-lines log
file suitable for ingestion by Logstash / ELK.

Input can be:
  - A plain NDJSON file
  - A ZIP archive containing one or more NDJSON data files at any depth;
    every non-directory entry in the archive is treated as a data file.

Usage:
    python detector.py <bsm_file_or_zip> [--log <log_file>] [--config <config_file>]
"""

import argparse
import hashlib
import io
import json
import sys
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from detectors.accel import AccelDetector
from detectors.brakes_inconsistency import BrakesInconsistencyDetector
from detectors.config import DetectorConfig
from detectors.heading_change_rate import HeadingChangeRateDetector
from detectors.heading_inconsistency import HeadingInconsistencyDetector
from detectors.position_jump import PositionJumpDetector
from detectors.speed import SpeedDetector
from detectors.speed_accel_consistency import SpeedAccelConsistencyDetector
from detectors.speed_position_consistency import SpeedPositionConsistencyDetector
from detectors.yaw_rate_consistency import YawRateConsistencyDetector
from detectors.utils import LAT_SCALE, LON_SCALE, _haversine_m, _parse_time, get_core

# Progress line update interval (seconds).
PROGRESS_INTERVAL = 1.0
# Width used to overwrite previous progress lines (avoids leftover characters).
_PROGRESS_WIDTH = 110

# Short labels for each misbehavior type shown on the live counts line.
# Order here is the display order.
_TYPE_ABBREV = {
    "speed_exceeded":                    "spd",
    "accel_exceeded":                    "acc",
    "brakes_on_no_decel":                "brk+",
    "decel_no_brakes":                   "brk-",
    "position_jump":                     "pos",
    "heading_inconsistency":             "hdg",
    "speed_position_inconsistency":      "spc",
    "speed_accel_inconsistency":         "sac",
    "implausible_heading_change_rate":   "hcr",
    "yaw_rate_inconsistency":            "yaw",
}

# Tracks how many lines the last _progress() call printed (0, 1, or 2).
# Used to correctly overwrite the previous output with ANSI cursor-up.
_progress_line_count = 0


def _build_detectors(cfg: DetectorConfig) -> list:
    """Instantiate all detectors from a loaded config."""
    n = cfg.confirm_n
    return [
        SpeedDetector(cfg.section("speed")),
        AccelDetector(cfg.section("accel")),
        BrakesInconsistencyDetector(cfg.section("brakes")),
        PositionJumpDetector(cfg.section("position_jump"), n),
        HeadingInconsistencyDetector(cfg.section("heading_inconsistency"), n),
        SpeedPositionConsistencyDetector(cfg.section("speed_position"), n),
        SpeedAccelConsistencyDetector(cfg.section("speed_accel"), n),
        HeadingChangeRateDetector(cfg.section("heading_change_rate"), n),
        YawRateConsistencyDetector(cfg.section("yaw_rate"), n),
    ]


def _fmt_eta(seconds: float) -> str:
    """Format a duration in seconds as a compact human-readable string."""
    seconds = int(seconds)
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _fmt_counts(counts: dict) -> str:
    """Format per-type misbehavior counts as a compact inline string."""
    parts = []
    for mtype, abbrev in _TYPE_ABBREV.items():
        cnt = counts.get(mtype, 0)
        if cnt:
            parts.append(f"{abbrev}:{cnt:,}")
    # Any future detector type not yet in _TYPE_ABBREV
    for mtype, cnt in counts.items():
        if mtype not in _TYPE_ABBREV and cnt:
            parts.append(f"{mtype[:5]}:{cnt:,}")
    return "  ".join(parts)


def _progress(msg: str, counts=None) -> None:
    """
    Overwrite the terminal progress display with msg (line 1) and, when
    counts is non-empty, a per-type breakdown (line 2).

    Uses ANSI cursor-up so that subsequent calls rewrite the same lines
    without scrolling.
    """
    global _progress_line_count

    line1 = f"{msg:<{_PROGRESS_WIDTH}}"
    counts_str = _fmt_counts(counts) if counts else ""

    if _progress_line_count == 2:
        # Previous output was 2 lines; cursor is at end of line 2.
        # Move up 1 row and go to column 0 to overwrite line 1.
        print(f"\033[1A\r{line1}", end="", flush=False)
    else:
        print(f"\r{line1}", end="", flush=False)

    if counts_str:
        line2 = f"{counts_str:<{_PROGRESS_WIDTH}}"
        print(f"\n\r{line2}", end="", flush=True)
        _progress_line_count = 2
    else:
        sys.stdout.flush()
        _progress_line_count = 1


def parse_args():
    parser = argparse.ArgumentParser(description="V2X BSM Misbehavior Detector")
    parser.add_argument(
        "bsm_file",
        help="Path to a NDJSON BSM data file or a ZIP archive of BSM data files",
    )
    parser.add_argument(
        "--log",
        default="logs/misbehaviors.log",
        help="Output log file path (default: logs/misbehaviors.log)",
    )
    parser.add_argument(
        "--config",
        default="ode_config.json",
        help="ODE config file (default: ode_config.json)",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Truncate the log file before writing (removes entries from previous runs)",
    )
    return parser.parse_args()


def extract_context(bsm: dict) -> dict:
    """Pull common fields used by all log entries."""
    meta = bsm.get("metadata", {})
    core = get_core(bsm)

    lat_raw = core.get("lat")
    lon_raw = core.get("long")

    return {
        "record_generated_at": meta.get("recordGeneratedAt", ""),
        "rsu_id": meta.get("RSUID", ""),
        "bsm_source": meta.get("bsmSource", ""),
        "vehicle_id": core.get("id", ""),
        "msg_cnt": core.get("msgCnt", ""),
        "lat": round(int(lat_raw) * LAT_SCALE, 7) if lat_raw is not None else None,
        "lon": round(int(lon_raw) * LON_SCALE, 7) if lon_raw is not None else None,
    }


def _process_bsm(bsm: dict, log_f, cooldown: dict, counts: dict,
                 detectors: list, cooldown_meters: float, cooldown_seconds: float) -> tuple:
    """
    Run all detectors against a single BSM dict.  Writes any flagged events
    to log_f and updates cooldown and counts in place.

    Returns (flagged, suppressed) counts for this BSM.

    This is the ODE hook — called once per incoming BSM message on deployment.
    """
    flagged = 0
    suppressed = 0
    context = extract_context(bsm)

    for detector in detectors:
        result = detector.check(bsm)
        if result is None:
            continue

        key = (context["vehicle_id"], result["misbehavior"])
        lat, lon = context.get("lat"), context.get("lon")
        bsm_time = _parse_time(context.get("record_generated_at", ""))
        prev = cooldown.get(key)

        if prev is not None and lat is not None and lon is not None:
            prev_lat, prev_lon, prev_time = prev
            close_space = _haversine_m(lat, lon, prev_lat, prev_lon) <= cooldown_meters
            if bsm_time is not None and prev_time is not None:
                close_time = (
                    abs((bsm_time - prev_time).total_seconds()) <= cooldown_seconds
                )
            else:
                close_time = False
            if close_space or close_time:
                suppressed += 1
                continue

        cooldown[key] = (lat, lon, bsm_time)

        event_id = hashlib.sha1(
            f"{context['vehicle_id']}|{context['record_generated_at']}|{result['misbehavior']}".encode()
        ).hexdigest()[:16]
        log_entry = {
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "event_id": event_id,
            **context,
            **result,
        }
        log_f.write(json.dumps(log_entry) + "\n")
        flagged += 1
        counts[result["misbehavior"]] += 1

    return flagged, suppressed


def _process_lines(lines, log_f, cooldown: dict, counts: dict,
                   detectors: list, cooldown_meters: float, cooldown_seconds: float,
                   report_progress: bool = False):
    """
    Core processing loop.  Runs all detectors over an iterable of raw text
    lines, writes flagged events to log_f, and updates the shared cooldown
    and counts dicts in place.

    When report_progress is True, a \r progress line is printed at most once
    per PROGRESS_INTERVAL seconds (used for plain-file mode).

    Returns (total_records, flagged, suppressed) for this batch of lines.
    """
    total = 0
    flagged = 0
    suppressed = 0
    t0 = time.monotonic()
    last_print = t0

    for line_num, line in enumerate(lines, start=1):
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.strip()
        if not line:
            continue
        total += 1

        if report_progress:
            now = time.monotonic()
            if now - last_print >= PROGRESS_INTERVAL:
                elapsed = now - t0
                rate = total / elapsed if elapsed > 0 else 0
                _progress(
                    f"  Records: {total:>10,} | Flagged: {flagged:>7,} | {rate:>8,.0f} rec/s",
                    counts,
                )
                last_print = now

        try:
            bsm = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[WARN] line {line_num}: JSON parse error – {exc}", file=sys.stderr)
            continue

        fl, sup = _process_bsm(bsm, log_f, cooldown, counts,
                               detectors, cooldown_meters, cooldown_seconds)
        flagged += fl
        suppressed += sup

    return total, flagged, suppressed


def process_input(bsm_path: Path, log_path: Path, cfg: DetectorConfig,
                  clear: bool = False):
    """
    Process a plain NDJSON file or a ZIP archive.  Returns
    (total_records, total_flagged, total_suppressed, counts_by_type).
    When clear=True the log is truncated; otherwise new entries are appended.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)

    detectors       = _build_detectors(cfg)
    cooldown_meters  = cfg.cooldown_meters
    cooldown_seconds = cfg.cooldown_seconds

    total = 0
    flagged = 0
    suppressed = 0
    counts = defaultdict(int)
    cooldown = {}

    mode = "w" if clear else "a"
    with log_path.open(mode) as log_f:
        if zipfile.is_zipfile(bsm_path):
            with zipfile.ZipFile(bsm_path) as zf:
                data_entries = [e for e in zf.infolist() if not e.filename.endswith("/")]
                n = len(data_entries)
                print(f"  ZIP contains {n:,} data file(s)")
                t0 = time.monotonic()
                last_print = t0
                for i, entry in enumerate(data_entries, start=1):
                    with zf.open(entry) as raw_f:
                        lines = io.TextIOWrapper(raw_f, encoding="utf-8", errors="replace")
                        t, fl, sup = _process_lines(
                            lines, log_f, cooldown, counts,
                            detectors, cooldown_meters, cooldown_seconds,
                        )
                    total += t
                    flagged += fl
                    suppressed += sup

                    now = time.monotonic()
                    if now - last_print >= PROGRESS_INTERVAL or i == n:
                        elapsed = now - t0
                        rate = total / elapsed if elapsed > 0 else 0
                        pct = 100.0 * i / n
                        eta = (
                            _fmt_eta(elapsed / i * (n - i))
                            if i < n and elapsed > 0
                            else "done"
                        )
                        _progress(
                            f"  Files: {i:>{len(str(n))},}/{n:,} ({pct:5.1f}%)"
                            f" | Records: {total:>10,}"
                            f" | Flagged: {flagged:>7,}"
                            f" | {rate:>8,.0f} rec/s"
                            f" | ETA: {eta}",
                            counts,
                        )
                        last_print = now
                print()  # move past the progress line
        else:
            with bsm_path.open() as f:
                t, fl, sup = _process_lines(
                    f, log_f, cooldown, counts,
                    detectors, cooldown_meters, cooldown_seconds,
                    report_progress=True,
                )
            print()  # move past the progress line
            total += t
            flagged += fl
            suppressed += sup

    return total, flagged, suppressed, counts


def _print_summary(total, flagged, suppressed, counts):
    print(f"\nProcessed : {total:,} records")
    print(f"Flagged   : {flagged:,} misbehaviors ({suppressed:,} suppressed as nearby duplicates)")
    if counts:
        print("\nMisbehaviors by type:")
        width = max(len(k) for k in counts)
        for mtype, cnt in sorted(counts.items()):
            print(f"  {mtype:<{width}}  {cnt:>6,}")
    else:
        print("\nNo misbehaviors detected.")


def main():
    args = parse_args()
    bsm_path    = Path(args.bsm_file)
    log_path    = Path(args.log)
    config_path = Path(args.config)

    if not bsm_path.exists():
        print(f"Error: file not found: {bsm_path}", file=sys.stderr)
        sys.exit(1)

    cfg = DetectorConfig.from_file(config_path)

    print(f"Input  : {bsm_path}")
    print(f"Log    : {log_path}{'  (clearing)' if args.clear else ''}")
    print(f"Config : {config_path}")

    total, flagged, suppressed, counts = process_input(
        bsm_path, log_path, cfg, clear=args.clear
    )
    _print_summary(total, flagged, suppressed, counts)


if __name__ == "__main__":
    main()
