# Market-First Discovery Pipeline

The protection/entry bots are execution kernels: they assume an operator
already chose a market and hand-filled its outcomes, token IDs, and rules.
`polybot/discovery/` inverts that: markets come first, and the executors are
the final component.

```
discover geopolitical markets
  -> read context and resolution rules
  -> compile a validated, instrument-bound RuleSpec twice
  -> derive source roles and score family eligibility
  -> extract rule-bound evidence facts twice
  -> evaluate facts with deterministic family code
  -> price and allocate a paper-only trade intent
  -> retain a complete decision proof
```

## Stages and commands

All commands take `--config configs/discovery/<name>.yaml`; all state lives
under the config's `data_dir` as atomic JSON records.

| Stage | Command | What it does |
| --- | --- | --- |
| 1. Universe | `discover-markets` | Enumerates the complete active Gamma universe through audited keyset pagination across liquidity, volume, creation, and deadline views. It records cursors, page hashes, payload conflicts, coverage status, and all early rejection reasons before building durable `MarketContext` records. A repeated cursor/page, malformed response, fetch failure, safety limit, or explicit cap marks coverage incomplete; unseen markets are never closed from that scan. |
| 2. Descriptive context | `grade-markets` (analysis step) | The legacy rule analyzer reads the verbatim rules and records ambiguity, parties, mediators, and decisive vocabulary. `RuleAnalysis` remains descriptive and cannot authorize execution. |
| 3. Rule contract | `compile-rules` | Two independent passes compile semantic fields and must agree. Deterministic code binds the market, condition, outcomes, and token IDs. A validated immutable `RuleSpec` records the closed rule family, predicate, time window, qualifiers, exclusions, source requirements, resolution behavior, and independent-confirmation count. Raw passes and diagnostics remain inspectable in WAL SQLite. A separately allowlisted `prepare-rule-review` / `import-reviewed-rule` path can resolve a real disagreement only through an exact operator-approved hash and immutable audit record; reviewed-only specs remain paper-only. |
| 4. Sources | `plan-sources` | Derives sources from the exact current `RuleSpec`: official predicate actors, named settlement sources, confirmation/context publishers, adapters, publication-time quality, and canonical syndication identity. Missing required sources or a stale spec/plan fails closed. |
| 5. Grading | `grade-markets` (scoring step) | Applies the closed family registry, source capability, rule freshness, clarity, liquidity, spread, horizon, resolution risk, and correlation gates. Supported but unpromoted families are paper-only; subjective/discretionary families are monitor-only. |
| 6. Evidence | `run-rule-market` | Promotes full publisher text, binds publisher identity and source roles in code, then runs two structured extraction passes. The model may report only article facts, exact supporting text, and affected rule clauses; it cannot assign an evidence state, probability, token, or trade action. Disagreement, fabricated quotes, stale rule/source bindings, or classifier-budget failure fails closed. |
| 7. Evaluation | (inside rule runner) | A closed registry evaluates occurrence, categorical, source-locked, status-at-deadline, numeric-threshold, and duration contracts. Deterministic code applies time windows, named settlement-source requirements, exclusions, comparison/duration semantics, and syndication-aware independent-confirmation counts. |
| 8. Confirmation decision | (inside rule runner) | Only terminal evaluations can create a confirmation trade intent. The engine evaluates both YES and NO positions against public depth, fee/slippage/resolution/uncertainty buffers, and the shared portfolio ledger. Opposite holdings are protected before new exposure is considered. |
| 9. Proof | `inspect-rule-market` | Every evaluation and action—including blocked and hold decisions—persists with the exact rule, source-plan, evaluation, intent, and claim hashes; article IDs, source domains, independence groups, supporting quotes, satisfied/violated clauses, executable prices, buffers, allocation, blockers, and paper result are retained. |
| 10. Forecast opportunity | `scan-opportunities` | Separately, for each eligible outcome: `edge = estimated_probability − executable ask − slippage − resolution-risk buffer − model-uncertainty buffer`, must clear `min_edge`. Probability estimates come from operator config or promoted forecast state; an outcome without an estimate is reported, never traded. |
| 11. Allocation | (inside scan/runner) | The `PortfolioAllocator` previews per-order, per-market, per-event, per-correlation-group, per-deadline-week, daily, and total caps plus a simultaneous-position limit, persisted in `allocations.json`. Discovering more markets must not multiply correlated risk. |
| 12. Legacy handoff | `emit-bot-config` | Renders a ready-to-review legacy executor config with both rule-text and source-plan identity pinned. Live mode continues to use the existing binary/location executors during migration; the generic rule runner is structurally paper-only. |
| 13. Measurement | `funnel-report` | all → compiled → sourced → observable → eligible → mispriced → executable, plus family/state counts, stale-plan blockers, runner health, and current portfolio exposure. |
| 14. Historical replay | `replay-rule-market` | Replays a strictly ordered evidence/book/resolution JSONL timeline through the same extractor, deterministic evaluator, decision engine, depth-aware paper broker, and proof store. Replay time replaces wall-clock time; future-dated evidence, crossed books, and unresolved open P&L fail closed. |
| 15. Promotion | `rule-promotion-report` | Aggregates content-hashed replay summaries by rule family and event cluster. Reports two-pass agreement, parser errors, proof completeness, executable-price availability, 1m/5m/30m CLV, duplicate entries, cost-adjusted P&L, and a deterministic clustered-bootstrap EV lower bound. A PASS is review-only and changes no live setting. |
| 16. Profit priority | `priority-report` | Uses only compatible point-in-time forward/frozen evidence to rank scarce monitoring and classifier resources. Cold-start markets receive an explicit exploration quota. Liquidity is a fill-cap input, never an alpha proxy, and a priority score cannot authorize execution. |
| 17. Profit funnel | `profit-funnel` | Reconciles the confirmation strategy from enumerated events through rules, sources, terminal evidence, measured-latency quote survival, stressed fills, and resolved after-cost P&L. Every stage reports an eligible denominator and mutually exclusive primary loss reasons. |

## Market states

- `DISCOVERED` — matched the geopolitical filter; no context analysis yet
- `RULES_REVIEW_REQUIRED` — missing/short rule text, unverified token mapping,
  compiler disagreement/failure, a deferred compilation, or a changed rule hash
- `PAPER_ELIGIBLE` — understandable and observable, but a live gate failed (liquidity, spread, horizon, resolution risk, correlation limit)
- `LIVE_CONFIRMATION_ELIGIBLE` — every live gate passed; may be emitted for the confirmed-entry executor path
- `MONITOR_ONLY` — discretionary/ambiguous rules or unobservable evidence
- `REJECTED` — not geopolitical / excluded vocabulary
- `CLOSED` — resolved, closed, past deadline, or not accepting orders

## Hard safety rules

- Missing full resolution text or current validated `RuleSpec` →
  `RULES_REVIEW_REQUIRED`, never tradeable.
- The compiler cannot supply or alter market, condition, outcome, or token IDs;
  deterministic discovery context binds all instruments.
- Compiler passes must agree semantically. Cached specs are keyed by exact rule
  hash and cannot survive a rule-text change.
- Discretionary or unclear rules → `MONITOR_ONLY`.
- Changed rule hash → analysis dropped, market demoted, emitted configs fail
  closed on their pinned SHA-256.
- Unverified token mapping → `RULES_REVIEW_REQUIRED`.
- No source plan → `emit-bot-config` refuses; a plan built for an older
  rule-text or `RuleSpec` version is also refused.
- An unresolved required named settlement source → `MONITOR_ONLY`.
- Syndicated copies share their canonical publisher identity: two Reuters
  mirrors, for example, cannot satisfy a two-source confirmation requirement.
- Evidence extraction may describe facts only. Market identity, source roles,
  terminal state, probability, token selection, and trade action are outside
  the model schema and are bound or computed deterministically.
- Terminal article evidence requires an authorized source role, a usable
  publication/event timestamp, and the configured number of independent
  confirmation groups. The same constraints apply to terminal foreclosure.
- The generic runner rejects `--live` and also refuses a discovery config whose
  fleet position mode is `live`.
- No rule family is live-promoted by default. A supported family remains
  `PAPER_ELIGIBLE` until explicitly promoted after replay and paper evidence.
- Only occurrence-before-deadline, categorical-exclusive, and source-locked
  announcement families are paper-execution enabled in production config.
  Status, numeric, and duration evaluators still record proofs, but their
  intents contain an explicit execution-disabled blocker.
- Excessive spread, price above cap, unknown quotes, or edge below minimum →
  blocked at scan time with the reason recorded.
- Allocator caps exceeded → blocked; commits are atomic and fail closed on a
  corrupt ledger.
- Emitted configs are always dry-run with the operator gate at its default
  (`alert_only`) — the standard inspect/preflight/ack/soak sequence from
  `autonomous-entry-hardening.md` still governs live arming.

## Profit levers

The confirmed-entry strategy's economics are dominated by latency, classifier
cost, and market selection. Four levers target them directly:

- **Small-size live tier**: thin markets are where confirmation edge persists
  longest (nobody competes for $20 of edge). When liquidity is the ONLY failed
  live gate, the market stays `LIVE_CONFIRMATION_ELIGIBLE` with
  `recommended_max_order_usd = liquidity * small_live_liquidity_fraction`;
  opportunity scans and emitted configs size orders to what the book can
  absorb instead of demoting the market to paper.
- **Screen classifier tier** (`classifier.screen_model`): every escalated
  article is first classified once by a cheap fast model; the expensive
  trade-grade model (with pass agreement) only runs when the screen sees
  anything other than NO_ACTION. Most escalated articles are noise, so this
  cuts the dominant classifier cost and answers faster on noise. A screen
  failure escalates rather than blocks, and the location bot still feeds
  screen signals to the paper forecast engine so priors keep updating.
- **Armed fast polling** (`safety.armed_poll_seconds`): live bots poll at
  seconds, not tens of seconds -- the race is lost in the gap between
  publication and the next cycle. Emitted configs default to 2s live / 30s
  dry-run.
- **Direct publisher feeds**: source plans now lead with direct RSS endpoints
  (state.gov press releases, UN news, Al Jazeera) ahead of Google News
  queries, whose indexing lag is often 5-15 minutes.
- **Fast ingestion**: all sources are fetched concurrently (cycle wall-time =
  slowest feed, not the sum) with conditional GETs (ETag/If-Modified-Since),
  so an unchanged feed costs a ~50ms 304 and second-scale polling is
  affordable. Trade-grade confirm passes also run concurrently.

## Fleet mode: the whole-universe autopilot

`run-fleet` is one supervisor for ALL geopolitical markets. Each cycle it:

1. runs the full discovery cycle (discover → analyze → compile rules → derive
   role-aware sources → final grade → scan);
2. monitors every eligible paper market when uncapped; when worker slots are
   scarce, assigns them by compatible forward-evidence profit priority plus a
   rotating cold-start exploration quota (`fleet.max_bots <= 0` = uncapped),
   re-emitting only when the rule hash or dry-run mode changed so the ack
   hash doesn't churn;
3. arms each market's operator gate with `fleet.position_mode` and — with
   `--live`, `position_mode: live`, and `auto_ack: true` — writes the config
   ack, so the operator arms the fleet once instead of each market;
4. supervises one bot subprocess per market (crashed bots restart next cycle);
5. stops flat bots for demoted/closed markets — but a bot defending a HELD
   position is never stopped by a grading change.

All bots share `data/geopolitics/operator/`, so `set-fleet-mode off` is a
single master kill switch that every executor obeys mid-cycle, and the shared
portfolio ledger caps total exposure regardless of how many bots run.
`services/geopolitics-fleet.service` deploys it (KillMode=control-group takes
the supervised bots down with the fleet).

Classifier calls use the same deterministic split: 70% exploitation, 20%
exploration, and 10% system work by default. Whole two-pass groups are
reserved atomically in shared SQLite. Allocation plans are immutable for an
hour, retries use stable reservation identities, and every admission or denial
records the market, purpose, score hash, requested calls, and reason. See
`profit-priority.md`.

## Recurring operation

`run-discovery` runs the full cycle (discover → analyze → compile rules →
plan sources → final grade → scan)
on `schedule.interval_minutes`, alerting via Telegram whenever a market newly
becomes `LIVE_CONFIRMATION_ELIGIBLE` or an opportunity newly clears every
gate. `run-discovery --once` performs a single cycle for cron/systemd timers.
Cycle diffs persist in `pipeline_state.json`; failures log and wait for the
next cycle instead of killing the loop.

## Shared portfolio ledger

The allocator persists its caps into `allocations.json` (the ledger) on every
pipeline run. Emitted executor configs carry a `portfolio:` section binding
them to that ledger, and both the location and binary executors then:

- clamp every entry/rotation/flip buy by the ledger preview **in addition to**
  their own risk budgets and guardrails (any ledger problem fails closed);
- debit the ledger on order attempt (reserve-on-attempt, matching RiskState
  semantics: an unfilled order still consumed allowance);
- free the simultaneous-position slot when the position closes (exit,
  incomplete rotation/flip, or wallet reconciliation to flat).

Hand-written configs without a `portfolio:` section are unaffected.

## Probability sources for the scan

`scan-opportunities` prices an outcome only when it has a probability
estimate, in priority order: fresh paper-forecast state
(`forecast_probability.json` written by the location bot's forecast engine
under the emitted config's data dir), then operator-supplied
`opportunity.probability_estimates`. Stale forecast state (older than
`forecast_max_age_hours`) is ignored, and an outcome with no estimate is
reported as `no_probability_estimate` -- never traded.

## What stays manual (deliberately)

- Probability estimates for the opportunity scan are operator- or
  forecast-supplied; the pipeline never invents them.
- New rule families remain paper-only until their deterministic evaluator,
  evidence extraction, replay corpus, and promotion evidence are complete.
- Emitted configs are reviewed before running: entry target lists should be
  trimmed, sources sanity-checked, and the rule text read by a human once.
- Live arming still requires the config-hash ack and position-mode flip.
