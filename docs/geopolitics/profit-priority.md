# Profit-priority allocation and funnel

This layer answers a resource question, not a trading question:

> Given finite publisher polling, WebSocket, worker, and classifier capacity,
> which markets should receive attention while the strategy collects honest
> forward evidence?

A rank cannot create an entry intent, change a `RuleSpec`, promote a rule
family, or enable live execution. The generic rules-first runner remains
paper-only.

## Complete universe coverage

`discover-markets` enumerates active events through Gamma's keyset endpoint.
Production requests use opaque cursors and never combine keyset pagination
with offsets. Multiple ordered views reduce the chance that a single ranking
hides thin, new, or near-deadline markets:

- liquidity descending;
- volume descending;
- creation/start time descending;
- deadline ascending.

The coverage manifest records:

- endpoint and view parameters;
- every input and output cursor;
- page and event counts;
- stable event identities and first-seen payload hashes;
- cross-view payload conflicts without overwriting the first payload;
- fetch failures, malformed pages, repeated cursors/pages, and safety stops;
- whether an operator-configured event cap truncated enumeration;
- separate stable coverage and full-manifest hashes.

`max_events: 0` means uncapped. Any incomplete or truncated scan returns a
nonzero discovery status when `fail_on_truncation` is enabled. Existing
contexts remain available, but unseen contexts are not marked closed from an
incomplete scan.

## Conservative priority score

Each market gets a strict `ProfitPriorityRecord` from compatible, point-in-time
evidence. The high-level score is:

\[
S =
\lambda_{LCB}
\times q_{LCB}
\times f_{LCB}
\times N_{p25}
\times e_{LCB}
- C_{processing}
- R_{error}
\]

where:

- \(\lambda_{LCB}\) is a conservative terminal-opportunity rate;
- \(q_{LCB}\) is quote survival at measured end-to-end decision latency;
- \(f_{LCB}\) is stressed fill availability;
- \(N_{p25}\) is the 25th percentile of fillable stressed notional;
- \(e_{LCB}\) is net edge after market-specific fees and a one-cent shock;
- \(C_{processing}\) is observed or configured processing cost;
- \(R_{error}\) reserves for human-label errors, source violations, and the
  loss from a false terminal action.

No component is replaced with a favorable default. Missing samples create a
cold-start record and route the market to exploration. False terminal or
source-policy violations make exploitation ineligible. A p95 decision latency
beyond the longest sampled survival horizon is an integrity blocker.
Until monetary model billing is metered, the configured processing reserve is
applied per recorded model call and the record explicitly marks that cost as
unmeasured.

Liquidity and displayed depth limit fillable notional only. They are never an
assumed probability edge or a substitute for forward quote survival.

## Point-in-time identity

A priority record binds:

- market and event identity;
- current rule family and `RuleSpec`;
- current source-plan adapter identity;
- fee, evidence, and execution-policy partition;
- all observation hashes used by the score;
- component values, sample sizes, blockers, and policy version.

Unknown fields, missing fields, duplicate observation hashes, future cutoffs,
or an unreconstructable score hash fail closed. Database locations and report
generation timestamps do not create new strategy identities. Only compatible
forward or frozen out-of-sample evidence contributes replay economics.

The current snapshot is:

```text
data/discovery/profit_priority.json
```

Changed score identities are also retained under:

```text
data/discovery/profit_priority_history.jsonl
```

Inspect the current ranking:

```bash
make priority
```

The report prints blockers, component sample sizes, p95 decision latency, and
family-level median quote half-life. It does not claim profitability.

## Exploration and classifier allocation

Production classifier capacity is divided by purpose:

```text
70% exploitation
20% exploration
10% system work
```

Exploitation is sorted by descending conservative priority. Exploration
rotates cold-start markets deterministically. System capacity is reserved for
rule compilation and other required control work.

The shared SQLite budget store:

- reserves a whole two-pass call group atomically;
- allocates only complete groups;
- writes one immutable plan per purpose and UTC hour;
- defers changed rankings until the next hour;
- gives retries stable reservation identities so they cannot double-count;
- prevents unallocated markets from consuming a ranked bucket;
- preserves fleet-wide hourly, daily, and error caps;
- records admitted and denied calls with purpose and score identity.

A tiny fleet does not become exploration-only: exploration slots are carved
out only when the configured worker cap can support them alongside
exploitation. With `max_bots <= 0`, all eligible paper markets remain
observable and priority controls scarce classifier work instead of hiding
markets.

## Reconciled profit funnel

Run:

```bash
make profit-funnel
```

The report moves through:

```text
active events enumerated
→ geopolitical candidates
→ parseable market contexts
→ current RuleSpecs
→ current source plans
→ objective supported families
→ forward sessions
→ terminal evidence
→ human-correct terminal decisions
→ fresh executable quotes
→ quotes surviving measured p95 latency
→ stressed simulated fills
→ resolved independent event clusters
→ positive after-cost trades
→ positive after one-cent execution shock
```

Every stage has one unit, an eligible denominator, a numerator, and mutually
exclusive primary loss reasons whose counts reconcile exactly. Repeated
observations are reported separately from market/event conversions.
Unresolved P&L is missing, not marked to an assumed price. Disconnected or
late quote samples are excluded. Model-pricing experiments remain in a
separate funnel.

Results are partitioned by rule family, source adapter, and policy hash so
incompatible campaigns cannot be pooled.

## Operator interpretation

Use the outputs in this order:

1. Check coverage is complete.
2. Inspect loss reasons in the profit funnel.
3. Compare publisher-to-submission p95 latency with quote half-life.
4. Allocate direct-source engineering to families where surviving quotes
   still exist.
5. Collect frozen forward samples until the promotion gates in
   `rules-first-replay.md` are met.

If correct terminal evidence consistently arrives after quote half-life, the
taker-confirmation edge is absent at that latency. Raising rank, weakening
price limits, or adding capital does not repair it.

The paper-only fast-source tranche now records direct-source stage latency,
strict deterministic-adapter results, and public trade prints. It still does
not infer maker fills or add authenticated execution. See
`fast-source-lane.md`.
