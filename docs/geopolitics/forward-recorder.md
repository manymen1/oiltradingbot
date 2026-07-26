# Forward evidence recorder

The forward recorder answers the strategy's economically decisive question:

> After qualifying terminal evidence was first available and processed, did
> executable stale liquidity survive long enough for this system to take it?

It is enabled only on the generic rules-first paper path. It has no wallet,
signing key, order client, or live-order method.

## Runtime architecture

`make paper` starts one fleet-wide public-book service before it starts the
generic paper workers. The service:

- resolves every current `RuleSpec` and source-plan binding;
- divides the active token universe into bounded WebSocket shards;
- seeds full depth from the public REST book in parallel;
- subscribes to the public market stream;
- sends the required application-level `PING` every 10 seconds;
- reconnects with bounded exponential backoff;
- reconstructs full depth from `book` and `price_change` messages;
- records server and local receive timestamps separately;
- mirrors `market_resolved` events into the forward store.

The production configuration uses at most 200 tokens per WebSocket shard.
Paper workers read the latest depth from the shared WAL database, so hundreds
of market workers do not open hundreds of independent market streams.

Running `run-rule-market` by itself in shared-book mode does not create a
second stream. It reads the latest shared snapshot and fails closed on a
missing or stale book. Use `make paper` for continuous collection. For an
isolated development worker, explicitly set
`forward_recorder.shared_book_service: false`.

## What is recorded

The append-only store is:

```text
data/discovery/forward_recorder.sqlite3
```

Every record is bound to a hash of the market instruments, immutable
`RuleSpec`, source plan, evidence/extractor policy, fee policy, and recorder
policy. Discovery scores, volume, liquidity, and refresh timestamps cannot
fragment a dataset. A real rule, instrument, evidence adapter/model/prompt,
fee-curve, source-plan, or recorder-policy change creates a new binding.

The store contains:

- full public depth after each initial book and incremental depth change;
- public `last_trade_price` prints with price, size, exchange-reported side,
  fee and transaction hash;
- exchange/server time and local receive time;
- articles exactly as first fetched, including full text and source identity;
- first-observation time;
- every extraction result and two-pass runtime;
- terminal and nonterminal decision proofs;
- executable quote anchors;
- public market-stream and Gamma-finalized resolutions;
- connection, reconnect, REST-seed, and runner-cycle events.

An article ID cannot be reused with different bytes. A proof, binding, or
resolution cannot be silently overwritten. Conflicting final resolutions
raise an error.

## Latency and quote survival

The completeness report keeps different delays separate:

- publisher timestamp to direct-source discovery;
- discovery to publisher-page request;
- publisher fetch and parse;
- parse to first observation by the rules worker;
- deterministic and model extraction runtime;
- latest required evidence to decision proof;
- publisher/discovery to simulated submission;
- decision to submission and submission round trip;
- exchange/server book timestamp to local receipt.

For every terminal entry intent that has a fresh executable ask, the recorder
anchors the actual decision-time book and samples it at:

```text
100 ms, 250 ms, 500 ms, 1 s, 2 s, 5 s, 10 s
```

Each sample measures whether the original executable notional still exists:

- at the original ask;
- after allowing a one-cent adverse price move.

A sample more than 250 ms late is marked `LATE`, not counted as quote
survival. A restart cannot convert an overdue sample into an on-time result.
Samples are also bound to the shard's recorded
`ws_open`/`PONG`/error/close/stop state. Cached depth observed after a known
stream failure is marked
`STREAM_UNAVAILABLE`, not survived. Displayed depth is evidence of
availability, not an assumed fill.

## Completeness and time-integrity audit

Inspect one market:

```bash
make forward-completeness MARKET=<market-id>
```

`READY_FOR_REPLAY` requires:

- at least one actual forward session;
- a full initial book for every bound token;
- at least one first-seen full-text article;
- a processing result for every recorded article;
- complete on-time quote-survival samples for every created anchor;
- no future book timestamps;
- no future article timestamps;
- no article fetched before its declared publication;
- no decision timestamp preceding its evidence;
- no reversed discovery, fetch, parse, decision, or submission stage.

A resolved outcome is not required to build a replay; unresolved replay P&L
remains `null`. A dataset is never declared promotion-ready by the
completeness command.

## Build and replay a forward timeline

Build a content-addressed JSONL timeline:

```bash
make build-forward-timeline MARKET=<market-id>
```

The default output is:

```text
data/discovery/forward_timelines/<market>/<timeline-sha256>.jsonl
```

A hash-bound manifest is written beside it. The builder combines the latest
known YES and NO depth at each recorded book time, first-observed full-text
articles, and explicit resolutions. It rejects incomplete datasets, invalid
levels, crossed books, and time-integrity failures.

Replay it without changing its forward designation:

```bash
make replay-rule-market \
  MARKET=<market-id> \
  TIMELINE=<timeline.jsonl> \
  DATASET_ROLE=forward
```

Then aggregate completed forward replay summaries with:

```bash
make promotion-report RUNS=<summary-directory>
```

The promotion report remains read-only and cannot enable a live family.

## Profit priority and quote half-life

After a discovery cycle has produced current specs and source plans, inspect
the conservative monitoring ranking:

```bash
make priority
```

The report includes each family's median measured quote half-life and the
market-level p95 end-to-end submission latency used by the ranking policy. A
market whose
p95 latency exceeds the recorder's longest quote-survival horizon is blocked
from exploitation rather than assigned an invented fill probability.

Inspect the reconciled confirmation-to-profit funnel with:

```bash
make profit-funnel
```

These reports are measurement and resource-allocation surfaces. Neither one
promotes a family, enables live mode, or proves positive expectancy. Their
identity and point-in-time rules are documented in `profit-priority.md`.

Use `make economics` for the source/family bottleneck view, including
publisher-to-submission latency, quote half-life, stressed opportunities and
the current primary loss mechanism.

## Configuration

The production defaults are:

```yaml
forward_recorder:
  enabled: true
  db_path: data/discovery/forward_recorder.sqlite3
  websocket_url: wss://ws-subscriptions-clob.polymarket.com/ws/market
  heartbeat_seconds: 10
  reconnect_min_seconds: 1
  reconnect_max_seconds: 30
  rest_seed: true
  shared_book_service: true
  max_tokens_per_connection: 200
  rest_seed_workers: 8
  max_book_levels: 20
  quote_survival_horizons_ms: [100, 250, 500, 1000, 2000, 5000, 10000]
  max_sample_lag_ms: 250
```

Unknown configuration fields, duplicate or unsorted horizons, non-WebSocket
URLs, unsafe bounds, and recorder-without-runner configurations are rejected.
Direct-source and deterministic-extraction settings are documented in
`fast-source-lane.md`.
