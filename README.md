# oiltradingbot

Standalone oil observation and research application. The runtime captures public source revisions, extracts evidence, tracks incidents, archives market fixtures, and exports immutable replay reports without broker execution or Polymarket-specific dependencies.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Quick start

```bash
oilbot preflight
oilbot record --component news --once
oilbot record --component market --once --fixture tests/fixtures/oil/market.json
oilbot record --component analysis --once
oilbot status
```

## Offline demo

```bash
oilbot demo --out data/oil-demo
oilbot replay --manifest data/oil-demo/snapshot/manifest.json
```

The demo writes a source/market manifest and JSON/HTML report without calling any remote model or broker service.

## Automated trading development

A local paper engine now converts recorded research candidates into delayed
orders and simulated fills, with durable state and configurable risk limits:

```bash
oilbot paper-replay --manifest data/oil-review-001/manifest.json \
  --instrument MCL-2026-10 --limits configs/oil/paper-limits.json \
  --out data/paper-review-001
```

Use a snapshot containing that explicit instrument and time-aligned decisions
and quotes. This is offline paper execution; broker integration and strategy
validation remain to be built. See [the development plan](docs/automated-trading.md)
for assumptions, limitations, and the path to forward paper and live execution.

## Repository layout

- `src/oilbot/` — standalone oil runtime package
- `configs/oil/` — observation and source configuration
- `tests/test_oil.py` — focused oil verification suite
- `scripts/oil_supervisor.py` — local supervisor for news/market/analysis workers
- `deploy/oilbot@.service` — systemd user unit for a single host
- `docs/oil-observation.md` — operational notes and guardrails
- `docs/automated-trading.md` — paper execution and the automated-trading development plan

The legacy `polybot.oil` import and module entrypoint delegate to `src/oilbot`;
install the package before using either entrypoint.

This project intentionally keeps the runtime focused on public oil intelligence and offline research, with no broker or market-data dependencies in the active package graph.
