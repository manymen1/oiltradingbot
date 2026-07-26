# Quickstart

Everything runs through `make` (which wraps `bin/geo`, which wraps the
Python CLI). `make help` lists every target.

## 1. One-time setup

```bash
make setup          # venv + deps + creates .env from the template
$EDITOR .env        # fill in TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
make test           # 400+ tests should pass
```

**Classification runs on your Claude subscription by default** (provider
`claude_cli` in the config): install the Claude CLI and log in once —

```bash
npm install -g @anthropic-ai/claude-code
claude login
```

No `ANTHROPIC_API_KEY` needed. If you set one in `.env` anyway, it becomes
an automatic fallback when the CLI fails; set `classifier.provider:
anthropic` to use the metered API exclusively.

## 2. Paper mode (the default posture — cannot trade)

```bash
make paper          # the whole-universe autopilot, simulated execution only
```

This discovers every geopolitical market on Polymarket and compiles each
market's verbatim rules twice into an immutable, strictly validated `RuleSpec`.
Instrument IDs are bound from discovery data; the model can only describe rule
semantics. The pipeline then derives role-aware news-source plans and spawns a
generic rules-first worker for each eligible market. Each worker extracts
article facts twice, evaluates them with deterministic rule-family code, and
writes a reconstructable decision proof before any simulated entry, exit, or
hold. No exchange order can leave: the generic command rejects `--live` and
every worker uses the stateful paper broker. Use `make watch-only` when alerts
without simulated entries or exits are desired.

Rule compilation and inspection are also available independently:

```bash
make compile-rules                 # compile at most the configured cycle cap
make inspect-rule MARKET=<id>      # spec plus both raw compiler passes
make validate-rule SPEC=<file>     # strict offline schema/hash validation
make rule-market-once MARKET=<id>  # one evidence/evaluation paper cycle
make inspect-rule-market MARKET=<id>
                                    # claims, evaluations, decision proofs
```

An unchanged rule uses its cached spec without classifier calls. Compiler
disagreement, a changed rule hash, an unresolved required settlement source,
or a stale source plan demotes the market instead of reusing an old decision.
Wire-service mirrors share one canonical origin and cannot count as independent
corroboration.

All six non-subjective rule families have deterministic evaluators. Only
`OCCURRENCE_BEFORE_DEADLINE`, `CATEGORICAL_EXCLUSIVE`, and
`SOURCE_LOCKED_ANNOUNCEMENT` may execute in paper mode. Status-at-deadline,
numeric-threshold, and duration markets still collect claims, evaluations, and
proofs, but every trade intent is blocked by configuration.

Paper FAK orders walk the public CLOB depth available inside their limit
price; thin books produce partial fills rather than unlimited top-quote fills.
Displayed liquidity cannot be reused until a new book revision arrives, and
books older than `execution.paper_max_book_age_seconds` fail closed. Cash,
fees, token balances, partial exposure, and actual filled cost basis persist
in the paper state.

The fleet also starts one central RSS/Atom fetcher. Generated bots consume
their subscribed rows from `data/discovery/central_feed.sqlite3` instead of
polling the same common publishers independently. This path is fail-closed:
if the producer heartbeat becomes stale, bots keep direct poll-URL monitoring
but do not fall back to per-bot RSS requests. `make status` reports the central
heartbeat, feed-error count, and stored-row count.

Classifier calls are fleet-global too. Generated bots atomically reserve each
screen call and the entire confirm-pass group in
`data/discovery/classifier_budget.sqlite3`; `make status` reports current
hour/day usage and remaining capacity. Hand-written standalone bot configs
without `classifier.budget_db_path` retain their local JSON budget. Publisher
page promotion is single-flight cached in the central-feed database, so
related markets reuse one normalized full-text fetch.

The paper fleet also starts one sharded public market-book service. It keeps
full depth current over persistent WebSockets, records first-seen articles and
decision latency, samples terminal quote survival from 100ms through 10s, and
captures final resolutions into `data/discovery/forward_recorder.sqlite3`.
Generic workers read those shared books instead of opening one WebSocket per
market.

While it runs (from another terminal):

```bash
make status         # positions, heartbeats, ledger, drawdown headroom, top edges
make funnel         # where edge died across the universe
make calibration    # are the probability estimates beating the market?
make forward-completeness MARKET=<id>
    # books/articles/extractions/time-integrity/quote-survival coverage
```

Let this soak for at least 1–2 weeks. The funnel and calibration reports —
not a good day — are what justify going live.

The model-priced opportunity scanner also ships in
`opportunity.model_pricing_mode: calibration_only`. It still records every
estimate, market midpoint, computed edge, and resolution, but cannot allocate
model-priced exposure. `make status` and `make funnel` report the pricing
equation's maximum theoretical edge and required model weight.

After calibration proves the estimator adds value, model-priced ranking and
allocation can be promoted independently:

```yaml
opportunity:
  model_pricing_mode: "allocatable"
  # Set model_weight from calibration evidence. Config loading fails if
  # the resulting equation cannot clear min_edge even theoretically.
```

Confirmation-bot live execution does not require this promotion.

## 3. Going live (not enabled for rules-first markets yet)

The production config currently has no promoted live rule family
(`rule_compiler.live_confirmation_families: []`). Consequently, changing the
fleet mode alone cannot make a rules-first discovered market live-eligible.
Promote one validated family only after its replay and paper evidence clears
the live gates, then use the controls below.

1. In `configs/geopolitics/discovery.yaml` set:
   ```yaml
   fleet:
     position_mode: "live"
     auto_ack: true
   ```
2. Add the Polymarket wallet credentials to `.env`.
3. Run:
   ```bash
   make live I_UNDERSTAND_LIVE_TRADING=yes
   ```

Money is still bounded by the ledger caps in the config ($50/order,
$100/market, $300/region, $1000 total, 5 open positions) and the whole
fleet halts itself at $150 realized drawdown.

## 4. Kill switches

```bash
make halt           # MASTER KILL: all execution stops mid-cycle, everywhere
make watch-only     # keep watching + alerting, never trade
make arm            # back to live (per-market gates still apply)
```

## 5. Before changing anything (prompts, models, thresholds)

```bash
make eval BOT=configs/geopolitics/generated/<market>.yaml
    # adversarial regression cases; nonzero exit = the change regressed

make replay BOT=configs/geopolitics/generated/<market>.yaml ARTICLES=logs/binary_articles.jsonl
    # rerun the real article archive through the changed pipeline, isolated

make replay-rule-market MARKET=<id> TIMELINE=<events.jsonl> DATASET_ROLE=development
    # rules-first point-in-time evidence + book + resolution replay

make promotion-report RUNS=data/discovery/rule_replays
    # family-specific PASS/FAIL gates; never changes live configuration

make build-forward-timeline MARKET=<id>
    # content-addressed, selection-bias-resistant forward replay input
```

Rules-first replay rejects out-of-order/future information and records
two-pass agreement, complete proofs, executable stale-price availability,
partial fills, 1m/5m/30m CLV, and resolution-scored net P&L. Promotion samples
are clustered by geopolitical event so correlated market rungs do not count as
independent evidence. Promotion accepts only policy-homogeneous frozen-OOS or
forward datasets with human-reviewed terminal labels, zero safety violations,
positive stressed P&L, and a strictly positive clustered EV lower bound.
Per-market fee schedules are bound into each decision and replay; missing or
stale fee metadata blocks generic entries. See
`docs/geopolitics/rules-first-replay.md` for the timeline schema and exact
thresholds. See `docs/geopolitics/forward-recorder.md` for forward collection,
latency, quote-survival, and completeness semantics.

## 6. Running it unattended

`make paper`/`make live` run in the foreground. For a real deployment
(survives reboots, hourly state backups), see
`docs/geopolitics/deployment.md` — units are in `deploy/`.

## Where things live

| What | Where |
|---|---|
| The one config | `configs/geopolitics/discovery.yaml` |
| Secrets | `.env` (never committed) |
| All state (ledger, holdings, journals) | `data/` — back it up (`make backup`) |
| Generated per-market bot configs | `configs/geopolitics/generated/` |
| Logs + article archive | `logs/` |
| Full risk analysis | `docs/geopolitics/risk-register.md` |
