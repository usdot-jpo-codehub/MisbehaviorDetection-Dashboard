# MBD — single entry-point
#
# Usage:
#   make run    DATA=data/sample.zip     detect misbehaviors, append to log
#   make filter                          push display-thresholds.json → ES alias
#   make ingest                          restart Logstash to ingest latest log
#   make full   DATA=data/sample.zip     run + ingest + filter
#   make fresh  DATA=data/sample.zip     clear ES + truncate log + full
#   make clear                           delete today's ES index
#   make test                            run pytest unit tests

DATA    ?= $(firstword $(wildcard data/*.zip data/*.json))
LOG     ?= logs/misbehaviors.log
ES      ?= http://localhost:9200
KIBANA  ?= http://localhost:5601
COMPOSE ?= docker compose
PYTHON  ?= venv/bin/python3

.PHONY: run filter ingest full clear fresh test help

help:
	@echo ""
	@echo "  make run    [DATA=<file>]   Detect misbehaviors and append to log"
	@echo "  make filter                 Push display-thresholds.json to ES alias"
	@echo "  make ingest                 Restart Logstash to ingest latest log"
	@echo "  make full   [DATA=<file>]   run + ingest + filter"
	@echo "  make clear                  Delete today's ES index"
	@echo "  make fresh  [DATA=<file>]   clear + run (with --clear) + ingest + filter"
	@echo "  make test                   Run pytest unit tests"
	@echo ""
	@echo "  DATA defaults to the first file found in data/"

run:
	@mkdir -p $(dir $(LOG))
	$(PYTHON) detector.py "$(DATA)" --log "$(LOG)"

filter:
	$(PYTHON) manage_display_filter.py --es-url $(ES) --kibana-url $(KIBANA) --setup-kibana

ingest:
	$(COMPOSE) restart logstash

full: run ingest filter

clear:
	@INDEX="mbd-misbehaviors-$$(date +%Y.%m.%d)"; \
	printf "Deleting $$INDEX ... "; \
	CODE=$$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$(ES)/$$INDEX"); \
	if [ "$$CODE" = "200" ]; then echo "deleted"; else echo "not found ($$CODE)"; fi

fresh: clear
	@mkdir -p $(dir $(LOG))
	$(PYTHON) detector.py "$(DATA)" --log "$(LOG)" --clear
	$(COMPOSE) restart logstash
	$(PYTHON) manage_display_filter.py --es-url $(ES) --kibana-url $(KIBANA) --setup-kibana

test:
	$(PYTHON) -m pytest tests/ -v
