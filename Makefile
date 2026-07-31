# Single surface for running the geopolitics trading system.
# `make help` lists everything. Every target uses configs/geopolitics/
# discovery.yaml (override: make status CONFIG=path/to/other.yaml) and
# sources .env for secrets via bin/geo.

CONFIG ?= configs/geopolitics/discovery.yaml
GEO := bin/geo
PY := .venv/bin/python

.DEFAULT_GOAL := help

help: ## show this list
	@grep -E '^[a-z][a-zA-Z_-]*:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-16s %s\n", $$1, $$2}'
	@echo ""
	@echo "  Any other CLI command: bin/geo <command> --config $(CONFIG) ..."

# ---- setup ----

setup: ## create venv, install deps, seed .env
	test -d .venv || python3 -m venv .venv
	.venv/bin/pip install --quiet -r requirements.txt
	@test -f .env || (cp .env.example .env && chmod 600 .env && echo ">>> created .env -- FILL IN YOUR KEYS before running")
	@command -v claude >/dev/null 2>&1 || echo ">>> claude CLI not found -- classification uses your Claude subscription via it: npm install -g @anthropic-ai/claude-code && claude login"
	@echo "setup complete. next: edit .env, then 'make paper'"

test: ## run the full test suite
	$(PY) -m pytest -q tests/

# ---- running (paper is the default posture) ----

paper: ## run the fleet: discover/grade/scan/alert + paper bots, cannot trade
	$(GEO) run-fleet --config $(CONFIG)

paper-once: ## one paper fleet cycle, then exit (smoke test)
	$(GEO) run-fleet --config $(CONFIG) --once

live: ## run the fleet LIVE (requires config armed live + I_UNDERSTAND_LIVE_TRADING=yes)
	@test "$(I_UNDERSTAND_LIVE_TRADING)" = "yes" || \
		(echo "refusing: run as 'make live I_UNDERSTAND_LIVE_TRADING=yes'"; \
		 echo "and first set fleet.position_mode: live + auto_ack: true in $(CONFIG)"; exit 1)
	$(GEO) run-fleet --config $(CONFIG) --live

# ---- controls ----

halt: ## MASTER KILL: stop all execution mid-cycle, everywhere
	$(GEO) set-fleet-mode --mode off

watch-only: ## fleet keeps watching + alerting but never trades
	$(GEO) set-fleet-mode --mode alert_only

arm: ## clear the master switch back to live (per-market gates still apply)
	$(GEO) set-fleet-mode --mode live

# ---- observability ----

status: ## the 3am view: positions, heartbeats, ledger, drawdown headroom, scan
	$(GEO) fleet-status --config $(CONFIG)

semantic-coverage: ## exact capture/rule/source/evidence readiness matrix
	$(GEO) semantic-coverage --config $(CONFIG)

funnel: ## where edge died across the whole universe
	$(GEO) funnel-report --config $(CONFIG)

priority: ## forward-evidence market priority and quote half-life
	$(GEO) priority-report --config $(CONFIG)

economics: ## source latency, quote half-life, stressed edge, and bottleneck
	$(GEO) economics-report --config $(CONFIG)

profit-funnel: ## reconciled rules-first confirmation-to-profit funnel
	$(GEO) profit-funnel --config $(CONFIG)

calibration: ## are the probability sources beating the market? (Brier report)
	$(GEO) calibration-report --config $(CONFIG)

compile-rules: ## compile current rule texts into immutable two-pass RuleSpecs
	$(GEO) compile-rules --config $(CONFIG) $(if $(MARKET),--market "$(MARKET)",)

plan-sources: ## derive current SourcePlans from RuleSpecs (optional MARKET=id)
	$(GEO) plan-sources --config $(CONFIG) $(if $(MARKET),--market "$(MARKET)",)

grade-markets: ## regrade discovered markets against current semantic assets
	$(GEO) grade-markets --config $(CONFIG)

inspect-rule: ## inspect current RuleSpec and both raw compiler passes (MARKET=id)
	@test -n "$(MARKET)" || (echo "usage: make inspect-rule MARKET=<market_id>"; exit 1)
	$(GEO) inspect-rule --config $(CONFIG) --market "$(MARKET)"

validate-rule: ## strictly validate an exported RuleSpec JSON (SPEC=path)
	@test -n "$(SPEC)" || (echo "usage: make validate-rule SPEC=<file.json>"; exit 1)
	$(GEO) validate-rule --spec "$(SPEC)"

prepare-rule-review: ## export one stored compiler pass for review (MARKET=id PASS_SHA256=hash OUT=file)
	@test -n "$(MARKET)" || (echo "usage: make prepare-rule-review MARKET=<market_id> PASS_SHA256=<hash> OUT=<file.json>"; exit 1)
	@test -n "$(PASS_SHA256)" || (echo "usage: make prepare-rule-review MARKET=<market_id> PASS_SHA256=<hash> OUT=<file.json>"; exit 1)
	@test -n "$(OUT)" || (echo "usage: make prepare-rule-review MARKET=<market_id> PASS_SHA256=<hash> OUT=<file.json>"; exit 1)
	$(GEO) prepare-rule-review --config $(CONFIG) --market "$(MARKET)" --pass-sha256 "$(PASS_SHA256)" --out "$(OUT)"

import-reviewed-rule: ## import an exact reviewed RuleSpec (MARKET=id SPEC=file REVIEWER=id NOTE=text APPROVE_SPEC_SHA256=hash)
	@test -n "$(MARKET)" || (echo "usage: make import-reviewed-rule MARKET=<market_id> SPEC=<file.json> REVIEWER=<id> NOTE=<text> APPROVE_SPEC_SHA256=<hash>"; exit 1)
	@test -n "$(SPEC)" || (echo "usage: make import-reviewed-rule MARKET=<market_id> SPEC=<file.json> REVIEWER=<id> NOTE=<text> APPROVE_SPEC_SHA256=<hash>"; exit 1)
	@test -n "$(REVIEWER)" || (echo "usage: make import-reviewed-rule MARKET=<market_id> SPEC=<file.json> REVIEWER=<id> NOTE=<text> APPROVE_SPEC_SHA256=<hash>"; exit 1)
	@test -n "$(NOTE)" || (echo "usage: make import-reviewed-rule MARKET=<market_id> SPEC=<file.json> REVIEWER=<id> NOTE=<text> APPROVE_SPEC_SHA256=<hash>"; exit 1)
	@test -n "$(APPROVE_SPEC_SHA256)" || (echo "usage: make import-reviewed-rule MARKET=<market_id> SPEC=<file.json> REVIEWER=<id> NOTE=<text> APPROVE_SPEC_SHA256=<hash>"; exit 1)
	$(GEO) import-reviewed-rule --config $(CONFIG) --market "$(MARKET)" --spec "$(SPEC)" --reviewer "$(REVIEWER)" --note "$(NOTE)" --approve-spec-sha256 "$(APPROVE_SPEC_SHA256)"

rule-market-once: ## run one generic rules-first paper cycle (MARKET=id)
	@test -n "$(MARKET)" || (echo "usage: make rule-market-once MARKET=<market_id>"; exit 1)
	$(GEO) run-rule-market --config $(CONFIG) --market "$(MARKET)" --once

inspect-rule-market: ## inspect claims/evaluations/proofs for one rules-first market
	@test -n "$(MARKET)" || (echo "usage: make inspect-rule-market MARKET=<market_id>"; exit 1)
	$(GEO) inspect-rule-market --config $(CONFIG) --market "$(MARKET)"

forward-completeness: ## audit forward books/articles/extractions/quote survival (MARKET=id)
	@test -n "$(MARKET)" || (echo "usage: make forward-completeness MARKET=<market_id>"; exit 1)
	$(GEO) forward-completeness --config $(CONFIG) --market "$(MARKET)"

build-forward-timeline: ## build content-addressed forward replay input (MARKET=id)
	@test -n "$(MARKET)" || (echo "usage: make build-forward-timeline MARKET=<market_id> [OUT=file.jsonl]"; exit 1)
	$(GEO) build-forward-timeline --config $(CONFIG) --market "$(MARKET)" $(if $(OUT),--out "$(OUT)",)

reconcile: ## ledger hygiene: free dead position slots, roll stale buckets
	$(GEO) reconcile-ledger --config $(CONFIG)

latency: ## publication -> fetch -> classify -> order percentiles (are we winning the race?)
	$(GEO) latency-report

portwatch: ## live IMF PortWatch chokepoint 7-day averages (Hormuz et al.)
	$(PY) -c "from polybot.discovery.portwatch import chokepoint_reading, CHOKEPOINT_NAMES; \
	seen=set(); \
	[print(f'{r.portname:22} 7d-avg {r.ma7:6.2f}  latest {r.latest_value:3d} on {r.latest_date}') \
	 for name in CHOKEPOINT_NAMES.values() if name not in seen and not seen.add(name) \
	 for r in [chokepoint_reading(name)] if r]"

trades: ## per-trade P&L table from the execution journals
	$(GEO) trades-report --ledger data/discovery/allocations.json

# ---- change validation (run before arming any prompt/config change) ----

# usage: make replay BOT=configs/geopolitics/generated/x.yaml ARTICLES=logs/binary_articles.jsonl
replay: ## rerun archived articles through the full pipeline (isolated, dry-run)
	@test -n "$(BOT)" -a -n "$(ARTICLES)" || (echo "usage: make replay BOT=<bot.yaml> ARTICLES=<articles.jsonl>"; exit 1)
	$(GEO) replay --config $(BOT) --articles $(ARTICLES)

replay-rule-market: ## replay timestamped evidence/book/resolution timeline (MARKET=id TIMELINE=file)
	@test -n "$(MARKET)" -a -n "$(TIMELINE)" || (echo "usage: make replay-rule-market MARKET=<id> TIMELINE=<events.jsonl> [DATASET_ROLE=development|frozen_oos|forward] [LABELS=<labels.json>] [OUT=<summary.json>]"; exit 1)
	$(GEO) replay-rule-market --config $(CONFIG) --market $(MARKET) --timeline $(TIMELINE) --dataset-role $(or $(DATASET_ROLE),development) $(if $(LABELS),--labels $(LABELS),) $(if $(OUT),--out $(OUT),)

promotion-report: ## strategy-specific rules-first PASS/FAIL report (RUNS=file-or-directory)
	@test -n "$(RUNS)" || (echo "usage: make promotion-report RUNS=<summary.json-or-directory> [OUT=<report.json>]"; exit 1)
	$(GEO) rule-promotion-report --config $(CONFIG) --runs $(RUNS) $(if $(OUT),--out $(OUT),)

# usage: make eval BOT=configs/geopolitics/generated/x.yaml
eval: ## adversarial regression cases; nonzero exit = the change regressed
	@test -n "$(BOT)" || (echo "usage: make eval BOT=<bot.yaml> [CASES=<cases.jsonl>]"; exit 1)
	$(GEO) eval-classifier --config $(BOT) --cases $(or $(CASES),configs/geopolitics/eval-cases/binary-adversarial.jsonl)

# ---- data safety ----

backup: ## snapshot data/ (ledger, journals, calibration, acks)
	deploy/backup.sh

.PHONY: help setup test paper paper-once live halt watch-only arm status funnel priority economics profit-funnel calibration semantic-coverage compile-rules plan-sources grade-markets inspect-rule validate-rule prepare-rule-review import-reviewed-rule rule-market-once inspect-rule-market forward-completeness build-forward-timeline reconcile latency trades replay replay-rule-market promotion-report eval backup
