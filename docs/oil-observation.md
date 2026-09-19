# Oil observation pilot

This independent application captures source revisions, extracts evidence, tracks
incident revisions, and exports immutable replay snapshots. It contains no trading
adapter. CL/MCL fixtures and simulator results are engineering diagnostics, not
evidence of executable market opportunity. All reports say **economic evaluation
unavailable** until a live provider is implemented and qualified.

## Run locally

From the repository root (no `.env` needed):

```bash
.venv/bin/python -m pip install -r requirements-oil.txt
.venv/bin/python -m polybot.oil preflight
.venv/bin/python -m polybot.oil record --component news --once
.venv/bin/python -m polybot.oil record --component market --once --fixture tests/fixtures/oil/market.json
.venv/bin/python -m polybot.oil record --component analysis --once
.venv/bin/python -m polybot.oil status
```

Use `--config` with any of these commands to isolate another pilot. Storage paths
are relative to the configuration file, not the working directory. Live/paper
execution modes, alternate providers, automatic model fallback, and unbounded
inference settings are rejected.

For a fully offline, synthetic disruption/restoration demonstration:

```bash
.venv/bin/python -m polybot.oil demo --out data/oil-demo
.venv/bin/python -m polybot.oil replay --manifest data/oil-demo/snapshot/manifest.json
```

The demo writes a source/market manifest and JSON/HTML report and invokes no model
or network service. Use a new output directory for another immutable run.

For independently supervised continuous collection on WSL:

```bash
.venv/bin/python scripts/oil_supervisor.py
```

Ctrl-C shuts down all three children. For a bounded runtime test add `--duration
45`. Logs are under the configured data directory. No service is installed or
enabled automatically. The optional systemd user template in
`deploy/polybot-oil@.service` can supervise `news`, `market`, and `analysis` on a
qualified always-on Linux host. Do not run both supervisors simultaneously.
Component locks reject duplicate writers. WSL sleep and restarts create visible
runtime gaps; this is not an always-on availability guarantee.

## Capture, source rights, and qualifications

Aramco and gCaptain use RSS. ADNOC uses a listing and bounded detail-page queue.
Fujairah captures notice rows and PDFs, with resource-limited text extraction;
scanned, encrypted, oversized or unreadable documents remain captured with an
explicit text-extraction failure. UKMTO is registered but disabled after a 403
response; no challenge bypass is implemented.

HTTP successes do not prove parser completeness or archival/model rights. The
shipped registry permits public pilot capture but leaves `model_processing:
pending` for third-party content until its downstream-use policy is qualified.
This causes visible deferral, not model calls. The synthetic demo is permitted.
Record the qualification basis in the source registration before changing it to
`permitted`; do not use that field to assert an unverified vendor licence.
The pilot itself buys no subscriptions or sends publisher material to another
provider. Retention is manual review; no automatic raw-payload deletion job runs.

Fetchers allow only registered HTTPS hosts, validate redirects, reject credentials
in URLs, cap response sizes, use conditional requests and honor Retry-After.
Listings commit before slower article/document fetching. A cosmetic listing does
not repeatedly replace a full article. Feed summaries remain feed summaries;
gCaptain article-body scraping is not enabled. Original wire attribution is
retained where explicit; otherwise source lineage is unknown.
The first captured feed snapshot is backfill for state construction, not a fresh
entry opportunity. Identical text from another publisher reuses extraction while
preserving its separate receipt and does not create new economic novelty.

## Evidence and timing

Raw bytes, complete-message receipt, commit observation, source revisions,
extraction results, incidents, and decisions are distinct immutable records.
Cursor writes share the revision transaction. SQLite uses WAL/FULL and serialized
transactions; each main component owns its own journal. Monotonic timestamps
carry host/boot identity. Clock uncertainty remains unknown unless a clock-health
integration supplies it; no trading authorization can result.

The Codex runner uses `gpt-5.5`, ignores user/project rules and user configuration,
disables shell, apps, plugins, hooks, multi-agent, image and web tools, and strips
API/broker secrets from the child environment. It retains the CLI authentication
location. See the [official configuration reference](https://developers.openai.com/codex/config-reference/).
There is no paid-API fallback. Two workers, 100 attempts/day and 180 seconds/call
are hard caps. Late output is retained but cannot qualify under the initial
10-second candidate deadline. Authentication/quota failures require an explicit
retry/policy decision instead of an unlimited retry loop.

Every accepted fact carries literal supporting text. Unique exact quotations can
be re-anchored deterministically when a model miscounts Unicode offsets; ambiguous
quotations are rejected. Raw output and offset repairs are retained. Capacity is
not converted to lost production. Refinery effects are outside crude-disruption
entry eligibility. Models never choose quantities, prices, orders, or confidence
probabilities.

Cross-source incident associations remain candidates until explicit adjudication.
`adjudicate --operation merge|split|link --story ID --target-incident ID --reason
TEXT` records a new timestamped relation for future processing. It does not rewrite
past decisions. Unknown lineage and same-origin copies do not constitute
independent corroboration. Explicit withdrawal is different from a missing feed
item: disappearance from a rotating RSS window is never inferred to be withdrawal.

## Snapshots and reporting

```bash
.venv/bin/python -m polybot.oil snapshot --out data/oil-review-001
.venv/bin/python -m polybot.oil replay --manifest data/oil-review-001/manifest.json
.venv/bin/python -m polybot.oil report --manifest data/oil-review-001/manifest.json --out data/oil-review-001/report.json
```

Snapshots use SQLite's consistent backup interface, hash every file, and reject
missing/future decision inputs. They preserve the code commit, dirty status,
configuration and extraction identity. Replay performs no inference/network calls.
Quotes use checksummed segments with one-second flushes. An unclean open segment
recovers its valid prefix and records an unknown crash tail. Open/live segments
are explicitly excluded from frozen snapshots until closed; their absence is
reported as a gap. Copying an active SQLite file alone is not a backup.

Reports include source health/revisions, capture and extraction latency,
incident evidence, candidate/abstention reasons, missing price anchors, market-data
qualification, and separate spending options. Descriptive midpoint markouts are
not P&L. Extraction accuracy remains unmeasured until independently labeled data
exists. The Databento $199 reference is dated 2026-09-19 and must be rechecked;
IBKR account/entitlement costs are unknown. No purchase is implied.

## Offline research and remaining gates

`research.py` provides a separate futures simulator: integer contracts, decimal
ticks, signed prices, delayed bid/ask fills, marketable limit caps, displayed-size
partial fills, explicit fees and unresolved exits. Hypothetical default fees are
not a broker quotation. Additional slippage and spread are not double-counted.
No passive queue or fill-probability claim is made.

The research helpers define all six baseline directions, preserve abstentions,
freeze complete prospective protocols, purge episode overlap across chronological
splits, and report descriptive clustered uncertainty. They are not a fitted
forecast or a completed statistical edge study. Continuous price baselines must
be sampled independently of news times in the eventual registered experiment.
`freeze-protocol --protocol FILE` rejects incomplete definitions, reused versions,
and evaluation windows already started. Fixture results cannot promote a policy.

Stage A provides engineering capture/replay. Stage B requires a qualified live
CL/MCL adapter and seven days of accountable capture. Stage C requires a frozen
prospective experiment; Stage D requires independent after-cost evidence. Stage E
requires the separate broker/risk/OMS implementation and fault matrix. Stage F
requires actual capital, permissions, loss limits and a separately reviewed live
configuration. None of these external gates is represented as passed.

## Tests

```bash
TMPDIR=/tmp TEMP=/tmp .venv/bin/python -m pytest -q -s tests/test_oil.py
```

The focused suite covers immutable revisions, parser failures, source ordering,
writer rollback, budgets, evidence/negation, stale completions, replay causality,
quote recovery, simulator accounting, protocol freezes, and singleton operation.
Run the repository suite after shared changes. No legacy bot is reconfigured or
restarted by this application.
