"""
replay-launcher.py — Local HTTP server that:
  1. Serves a self-contained Leaflet map at /map (clickable dots → in-browser replay)
  2. Proxies ES misbehavior data as GeoJSON at /events
  3. Serves the in-browser replay animation at /replay
  4. Loads BSM trajectory data as JSON at /replay-data (fetched by the replay page)

Usage:
    python replay-launcher.py [--port 8765] [--file data/tampa_BSM_2021.zip]

Endpoints:
    GET /              health check
    GET /map           Leaflet companion map with clickable dots
    GET /events        GeoJSON proxy from Elasticsearch
    GET /replay?vehicle_id=<id>&time_at=<timestamp>
                       in-browser Leaflet animation page
    GET /replay-data?vehicle_id=<id>&time_at=<timestamp>
                       JSON trajectory frames (consumed by the replay page)
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Set a non-interactive matplotlib backend before importing replay so the
# data-loading functions work on headless servers (no display needed).
os.environ.setdefault("MPLBACKEND", "Agg")
import replay as _replay  # noqa: E402  (import after env-var set)

HERE         = Path(__file__).parent
DEFAULT_FILE = str(HERE / "data" / "tampa_BSM_2021.zip")
DEFAULT_LOG  = str(HERE / "logs" / "misbehaviors.log")
ES_URL       = "http://localhost:9200"
ES_INDEX     = "mbd-misbehaviors-*"

# Colour palette per misbehavior type (CSS colours)
MISBEHAVIOR_COLORS = {
    "accel_exceeded":                  "#e74c3c",
    "brakes_on_no_decel":              "#e67e22",
    "decel_no_brakes":                 "#f39c12",
    "heading_inconsistency":           "#9b59b6",
    "implausible_heading_change_rate": "#8e44ad",
    "position_jump":                   "#2980b9",
    "speed_accel_inconsistency":       "#27ae60",
    "speed_exceeded":                  "#c0392b",
    "speed_position_inconsistency":    "#16a085",
    "yaw_rate_inconsistency":          "#d35400",
}
DEFAULT_COLOR = "#555555"

# ---------------------------------------------------------------------------
# Companion map HTML
# ---------------------------------------------------------------------------
MAP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>MBD Misbehavior Map</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body{margin:0;padding:0;height:100%;}
  #map{height:100%;}
  #legend{
    position:absolute;bottom:24px;right:8px;z-index:1000;
    background:rgba(255,255,255,0.92);padding:8px 12px;
    border-radius:6px;font:13px/1.6 sans-serif;
    box-shadow:0 2px 6px rgba(0,0,0,.3);
  }
  #legend h4{margin:0 0 4px;font-size:13px;}
  .leg-item{display:flex;align-items:center;gap:6px;}
  .leg-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0;}
  #status{
    position:absolute;top:8px;left:50%;transform:translateX(-50%);
    z-index:1000;background:rgba(255,255,255,.88);padding:4px 14px;
    border-radius:4px;font:13px sans-serif;pointer-events:none;
  }
</style>
</head>
<body>
<div id="map"></div>
<div id="status">Loading events…</div>
<div id="legend"><h4>Misbehavior type</h4></div>
<script>
const COLORS = __COLORS_JSON__;

const map = L.map('map').setView([27.97, -82.50], 11);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{
  attribution:'© OpenStreetMap contributors',maxZoom:19
}).addTo(map);

const status = document.getElementById('status');
const legend = document.getElementById('legend');

// Build legend
for(const [type, color] of Object.entries(COLORS)){
  const d = document.createElement('div');
  d.className = 'leg-item';
  d.innerHTML = `<span class="leg-dot" style="background:${color}"></span>${type.replace(/_/g,' ')}`;
  legend.appendChild(d);
}

function loadEvents(){
  status.textContent = 'Loading events…';
  fetch('/events')
    .then(r=>{
      if(!r.ok) throw new Error('HTTP '+r.status);
      return r.json();
    })
    .then(geojson=>{
      const n = geojson.features ? geojson.features.length : 0;
      status.textContent = n + ' events loaded';
      setTimeout(()=>{ status.style.display='none'; }, 3000);

      L.geoJSON(geojson, {
        pointToLayer(feature, latlng){
          const type  = feature.properties.misbehavior || '';
          const color = COLORS[type] || '__DEFAULT_COLOR__';
          return L.circleMarker(latlng,{
            radius:5, color:'#fff', weight:1,
            fillColor:color, fillOpacity:0.85
          });
        },
        onEachFeature(feature, layer){
          const p = feature.properties;
          const replayUrl = '/replay?vehicle_id='
            + encodeURIComponent(p.vehicle_id||'')
            + '&time_at='
            + encodeURIComponent(p.time_at||'');
          layer.bindPopup(
            `<b>${(p.misbehavior||'').replace(/_/g,' ')}</b><br>`+
            `Vehicle: <code>${p.vehicle_id||''}</code><br>`+
            `Time: <code>${p.time_at||''}</code><br>`+
            `<a href="${replayUrl}" target="_blank" `+
            `style="display:inline-block;margin-top:6px;padding:4px 10px;`+
            `background:#2980b9;color:#fff;border-radius:4px;text-decoration:none;`+
            `font-weight:bold;">▶ Replay</a>`
          );
        }
      }).addTo(map);
    })
    .catch(err=>{
      status.textContent = 'Error loading events: ' + err.message;
      console.error(err);
    });
}

loadEvents();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# In-browser replay HTML
# Placeholders replaced at request time:
#   __VEHICLE_ID_JSON__  → JSON-encoded vehicle_id string
#   __TIME_AT_JSON__     → JSON-encoded time_at string
# ---------------------------------------------------------------------------
REPLAY_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>MBD Replay</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body{margin:0;padding:0;height:100%;}
  #map{position:absolute;top:0;left:0;right:0;bottom:72px;}
  #bar{
    position:absolute;bottom:0;left:0;right:0;height:72px;
    background:#1e2a3a;color:#e0e0e0;
    display:flex;align-items:center;gap:10px;padding:0 14px;
    box-shadow:0 -2px 6px rgba(0,0,0,.4);font:13px sans-serif;
  }
  #bar button{
    padding:6px 14px;border:none;border-radius:4px;
    font-size:13px;font-weight:bold;cursor:pointer;
  }
  #btn-replay{background:#c0392b;color:#fff;}
  #btn-pause {background:#d35400;color:#fff;}
  #btn-play  {background:#27ae60;color:#fff;}
  #speed-wrap{display:flex;align-items:center;gap:6px;margin-left:6px;}
  #speed-range{width:110px;accent-color:#3498db;}
  #info{
    margin-left:auto;text-align:right;
    font:12px/1.8 monospace;color:#aaa;
  }
  #status{
    position:absolute;top:10px;left:50%;transform:translateX(-50%);
    z-index:1000;background:rgba(255,255,255,.92);
    padding:5px 18px;border-radius:5px;font:13px sans-serif;
    pointer-events:none;white-space:nowrap;
    display:flex;align-items:center;gap:8px;
  }
  @keyframes spin{to{transform:rotate(360deg);}}
  #spinner{
    width:16px;height:16px;border-radius:50%;flex-shrink:0;
    border:3px solid rgba(0,0,0,.12);border-top-color:#2980b9;
    animation:spin .75s linear infinite;
  }
</style>
</head>
<body>
<div id="map"></div>
<div id="status"><span id="spinner"></span>Loading trajectory…</div>
<div id="bar">
  <button id="btn-replay">⏮ Replay</button>
  <button id="btn-pause">⏸ Pause</button>
  <button id="btn-play">▶ Play</button>
  <span id="speed-wrap">
    Speed&nbsp;<span id="speed-val">0.50</span>×
    <input type="range" id="speed-range" min="0.05" max="2.0" step="0.05" value="0.50"/>
  </span>
  <div id="info">
    <div id="ts-box">—</div>
    <div id="dt-box">—</div>
    <div id="spd-box">—</div>
  </div>
</div>
<script>
const VEHICLE_ID = __VEHICLE_ID_JSON__;
const TIME_AT    = __TIME_AT_JSON__;
const TRAIL_LEN  = 8;

const map = L.map('map');
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{
  attribution:'© OpenStreetMap contributors', maxZoom:19
}).addTo(map);

const statusEl   = document.getElementById('status');
const tsBox      = document.getElementById('ts-box');
const dtBox      = document.getElementById('dt-box');
const spdBox     = document.getElementById('spd-box');
const speedRange = document.getElementById('speed-range');
const speedVal   = document.getElementById('speed-val');

let frames = [], speed = 0.50;
let state  = {frame: 0, running: false, timerId: null};
let trailLine, dotMarker, arrowMarker;

// CSS-border triangle icon: points up (North) at deg=0; rotates clockwise.
// Matches SAE J2735 heading convention (0=North, 90=East, …).
function arrowIcon(deg) {
  return L.divIcon({
    className: '',
    html: `<div style="
      width:0;height:0;
      border-left:7px solid transparent;
      border-right:7px solid transparent;
      border-bottom:20px solid #2980b9;
      transform:rotate(${deg}deg);
      transform-origin:center center;
      filter:drop-shadow(0 0 2px rgba(255,255,255,.8));
    "></div>`,
    iconSize: [14, 20],
    iconAnchor: [7, 10],
  });
}

function starIcon() {
  return L.divIcon({
    className: '',
    html: '<div style="font-size:26px;color:#e74c3c;line-height:1;text-shadow:0 0 4px rgba(0,0,0,.6)">★</div>',
    iconSize: [26, 26],
    iconAnchor: [13, 13],
  });
}

function drawFrame(i) {
  const f = frames[i];
  const s = Math.max(0, i - TRAIL_LEN);
  trailLine.setLatLngs(frames.slice(s, i + 1).map(fr => [fr.lat, fr.lon]));
  dotMarker.setLatLng([f.lat, f.lon]);
  if (f.heading_deg !== null) {
    arrowMarker.setLatLng([f.lat, f.lon]);
    arrowMarker.setIcon(arrowIcon(f.heading_deg));
    arrowMarker.setOpacity(1);
  } else {
    arrowMarker.setOpacity(0);
  }
  tsBox.textContent  = f.timestamp_label;
  dtBox.textContent  = `\u0394 ${f.dt >= 0 ? '+' : ''}${f.dt.toFixed(3)}s`;
  spdBox.textContent = f.speed_kmh !== null ? `${f.speed_kmh.toFixed(1)} km/h` : '\u2014 km/h';
}

function intervalMs() {
  if (frames.length < 2) return 500;
  const totalS = frames[frames.length - 1].elapsed_s - frames[0].elapsed_s;
  return Math.max(50, Math.round(totalS / (frames.length - 1) * 1000 / speed));
}

function tick() {
  drawFrame(state.frame);
  if (state.frame < frames.length - 1) {
    state.frame++;
    state.timerId = setTimeout(tick, intervalMs());
  } else {
    state.running = false;
    state.timerId = null;
  }
}

function play() {
  if (state.timerId) clearTimeout(state.timerId);
  state.running = true;
  state.timerId = setTimeout(tick, intervalMs());
}

function pause() {
  if (state.timerId) clearTimeout(state.timerId);
  state.timerId = null;
  state.running = false;
}

document.getElementById('btn-replay').onclick = () => { pause(); state.frame = 0; drawFrame(0); play(); };
document.getElementById('btn-pause').onclick   = pause;
document.getElementById('btn-play').onclick    = () => {
  if (state.running) return;
  if (state.frame >= frames.length - 1) state.frame = 0;
  play();
};
speedRange.oninput = () => {
  speed = parseFloat(speedRange.value);
  speedVal.textContent = speed.toFixed(2);
};

const dataUrl = '/replay-data?vehicle_id=' + encodeURIComponent(VEHICLE_ID)
              + '&time_at='    + encodeURIComponent(TIME_AT);

fetch(dataUrl)
  .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
  .then(data => {
    if (data.error) throw new Error(data.error);
    frames = data.frames;
    if (!frames.length) { statusEl.textContent = 'No frames found.'; return; }

    map.fitBounds(L.latLngBounds(frames.map(f => [f.lat, f.lon])).pad(0.15));

    // Full trajectory (static grey line)
    L.polyline(frames.map(f => [f.lat, f.lon]),
               {color: '#555', weight: 2, opacity: 0.6}).addTo(map);

    // Red star at the misbehavior time
    const ri = frames.reduce((b, f, i) =>
      Math.abs(f.dt) < Math.abs(frames[b].dt) ? i : b, 0);
    L.marker([frames[ri].lat, frames[ri].lon], {icon: starIcon(), zIndexOffset: 1000})
      .addTo(map)
      .bindTooltip('time_at  ' + data.centre_label, {permanent: false});

    // Animated layers
    trailLine   = L.polyline([], {color: 'steelblue', weight: 3, opacity: 0.65}).addTo(map);
    dotMarker   = L.circleMarker([frames[0].lat, frames[0].lon],
                    {radius: 8, color: '#2471a3', fillColor: '#2980b9',
                     fillOpacity: 0.9, weight: 2}).addTo(map);
    arrowMarker = L.marker([frames[0].lat, frames[0].lon],
                    {icon: arrowIcon(0), interactive: false}).addTo(map);

    statusEl.style.display = 'none';
    drawFrame(0);
    play();
  })
  .catch(err => {
    statusEl.textContent = 'Error: ' + err.message;
    statusEl.style.background = '#fdd';
    console.error(err);
  });
</script>
</body>
</html>
"""


def _build_map_html() -> bytes:
    colors_json = json.dumps(MISBEHAVIOR_COLORS)
    html = MAP_HTML.replace("__COLORS_JSON__", colors_json)
    html = html.replace("'__DEFAULT_COLOR__'", json.dumps(DEFAULT_COLOR))
    return html.encode("utf-8")


def _build_replay_html(vehicle_id: str, time_at: str) -> bytes:
    html = REPLAY_HTML.replace("__VEHICLE_ID_JSON__", json.dumps(vehicle_id))
    html = html.replace("__TIME_AT_JSON__", json.dumps(time_at))
    return html.encode("utf-8")


def _fetch_events() -> dict:
    """Query ES for all misbehavior events and return as GeoJSON FeatureCollection."""
    query = {
        "size": 10000,
        "_source": ["vehicle_id", "record_generated_at", "misbehavior", "location"],
        "query": {"match_all": {}},
        "sort": [{"record_generated_at": "asc"}],
    }
    url = f"{ES_URL}/{ES_INDEX}/_search"
    data = json.dumps(query).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())

    features = []
    for hit in result.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        loc = src.get("location")
        if not loc:
            continue
        # geo_point can be "lat,lon" string or {"lat":..,"lon":..} dict
        if isinstance(loc, str):
            parts = loc.split(",")
            if len(parts) != 2:
                continue
            try:
                lat, lon = float(parts[0]), float(parts[1])
            except ValueError:
                continue
        elif isinstance(loc, dict):
            try:
                lat, lon = float(loc["lat"]), float(loc["lon"])
            except (KeyError, ValueError):
                continue
        else:
            continue

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "vehicle_id":  src.get("vehicle_id", ""),
                "time_at":     src.get("record_generated_at", ""),
                "misbehavior": src.get("misbehavior", ""),
            },
        })

    return {"type": "FeatureCollection", "features": features}


def _load_replay_frames(bsm_file: str, vehicle_id: str, time_at: str,
                        start_off: float = 10.0, end_off: float = 5.0):
    """
    Load BSM frames for a replay request using replay.py data helpers.
    Returns (frames_list, centre_label_str).
    Falls back to misbehaviors.log if no BSMs match the given time_at directly.
    """
    target_s = _replay._parse_time_at(time_at)
    frames = _replay.load_frames(
        Path(bsm_file), vehicle_id, target_s, start_off, end_off,
    )

    if not frames:
        log_p = Path(DEFAULT_LOG)
        hits = _replay.resolve_via_log(log_p, vehicle_id, target_s)
        if hits:
            rec_ts, _ = hits[0]
            bsm_time_s = _replay._tod_s(rec_ts)
            frames = _replay.load_frames(
                Path(bsm_file), vehicle_id, bsm_time_s, start_off, end_off,
            )

    _dt_label = _replay._parse_ts(time_at)
    centre_label = (
        _dt_label.strftime("%H:%M:%S.%f")[:-3] if _dt_label else time_at
    )
    return frames, centre_label


def _serialize_frames(frames: list, centre_label: str) -> dict:
    """Convert load_frames() output to a JSON-serialisable dict."""
    if not frames:
        return {"frames": [], "centre_label": centre_label}
    t0 = frames[0]["timestamp"]
    serialized = []
    for f in frames:
        serialized.append({
            "timestamp_label": f["timestamp"].strftime("%H:%M:%S.%f")[:-3],
            "elapsed_s":       (f["timestamp"] - t0).total_seconds(),
            "dt":              f["dt"],
            "lat":             f["lat"],
            "lon":             f["lon"],
            "speed_kmh":       f["speed_kmh"],
            "heading_deg":     f["heading_deg"],
        })
    return {"frames": serialized, "centre_label": centre_label}


class ReplayHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # Health-check endpoint
        if parsed.path == "/":
            self._respond(200, "Replay launcher is running.")
            return

        # Companion Leaflet map
        if parsed.path == "/map":
            body = _build_map_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # GeoJSON proxy (avoids CORS issues in the browser)
        if parsed.path == "/events":
            try:
                geojson = _fetch_events()
                body = json.dumps(geojson).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                err = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return

        # In-browser replay animation page
        if parsed.path == "/replay":
            params     = dict(urllib.parse.parse_qsl(parsed.query))
            vehicle_id = params.get("vehicle_id", "").strip()
            time_at    = params.get("time_at",    "").strip()
            if not vehicle_id or not time_at:
                self._respond(400,
                    "Missing parameter(s).<br>"
                    "Required: <code>vehicle_id</code> and <code>time_at</code>.")
                return
            body = _build_replay_html(vehicle_id, time_at)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # JSON trajectory data fetched by the replay page
        if parsed.path == "/replay-data":
            params     = dict(urllib.parse.parse_qsl(parsed.query))
            vehicle_id = params.get("vehicle_id", "").strip()
            time_at    = params.get("time_at",    "").strip()
            if not vehicle_id or not time_at:
                err = json.dumps({"error": "Missing vehicle_id or time_at"}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            try:
                print(f"[replay-data] vehicle={vehicle_id}  time_at={time_at}")
                frames, centre_label = _load_replay_frames(
                    self.server.bsm_file, vehicle_id, time_at,
                )
                result = _serialize_frames(frames, centre_label)
                body = json.dumps(result).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
                print(f"[replay-data] → {len(frames)} frames")
            except Exception as exc:
                err = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            return

        self._respond(404, "Not found.")

    def _respond(self, code: int, body_html: str) -> None:
        body = f"<html><body style='font-family:sans-serif;padding:1em'>{body_html}</body></html>".encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # suppress per-request noise
        pass


def main() -> None:
    global ES_URL
    p = argparse.ArgumentParser(description="MBD map and in-browser replay server")
    p.add_argument("--port", type=int, default=8765,
                   help="Port to listen on (default: 8765)")
    p.add_argument("--file", default=DEFAULT_FILE,
                   help=f"BSM ZIP or NDJSON file used for replay data "
                        f"(default: {DEFAULT_FILE})")
    p.add_argument("--es-url", default=ES_URL,
                   help=f"Elasticsearch base URL (default: {ES_URL})")
    args = p.parse_args()

    ES_URL = args.es_url

    server = HTTPServer(("0.0.0.0", args.port), ReplayHandler)
    server.bsm_file = args.file

    print(f"Launcher listening on  http://localhost:{args.port}/")
    print(f"BSM file : {args.file}")
    print(f"Map      : http://localhost:{args.port}/map")
    print("Press Ctrl-C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
