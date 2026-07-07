"""
replay.py — Replay a single vehicle's BSM movement on an animated map.

Usage:
    python replay.py --vehicle-id 8273834 \
                     --time-at "2021-02-02 18:17:50.380 [ET]" \
                     --start-offset 10 --end-offset 5 \
                     --speed 0.1 --file data/tampa_BSM_2021.zip

Arguments:
    --vehicle-id      Vehicle ID to replay (coreData.id)
    --time-at         Centre time.  Accepts either:
                        • Full Kibana timestamp: "YYYY-MM-DD HH:MM:SS[.mmm] [TZ]"
                        • Time-only:             HH:MM:SS[.mmm]
                      Pass the Kibana @timestamp directly — after the Logstash
                      fix, @timestamp equals record_generated_at (BSM time, ET).
    --start-offset    Seconds before --time-at to include  (default 10)
    --end-offset      Seconds after  --time-at to include  (default 5)
    --speed           Playback speed multiplier (0.1 = 10× slower, default 1.0)
    --file            Path to a BSM ZIP archive or plain NDJSON file
    --log             Path to misbehaviors.log used as a fallback when --time-at
                      matches no BSMs (default: logs/misbehaviors.log)

Display:
    - Grey line       Full trajectory of the vehicle in the window
    - Red star        Position at the centre time
    - Blue arrow      Current heading; label = speed in km/h
    - Blue trail      Last 8 positions
    - Top-left        Current BSM timestamp (HH:MM:SS.mmm)
    - Top-right       Offset from centre time (e.g. Δ −3.200s)

ZIP search performance:
    The Tampa BSM ZIP encodes date and hour in each entry's path
    (tampa/BSM/YYYY/MM/DD/HH/...).  Only entries whose encoded hour falls
    within the time window are opened, reducing scanned entries from ~37 K
    to the handful of files that cover the target hour.
"""

import argparse
import io
import json
import math
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import contextily as ctx
import matplotlib.animation as animation
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------------
# SAE J2735 constants
# ---------------------------------------------------------------------------
LAT_SCALE       = 1e-7     # degrees per LSB
LON_SCALE       = 1e-7
SPEED_UNIT_MS   = 0.02     # m/s per LSB
HEADING_UNIT    = 0.0125   # degrees per LSB
MS_TO_KMH       = 3.6
SPEED_UNAVAIL   = 8191
HEADING_UNAVAIL = 28800

TRAIL_LEN = 8              # number of previous positions to highlight


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(
        description="Animate a vehicle's BSM movement on a map."
    )
    p.add_argument("--vehicle-id",   required=True,
                   help="Vehicle ID to replay (coreData.id)")
    p.add_argument("--time-at",      required=True,
                   help="Centre time: 'YYYY-MM-DD HH:MM:SS[.mmm] [TZ]' or HH:MM:SS[.mmm]")
    p.add_argument("--start-offset", type=float, default=10,
                   help="Seconds before --time-at to include (default 10)")
    p.add_argument("--end-offset",   type=float, default=5,
                   help="Seconds after --time-at to include (default 5)")
    p.add_argument("--speed",        type=float, default=1.0,
                   help="Playback speed multiplier (0.1 = 10× slower)")
    p.add_argument("--file",         required=True,
                   help="BSM ZIP archive or plain NDJSON file")
    p.add_argument("--log",          default="logs/misbehaviors.log",
                   help="misbehaviors.log for fallback time resolution "
                        "(default: logs/misbehaviors.log)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------
def _parse_ts(ts: str):
    """Parse a datetime string; return datetime or None."""
    if not ts:
        return None
    clean = ts.split("[")[0].strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(clean.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _tod_s(dt: datetime) -> float:
    """Return time-of-day as total seconds since midnight."""
    return dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6


def _parse_time_at(time_at: str) -> float:
    """Parse --time-at in any supported format → seconds since midnight.

    Accepts:
        • Full datetime: "YYYY-MM-DD HH:MM:SS[.mmm] [TZ]"  (Kibana @timestamp)
        • Time only:     "HH:MM:SS[.mmm]"
    """
    # Try full datetime first (strips the "[ET]" / "[UTC]" suffix automatically)
    dt = _parse_ts(time_at)
    if dt is not None:
        return _tod_s(dt)
    # Fall back to bare time-of-day
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
        try:
            return _tod_s(datetime.strptime(time_at, fmt))
        except ValueError:
            pass
    raise ValueError(
        f"Cannot parse --time-at: {time_at!r}\n"
        "  Expected 'YYYY-MM-DD HH:MM:SS[.mmm] [TZ]' or 'HH:MM:SS[.mmm]'"
    )


# ---------------------------------------------------------------------------
# ZIP entry hour filtering
# ---------------------------------------------------------------------------
def _zip_entry_hour(filename: str):
    """
    Extract the encoded hour from a Tampa BSM ZIP entry path.
    Expected format: tampa/BSM/YYYY/MM/DD/HH/filename
    Returns int (0–23) or None if the path does not match that structure.
    """
    parts = filename.split("/")
    if len(parts) >= 7:
        try:
            return int(parts[5])
        except ValueError:
            pass
    return None


def _target_hours(target_s: float, start_off: float, end_off: float) -> set:
    """
    Return the set of hours (int, 0–23) that the window
    [target_s − start_off, target_s + end_off] spans.
    Handles midnight crossings via modulo arithmetic.
    """
    lo_h = int(max(0.0,     target_s - start_off)) // 3600
    hi_h = int(min(86399.0, target_s + end_off))   // 3600
    hours = set()
    h = lo_h % 24
    while True:
        hours.add(h)
        if h == hi_h % 24:
            break
        h = (h + 1) % 24
        if len(hours) >= 25:    # failsafe
            break
    return hours


# ---------------------------------------------------------------------------
# BSM data loading
# ---------------------------------------------------------------------------
def _iter_bsms(path: Path, hours=None):
    """
    Yield raw BSM dicts from a ZIP archive or a plain NDJSON file.
    When hours is a set of ints, ZIP entries whose encoded hour is not in
    hours are skipped entirely (large speedup for Tampa ZIP structure).
    """
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            entries = zf.infolist()
            if hours is not None:
                entries = [
                    e for e in entries
                    if not e.filename.endswith("/")
                    and _zip_entry_hour(e.filename) in hours
                ]
                print(f"  Scanning {len(entries)} ZIP entries "
                      f"(hour filter: {sorted(hours)})")
            for entry in entries:
                if entry.filename.endswith("/"):
                    continue
                with zf.open(entry) as raw:
                    for line in io.TextIOWrapper(raw, encoding="utf-8",
                                                 errors="replace"):
                        line = line.strip()
                        if line:
                            try:
                                yield json.loads(line)
                            except json.JSONDecodeError:
                                pass
    else:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        pass


def load_frames(path: Path, vehicle_id: str, target_s: float,
                start_off: float, end_off: float) -> list:
    """
    Return a time-sorted list of frame dicts for vehicle_id within
    [target_s − start_off, target_s + end_off] seconds (time-of-day match
    against recordGeneratedAt).
    """
    hours = _target_hours(target_s, start_off, end_off)
    frames = []

    for bsm in _iter_bsms(path, hours=hours):
        core = bsm.get("payload", {}).get("data", {}).get("coreData", {})
        if str(core.get("id", "")) != str(vehicle_id):
            continue

        ts = _parse_ts(bsm.get("metadata", {}).get("recordGeneratedAt", ""))
        if ts is None:
            continue

        dt = _tod_s(ts) - target_s
        if not (-start_off <= dt <= end_off):
            continue

        lat_raw = core.get("lat")
        lon_raw = core.get("long")
        if lat_raw is None or lon_raw is None:
            continue

        spd_raw = core.get("speed")
        speed_kmh = None
        if spd_raw is not None:
            s = int(spd_raw)
            if s != SPEED_UNAVAIL:
                speed_kmh = round(s * SPEED_UNIT_MS * MS_TO_KMH, 1)

        hdg_raw = core.get("heading")
        heading_deg = None
        if hdg_raw is not None:
            h = int(hdg_raw)
            if h != HEADING_UNAVAIL:
                heading_deg = h * HEADING_UNIT

        frames.append({
            "timestamp":   ts,
            "dt":          dt,
            "lat":         int(lat_raw) * LAT_SCALE,
            "lon":         int(lon_raw) * LON_SCALE,
            "speed_kmh":   speed_kmh,
            "heading_deg": heading_deg,
        })

    frames.sort(key=lambda x: x["timestamp"])
    return frames


# ---------------------------------------------------------------------------
# Fallback: resolve time via misbehaviors.log when no BSMs are found
# ---------------------------------------------------------------------------
def resolve_via_log(log_path: Path, vehicle_id: str, target_s: float,
                    window: float = 60.0):
    """
    Search misbehaviors.log for events matching vehicle_id whose
    record_generated_at time-of-day is within `window` seconds of target_s.
    Returns a sorted list of (record_generated_at datetime, diff) tuples.
    """
    if not log_path.exists():
        return []
    matches = []
    with open(log_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(event.get("vehicle_id", "")) != str(vehicle_id):
                continue
            rec_ts = _parse_ts(event.get("record_generated_at", ""))
            if rec_ts is None:
                continue
            diff = abs(_tod_s(rec_ts) - target_s)
            if diff <= window:
                matches.append((rec_ts, diff))
    matches.sort(key=lambda x: x[1])
    return matches


# ---------------------------------------------------------------------------
# Animation
# ---------------------------------------------------------------------------
def run_animation(frames: list, vehicle_id: str, speed: float,
                  centre_label: str) -> None:
    from matplotlib.widgets import Button, Slider

    lats = [f["lat"] for f in frames]
    lons = [f["lon"] for f in frames]

    extent    = max(max(lons) - min(lons), max(lats) - min(lats), 1e-5)
    arrow_len = max(0.0001, extent * 0.04)

    if len(frames) > 1:
        total_s          = (frames[-1]["timestamp"] - frames[0]["timestamp"]).total_seconds()
        base_interval_ms = max(50, int(total_s / (len(frames) - 1) * 1000))
    else:
        base_interval_ms = 500

    fig, ax = plt.subplots(figsize=(10, 8))
    fig.subplots_adjust(bottom=0.20)
    ax.set_aspect("equal")

    margin = max(0.0003, extent * 0.12)
    ax.set_xlim(min(lons) - margin, max(lons) + margin)
    ax.set_ylim(min(lats) - margin, max(lats) + margin)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.grid(True, alpha=0.25, color="white")

    print("Fetching map tiles …")
    ctx.add_basemap(ax, crs="EPSG:4326",
                    source=ctx.providers.OpenStreetMap.Mapnik, zoom="auto")

    ax.plot(lons, lats, color="#333333", linewidth=1.5, zorder=1,
            label="Trajectory")

    ref_idx = min(range(len(frames)), key=lambda i: abs(frames[i]["dt"]))
    ax.plot(lons[ref_idx], lats[ref_idx],
            "r*", markersize=14, zorder=4, label=f"time_at  {centre_label}")

    ax.legend(loc="lower right", fontsize=9)
    title = fig.suptitle(f"Vehicle  {vehicle_id}     ×{speed:.2f} speed", fontsize=12)

    trail_line, = ax.plot([], [], color="steelblue", linewidth=2.5,
                          alpha=0.55, zorder=2)
    dot,        = ax.plot([], [], "o", color="steelblue", markersize=9,
                          zorder=5)
    ts_box = ax.text(0.02, 0.97, "", transform=ax.transAxes, fontsize=10,
                     va="top", ha="left",
                     bbox=dict(boxstyle="round,pad=0.3", fc="white", alpha=0.85))
    dt_box = ax.text(0.98, 0.97, "", transform=ax.transAxes, fontsize=10,
                     va="top", ha="right",
                     bbox=dict(boxstyle="round,pad=0.3", fc="white", alpha=0.85))
    _arrow = [None]

    # ----------------------------------------------------------------
    # Manual timer instead of FuncAnimation — gives full control over
    # pause / play / speed without fighting FuncAnimation's internals.
    # ----------------------------------------------------------------
    state = {"frame": 0, "running": True}

    def draw_frame(i):
        f   = frames[i]
        lat = f["lat"]
        lon = f["lon"]

        s = max(0, i - TRAIL_LEN)
        trail_line.set_data(
            [fr["lon"] for fr in frames[s : i + 1]],
            [fr["lat"] for fr in frames[s : i + 1]],
        )
        dot.set_data([lon], [lat])

        if _arrow[0] is not None:
            _arrow[0].remove()
            _arrow[0] = None

        hdg = f.get("heading_deg")
        spd = f.get("speed_kmh")
        if hdg is not None:
            # dx = sin(H), dy = cos(H) gives visually correct compass direction
            # when the plot has equal x/y data units (set_aspect="equal").
            rad = math.radians(hdg)
            dx  = math.sin(rad) * arrow_len
            dy  = math.cos(rad) * arrow_len
            label = f"{spd:.1f} km/h" if spd is not None else "? km/h"
            _arrow[0] = ax.annotate(
                label,
                xy=(lon + dx, lat + dy),
                xytext=(lon, lat),
                arrowprops=dict(arrowstyle="-|>", color="steelblue",
                                lw=2.5, mutation_scale=18),
                fontsize=9, fontweight="bold", color="navy",
                ha="center", va="bottom",
                bbox=dict(boxstyle="round,pad=0.2", fc="lightyellow", alpha=0.92),
                zorder=6,
            )

        ts_box.set_text(f["timestamp"].strftime("%H:%M:%S.%f")[:-3])
        sign = "+" if f["dt"] >= 0 else ""
        dt_box.set_text(f"Δ {sign}{f['dt']:.3f}s")
        fig.canvas.draw_idle()

    def tick():
        i = state["frame"]
        draw_frame(i)
        if i < len(frames) - 1:
            state["frame"] = i + 1
        else:
            state["running"] = False
            timer.stop()

    timer = fig.canvas.new_timer(interval=max(50, int(base_interval_ms / speed)))
    timer.add_callback(tick)
    timer.start()

    # --- Controls (Replay / Pause / Play buttons + Speed slider) ---
    ax_replay = fig.add_axes([0.10, 0.04, 0.14, 0.05])
    ax_pause  = fig.add_axes([0.28, 0.04, 0.14, 0.05])
    ax_play   = fig.add_axes([0.46, 0.04, 0.14, 0.05])
    ax_speed  = fig.add_axes([0.15, 0.12, 0.55, 0.03])

    btn_replay = Button(ax_replay, "Replay")
    btn_pause  = Button(ax_pause,  "Pause")
    btn_play   = Button(ax_play,   "Play")
    sl_speed   = Slider(ax_speed, "Speed", 0.1, 1.0,
                        valinit=speed, valstep=0.05)

    def on_replay(event):
        state["frame"]   = 0
        state["running"] = True
        timer.stop()
        timer.start()

    def on_pause(event):
        state["running"] = False
        timer.stop()

    def on_play(event):
        if state["running"]:
            return
        if state["frame"] >= len(frames) - 1:
            state["frame"] = 0
        state["running"] = True
        timer.start()

    def on_speed(val):
        timer.stop()
        timer.interval = max(50, int(base_interval_ms / val))
        if state["running"]:
            timer.start()
        title.set_text(f"Vehicle  {vehicle_id}     ×{val:.2f} speed")

    btn_replay.on_clicked(on_replay)
    btn_pause.on_clicked(on_pause)
    btn_play.on_clicked(on_play)
    sl_speed.on_changed(on_speed)

    plt.show()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    args  = parse_args()
    path  = Path(args.file)
    log_p = Path(args.log)

    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    target_s = _parse_time_at(args.time_at)

    # Build a clean HH:MM:SS.mmm label regardless of which --time-at format was used
    _dt_label = _parse_ts(args.time_at)
    centre_label = (
        _dt_label.strftime("%H:%M:%S.%f")[:-3] if _dt_label else args.time_at
    )

    print(
        f"Loading BSMs for vehicle {args.vehicle_id} "
        f"(time_at={args.time_at}, "
        f"window=[−{args.start_offset}s, +{args.end_offset}s]) …"
    )

    frames = load_frames(
        path, args.vehicle_id, target_s,
        args.start_offset, args.end_offset,
    )

    # ------------------------------------------------------------------
    # Fallback: if time_at doesn't match any BSM by recordGeneratedAt,
    # try resolving it via record_generated_at in misbehaviors.log.
    # ------------------------------------------------------------------
    if not frames:
        print(f"  No BSMs matched. Checking {log_p} for a matching event …")
        hits = resolve_via_log(log_p, args.vehicle_id, target_s)
        if not hits:
            print(
                "  Nothing found in the log either.\n"
                "  In Kibana, open the event and use the record_generated_at "
                "field value as --time-at.",
                file=sys.stderr,
            )
            sys.exit(1)

        rec_ts, _ = hits[0]
        bsm_time_s = _tod_s(rec_ts)
        centre_label = rec_ts.strftime("%H:%M:%S.%f")[:-3]
        print(f"  Resolved to record_generated_at {centre_label}")

        frames = load_frames(
            path, args.vehicle_id, bsm_time_s,
            args.start_offset, args.end_offset,
        )
        if not frames:
            print(
                f"  Still no BSMs around {centre_label}. "
                "Try increasing --start-offset / --end-offset.",
                file=sys.stderr,
            )
            sys.exit(1)

    print(
        f"Found {len(frames)} BSMs  "
        f"{frames[0]['timestamp'].strftime('%H:%M:%S.%f')[:-3]} – "
        f"{frames[-1]['timestamp'].strftime('%H:%M:%S.%f')[:-3]}"
    )
    run_animation(frames, args.vehicle_id, args.speed, centre_label)


if __name__ == "__main__":
    main()
