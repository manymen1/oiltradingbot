# Rules-first replay and promotion

The rules-first replay is a point-in-time simulation of one compiled market.
It interleaves only information that was available at each event timestamp:

- full-text articles as first fetched;
- executable YES and NO order-book depth;
- explicit final resolutions.

It uses the current immutable `RuleSpec` and source plan for the selected
market, but writes claims, evaluations, proofs, broker balances, and allocator
state into a content-addressed replay directory. It never reads credentials and
the generic runner has no live-order surface.

For unbiased forward inputs, use the automated recorder described in
`docs/geopolitics/forward-recorder.md`. It captures first-seen full text,
shared WebSocket books, decision-time latency, quote survival, and explicit
resolutions, then emits this timeline format without manual event selection.

## Timeline format

The input is JSONL in nondecreasing timestamp order. Timestamps must include a
timezone. Unknown fields, duplicate article hashes, crossed books, conflicting
resolutions, and articles exposed before their `published_at` or `fetched_at`
timestamp are rejected.

```json
{"type":"BOOK","at":"2026-07-25T10:00:00Z","outcome":"yes","yes":{"bids":[[0.78,100]],"asks":[[0.80,100]]},"no":{"bids":[[0.20,100]],"asks":[[0.22,100]]}}
{"type":"ARTICLE","at":"2026-07-25T10:00:02Z","article":{"url":"https://www.reuters.com/example","domain":"reuters.com","title":"Talks began","published_at":"2026-07-25T10:00:01Z","fetched_at":"2026-07-25T10:00:02Z","raw_text":"Both senior delegations entered the room and talks began.","hash":"publisher-content-hash","source_kind":"article"}}
{"type":"RESOLUTION","at":"2026-09-30T23:59:59Z","outcome":"yes","resolved_yes":true}
```

`outcome` is the normalized outcome name in `inspect-rule`. Both token books
are required. Each level is `[price, shares]`; prices must be strictly between
zero and one and sizes must be positive.

Run:

```bash
make replay-rule-market \
  MARKET=<market-id> \
  TIMELINE=<events.jsonl> \
  DATASET_ROLE=development
```

`DATASET_ROLE` must be `development`, `frozen_oos`, or `forward`. Development
runs are diagnostic and can never pass promotion. Once collection starts for a
frozen out-of-sample set, do not change the model, prompt, evaluator, source
adapter, fee policy, execution policy, or thresholds. Forward runs represent
time-ordered paper observations collected without hindsight.

Human-reviewed terminal labels are supplied as a strict JSON array:

```json
[
  {
    "evaluation_sha256": "<evaluation hash>",
    "expected_state": "TERMINAL_YES",
    "labeler_id": "reviewer-01",
    "rationale": "The named settlement source explicitly confirmed the predicate."
  }
]
```

Run a labelled frozen replay with:

```bash
make replay-rule-market \
  MARKET=<market-id> \
  TIMELINE=<events.jsonl> \
  DATASET_ROLE=frozen_oos \
  LABELS=<labels.json>
```

The result is stored under:

```text
<data_dir>/rule_replays/<market>/<content-addressed-run>/summary.json
```

The run identity binds the exact timeline, labels, dataset role, `RuleSpec`,
classifier/model, prompt and evaluator versions, source-adapter policy,
per-market fee curve, confirmation thresholds, slippage policy, book-age
limit, and allocator caps. Repeating unchanged input uses the same run identity
and produces the same result hash; changing a trading assumption creates a
different run instead of overwriting the old evidence. Replay time is passed
through article-age checks, fee-schedule freshness, extraction, evaluation,
intent, proof, deadline, book-age, and markout logic; wall-clock time cannot
change the trading result. An already-complete identical run is hash-verified
and reused; an incomplete run directory is rebuilt, while a corrupt or stale
cached summary fails closed.

Market fees are not a global flat basis-point assumption. Each discovered
outcome carries its observed Gamma `feeSchedule`, including an explicit
`feesEnabled=false` zero-fee declaration. Entry is blocked when that metadata
is missing, malformed, future-dated, stale, or incompatible with immediate
taker execution. The paper broker applies the nonlinear fee curve per fill
with exchange-style rounding, and each decision proof binds the fee-policy
version, schedule, and schedule hash.

## What the replay measures

Each summary records:

- two-pass extraction agreement, disagreement, and invalid-output rates;
- every evidence state, blocker, intent, fill, and complete decision proof;
- terminal opportunities with or without a fresh executable quote;
- confirmation prices that clear all configured buffers and minimum edge;
- partial depth-aware fills, fees, slippage, and duplicate-entry violations;
- executable-bid CLV near 1, 5, and 30 minutes (late observations outside the
  bounded markout window are missing, not silently treated as on-time);
- cost-adjusted P&L from actual paper cash flows and explicit resolutions;
- one-cent and two-cent adverse-execution stress P&L;
- human-label agreement and false-terminal, settlement-source,
  deadline-silence, and fee-schedule safety violations;
- a policy partition hash that prevents incompatible models, prompts,
  evaluators, source adapters, fee policies, or execution policies from being
  pooled.

If an open token has no resolution event, cost-adjusted P&L is `null`; the
report never marks an unresolved position at an assumed price.

An occurrence deadline passing without a qualifying explicit resolution claim
is `AMBIGUOUS`, not terminal NO. A local feed's silence is never tradeable
evidence.

## Promotion report

Aggregate one summary or a directory of summaries:

```bash
make promotion-report RUNS=<summary-or-directory>
```

The report evaluates every rule family independently. Multiple markets or
timeline variants sharing one `event_slug` are one statistical cluster, so a
ladder of correlated contracts cannot manufacture sample size.

Only `frozen_oos` and `forward` datasets are eligible for PASS. All summaries
in a family report must have the same policy partition. Default PASS gates
require:

| Gate | Default |
|---|---:|
| Replay runs | 30 |
| Independent event clusters | 30 |
| Evidence articles | 40 |
| Two-pass extraction agreement | at least 98% |
| Invalid extraction rate | at most 2% |
| Proof completeness | 100% |
| Terminal opportunities | 20 |
| Fresh quote availability | at least 25% |
| Positive cost-adjusted edge opportunities | 5 |
| Resolved event clusters | 30 |
| Five-minute CLV samples | 10 |
| Independent events with five-minute CLV | 20 |
| Human-labelled terminal decisions | 100 |
| Human-label disagreements | 0 |
| Conflicting labels for one evaluation | 0 |
| False terminal actions | 0 |
| Settlement-source violations | 0 |
| Deadline-silence NO actions | 0 |
| Fee-schedule violations | 0 |
| Mean five-minute net CLV | at least 0 |
| Total cost-adjusted P&L | strictly above $0 |
| P&L after one-cent adverse execution shock | strictly above $0 |
| 95% clustered bootstrap EV lower bound | strictly above $0 |
| Duplicate entries | 0 |

Only occurrence-before-deadline, categorical-exclusive, and source-locked
announcement families can pass the confirmation-execution-family gate.
Status, numeric, and duration families remain evaluation-only.

A PASS means eligible for manual canary review. The report is read-only: it
does not edit `live_confirmation_families`, change fleet mode, acknowledge a
configuration, or enable live execution.

Human-label sample size is based on unique `evaluation_sha256` values.
Replaying the same labelled evaluation cannot increase the count, and
conflicting expected states for one evaluation fail the report.

For campaign-wide loss attribution, `make profit-funnel` keeps replay
economics partitioned by rule family, source adapter, and policy hash. Only
unique `forward` or `frozen_oos` replay runs enter the resolved/P&L stages;
development runs and unresolved positions cannot inflate profitability.
