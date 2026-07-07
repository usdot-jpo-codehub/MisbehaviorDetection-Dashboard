"""
MBD daily summary report — queries Elasticsearch directly.

Usage:
    python tools/report.py                    # last 1 day, ES on localhost:9200
    python tools/report.py --days 7
    python tools/report.py --es http://my-es-host:9200 --days 30
"""

import argparse
from datetime import datetime, timezone, timedelta

try:
    from elasticsearch import Elasticsearch, NotFoundError
except ImportError:
    raise SystemExit("Run: pip install 'elasticsearch>=8.0.0,<9.0.0'")

INDEX = "mbd-misbehaviors-*"
LINE  = "─" * 60


def build_query(since_iso: str) -> dict:
    return {"range": {"detected_at": {"gte": since_iso}}}


def main() -> None:
    parser = argparse.ArgumentParser(description="MBD Misbehavior Summary Report")
    parser.add_argument("--es",   default="http://localhost:9200", help="Elasticsearch base URL")
    parser.add_argument("--days", type=int, default=1,             help="Look-back window in days (default: 1)")
    args = parser.parse_args()

    es    = Elasticsearch(args.es)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    query = build_query(since_iso)

    # ── total count ───────────────────────────────────────────────────────────
    total = es.count(index=INDEX, query=query)["count"]

    # ── breakdown by misbehavior type ─────────────────────────────────────────
    by_type = es.search(
        index=INDEX, size=0, query=query,
        aggs={"by_type": {"terms": {"field": "misbehavior", "size": 20}}}
    )["aggregations"]["by_type"]["buckets"]

    # ── top 10 offending vehicles ─────────────────────────────────────────────
    top_vehicles = es.search(
        index=INDEX, size=0, query=query,
        aggs={"top_vehicles": {"terms": {"field": "vehicle_id", "size": 10,
                                         "order": {"_count": "desc"}}}}
    )["aggregations"]["top_vehicles"]["buckets"]

    # ── top RSUs by incident volume ───────────────────────────────────────────
    top_rsus = es.search(
        index=INDEX, size=0, query=query,
        aggs={"top_rsus": {"terms": {"field": "rsu_id", "size": 10}}}
    )["aggregations"]["top_rsus"]["buckets"]

    # ── speed statistics (speed_exceeded events only) ─────────────────────────
    speed_query = {
        "bool": {
            "filter": [
                {"range": {"detected_at": {"gte": since_iso}}},
                {"term":  {"misbehavior": "speed_exceeded"}}
            ]
        }
    }
    speed_aggs = es.search(
        index=INDEX, size=0, query=speed_query,
        aggs={"stats": {"stats": {"field": "speed_kmh"}}}
    )["aggregations"]["stats"]

    # ── print report ──────────────────────────────────────────────────────────
    print(LINE)
    print("MBD MISBEHAVIOR REPORT")
    print(f"Period : last {args.days} day(s)  (since {since_iso})")
    print(f"Source : {args.es}")
    print(LINE)

    print(f"\nTotal misbehaviors : {total}")

    if by_type:
        print("\n--- By Type " + "─" * 48)
        for b in by_type:
            print(f"  {b['key']:<35}  {b['doc_count']:>6}")

    if top_vehicles:
        print("\n--- Top 10 Vehicles " + "─" * 40)
        for b in top_vehicles:
            print(f"  {b['key']:<25}  {b['doc_count']:>6} events")

    if top_rsus:
        print("\n--- Top RSUs " + "─" * 47)
        for b in top_rsus:
            print(f"  {b['key']:<25}  {b['doc_count']:>6} events")

    if speed_aggs["count"] > 0:
        print("\n--- Speed Statistics (speed_exceeded) " + "─" * 22)
        print(f"  Count  : {speed_aggs['count']}")
        print(f"  Min    : {speed_aggs['min']:.2f} km/h")
        print(f"  Max    : {speed_aggs['max']:.2f} km/h")
        print(f"  Avg    : {speed_aggs['avg']:.2f} km/h")

    print("\n" + LINE)


if __name__ == "__main__":
    main()
