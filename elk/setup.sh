#!/bin/sh
# Applies the ES index template, creates the display-filter alias, and
# imports the Kibana dashboard.
# Critical: index template and Kibana dashboard import must succeed.

ES="http://elasticsearch:9200"
KB="http://kibana:5601"

# curl_json <method> <url> [extra curl args...]
# Writes body to /tmp/resp.txt, returns HTTP status code only.
curl_json() {
  method="$1"; url="$2"; shift 2
  curl -s -o /tmp/resp.txt -w "%{http_code}" -X "$method" "$url" "$@"
}

# ── Wait for Elasticsearch ────────────────────────────────────────────────────
echo "[setup] Waiting for Elasticsearch..."
until curl -s "$ES/_cat/health?h=status" 2>/dev/null | grep -qE "green|yellow"; do
  sleep 3
done
echo "[setup] Elasticsearch is up."

# ── Apply index template ──────────────────────────────────────────────────────
echo "[setup] Applying index template..."
code=$(curl_json PUT "$ES/_index_template/mbd-misbehaviors" \
  -H "Content-Type: application/json" \
  --data-binary @/setup/index-template.json)
echo "[setup] Template response (HTTP $code): $(cat /tmp/resp.txt)"

if [ "$code" != "200" ]; then
  echo "[setup] ERROR: index template failed — aborting."
  exit 1
fi
echo "[setup] Index template applied."

# ── Patch replay_url runtime field into any existing indices ──────────────────
# The template above covers future indices; this updates indices already on disk
# in the persisted esdata volume (created before this runtime field existed).
echo "[setup] Patching replay_url runtime field into existing indices..."
cat > /tmp/runtime-field.json << 'RUNTIME_JSON'
{"runtime":{"replay_url":{"type":"keyword","script":{"source":"if (!doc['vehicle_id'].empty && !doc['record_generated_at'].empty) { emit('http://localhost:8765/replay?vehicle_id=' + doc['vehicle_id'].value + '&time_at=' + doc['record_generated_at'].value); }"}}}}
RUNTIME_JSON
code=$(curl_json PUT "$ES/mbd-misbehaviors-*/_mapping" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/runtime-field.json)
echo "[setup] Runtime field patch (HTTP $code): $(cat /tmp/resp.txt)"

# ── Create Level-2 display-filter alias ──────────────────────────────────────
# The alias  mbd-display  wraps mbd-misbehaviors* with the default L2 filter
# defined in elk/elasticsearch/display-alias.json (generated from display-thresholds.json).
# To change thresholds later: edit display-thresholds.json and run manage_display_filter.py.
echo "[setup] Creating display-filter alias  mbd-display..."
code=$(curl_json POST "$ES/_aliases" \
  -H "Content-Type: application/json" \
  --data-binary @/setup/display-alias.json)
echo "[setup] Alias response (HTTP $code): $(cat /tmp/resp.txt)"

if [ "$code" = "200" ]; then
  echo "[setup] Alias 'mbd-display' created."
else
  echo "[setup] WARNING: alias creation returned HTTP $code — continuing anyway."
  echo "[setup]   Run manually:  python manage_display_filter.py --es-url $ES"
fi

# ── Wait for Kibana saved objects service ────────────────────────────────────
echo "[setup] Waiting for Kibana saved objects service..."
until curl -s -o /dev/null -w "%{http_code}" \
    -H "kbn-xsrf: true" \
    "$KB/api/saved_objects/_find?type=index-pattern&per_page=1" \
    2>/dev/null | grep -q "200"; do
  sleep 5
done
echo "[setup] Kibana is up."

# ── Import display-filter data view (mbd-display) ────────────────────────────
echo "[setup] Importing display-filter data view..."
code=$(curl_json POST "$KB/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" \
  -F file=@/setup/display-filter.ndjson)
echo "[setup] Display-filter response (HTTP $code): $(cat /tmp/resp.txt)"

# ── Import KPI Vega visualizations first (must exist before dashboard import) ─
echo "[setup] Importing KPI Vega visualizations..."
code=$(curl_json POST "$KB/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" \
  -F file=@/setup/kpi-vega.ndjson)
echo "[setup] KPI Vega response (HTTP $code): $(cat /tmp/resp.txt)"

# ── Import dashboard (fatal) ──────────────────────────────────────────────────
echo "[setup] Importing Kibana dashboard..."
code=$(curl_json POST "$KB/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" \
  -F file=@/setup/dashboard.ndjson)
echo "[setup] Import response (HTTP $code): $(cat /tmp/resp.txt)"

if [ "$code" = "200" ]; then
  echo "[setup] Dashboard imported."
else
  echo "[setup] ERROR: dashboard import returned HTTP $code — aborting."
  echo "[setup] Import manually: Kibana → Stack Management → Saved Objects → Import"
  echo "[setup]   file: elk/kibana/dashboard.ndjson"
  exit 1
fi

# ── Import filtered dashboard (fatal) ────────────────────────────────────────
echo "[setup] Importing filtered Kibana dashboard..."
code=$(curl_json POST "$KB/api/saved_objects/_import?overwrite=true" \
  -H "kbn-xsrf: true" \
  -F file=@/setup/display-dashboard.ndjson)
echo "[setup] Import response (HTTP $code): $(cat /tmp/resp.txt)"

if [ "$code" = "200" ]; then
  echo "[setup] Filtered dashboard imported."
else
  echo "[setup] ERROR: filtered dashboard import returned HTTP $code — aborting."
  echo "[setup] Import manually: Kibana → Stack Management → Saved Objects → Import"
  echo "[setup]   file: elk/kibana/display-dashboard.ndjson"
  exit 1
fi

echo ""
echo "[setup] Done — open http://localhost:5601"
echo ""
echo "[setup] Two data views are available in Kibana:"
echo "[setup]   mbd-misbehaviors*  — all Level-1 records (raw dataset)"
echo "[setup]   mbd-display        — Level-2 filtered view (default for analysts)"
echo ""
echo "[setup] Two dashboards are available in Kibana:"
echo "[setup]   Misbehavior Report - Unfiltered  — all Level-1 records"
echo "[setup]   Misbehavior Report - Main        — Level-2 filtered view"
echo ""
echo "[setup] To adjust Level-2 thresholds:"
echo "[setup]   1. Edit  display-thresholds.json"
echo "[setup]   2. Run   python manage_display_filter.py [--setup-kibana]"
