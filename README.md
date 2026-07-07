# V2X BSM Misbehavior Detection (MBD)

A Python + ELK-stack pipeline that ingests SAE J2735 Basic Safety Messages (BSMs)
from connected vehicles, runs a suite of physics-based detectors to flag
anomalous behaviour, and visualises findings in Kibana.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Prerequisites](#prerequisites)
3. [Quick Start](#quick-start)
4. [Workflow: Ingesting New Data](#workflow-ingesting-new-data)
5. [Workflow: Wiping Data and Starting Fresh](#workflow-wiping-data-and-starting-fresh)
6. [Workflow: ODE Mode (Kafka)](#workflow-ode-mode-kafka)
7. [Makefile Reference](#makefile-reference)
8. [Detectors](#detectors)
9. [Threshold System (L1 / L2)](#threshold-system-l1--l2)
10. [Kibana Dashboards](#kibana-dashboards)
11. [Network Access & Security](#network-access--security)
12. [Troubleshooting](#troubleshooting)
13. [Vehicle Replay Tool](#vehicle-replay-tool) — in-browser (`replay-launcher.py`) and command-line (`replay.py`)
14. [Development: Unit Tests](#development-unit-tests)
15. [Future Work](#future-work)
16. [Project Structure](#project-structure)

---

## Architecture

### Local / batch mode

```
BSM data file(s)
(NDJSON or ZIP)
       │
       ▼
 detector.py          ← runs all 9 detectors via _process_bsm(), one BSM at a time
       │
       │  logs/misbehaviors.log  (JSON-lines, one event per line)
       ▼
  Logstash             ← reads log file directly; parses & indexes
       │
       ▼
Elasticsearch          ← Index: mbd-misbehaviors-YYYY.MM.dd
       │
       ├─ mbd-display alias  ← L2 filter from display-thresholds.json
       │                        (manage_display_filter.py)
       ▼
   Kibana               ← Dashboards and KPI panels

   launcher             ← Leaflet map + in-browser replay (port 8765)
                           fetches events from Elasticsearch
```

### ODE / streaming mode

```
ODE Kafka
(topic.OdeBsmJson)
       │
       ▼
 bsm_agent.py         ← subscribes per RSU; calls _process_bsm() for each BSM
       │
       │  logs/misbehaviors.log  (JSON-lines, one event per line)
       ▼
  Filebeat             ← tails log; ships to remote Logstash via Beats protocol
       │
       ▼
  Logstash (remote)    ← parses & indexes
       │
       ▼
Elasticsearch + Kibana
```

**Key components:**

| Component | Role |
|---|---|
| `detector.py` | Batch entry point — reads BSM files/ZIPs, calls `_process_bsm()` |
| `bsm_agent.py` | ODE entry point — Kafka consumer, calls `_process_bsm()` per BSM |
| `detectors/` | Nine physics-based detector modules |
| `ode_config.json` | ODE configuration: Logstash endpoint, Kafka broker/topic, L1 detector thresholds |
| `logs/misbehaviors.log` | JSON-lines event log; read by Logstash (local) or Filebeat (ODE) |
| Logstash | Parses log entries and indexes to Elasticsearch |
| Filebeat | ODE sidecar — tails the log and ships to the remote Logstash |
| Elasticsearch | Stores events; hosts the `mbd-display` filtered alias |
| Kibana | Dashboards and KPI panels for interactive exploration |
| `manage_display_filter.py` | Pushes L2 filter from `display-thresholds.json` into ES as an alias |
| `display-thresholds.json` | Editable per-type L2 display thresholds |
| `docker-compose.yml` | Local ELK stack (Elasticsearch, Logstash, Kibana, setup, launcher) |
| `docker-compose-ode.yml` | ODE overlay — adds Filebeat and `bsm_agent` (profile `ode`) |
| `Makefile` | Single entry point for all common operations |
| `replay-launcher.py` | HTTP server (port 8765) — Leaflet map + in-browser replay; starts automatically as a Docker service |
| `replay.py` | Standalone command-line animation tool; data-loading functions reused by `replay-launcher.py` |

All ELK services run in Docker.  In local mode `detector.py` runs on the host
and writes to `logs/`, which is volume-mounted into Logstash.  In ODE mode
`bsm_agent.py` and Filebeat run as containers alongside the agent.

---

## Prerequisites

- Docker + Docker Compose
- Python 3.9+ with dependencies:
- **BSM data file** — the Tampa CV Pilot dataset used for testing is available
  on request from the
  [Connected Vehicle Pilot (CVP) Open Data](https://data.transportation.gov/stories/s/Connected-Vehicle-Pilot-Sandbox/hr8h-ufhq).
  Place the downloaded ZIP in the `data/` directory and pass it with `DATA=`
  or `--file`.

```bash
# Create and populate the virtual environment (required before running make)
python3 -m venv venv

# Local / batch mode
venv/bin/pip install -r requirements.txt

# ODE / streaming mode (adds confluent-kafka)
venv/bin/pip install -r requirements.txt -r requirements-ode.txt
```

`requirements.txt` includes `elasticsearch>=8,<9` and `pytest>=8.0`.
The Makefile uses `venv/bin/python3` directly, so the venv does not need
to be activated before running `make`.

---

## Quick Start

```bash
# 1. Start the ELK stack + launcher (first run pulls/builds images; takes a few minutes)
docker compose up -d

# 2. Detect misbehaviors in a BSM data file
make run DATA=data/tampa_BSM_2021.zip

# 3. Restart Logstash so it ingests the new log entries
make ingest

# 4. Push the L2 display filter to Elasticsearch
make filter

# 5. Open Kibana and select the "Misbehavior Report - Main" dashboard
#    http://localhost:5601

# 6. Open the companion map (click any dot → ▶ Replay)
#    http://localhost:8765/map
```

Or run all three steps at once:

```bash
make full DATA=data/tampa_BSM_2021.zip
```

---

## Workflow: Ingesting New Data

### Step-by-step

```bash
# Run detectors (appends to the existing log by default)
make run DATA=data/new_data.zip

# Tell Logstash to re-read the log
make ingest

# Refresh the L2 alias filter (only needed if display-thresholds.json changed)
make filter
```

### What happens under the hood

1. `detector.py` reads every BSM in the file or ZIP archive.
2. Each BSM is passed through all 9 detectors.  When a detector fires, a
   JSON event is written to `logs/misbehaviors.log`.
3. A cooldown mechanism suppresses duplicate events for the same vehicle
   and misbehavior type: suppressed if within 50 m **or** within 30 s of
   the last logged event (OR, not AND — a vehicle at highway speed exits
   50 m in under a second, so AND would never suppress moving vehicles).
4. Restarting Logstash (`make ingest`) causes it to re-read the log file
   from the beginning and index all lines to Elasticsearch.
5. Logstash creates or appends to today's date-stamped index
   (`mbd-misbehaviors-YYYY.MM.dd`).
6. Kibana's `mbd-misbehaviors*` data view covers all daily indices
   automatically.

### Checking progress while `detector.py` runs

The terminal displays a live progress line:

```
  Files: 142/500 ( 28.4%) | Records:  1,423,719 | Flagged:  3,218 | 52,340 rec/s | ETA: 4m 12s
  spd:14  acc:22  pos:3,180  hdg:0  sac:2
```

The second line shows counts by misbehavior abbreviation:
`spd` speed_exceeded, `acc` accel_exceeded, `brk+` brakes_on_no_decel,
`brk-` decel_no_brakes, `pos` position_jump, `hdg` heading_inconsistency,
`spc` speed_position_inconsistency, `sac` speed_accel_inconsistency,
`hcr` implausible_heading_change_rate, `yaw` yaw_rate_inconsistency.

---

## Workflow: Wiping Data and Starting Fresh

Use this when you want to discard **today's** results and re-run a clean
analysis (e.g., after changing detector thresholds or fixing a bug).

> **Scope:** `make fresh` only removes today's ES index.  Indices from
> previous dates remain in Elasticsearch and will still appear in Kibana.
> To wipe all historical data see [Deleting all historical indices](#deleting-all-historical-indices) below.

```bash
make fresh DATA=data/tampa_BSM_2021.zip
```

This single command:
1. Deletes today's Elasticsearch index (`mbd-misbehaviors-YYYY.MM.dd`).
2. Truncates `logs/misbehaviors.log` (via `--clear` flag on `detector.py`).
3. Runs `detector.py` on the specified data file.
4. Restarts Logstash to re-ingest the fresh log.
5. Pushes the L2 display filter to Elasticsearch.

### Deleting all historical indices

The easiest way to wipe all MBD data across all dates is to bring the stack
down with the `-v` flag (removes Docker volumes: Elasticsearch data and Kibana
state), clear and regenerate the detection log, then restart:

```bash
docker compose down -v
rm logs/*
make run DATA=data/tampa_BSM_2021.zip
docker compose up -d

# Wait for the stack to be healthy, then restore data
make ingest    # re-ingest the detection log into Elasticsearch
make filter    # re-create the mbd-display alias (filtered dashboard)
```

Dashboards and data views are re-imported automatically on the next `up`
(see `setup.sh`), but Elasticsearch data and the display alias are not —
`make ingest` and `make filter` are required to repopulate both dashboards.

If you want to keep Kibana configuration (saved dashboards, customisations)
and only remove Elasticsearch indices, delete them one by one — wildcards are
blocked by the ES safety setting:

```bash
# List all MBD indices
curl http://localhost:9200/mbd-misbehaviors*?pretty

# Delete them one by one
curl -X DELETE http://localhost:9200/mbd-misbehaviors-2024.06.01
curl -X DELETE http://localhost:9200/mbd-misbehaviors-2024.06.02
# ... repeat for each index shown above ...

# Re-establish the alias after deleting all indices
make filter
```

---

## Workflow: ODE Mode (Kafka)

`bsm_agent.py` subscribes to `topic.OdeBsmJson` on the ODE Kafka broker and
processes each incoming BSM through the same detectors as the batch pipeline.
One agent instance is deployed per RSU.  Misbehavior events are written to
`logs/misbehaviors.log`; the Filebeat sidecar tails that file and ships events
to the remote Logstash.

### Configuration

Edit `ode_config.json` before deploying:

```json
"logstash": { "url": "tcp://<logstash-host>:5044" },
"kafka":    { "bootstrap_servers": "<kafka-host>:9092",
              "topic": "topic.OdeBsmJson",
              "group_id": "bsm_mbd_group" }
```

### Combined local mode (zip-file testing with Filebeat)

Runs the full local ELK stack **plus** the Filebeat sidecar.  Logstash accepts
events from both its direct file-read and the Beats input; ES deduplicates via
`event_id` so no duplicate records appear.

```bash
docker compose -f docker-compose.yml -f docker-compose-ode.yml up -d
python detector.py data/tampa_BSM_2021.zip
```

### ODE production mode

Filebeat ships to the remote Logstash.  `bsm_agent` starts only when the
`ode` profile is requested.  No local ELK stack is needed.

```bash
LOGSTASH_URL=<logstash-host>:5044 \
docker compose -f docker-compose-ode.yml --profile ode up -d
```

`bsm_agent.py` can also be run directly (outside Docker) for development:

```bash
python bsm_agent.py --config ode_config.json --log logs/misbehaviors.log
```

### State and continuity

Per-vehicle detector state (position, heading history, etc.) is kept in
process memory and is lost on restart — at most `CONFIRM_N` detections per
vehicle are missed.  Continuity is also lost when a vehicle moves between
RSUs; both are accepted trade-offs for this deployment model.

---

## Makefile Reference

```
make run    [DATA=<file>]   Detect misbehaviors; append to log
make filter                 Push display-thresholds.json → ES display alias
make ingest                 Restart Logstash to ingest the latest log
make full   [DATA=<file>]   run + ingest + filter
make clear                  Delete today's ES index
make fresh  [DATA=<file>]   clear + run (truncating log) + ingest + filter
make test                   Run pytest unit tests
make help                   Print this list
```

`DATA` defaults to the first `*.zip` or `*.json` file found in `data/`.

Override defaults:

```bash
make run DATA=data/custom.zip LOG=logs/custom.log
make filter ES=http://remote-host:9200
```

---

## Detectors

All detectors read from `payload.data.coreData` (SAE J2735 BSM core data).
Fields are integers encoded per J2735 unit conventions; the detector code
converts them to SI / human-readable units.

### Stateless detectors (single-message checks)

#### 1. Speed Exceeded (`speed_exceeded`)

**Field:** `coreData.speed`
**Unit:** 0.02 m/s per LSB · 3.6 = km/h
**Threshold:** > 200 km/h

Flags any BSM where the reported speed exceeds a physically implausible
absolute limit.  The L2 threshold raises this to 200 km/h (same as L1 here)
to retain all hits in the display filter by default.

**Output fields:** `speed_kmh`, `threshold_kmh`, `speed_raw`

---

#### 2. Acceleration Exceeded (`accel_exceeded`)

**Field:** `coreData.accelSet.long`
**Unit:** 0.01 m/s² per LSB
**Threshold:** |accel| > 2.0 g (19.61 m/s²)

Flags BSMs reporting longitudinal acceleration or deceleration beyond the
physical limit of tyre-road friction for a road vehicle.  Covers both
hard-braking and implausible forward acceleration.

**Output fields:** `accel_g`, `accel_ms2`, `threshold_g`, `accel_raw`

---

#### 3. Brakes–Deceleration Inconsistency (`brakes_on_no_decel` / `decel_no_brakes`)

**Fields:** `coreData.brakes.wheelBrakes` (5-bit bitmap), `coreData.accelSet.long`

Two sub-types are detected:

- **`brakes_on_no_decel`** — wheel brakes reported as applied, yet the
  vehicle is accelerating beyond 1.0 g.  Applied wheel brakes cannot produce
  net forward acceleration; this combination is physically impossible.

- **`decel_no_brakes`** — heavy deceleration (> 1.0 g magnitude) reported
  with no wheel brakes active.  Engine braking tops out around 0.15 g;
  anything harder requires wheel brakes.

The `wheelBrakes` unavailable bit (bit 0) causes the entire message to be
skipped so corrupted bitmap values do not generate spurious flags.

**Output fields:** `accel_g`, `accel_ms2`, `threshold_ms2`, `wheel_brakes`

---

### Stateful detectors (compare consecutive messages per vehicle)

Stateful detectors maintain a `_last` dictionary keyed by vehicle ID
(`coreData.id`).  They pair each incoming BSM with the previous one from
the same vehicle and use the **secMark** field (`coreData.secMark`) for
elapsed-time calculations.

**secMark** is a J2735 DSSecond value: milliseconds within the current
minute, 0–59999.  Value 65535 = unavailable.  The helper
`_secmark_elapsed_s(prev, curr)` handles minute wraparound via modulo 60 000.

All stateful detectors apply a **timing window** filter and a
**multi-message confirmation** requirement:

| Guard | Value | Purpose |
|---|---|---|
| `MIN_GAP_SECONDS` | 0.05 s | Reject near-simultaneous pairs (timing artifacts) |
| `MAX_GAP_SECONDS` | 0.15 s | Reject large gaps; vehicle may have turned or moved |
| `CONFIRM_N` | 2 | Consecutive violations required before flagging; single-message GPS artefacts (tunnel exits, multipath) are suppressed |

BSMs are transmitted at ~10 Hz (≈ 100 ms apart), so valid consecutive pairs
land squarely in the 50–150 ms window.

Early returns caused by data-quality guards (timing gap out of range, missing
secMark, poor GPS fix) do **not** reset the confirmation streak — they carry no
information about whether the vehicle is behaving correctly.  Only a
confirmed-clean observation resets it.

---

#### 4. Position Jump (`position_jump`)

**Fields:** `lat`, `long`, `secMark`
**Threshold:** implied speed > 10 km/h AND displacement > 100 m in one interval
**GPS accuracy gate:** skip if `accuracy.semiMajor` > 5 m (poor fix mimics a jump)
**Confirmation:** 2 consecutive violations required

Calculates the Haversine distance between consecutive positions of the same
vehicle.  A jump larger than 100 m in ≤ 150 ms implies a speed impossible
for a ground vehicle — a strong indicator of position spoofing or a GPS
outlier.

**Output fields:** `jump_m`, `elapsed_s`, `implied_speed_kmh`, `threshold_kmh`,
`prev_lat`, `prev_lon`

---

#### 5. Heading Inconsistency (`heading_inconsistency`)

**Fields:** `heading`, `lat`, `long`, `speed`, `secMark`
**Threshold:** |reported heading − GPS-derived bearing| > 90°
**Speed gate:** skip if either the current or the previous message speed < 20 km/h
**Minimum displacement:** 5 m
**GPS accuracy gate:** skip if `accuracy.semiMajor` > 5 m
**Confirmation:** 2 consecutive violations required

Derives the true bearing of motion from GPS displacement and compares it
against the reported heading.  A discrepancy larger than 90° means the
vehicle claims to be pointing in a direction more than perpendicular to its
actual movement — a strong sign of heading field spoofing.

**Output fields:** `reported_heading`, `gps_bearing`, `heading_diff`,
`threshold_deg`, `speed_kmh`, `distance_m`

---

#### 6. Speed–Position Consistency (`speed_position_inconsistency`)

**Fields:** `speed`, `heading`, `lat`, `long`, `secMark`
**Threshold:** |reported speed − implied speed| > 500 km/h
**Speed gate:** skip if either reported or implied speed < 10 km/h (lower than heading/yaw detectors because magnitude is less sensitive to GPS noise than direction)
**Heading correction:** skip if the reported heading changed > 30° between messages (haversine underestimates travel distance mid-turn)
**Minimum displacement:** 5 m
**GPS accuracy gate:** skip if `accuracy.semiMajor` > 5 m
**Confirmation:** 2 consecutive violations required

Computes implied speed from GPS displacement ÷ elapsed time and compares it
against the reported speed field.  A large discrepancy in either direction is
suspicious:

- **`reported_exceeds_implied`** — speed field inflated (ghost-vehicle attack)
- **`implied_exceeds_reported`** — position jumps faster than speed claims

**Output fields:** `direction`, `reported_speed_kmh`, `implied_speed_kmh`,
`diff_kmh`, `diff_abs_kmh`, `threshold_kmh`, `distance_m`, `elapsed_s`

---

#### 7. Speed–Acceleration Consistency (`speed_accel_inconsistency`)

**Fields:** `speed`, `accelSet.long`, `secMark`
**Threshold:** |observed Δspeed − expected Δspeed| > 5 m/s
**Minimum Δspeed:** 20 km/h (filters near-constant-speed segments)
**Confirmation:** 2 consecutive violations required

Newton's second law says speed change ≈ acceleration × time.  The detector
computes `expected_Δspeed = prev_accel × elapsed_s` and compares it against
the actual speed change between messages.  An error larger than 5 m/s means
at least one of the three fields (speed, acceleration, timestamp) is spoofed
or severely corrupted.

**Output fields:** `observed_delta_ms`, `expected_delta_ms`, `error_ms`,
`error_kmh`, `threshold_ms`, `accel_ms2`, `elapsed_s`

---

#### 8. Implausible Heading Change Rate (`implausible_heading_change_rate`)

**Fields:** `heading`, `lat`, `long`, `speed`, `secMark`
**Threshold:** heading change rate > 90 °/s
**Speed gate:** skip if either the current or the previous message speed < 20 km/h
**Minimum displacement:** 5 m
**GPS accuracy gate:** skip if `accuracy.semiMajor` > 5 m
**Confirmation:** 2 consecutive violations required

Divides the angular heading change by elapsed time to get a turning rate in
°/s.  Tyre-friction physics limit how fast a road vehicle can yaw; the
empirical maximum in clean BSM data is ≈ 65.9 °/s.  Rates above 90 °/s
indicate a spoofed heading sequence.

**Output fields:** `heading_rate_deg_s`, `threshold_deg_s`, `heading_diff_deg`,
`elapsed_s`, `speed_kmh`

---

#### 9. Yaw Rate Consistency (`yaw_rate_inconsistency`)

**Fields:** `heading`, `accelSet.yaw`, `lat`, `long`, `speed`, `secMark`
**Threshold:** |reported yaw rate − GPS-derived yaw rate| > 90 °/s
**Speed gate:** skip if either the current or the previous message speed < 20 km/h
**Minimum displacement:** 5 m
**GPS accuracy gate:** skip if `accuracy.semiMajor` > 5 m
**Confirmation:** 2 consecutive violations required

Compares the gyroscope yaw rate (`accelSet.yaw`, signed °/s) against the
heading change rate derived from consecutive GPS positions.  The two values
should agree in both magnitude and sign (positive = right turn in SAE J2735).
A large disagreement means the inertial sensor output and the position/heading
fields are mutually inconsistent — a strong signal of sensor injection or
data fabrication.

**Output fields:** `reported_yaw_deg_s`, `gps_yaw_rate_deg_s`, `yaw_diff_deg_s`,
`threshold_deg_s`, `elapsed_s`, `speed_kmh`

---

## Threshold System (L1 / L2)

The system uses two filtering levels:

| Level | Description | Where configured |
|---|---|---|
| **L1** | Detector fires: the physics check failed | `ode_config.json` |
| **L2** | Display filter: higher-confidence subset shown in Kibana | `display-thresholds.json` → `mbd-display` ES alias |

**L1** produces all flagged events in `logs/misbehaviors.log` and in the
raw `mbd-misbehaviors-*` indices.

**L2** is a server-side Elasticsearch alias filter.  Kibana's default data
view (`mbd-display`) points at this alias, so analysts see only the
higher-significance slice without re-ingesting data.

### Adjusting L2 thresholds

1. Edit `display-thresholds.json` (values are in km/h, m, g, °, °/s as labelled).
2. Push the new filter — no data re-ingestion needed:

```bash
make filter
# or directly:
python manage_display_filter.py
```

3. Refresh Kibana.

To inspect the currently active alias filter:

```bash
python manage_display_filter.py --show
```

To preview the filter JSON without pushing:

```bash
python manage_display_filter.py --dry-run
```

To switch between L1 (all events) and L2 (filtered) in Kibana, change the
**data view** in the Kibana toolbar:

- `mbd-misbehaviors*` — all L1 events
- `mbd-display` — L2 filtered events

---

## Kibana Dashboards

Two dashboards are imported automatically on first `docker compose up`:

| Dashboard | Data view | Contents |
|---|---|---|
| **Misbehavior Report - Main** | `mbd-display` (L2 filtered) | Maps, time series, breakdown tables — higher-confidence subset |
| **Misbehavior Report - Unfiltered** | `mbd-misbehaviors*` (all L1 events) | Same layout, all detected events |

### Filtering within the dashboard

Kibana's built-in **KQL bar** (top of every dashboard) supports ad-hoc
filtering on any field without modifying the data or the L2 alias.
Examples:

```
# Show only position jumps
misbehavior : "position_jump"

# Speed events above 250 km/h
misbehavior : "speed_exceeded" and speed_kmh > 250

# Large heading discrepancies
heading_diff > 170

# Specific vehicle
vehicle_id : "0123456789abcdef"
```

Click the **+** icon in the filter bar for a point-and-click field/value
picker if you prefer not to type KQL.

### Preserving dashboard changes

Dashboard edits made in the Kibana UI are stored in Elasticsearch (the
`.kibana_*` index), not on disk.  The NDJSON files in `elk/kibana/` are only
read at `docker compose up` time.  If you wipe the ES volume
(`docker compose down -v`) your changes are lost.

To save your current Kibana state back to disk before a destructive operation:

```bash
curl -s "http://localhost:5601/api/saved_objects/_export" \
  -H "kbn-xsrf: true" \
  -H "Content-Type: application/json" \
  -d '{"type":"dashboard","includeReferencesDeep":true}' \
  > elk/kibana/dashboard.ndjson
```

This overwrites `elk/kibana/dashboard.ndjson` with your current dashboard
state so it is reimported the next time the stack starts from scratch.

### Pushing edited NDJSON files to Kibana

After editing the NDJSON files on disk (e.g., changing the default time range,
adding a panel, or modifying a visualisation), push the changes to the live
Kibana instance without restarting the stack:

```bash
curl -X POST "http://localhost:5601/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" \
  -F "file=@elk/kibana/dashboard.ndjson"

curl -X POST "http://localhost:5601/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" \
  -F "file=@elk/kibana/display-dashboard.ndjson"
```

Then hard-refresh Kibana (`Ctrl+Shift+R`).

### Discarding dashboard edits and reloading from disk

To throw away all Kibana UI changes and restore the dashboards exactly as
they are in `elk/kibana/`:

```bash
# Reimport all Kibana saved objects from the source files (overwrites live state)
curl -X POST "http://localhost:5601/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" \
  -F "file=@elk/kibana/dashboard.ndjson"

curl -X POST "http://localhost:5601/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" \
  -F "file=@elk/kibana/display-dashboard.ndjson"
```

Then hard-refresh Kibana (`Ctrl+Shift+R`).  No stack restart is needed.

### Docker down vs. curl reimport

| Command | ES data | Kibana saved objects | Reimports NDJSON? |
|---|---|---|---|
| `docker compose down` | kept | kept | No |
| `docker compose down -v` | **wiped** | **wiped** | Yes, on next `up` |
| curl reimport (above) | kept | overwritten | Immediately, no restart |

Use `docker compose down -v` only as a last resort — it also wipes all
misbehavior event indices, requiring a full `make fresh` to reingest data.

---

## Network Access & Security

### Default binding

The `docker-compose.yml` publishes the following ports, each bound to
**all network interfaces** (`0.0.0.0`) by default:

| Port | Service | Notes |
|---|---|---|
| 5601 | Kibana | Full read/write access; no login prompt on Basic licence |
| 9200 | Elasticsearch | REST API; also accessible from the host |
| 8765 | launcher | Leaflet map and in-browser replay |

Kibana is therefore reachable from any host that can reach this machine on
port 5601.  With the Basic license there is no login prompt, so anyone who
can reach the port has full read/write access to the dashboards.

### Restricting to localhost

For a machine with a public IP, restrict the binding to loopback only:

```yaml
# docker-compose.yml
ports:
  - "127.0.0.1:5601:5601"
```

Then restart the stack:

```bash
docker compose down && docker compose up -d
```

### Secure remote access via SSH tunnel

Keep the `127.0.0.1` binding and open a tunnel from your laptop:

```bash
ssh -L 5601:localhost:5601 user@your-host
```

Then browse to `http://localhost:5601` on your laptop.  The tunnel encrypts
the traffic and no port needs to be opened in the firewall.

### Verifying exposure

To check whether Kibana is reachable from another machine:

```bash
curl -s http://<host-ip>:5601/api/status | head -c 100
```

A JSON response means Kibana is publicly accessible; a connection error means
the firewall is blocking the port.

---

## Troubleshooting

### WSL: `PermissionError` on `logs/misbehaviors.log`

On WSL, the `logs/` directory can end up owned by `root` if it was first
created by a Docker container (e.g. Logstash).  `make run` then fails with:

```
PermissionError: [Errno 13] Permission denied: 'logs/misbehaviors.log'
```

Fix: delete the directory and let `detector.py` recreate it as your user:

```bash
rm -rf logs/
make run DATA=<your-file>
```

Alternatively, take ownership without deleting existing logs:

```bash
sudo chown -R $USER logs/
```

---

### Elasticsearch

```bash
# Cluster health
curl http://localhost:9200/_cat/health?v

# List all MBD indices with document counts
curl http://localhost:9200/_cat/indices/mbd-misbehaviors*?v&h=index,docs.count,store.size

# Check the mbd-display alias and its filter
python manage_display_filter.py --show
# or
curl http://localhost:9200/_alias/mbd-display?pretty

# Count documents in the alias
curl http://localhost:9200/mbd-display/_count?pretty

# Delete today's index (e.g., to restart a run)
make clear
# or manually:
curl -X DELETE http://localhost:9200/mbd-misbehaviors-$(date +%Y.%m.%d)
```

**Common issues:**

- `NotFoundError` from `manage_display_filter.py` after deleting all indices:
  the alias needs at least one concrete index.  Run `make filter` — it
  creates a dated placeholder automatically.

- `action.destructive_requires_name` error on wildcard DELETE: ES blocks
  wildcard deletes by safety default.  Use the exact index name.

---

### Logstash

```bash
# Container status
docker compose ps logstash

# Live logs (shows parse errors and indexing activity)
docker compose logs -f logstash

# Force re-ingestion (Logstash re-reads the log from the start on restart)
make ingest
# or:
docker compose restart logstash

# Check Logstash API health
curl http://localhost:9600/?pretty
```

**Common issues:**

- Logstash shows no output after `make run`: check that
  `logs/misbehaviors.log` exists and is non-empty.

- Documents not appearing in Kibana: confirm the index name with
  `curl http://localhost:9200/_cat/indices/mbd*?v` and verify the data view
  pattern matches.

---

### Kibana

```bash
# Container status
docker compose ps kibana

# Live logs
docker compose logs -f kibana

# API health (returns JSON with status)
curl http://localhost:5601/api/status | python3 -m json.tool | grep '"level"'
```

**Common issues:**

- Kibana shows "No results": check the time picker — set it to cover the
  `@timestamp` range of your data (usually the run date).

- Dashboard missing: re-run the setup container:

```bash
docker compose up setup
```

- Unexpected "can't be loaded" errors on panels: reimport the dashboard from the source file — `curl -X POST "http://localhost:5601/api/saved_objects/_import?overwrite=true" -H "kbn-xsrf: true" -F "file=@elk/kibana/dashboard.ndjson"`

---

### Launcher

```bash
# Container status
docker compose ps launcher

# Live logs
docker compose logs -f launcher

# Rebuild the image after changing Dockerfile.launcher
docker compose build launcher && docker compose up -d launcher
```

**Common issues:**

- `Site can't be reached` on `http://localhost:8765`: check that the
  container is running (`docker compose ps launcher`).  If it shows
  `Restarting`, inspect logs for a Python import error.

- Launcher starts but `/map` shows no dots: Elasticsearch may not yet have
  data.  Run `make ingest` and `make filter` first, then refresh.

- Wrong BSM file: the container mounts `./data` read-only.  Place the file
  there and pass `--file` via the `command` override in `docker-compose.yml`,
  or run `replay-launcher.py` directly outside Docker.

---

### Python detector

```bash
# Run with explicit paths
python detector.py data/tampa_BSM_2021.zip --log logs/misbehaviors.log

# Override the default config file
python detector.py data/tampa_BSM_2021.zip --config ode_config.json

# Fresh run — truncate the log first
python detector.py data/tampa_BSM_2021.zip --log logs/misbehaviors.log --clear

# Inspect the last few log entries
tail -5 logs/misbehaviors.log | python3 -m json.tool

# Count events by type in the log
python3 -c "
import json, collections
counts = collections.Counter()
for line in open('logs/misbehaviors.log'):
    try: counts[json.loads(line)['misbehavior']] += 1
    except: pass
for k,v in sorted(counts.items()): print(f'{v:>8,}  {k}')
"

# Preview the L2 filter without pushing it
python manage_display_filter.py --dry-run
```

---

### Docker (general)

```bash
# Start all services
docker compose up -d

# Stop all services
docker compose down

# Rebuild images after config changes
docker compose down && docker compose up -d --build

# Check volume usage
docker system df -v | grep mbd
```

---

## Vehicle Replay Tool

The replay system animates a single vehicle's BSM trajectory around the time
of a flagged misbehavior — position, heading, and speed — for visual
inspection and investigation.

### In-browser replay via `replay-launcher.py` (recommended)

`replay-launcher.py` is a lightweight HTTP server.  BSM data is loaded server-side;
the animation runs entirely in the browser, so the server and browser can be
on different machines.

**Endpoints:**

| Endpoint | Description |
|---|---|
| `GET /map` | Leaflet map — misbehavior events as colour-coded dots |
| `GET /events` | GeoJSON proxy — fetches events from Elasticsearch |
| `GET /replay` | In-browser animated replay page (Leaflet + JavaScript) |
| `GET /replay-data` | JSON trajectory frames consumed by the replay page |

**Start the server:**

`replay-launcher.py` starts automatically as the `mbd-launcher` Docker service when
you run `docker compose up -d`.  It connects to Elasticsearch at
`http://elasticsearch:9200` and mounts `./data` and `./logs` read-only.

```bash
# Automatic — no manual step needed:
docker compose up -d
# → launcher available at http://localhost:8765/map
```

To run with a non-default BSM file or port (outside Docker):

```bash
python replay-launcher.py --file data/custom.zip --port 8765
```

**Workflow:**

1. Open `http://localhost:8765/map` in a browser.
2. Click any dot to open a popup showing vehicle ID, time, and misbehavior type.
3. Click **▶ Replay** — a new tab opens and animates the trajectory.

A spinner is shown in the new tab while trajectory data loads from the server.

> **Kibana tooltip:** each misbehavior event now shows a `replay_url` field in
> the Street Map tooltip.  The URL is displayed as plain text (Kibana tooltips
> do not render hyperlinks); copy-paste it into a browser tab or use `/map`
> above for one-click replay.

**Display:**

| Element | Description |
|---|---|
| Grey polyline | Full trajectory across the time window |
| Red star ★ | Vehicle position at the misbehavior time |
| Blue triangle ▲ | Current heading (0 = North, clockwise per SAE J2735) |
| Blue trail | Last 8 positions |
| Bottom bar | Current timestamp · Δ offset from misbehavior time · speed in km/h |

**Controls:**

| Control | Action |
|---|---|
| ⏮ Replay | Reset to start and play |
| ⏸ Pause | Pause playback |
| ▶ Play | Resume from current position |
| Speed slider | Playback rate (0.05× – 2.0×) |

---

### Command-line replay via `replay.py`

`replay.py` can also be run directly, opening a Matplotlib animation window on
the local machine.  Useful for development or scripted use.

**Prerequisites:**

```bash
pip install matplotlib contextily
```

**Arguments:**

| Argument | Default | Description |
|---|---|---|
| `--vehicle-id` | *(required)* | Vehicle ID (`coreData.id`) |
| `--time-at` | *(required)* | Centre time — paste the Kibana `@timestamp` directly |
| `--start-offset` | `10` | Seconds before `--time-at` to include |
| `--end-offset` | `5` | Seconds after `--time-at` to include |
| `--speed` | `1.0` | Playback speed multiplier (`0.1` = 10× slower) |
| `--file` | *(required)* | BSM ZIP archive or plain NDJSON file |
| `--log` | `logs/misbehaviors.log` | Fallback if `--time-at` matches no BSMs directly |

**Usage:**

```bash
python replay.py \
  --vehicle-id 8273834 \
  --time-at "2021-02-02 18:17:50.380 [ET]" \
  --start-offset 10 \
  --end-offset 5 \
  --speed 0.1 \
  --file data/tampa_BSM_2021.zip
```

The `@timestamp` in Kibana maps to `record_generated_at` (BSM source time in
ET), so it reflects when the vehicle broadcast the message, not when the
detector ran.

If `--time-at` matches no BSM by `recordGeneratedAt`, the tool automatically
falls back to `misbehaviors.log` and prints the resolved time.

---

### ZIP search performance

Both the browser and command-line paths share the same optimised BSM loader.
The Tampa BSM ZIP encodes date and hour in each entry path
(`tampa/BSM/YYYY/MM/DD/HH/…`).  Only entries whose hour falls within the
target time window are opened, reducing scanned entries from ~37 000 to the
handful that cover the target hour:

```
Scanning 8 ZIP entries (hour filter: {18})
```

---

## Development: Unit Tests

Tests live in `tests/` and use `pytest`.  They run **outside Docker** on
your local Python environment.

```bash
make test
# or:
pytest tests/ -v
```

### Test coverage

| Test file | Detector |
|---|---|
| `test_speed.py` | speed_exceeded |
| `test_accel.py` | accel_exceeded |
| `test_brakes.py` | brakes_on_no_decel / decel_no_brakes |
| `test_position_jump.py` | position_jump |
| `test_heading_inconsistency.py` | heading_inconsistency |
| `test_heading_change_rate.py` | implausible_heading_change_rate |
| `test_speed_position.py` | speed_position_inconsistency |
| `test_speed_accel.py` | speed_accel_inconsistency |
| `test_yaw_rate.py` | yaw_rate_inconsistency |

### Writing new tests

`tests/conftest.py` exports `make_bsm()` which builds a synthetic BSM dict
from physical-unit parameters.  All J2735 encoding (multiply by the
appropriate scale factor) is handled internally:

```python
from conftest import make_bsm

bsm = make_bsm(
    vehicle_id="veh-001",
    secmark=100,          # ms within the minute
    lat_deg=41.0,
    lon_deg=-81.0,
    speed_kmh=72.0,
    heading_deg=0.0,
    accel_long_ms2=0.0,
    yaw_deg_s=0.0,
    wheel_brakes="00000",
    accuracy_m=2.0,       # GPS semiMajor in metres; None = field omitted
)
```

---

## Future Work

### Project structure: split into `ode-agent` and `elk-server`

The project currently lives in a single directory.  A cleaner split would
separate concerns into two independently deployable components:

- **`ode-agent`** — the detector pipeline and BSM agent that run alongside
  or within the ODE host.
- **`elk-server`** — the ELK stack configuration, Kibana dashboards,
  `manage_display_filter.py`, and replay tools (`replay-launcher.py`,
  `replay.py`) that run on a separate server.

This separation makes it easier to deploy, version, and secure each component
independently.

---

### Ingestion of zip/log files: demote to test/development tool

Direct ingestion of `.zip` / `.log` files via `detector.py` and Logstash is
useful for development and testing but is not the production data path.  The
production path is a live BSM feed from the ODE.  The file-based ingestion
path should be clearly labelled as a development/test facility and kept out of
production deployment documentation.

---

### Dashboard: Grafana health monitoring for BSM agents and ELK

Add a Grafana dashboard (backed by the existing Elasticsearch data source) that surfaces:

- Per-agent heartbeat status and message throughput
- ELK cluster health (index size, shard state, ingest lag)
- Alerting on agent silence or indexing failures

Grafana is preferred over a Kibana dashboard here because it is purpose-built
for operational monitoring, has richer native alerting (Slack, PagerDuty, etc.),
and is backend-agnostic — it will survive the planned OpenSearch migration
without changes.

---

### Security: authentication and TLS for all exposed endpoints

All endpoints accessible from the internet must be secured before production
deployment:

- Enable TLS and authentication on Kibana, Elasticsearch, and Logstash.
- Restrict Logstash input to authenticated, encrypted connections — it is
  particularly exposed as it accepts inbound data.  Use mutual TLS (mTLS)
  with per-agent client certificates for the Filebeat → Logstash interface
  rather than a shared secret key: mTLS allows individual agents to be
  revoked without affecting others, prevents man-in-the-middle injection of
  fake misbehavior data, and is natively supported by Filebeat.  A
  lightweight CA (e.g. `step-ca`, `cfssl`) is needed to issue and manage
  the certificates.
- Restrict dashboard access (Kibana/OpenSearch Dashboards, Grafana) to
  authorized users via RBAC.  Integrate with an existing identity provider
  (Active Directory, OAuth2/SAML) rather than managing local users.
- Place all dashboards and APIs behind a reverse proxy (nginx, Traefik) that
  enforces TLS and authentication at the edge, so no dashboard or API port
  is directly internet-facing.  For initial production deployment a simpler
  alternative is to bind dashboards to localhost only and access them
  remotely via SSH tunnelling (`ssh -L`).  This requires no reverse proxy or
  certificate management, leverages existing SSH access controls, and
  ensures dashboard ports are never exposed on the network.  The reverse
  proxy approach can be layered on later as the team and operational
  requirements grow.
- Use an OpenSearch stack (Apache 2.0) to access built-in RBAC and security
  features without a paid Elastic license (see migration item below).

---

### Security: network isolation and deployment architecture

The ELK stack should be deployed in its own isolated network segment:

- ELK components must not have any route back into the V2X / ODE network, so
  a compromised ELK host cannot be used as a pivot point to attack the V2X
  infrastructure.
- The deployment architecture must not require enabling any ingress traffic
  to the ODE.  All data flow should be ODE-agent → ELK (push), never
  ELK → ODE (pull).

---

### Platform: migrate to OpenSearch

The current stack uses Elasticsearch and Kibana under the **Elastic Basic**
license, which is free but has notable restrictions:

- **Kibana Controls** (range sliders for interactive threshold filtering)
  require a Gold/Platinum license.
- Certain security features (TLS, role-based access control) also require
  a paid tier.

**OpenSearch** (Apache 2.0 licence, fork of ES 7.10 / Kibana 7.10) includes
all of these features at no cost.  Migration is largely a matter of swapping
Docker images (`elasticsearch` → `opensearchproject/opensearch`,
`kibana` → `opensearchproject/opensearch-dashboards`) and adjusting a handful
of API paths.  Doing so would allow interactive Controls panels for the
L2 threshold sliders directly in the dashboard, removing the need to edit
`display-thresholds.json` and rerun `make filter`.

---

### Detectors: candidates for future implementation

#### Frozen / Replayed BSM

If a vehicle sends the same latitude, longitude, speed, and heading for N
consecutive messages with non-trivial time elapsed, the data is either frozen
(sensor failure) or replayed (attack).  Implementation is a simple stateful
counter per vehicle — no geometry required.

#### Message Counter Anomaly (`msg_cnt`)

The `msg_cnt` field is a 1-byte wrapping counter (0–127).  If it goes
backwards, repeats, or jumps by a large amount without a wrap, it signals
replayed or injected messages.  Can be implemented as a stateful check
alongside the existing heading/position detectors.

#### BSM Frequency Anomaly

SAE J2735 specifies ~10 Hz broadcast rate.  A vehicle sending 200 msg/s is
flooding the channel; 0.1 msg/s is suspiciously slow.  Requires a sliding time
window per vehicle to compute the observed message rate and compare it against
configurable bounds.

#### Stale / Replayed Timestamp

Flag BSMs whose `recordGeneratedAt` timestamp is significantly older than the
median or current processing time (e.g., > 10 seconds behind).  A classic
replay attack indicator that requires no per-vehicle state — just a comparison
against a rolling reference time.

#### Sybil Detection (Co-location)

Multiple distinct vehicle IDs reporting positions within a very small radius
(e.g., < 5 m) at the same time.  One physical device impersonating several
virtual vehicles is a **Sybil attack** — a major V2X security threat.
Implementation requires a spatial index (e.g., geohash bucketing) over all
active vehicles in each time window, making it the most computationally
intensive candidate on this list.

#### Missing BSM as Misbehavior

Treat the absence of expected BSMs as a detectable misbehavior event.  If a
known vehicle (or a roadside coverage zone) goes silent for longer than a
configurable threshold, flag the gap as a potential anomaly.  This catches
locations where no coverage is available — such as tunnels — as well as sensor
failures or deliberate suppression.  Implementation requires tracking
last-seen timestamps per vehicle ID and emitting a synthetic misbehavior record
when the silence duration exceeds the threshold.

---

#### Signature Validation Failure

V2X messages are signed using the **Security Credential Management System
(SCMS)**.  A BSM or other V2X message whose digital signature fails
verification is a strong misbehavior indicator — the message may be forged,
tampered with, or replayed from a revoked certificate.

**Open questions:**
- Does the ODE perform SCMS signature verification, and if so, does it expose
  validation results (pass / fail / certificate status) as metadata on the
  Kafka topic alongside the decoded payload?
- If the ODE does not verify signatures, can verification be added as a
  pre-processing step in `bsm_agent.py` using an available SCMS client library?
- Should signature failures be treated as a first-class misbehavior type in
  the existing schema, or routed to a separate high-priority alert path given
  their severity?
- How should certificate revocation list (CRL) / OCSP lookups be handled in a
  deployment where the agent pod may have limited external network access?

---

#### Phantom / Ghost Vehicle

BSMs transmitted by a fabricated vehicle identity with no corresponding
physical vehicle.  The attacker generates plausible-looking position, speed,
and heading data for a vehicle ID that does not exist on the road.  Detection
approaches include cross-referencing reported positions against infrastructure
sensor data (camera, radar, loop detector) or flagging vehicle IDs that appear
and disappear without plausible entry/exit points.

---

#### Replay Attack

A previously captured valid BSM — complete with a legitimate signature — is
retransmitted at a later time or from a different location.  Distinguished
from the "Stale / Replayed Timestamp" detector above in that the message was
genuinely valid when first broadcast; the misbehavior lies in its reuse.
Detection requires tracking `(vehicle_id, secMark)` pairs and flagging
duplicates, or combining timestamp staleness with position plausibility checks.

---

#### Time Falsification

Manipulation of the `generationTime` or `secMark` field to make a message
appear newer or older than it is.  Can mask replay attacks, defeat
timestamp-based detectors, or create artificial gaps in coverage.  Detection
relies on comparing reported time against receiver wall-clock time and
flagging messages whose timestamps diverge beyond a configurable tolerance.

---

#### Flooding / DoS

High-rate BSM injection from one or more vehicle IDs intended to saturate
receiver processing capacity or the ODE ingest pipeline.  Detection is a
rate-limit check per vehicle ID (already listed under "BSM Frequency Anomaly"
above) extended to aggregate across all vehicle IDs to catch distributed
flooding where each individual sender stays below the per-vehicle threshold.

---

#### Expired Certificate

An OBU that failed to rotate to a fresh pseudonym certificate continues
broadcasting with an expired credential.  This is not necessarily a malicious
act — it may indicate a software defect or SCMS enrollment failure — but it is
a policy violation and a signal worth surfacing.  Detection requires access to
certificate validity periods from the SCMS or from ODE metadata; see
"Signature Validation Failure" above for related open questions on certificate
status availability.

---

#### Encoding Bug (Malformed ASN.1 / UPER)

A BSM that fails ASN.1 / UPER decoding due to an OBU software defect.  Not a
security attack, but a data-quality misbehavior that should be logged and
counted separately from valid messages so that defective OBU firmware versions
can be identified and remediated.  The ODE typically drops or rejects
malformed messages before they reach the Kafka topic; confirming whether
rejected messages are surfaced anywhere (dead-letter topic, ODE error log) is
a prerequisite for implementing this detector.

---

#### Missing V2X Messages (e.g. tunnel)

Beyond BSMs, the ODE receives other V2X message types (SPaT, MAP, TIM, PSM,
etc.).  Gaps in these message streams — for example, a roadside unit in a
tunnel that stops broadcasting SPaT or MAP data — may represent infrastructure
failures or deliberate suppression and could be flagged as misbehavior events
in their own right.

**Open questions:**
- Which non-BSM V2X message types flow through the ODE and are available on
  Kafka topics?
- What constitutes a "normal" broadcast cadence for each type, and how should
  expected vs. observed rates be defined?
- Should non-BSM misbehavior events share the existing `misbehaviors.log`
  schema and Kibana dashboards, or require a separate pipeline?

---

#### Red-Light Violation

A vehicle whose BSM reports a position inside or beyond an intersection stop
line while the corresponding SPaT message for that intersection indicates a
red phase.  Cross-referencing BSM position and speed against real-time SPaT
data from the roadside unit (RSU) can flag vehicles that enter the
intersection on red — either a genuine traffic violation or a falsified
position report intended to make the vehicle appear to be somewhere it is not.

**Open questions:**
- Are SPaT messages available on a Kafka topic alongside BSMs, and do they
  carry the intersection ID needed to correlate with a vehicle's reported
  position?
- What MAP (intersection geometry) data is available to define the stop-line
  boundary and approach lanes for each signalised intersection?
- Should this detector operate per-BSM in `bsm_agent.py` (requiring SPaT
  state to be cached per intersection) or as a post-processing join over a
  short time window?

---

#### Trajectory Anomaly Detection (Obstruction / Veering)

Unsupervised clustering (e.g. DBSCAN, isolation forest) applied to short
trajectory windows — sequences of reported position, heading, and speed —
can detect vehicles deviating from the expected road path.  When many
vehicles deviate in the same geographic area within a short time window,
the pattern is consistent with a real obstruction (debris, collision,
emergency vehicle); when only a single vehicle shows the deviation it is a
candidate for position falsification or sensor error.  No labelled training
data is required: the model learns normal road-following behaviour from the
BSM stream itself and flags statistical outliers.

**Open questions:**
- What trajectory window length (number of BSMs, or elapsed time) gives the
  best signal-to-noise ratio for obstruction vs. single-vehicle anomaly
  classification?
- How should the model handle expected deviations such as lane changes,
  roundabouts, or construction zones that are already mapped?
- Should obstruction events (multi-vehicle deviation cluster) be surfaced as
  a separate event type from single-vehicle trajectory anomalies?

---

#### LSTM Motion Prediction

A long short-term memory (LSTM) sequence model trained on normal BSM streams
learns the expected next state (position, speed, heading) given a vehicle's
recent history.  At inference time, the residual between the model's
prediction and the reported BSM state is computed; messages whose residual
exceeds a learned threshold are flagged as anomalous.  This approach catches
physically implausible jumps and gradual drift that evade fixed-threshold
detectors, and naturally adapts to different road types and speed regimes
because the model conditions on the vehicle's own recent context.

**Open questions:**
- Should a single global model be trained across all vehicles, or per-vehicle
  or per-road-segment models to capture local driving patterns?
- What is the minimum BSM history length needed before predictions are
  reliable enough to flag anomalies without excessive false positives?
- How should the model handle legitimate high-residual events such as hard
  braking, evasive manoeuvres, or GPS signal recovery after a tunnel?

---

#### Collective / Swarm Anomaly Detection

Analyses the joint behaviour of all active vehicle IDs within a time-space
window rather than each vehicle in isolation.  A coordinated pattern — all
vehicles accelerating identically, a fleet of IDs appearing and disappearing
in lockstep, or suspiciously uniform inter-vehicle spacing — is statistically
improbable among independent real drivers and is a strong indicator of a
multi-vehicle Sybil attack, coordinated replay, or a phantom vehicle farm.
Candidate techniques include multivariate time-series anomaly detection and
graph-based methods that model pairwise similarity across vehicle trajectories.

**Open questions:**
- What spatial and temporal window sizes are appropriate for grouping vehicles
  into a "swarm" without merging unrelated traffic streams?
- How should the detector distinguish a legitimate convoy (e.g. platooning
  trucks) from a suspicious coordinated cluster?
- Does the volume of simultaneous active vehicles in the deployment area
  provide enough data for reliable statistical baselines?

---

#### Behavioural Fingerprinting / Profile Drift

Builds a long-term motion profile for each vehicle ID capturing its
characteristic speed distribution, braking patterns, acceleration envelope,
and turning behaviour.  A lightweight model (e.g. one-class SVM, autoencoder,
or simple statistical summary) is updated incrementally as new BSMs arrive.
When a vehicle ID's recent behaviour diverges sharply from its own historical
baseline — for example, a previously cautious driver suddenly exhibiting
aggressive acceleration — it is flagged as a candidate for pseudonym hijacking,
credential reuse across physically different vehicles, or Sybil identity
rotation.

**Open questions:**
- V2X pseudonym certificates rotate frequently by design (privacy); how
  should the detector link behaviour across pseudonym changes without
  re-identifying drivers?
- What minimum observation period is needed to establish a stable baseline
  before drift detection is meaningful?
- Should profile drift be treated as a standalone misbehavior or used as a
  corroborating signal to elevate the confidence of other detectors?

---

#### Map-Constrained Trajectory Scoring

A map-matching model scores each reported BSM position against the
probability of reaching it from the vehicle's prior position via the
road network — accounting for road topology, legal travel directions,
and realistic travel time given reported speed.  Positions that score
below a learned threshold are flagged as off-road or physically impossible
route transitions that rule-based range checks miss (e.g. a vehicle
teleporting across a city block while reporting a plausible speed).
Candidate approaches include hidden Markov model (HMM) map matching and
graph neural networks over the road network.

**Open questions:**
- Which road network data source is available in the deployment environment
  (OpenStreetMap, HERE, a proprietary GIS layer) and how frequently is it
  updated to reflect construction and closures?
- How should the model handle GPS multipath error in urban canyons, which
  can produce legitimate off-road positions without any misbehavior?
- Can the scoring run in real time per BSM, or does it require a short
  look-ahead buffer of consecutive positions to produce a reliable score?

---

#### Wrong-Way Driving

Cross-references the vehicle's reported heading with the legal travel
direction of the road segment it occupies, as defined by MAP messages or a
road network layer.  A vehicle whose heading is opposite to the permitted
direction of travel on a one-way segment, or is clearly misaligned with any
adjacent lane in a divided road, is flagged.  This catches both falsified
heading data and genuine wrong-way driving events that may warrant a safety
alert to other road users via V2X infrastructure.

**Open questions:**
- Are MAP messages available in the deployment with sufficient lane-level
  geometry and directionality attributes?
- How should the detector handle legally reversible lanes, contraflow
  construction zones, and emergency vehicle exemptions?
- What heading tolerance (degrees of misalignment) should trigger a flag,
  given GPS heading noise at low speeds?

---

#### Speed Limit Violation

Compares a vehicle's reported speed against the posted speed limit for the
road segment it occupies, sourced from MAP messages or a GIS speed-limit
layer.  Sustained, significant exceedance (configurable margin above the
limit) is flagged as a potential misbehavior — either the speed data is
falsified, or the vehicle represents a genuine safety risk that may be worth
surfacing to traffic operators.  Unlike the BSM Frequency Anomaly detector,
this check is spatial: the same speed may be normal on a motorway and
anomalous on a residential street.

**Open questions:**
- What data source provides reliable, up-to-date speed limits for the
  deployment area, and how are temporary limits (school zones, work zones)
  handled?
- Should the detector distinguish between a brief transient (overtaking)
  and sustained exceedance, and if so, over what time window?
- How should the flagged event be classified — misbehavior, safety event,
  or a separate category?

---

#### Geofence Violation

Flags vehicles reporting positions that are physically impossible or
operationally out-of-bounds: inside a building footprint, in a body of
water, underground (below terrain surface without a known tunnel), or
outside the defined coverage area of the deployment.  A simple point-in-polygon
check against a set of exclusion zones and the operational boundary catches
coarse position falsification that more sophisticated detectors may overlook
if the fabricated coordinates are otherwise internally consistent.

**Open questions:**
- What geospatial layers are available for building footprints, water bodies,
  and terrain in the deployment area, and how are they kept current?
- Should known tunnels, parking garages, and ferry routes be explicitly
  whitelisted to avoid false positives for vehicles that legitimately pass
  through or over water?
- What should the operational boundary be — the city boundary, the RSU
  coverage footprint, or a configurable polygon?

---

#### Implausible Vehicle Dimensions

BSMs carry self-reported vehicle length and width fields.  This detector
flags two classes of anomaly: (1) dimensions that change between consecutive
BSMs from the same vehicle, which should never occur for a physical vehicle;
and (2) dimensions that are inconsistent with the vehicle's stated class
(e.g., a vehicle broadcasting a motorcycle class code but reporting
truck-scale dimensions, or values outside the physically plausible range
for any road vehicle).

**Open questions:**
- Are the length and width fields reliably populated in the BSMs from the
  ODE deployment, or are they frequently absent or set to default/zero?
- What tolerance should be applied to dimension comparisons given that
  different OBU firmware versions may quantise the fields differently?
- Should mismatched class-vs-dimension combinations be treated as a
  misbehavior or as a data-quality issue attributed to OBU misconfiguration?

---

#### Contradictory Event Flags

BSMs contain a set of event flags (hard braking, stability control active,
hazard lights on, airbag deployed, etc.) that should be consistent with the
reported kinematic state.  This detector checks for logical contradictions:
an airbag-deployed flag set while speed and position are completely normal
across subsequent messages; hard-braking flagged while speed is increasing;
or hazard lights reported on a vehicle whose trajectory shows no slowdown
or stop.  These contradictions may indicate flag injection, firmware bugs,
or deliberate falsification of safety-critical event data.

Note: the wheel-brake flag vs. longitudinal acceleration case is already
implemented as `brakes_on_no_decel` / `decel_no_brakes` (detector #3).
This entry covers the remaining event flags not yet handled.

**Open questions:**
- Which event flag / kinematic combinations can be checked reliably without
  access to vehicle-internal state (e.g., airbag deployment does not
  necessarily stop a vehicle immediately)?
- Should contradictory flags generate a misbehavior alert or a lower-severity
  data-quality warning, given the possibility of OBU firmware defects?
- Are event flags consistently populated across the OBU hardware types in
  the deployment, or are some flags always zero?

---

#### Elevation Falsification

If BSMs include an altitude field, the reported elevation is cross-checked
against a digital elevation model (DEM) for the reported lat/lon.  A vehicle
claiming an altitude that is significantly above or below the known terrain
surface — and not explained by a mapped structure such as a bridge, overpass,
or tunnel — is flagged as a likely position spoof.  Elevation is a dimension
of position that attackers frequently overlook when fabricating plausible
lat/lon coordinates, making it a useful low-cost consistency check.

**Open questions:**
- Is the altitude field reliably populated in the BSMs from the ODE
  deployment, and what vertical datum and precision does it use?
- What DEM resolution and source (SRTM, lidar-derived, national mapping
  agency) is available for the deployment area, and how are bridges and
  elevated structures represented?
- What vertical tolerance should be applied to account for GPS altitude
  error, which is typically larger than horizontal error?

---

#### Infrastructure Impersonation

Detects messages that purport to originate from a roadside unit (RSU) or
other fixed infrastructure node but are inconsistent with the known RSU
registry — for example, a SPaT or MAP message sourced from a position that
does not match any registered RSU location, or from a source whose position
changes over time (indicating a mobile transmitter).  Infrastructure
impersonation can be used to inject false signal phase data, redirect
vehicles, or suppress legitimate RSU broadcasts.

**Open questions:**
- Is a registry of known RSU positions and IDs available and maintainable
  for the deployment area?
- Does the ODE expose the source RSU ID and position metadata alongside
  decoded SPaT/MAP messages on the Kafka topic?
- Should moving sources be flagged immediately, or only after a position
  drift threshold is exceeded to tolerate minor GPS error in stationary RSUs?

---

#### Sensor Fusion Cross-Validation

Where roadside cameras, radar, lidar, or inductive loop detectors are
available, their observations can be used to independently validate
BSM-reported position and speed.  A vehicle whose BSM claims a position
or speed that is inconsistent with what infrastructure sensors observe at
the same time and location is a strong candidate for data falsification.
This is the highest-confidence form of misbehavior detection available
because it grounds V2X data in independent physical measurements, but it
is also the most infrastructure-intensive to deploy.

**Open questions:**
- Which roadside sensor types are present in the deployment area, and do
  they produce real-time data streams accessible to the MBD pipeline?
- How are sensor observations correlated with vehicle IDs across modalities
  (camera track ID, loop detector count) when sensors do not natively
  identify individual vehicles?
- What latency and positional uncertainty does each sensor type introduce,
  and how should the fusion logic handle asynchronous or missing sensor
  readings?

---

### Reporting: distinguishing systemic errors from attacks

A firmware bug producing slightly wrong headings is individually
indistinguishable from a spoofing attack at any single receiver.  The
difference only becomes visible in aggregate:

- A **bug** produces the same anomaly pattern across many vehicles of the
  same make, model, or firmware version, spread geographically, with no
  correlation to time of day or traffic conditions.
- An **attack** tends to be localised in space and time, targeting specific
  corridors or events.

Without a centralised reporting layer accumulating observations across
receivers and vehicles, this population-level pattern is never visible.
Every receiver silently filters the bad messages, the bug goes undetected
and unpatched indefinitely, and safety-critical applications continue to
receive corrupted data from the entire affected fleet.

This distinction also determines the correct response.  A bug affecting
tens of thousands of vehicles should trigger a manufacturer notification
and an OTA firmware update — not tens of thousands of certificate
revocations.  Getting that response right requires the MBD authority to
see the population-level picture, which only exists if receivers report
anomalies rather than just filter them locally.

Systemic errors are therefore the clearest argument for the reporting
layer: local filtering is a receiver-side defence that actively *hides*
fleet-wide problems from the people who need to act on them.

**Open questions:**
- What attributes should be captured in a misbehavior report to enable
  population-level analysis — firmware version, OBU model, geographic
  cluster, anomaly type distribution?
- What statistical tests (e.g. clustering by make/model, spatial vs.
  temporal correlation) should the MBD authority apply to distinguish a
  systemic error from a coordinated attack?
- How should the pipeline handle the transition from "possible bug" to
  "confirmed attack" if the same anomaly pattern later becomes spatially
  concentrated, suggesting the initial spread was cover?

---

### Architecture: distributed ODE deployment

The current implementation processes a single ZIP/NDJSON file on one machine.
A production deployment would integrate with the
**USDOT Operational Data Environment (ODE)**, which runs as a distributed
Kubernetes cluster.  In that model:

- **Agents** run inside the ODE cluster, one per data source or geographic
  region.  Each agent continuously consumes BSM streams from the ODE and runs
  the misbehavior detectors in near-real time.
- **Each agent writes misbehavior events to a local JSON-lines log file.**
  A Filebeat sidecar tails the file and ships events to the central Logstash
  instance — agents have no direct connection to Elasticsearch.
- **Logstash** (or OpenSearch's Data Prepper) acts as the ingest layer,
  providing buffering, back-pressure handling, and field normalisation before
  events reach Elasticsearch.

**Already implemented toward ODE deployment:**

- `_process_bsm(bsm: dict, …)` in `detector.py` — single-BSM processing hook
  shared by both `detector.py` (batch) and `bsm_agent.py` (streaming).
- `bsm_agent.py` — Kafka consumer that subscribes to `topic.OdeBsmJson`,
  normalises the ODE `wheelBrakes` format, and calls `_process_bsm()` per message.
- `ode_config.json` — carries Logstash endpoint URL and Kafka broker/topic so
  each deployed agent is configured without code changes.
- `docker-compose-ode.yml` — ODE overlay with Filebeat (unconditional) and
  `bsm_agent` (Docker Compose profile `ode`); `./logs` bind-mounted between both.
- `Dockerfile.agent` — `python:3.12-slim` image for `bsm_agent.py`.
- Logstash pipeline — accepts both direct file-read (local mode) and Beats
  input (ODE mode) on port 5044; ES deduplicates via `document_id`.

Key changes still required relative to the current design:

| Concern | Current | Production |
|---|---|---|
| Input | ZIP/NDJSON file or Kafka (`bsm_agent`) | ODE Kafka stream ✓ |
| Execution | Single process, one machine | Kubernetes `Deployment` (one pod per RSU) |
| Output | JSON-lines log → Filebeat → Logstash | ✓ already this path |
| State (stateful detectors) | In-process Python dict | In-process per RSU ✓ (cross-RSU loss accepted) |
| Index naming | `mbd-misbehaviors` | Data stream with ILM rollover policy |

#### Recommended ingest path: agent → Filebeat → Logstash → ES

The agent (detector process) writes misbehavior events to a local JSON-lines
log file, exactly as it does today.  **Filebeat** runs as a sidecar container
in the same Kubernetes pod, tails the log file, and ships events to the
central Logstash instance.  This approach:

- **Keeps the agent simple** — no ES client code, no network retry logic.
- **Decouples transport from detection** — Filebeat handles back-pressure,
  retries, and TLS without any changes to detector code.
- **Matches the current architecture** — the existing Logstash pipeline and
  Elasticsearch index template require no changes.
- **Scales naturally** — each agent pod has its own Filebeat sidecar; all
  sidecars fan-in to the same Logstash endpoint.

```
ODE Kafka (topic.OdeBsmJson)
      │
      ▼
 Agent pod (Kubernetes)
 ┌──────────────────────────────────┐
 │  bsm_agent.py → misbehaviors.log │
 │  (JSON-lines, one event per line) │
 │               │                  │
 │   Filebeat sidecar ──────────────┼──► Logstash ──► Elasticsearch ──► Kibana
 └──────────────────────────────────┘
```

#### Eliminating shared state with pinned routing

The stateful detectors (position jump, heading, yaw, speed/accel consistency)
keep per-vehicle state in a Python dictionary.  In a naive multi-replica
deployment, BSMs from the same vehicle could arrive at different replicas,
corrupting state.

This can be avoided without Redis by **pinning each Filebeat instance to a
dedicated Logstash replica** and configuring the ODE to route BSMs from a
given geographic region or vehicle ID range to the same agent pod.  Because
each Logstash replica then sees a consistent subset of vehicles, per-vehicle
state stays in-process with no shared store required.

```
ODE region A ──► Agent pod A ──► Filebeat A ──► Logstash replica A ──┐
ODE region B ──► Agent pod B ──► Filebeat B ──► Logstash replica B ──┼──► ES ──► Kibana
ODE region C ──► Agent pod C ──► Filebeat C ──► Logstash replica C ──┘
```

Filebeat's `output.logstash` supports a static `hosts` list, so pinning is
simply a matter of pointing each Filebeat sidecar at a specific Logstash
`ClusterIP` or pod DNS name in the Kubernetes manifest.  The Logstash replicas
do not need to communicate with each other, and ES handles fan-in from all
replicas without coordination.

#### Index Lifecycle Management (ILM)

In a continuous ODE deployment, misbehavior events accumulate indefinitely.
**ILM** is an Elasticsearch feature that automatically manages index size and
age by moving data through a series of phases:

| Phase | Description |
|---|---|
| **Hot** | Active writes and fast reads |
| **Warm** | Read-only; compressed to slower, cheaper storage |
| **Cold** | Rarely accessed; further compressed |
| **Delete** | Automatically removed after a configured retention period |

Rollover rules trigger a transition to a new index when the current one
exceeds a size limit (e.g., 50 GB), a document count, or a time threshold
(e.g., 30 days).  This keeps individual indices at a manageable size and
query performance consistent over time.

The current MBD setup uses date-suffixed indices (`mbd-misbehaviors-YYYY.MM.DD`)
as a simple manual approximation of the same idea; ILM would automate and
generalise this for a production deployment.

---

### ODE integration: remaining work

The items below are the outstanding tasks before `bsm_agent.py` is ready for
a production ODE deployment.

**Kafka TLS / SASL authentication** — the current `ode_config.json` has no
auth config.  The ODE supports Confluent Cloud (SASL/SCRAM) and on-prem TLS.
A `kafka.security` sub-section (`protocol`, `sasl_mechanism`, `username`,
`password`) should be added to `ode_config.json` and wired into
`_build_consumer()` in `bsm_agent.py`.  `confluent-kafka` supports both modes
natively.

**Kubernetes manifests** — `docker-compose-ode.yml` is a local testing
convenience, not a production deployment artifact.  A `k8s/` directory with a
`Deployment` (bsm_agent + Filebeat sidecar containers), `ConfigMap`
(ode_config.json), and `emptyDir` shared volume between the two containers is
the actual deliverable for ODE cluster integration.

**Health / liveness probe** — Kubernetes needs a liveness probe to detect a
stuck or crashed `bsm_agent`.  The simplest approach: `bsm_agent.py` writes a
heartbeat timestamp to a file after each successful `poll()` cycle; the K8s
`livenessProbe.exec` command checks that the file is younger than a threshold
(e.g., 30 s).  No HTTP server required.

**Tests for `bsm_agent.py`** — `_normalise_bsm()` and `_adapt_wheel_brakes()`
have no test coverage.  A `tests/test_bsm_agent.py` covering the wheelBrakes
adapter (dict → binary string, string passthrough, missing/malformed values)
and the normalise path would close this gap.

**RSU ID from ODE metadata** — ODE Kafka BSMs do not carry `metadata.RSUID`.
`extract_context()` will silently produce a blank `rsu_id` in every event.
The RSU ID should be injected from `ode_config.json` (one value per deployed
agent) or derived from `metadata.receivedMessageDetails` and added to the
event by `bsm_agent.py` before calling `_process_bsm()`.

**Structured logging** — `bsm_agent.py` uses `print()` statements.  Replacing
them with the Python `logging` module in JSON format would integrate cleanly
with whatever log aggregation the ODE cluster runs (e.g., Fluentd, Loki).

**Configurable Kafka consumer settings** — `auto.offset.reset` and
`enable.auto.commit` are hardcoded in `_build_consumer()`.  Exposing them in
`ode_config.json` under `kafka` (e.g., `auto_offset_reset`, `auto_commit`)
avoids a code change when operations teams need different behaviour.

**Dead letter queue** — messages that fail JSON parsing or raise an unexpected
exception are currently print-and-skip.  Forwarding them to an error Kafka
topic (e.g., `topic.MbdErrors`) would provide visibility without blocking the
main consumer.

**Prometheus metrics** — counters for BSMs processed / flagged / errors and a
per-detector-type breakdown would make `bsm_agent.py` observable from the ODE
cluster's monitoring stack (Prometheus + Grafana).

**Log rotation** — in continuous streaming mode `misbehaviors.log` grows
without bound.  See [Pipeline: log rotation and duplicate ingestion](#pipeline-log-rotation-and-duplicate-ingestion)
below.

---

### Detectors: coverage gaps in existing logic

The current detectors flag individual BSMs in isolation or against a single
previous message.  Several attack patterns are not yet covered:

- **Replay / frozen BSM** — a vehicle sending identical position, speed, and
  heading across many consecutive messages with non-trivial time elapsed.
  Already listed as a candidate detector above; also manifests as a gap in
  the stateful detectors which only compare adjacent pairs.
- **Cross-vehicle Sybil detection** — one physical device impersonating
  multiple vehicle IDs at nearby positions (< 5 m apart, same time window).
  Requires a spatial index across all active vehicles, not just per-vehicle
  state.  Already listed above; noted here because it is the most significant
  undetected attack class.
- **Gradual drift attacks** — an attacker who increments position or speed
  slightly each message can stay under the per-step threshold indefinitely.
  Accumulating error over a rolling window (e.g., 10-message sliding sum)
  would catch this class.

---

### Detectors: per-vehicle behavioural baselines

All current thresholds are **global** — the same limit applies to every
vehicle regardless of type (car, truck, motorcycle) or context (highway,
city).  A more robust approach would learn a normal behaviour profile per
vehicle ID from the first N messages and flag deviations from that baseline
rather than from a fixed constant.  This would significantly reduce false
positives for edge-case vehicles while improving sensitivity for subtle
spoofing.

---

### Pipeline: log rotation and duplicate ingestion

**Log rotation** — `misbehaviors.log` grows indefinitely.  In batch mode this
is manageable; in ODE streaming mode it will eventually fill the disk.  On
`make ingest`, Logstash restarts from the beginning of the file, which becomes
increasingly slow as the log grows.  A rotation policy (e.g., daily rotation,
keep 7 files, via Python's `RotatingFileHandler` or a system logrotate config)
would cap both disk usage and ingest time.

**Duplicate ingestion** — running `make ingest` twice re-ingests the entire
log, creating duplicate records in Elasticsearch.  Assigning a deterministic
`_id` to each ES document (e.g., a hash of `vehicle_id + secmark +
misbehavior + detected_at`) would make writes idempotent and eliminate
duplicates regardless of how many times Logstash restarts.

---

### Pipeline: BSM event context window

The surrounding raw BSMs at the time of a misbehavior detection are essential
for post-hoc investigation with `replay.py`.  The two deployment modes have
different constraints:

**ZIP / batch mode** — the source ZIP archive is available today and `replay.py`
can scan it using `--time-at` and `--vehicle-id`.  However, ZIP archives may
not be retained indefinitely once the ODE is in production, and scanning a
large archive is slow.  Capturing context at detection time makes replay
instant and future-proof regardless of whether the original file is still
available.

**ODE / streaming mode** — BSMs are consumed from the Kafka stream and not
persisted anywhere outside the detector process.  Once a BSM has been
processed it is gone.  A context window captured at detection time is the
**only** mechanism available for post-hoc replay; there is no archive to fall
back on.

**Proposed design (common to both modes):**

- Each active vehicle slot maintains a **per-vehicle ring buffer** of the last
  N seconds of raw BSMs (e.g., 3 s ≈ 30 messages at 10 Hz), held in the same
  in-process state as the stateful detectors.  When an event fires, the buffer
  holds the pre-event context.
- `_process_bsm()` continues accepting BSMs from that vehicle for a
  configurable post-event window (e.g., 2 s) before writing.  The misbehavior
  log entry is **delayed** until the post-window closes.
- Pre- and post-event BSMs are written to a **separate context file**
  `logs/context/<event_id>.json`, keeping `misbehaviors.log` and the ELK
  pipeline unchanged.
- The misbehavior log entry gains a `context_file` field pointing to the
  context file so the two can be correlated.
- `replay.py` is extended to accept `--event-id`, read the context file
  directly, and animate the window without requiring the source ZIP archive.

Window sizes (pre and post, in seconds) and ring buffer capacity would be
configurable in `ode_config.json` under a new `context` section.

---

### Pipeline: streaming ingestion

The batch pipeline (detect from a ZIP file, write a log, restart Logstash) is
now complemented by `bsm_agent.py`, which consumes `topic.OdeBsmJson` from the
ODE Kafka broker and writes events in real time.  The Filebeat → Logstash path
eliminates the `make ingest` step.

A further option — not yet implemented — would bypass the log file entirely:

- Writing directly to ES from the detector using the Python ES client
  (simplest, but couples detector code to ES).
- Publishing to a Kafka topic and using Logstash's Kafka input plugin
  (decoupled, supports back-pressure and replay).

---

### Operational tooling

**Alerting** — the dashboards are currently observational only.  Kibana
Alerting (Basic licence, available without Gold) can trigger notifications
when event counts exceed a threshold — for example, paging an analyst when
more than 50 `speed_position_inconsistency` events appear in a 5-minute
window.  A simpler alternative is a lightweight Python script that polls ES
on a cron schedule and sends an email or Slack message.

**Dashboard drill-down** — the "All Misbehavior Events" table shows the
maximum observed field value per vehicle × misbehavior type, but does not
link to the individual worst event.  Adding a Kibana URL drilldown from each
row to a pre-filtered Discover view (filtered to that vehicle ID and
misbehavior type, sorted by the relevant field descending) would let analysts
jump directly to the specific timestamp and coordinates of the worst event
without manual KQL queries.

**Dashboard export automation** — saving UI edits back to the NDJSON source
files currently requires a manual `curl` command documented in the README.
A `make export` Makefile target wrapping that command would make the
round-trip less error-prone and easier to remember.

**Stack health check** — there is no quick way to verify the stack is ready
before running detection or ingest.  A `make status` target that checks ES
cluster health, confirms the `mbd-display` alias exists, and verifies Kibana
is reachable would surface misconfiguration before a long detector run.

---

## Project Structure

```
MBD/
├── detector.py                  Batch entry point — reads BSM files/ZIPs; _process_bsm() is the shared ODE hook
├── bsm_agent.py                 ODE entry point — Kafka consumer; calls _process_bsm() per BSM
├── replay-launcher.py                  HTTP server — Leaflet map (/map) + in-browser replay (/replay, /replay-data)
├── replay.py                    Standalone animation tool; data-loading functions reused by replay-launcher.py
├── manage_display_filter.py     Pushes L2 thresholds to ES; creates mbd-display data view
├── tools/
│   └── report.py                Daily summary report — queries ES for counts, top vehicles, top RSUs
├── ode_config.json              ODE configuration: Logstash endpoint, Kafka broker/topic, L1 thresholds
├── display-thresholds.json              L2 display thresholds (editable)
├── Makefile                     Single entry point for all common operations
├── requirements.txt             Python dependencies (local / batch mode)
├── requirements-ode.txt         Additional ODE dependencies (confluent-kafka)
├── Dockerfile.agent             Container image for bsm_agent.py (python:3.12-slim)
├── Dockerfile.launcher          Container image for replay-launcher.py (python:3.11-slim)
├── docker-compose.yml           Local ELK stack (Elasticsearch, Logstash, Kibana, setup, launcher)
├── docker-compose-ode.yml       ODE overlay — Filebeat + bsm_agent (profile: ode)
│
├── detectors/
│   ├── utils.py                 J2735 constants, geometry helpers, BaseDetector
│   ├── speed.py                 speed_exceeded
│   ├── accel.py                 accel_exceeded
│   ├── brakes_inconsistency.py  brakes_on_no_decel / decel_no_brakes
│   ├── position_jump.py         position_jump
│   ├── heading_inconsistency.py heading_inconsistency
│   ├── heading_change_rate.py   implausible_heading_change_rate
│   ├── speed_position_consistency.py  speed_position_inconsistency
│   ├── speed_accel_consistency.py     speed_accel_inconsistency
│   └── yaw_rate_consistency.py        yaw_rate_inconsistency
│
├── tests/
│   ├── conftest.py              make_bsm() helper; shared fixtures
│   ├── test_speed.py
│   ├── test_accel.py
│   ├── test_brakes.py
│   ├── test_position_jump.py
│   ├── test_heading_inconsistency.py
│   ├── test_heading_change_rate.py
│   ├── test_speed_position.py
│   ├── test_speed_accel.py
│   └── test_yaw_rate.py
│
├── elk/
│   ├── elasticsearch/
│   │   ├── index-template.json  Field mappings + replay_url runtime field for mbd-misbehaviors-* indices
│   │   └── display-alias.json   Initial alias definition (superseded by manage_display_filter.py)
│   ├── filebeat/
│   │   └── filebeat.yml              Tails misbehaviors.log; ships to ${LOGSTASH_URL:-logstash:5044}
│   ├── logstash/
│   │   ├── config/logstash.yml
│   │   └── pipeline/misbehaviors.conf  Logstash pipeline: file + Beats inputs → ES
│   ├── kibana/
│   │   ├── dashboard.ndjson      Misbehavior Report - Unfiltered dashboard
│   │   ├── display-dashboard.ndjson
│   │   ├── display-filter.ndjson
│   │   └── kpi-vega.ndjson       Vega KPI panel
│   └── setup.sh                  One-shot setup: templates, alias, Kibana imports
│
├── data/                         BSM input files (not committed; see Prerequisites for data access)
├── logs/
│   └── misbehaviors.log          Detector output; volume-mounted into Logstash
└── docs/
    └── V2X Communications Message Set Dictionary.pdf   SAE J2735 reference
```
