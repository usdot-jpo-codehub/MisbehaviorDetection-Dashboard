"""
ODE BSM Misbehavior Detection Agent

Subscribes to topic.OdeBsmJson on the ODE Kafka broker and runs all
misbehavior detectors against each received BSM via _process_bsm().
Misbehavior events are written to a local JSON-lines log file for the
Filebeat sidecar to ship to the remote Logstash instance.

One agent is deployed per RSU.  Per-vehicle detector state (position,
heading, speed history, etc.) is kept in process memory and is lost on
restart, resulting in at most CONFIRM_N missed detections per vehicle —
acceptable for this deployment model.  Continuity is also lost when a
vehicle moves between RSUs; this is also accepted.

Configuration is read from ode_config.json (--config flag or the
ODE_CONFIG environment variable).

Usage:
    python bsm_agent.py [--config ode_config.json] [--log logs/misbehaviors.log]
"""

import argparse
import json
import os
import signal
import sys
from collections import defaultdict
from pathlib import Path

from confluent_kafka import Consumer, KafkaError

from detector import _build_detectors, _process_bsm
from detectors.config import DetectorConfig


# ---------------------------------------------------------------------------
# ODE BSM format adapter
# ---------------------------------------------------------------------------

def _adapt_wheel_brakes(wb) -> str:
    """
    Convert an ODE Kafka wheelBrakes object to the 5-character binary string
    expected by BrakesInconsistencyDetector.

    ODE format (dict):
        {"unavailable": bool, "leftFront": bool, "rightFront": bool,
         "leftRear": bool, "rightRear": bool}

    Target format (str):  bit 0 = unavailable
                          bit 1 = leftFront
                          bit 2 = rightFront
                          bit 3 = leftRear
                          bit 4 = rightRear

    If wb is already a string (e.g. during local file-based testing) it is
    returned unchanged.  An unrecognised type returns "10000" (unavailable
    bit set) so the detector skips the field rather than mis-classifying it.
    """
    if isinstance(wb, str):
        return wb
    if not isinstance(wb, dict):
        return "10000"
    return (
        ('1' if wb.get('unavailable', False) else '0') +
        ('1' if wb.get('leftFront',   False) else '0') +
        ('1' if wb.get('rightFront',  False) else '0') +
        ('1' if wb.get('leftRear',    False) else '0') +
        ('1' if wb.get('rightRear',   False) else '0')
    )


def _normalise_bsm(bsm: dict) -> dict:
    """
    Normalise an ODE Kafka BSM to the format expected by the detectors.

    The only structural difference between ODE Kafka BSMs and the file-based
    BSMs processed by detector.py is the wheelBrakes field encoding.  All
    other coreData fields already match detector expectations.
    """
    try:
        core = bsm['payload']['data']['coreData']
        wb = core.get('brakes', {}).get('wheelBrakes')
        if wb is not None:
            core['brakes']['wheelBrakes'] = _adapt_wheel_brakes(wb)
    except (KeyError, TypeError):
        pass
    return bsm


# ---------------------------------------------------------------------------
# Kafka consumer
# ---------------------------------------------------------------------------

def _build_consumer(cfg: DetectorConfig) -> Consumer:
    kafka = cfg.section('kafka')
    return Consumer({
        'bootstrap.servers': kafka['bootstrap_servers'],
        'group.id':          kafka['group_id'],
        'auto.offset.reset': 'latest',
        'enable.auto.commit': True,
    })


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args():
    default_config = os.environ.get(
        'ODE_CONFIG',
        str(Path(__file__).parent / 'ode_config.json'),
    )
    parser = argparse.ArgumentParser(description='ODE BSM Misbehavior Detection Agent')
    parser.add_argument(
        '--config',
        default=default_config,
        help='ODE config file (default: ode_config.json or $ODE_CONFIG)',
    )
    parser.add_argument(
        '--log',
        default='logs/misbehaviors.log',
        help='Output log file path (default: logs/misbehaviors.log)',
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    args   = _parse_args()
    cfg    = DetectorConfig.from_file(Path(args.config))

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    detectors        = _build_detectors(cfg)
    cooldown_meters  = cfg.cooldown_meters
    cooldown_seconds = cfg.cooldown_seconds
    cooldown         = {}
    counts           = defaultdict(int)
    total = flagged = suppressed = 0

    kafka_cfg   = cfg.section('kafka')
    kafka_topic = kafka_cfg['topic']
    consumer    = _build_consumer(cfg)
    consumer.subscribe([kafka_topic])

    running = True

    def _shutdown(signum, _frame):
        nonlocal running
        print(f'\nShutting down (signal {signum})…', file=sys.stderr)
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    print(f'Config  : {args.config}')
    print(f'Log     : {args.log}')
    print(f'Broker  : {kafka_cfg["bootstrap_servers"]}')
    print(f'Topic   : {kafka_topic}')
    print(f'Group   : {kafka_cfg["group_id"]}')

    with log_path.open('a') as log_f:
        while running:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    print(f'[ERROR] Kafka: {msg.error()}', file=sys.stderr)
                continue

            try:
                bsm = json.loads(msg.value().decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                print(f'[WARN] JSON parse error: {exc}', file=sys.stderr)
                continue

            _normalise_bsm(bsm)
            total += 1

            fl, sup = _process_bsm(
                bsm, log_f, cooldown, counts,
                detectors, cooldown_meters, cooldown_seconds,
            )
            flagged    += fl
            suppressed += sup

    consumer.close()
    print(f'\nProcessed : {total:,} BSMs')
    print(f'Flagged   : {flagged:,} misbehaviors ({suppressed:,} suppressed)')


if __name__ == '__main__':
    main()
