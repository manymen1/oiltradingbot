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

## Repository layout

- `src/oilbot/` — standalone oil runtime package
- `configs/oil/` — observation and source configuration
- `tests/test_oil.py` — focused oil verification suite
- `scripts/oil_supervisor.py` — local supervisor for news/market/analysis workers
- `deploy/oilbot@.service` — systemd user unit for a single host
- `docs/oil-observation.md` — operational notes and guardrails

This project intentionally keeps the runtime focused on public oil intelligence and offline research, with no broker or market-data dependencies in the active package graph.
