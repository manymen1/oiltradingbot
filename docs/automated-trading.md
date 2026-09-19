# Development toward automated oil trading

The first implementation milestone is a local paper execution engine. It consumes
recorded research candidates and market quotes in event-time order, without a
broker connection. It is an engineering foundation, not a validated profitable
strategy or an unattended live trading system.

## Run the paper replay

Install the project with `python -m pip install -e ".[dev]"` from the repository
root, then use a verified observation snapshot:

```bash
oilbot paper-replay \
  --manifest data/oil-review-001/manifest.json \
  --instrument MCL-2026-10 \
  --limits configs/oil/paper-limits.json \
  --out data/paper-review-001
```

The instrument must match one explicit contract in the snapshot. Use a new output
directory for each run. The example contract ID is historical and must be replaced
with the contract present in the selected dataset. The shipped observation demo
uses current news timestamps and historical quotes, so it may correctly produce
no fills; it is not a performance dataset. The integration tests include a
time-aligned synthetic snapshot that demonstrates submission, entry, and exit.

Outputs are `paper.sqlite3` (inputs, order/fill events, durable account state) and
`report.json` (assumptions, events, cash P&L, last marked equity, open exposure,
and source manifest identity). Unclosed positions and pending orders remain
visible when the dataset ends. Marked equity can be stale; its quote timestamp
is included. No liquidation or terminal profit is invented.

## Implemented behavior

- One explicit contract and at most one pending order or position per account.
- Research-candidate gating, fixed integer size, delayed limit entry, expiry of
  pending orders, spread/freshness checks, and session/contract expiry restrictions.
- Partial fills limited to displayed size; unfilled entry quantity is cancelled.
- Hypothetical fees and slippage, long/short accounting, stop-loss, take-profit,
  holding-period exits, daily marked-loss halt, and daily entry-count limit.
- `PaperEngine.process("halt", ...)` cancels pending entry and requests exit on
  the next usable quote. Halts never manufacture liquidity. Manual and sequence
  gap halts remain latched; there is no automatic resume command.
- Atomic input deduplication, journal/state updates, crash recovery, and refusal
  to reuse an account journal after changing instrument or risk assumptions.
- Quote sequence gaps halt the engine; the replay CLI rejects archives with
  recorded gaps. Absent or delayed quotes cannot create fills.

The numbers in `paper-limits.json` are synthetic test assumptions. Stops and daily
loss limits trigger exits; they do not guarantee a maximum realized loss when
prices gap or executable liquidity disappears. They are not live sizing advice.
The daily accounting boundary is UTC. A new day clears the daily-loss latch and
trade counter, but never clears a manual halt. Software exits require a running
engine and usable quotes; these are not broker-held protective orders.

## Implementation stages

1. **Current: signal correctness and local paper execution.** Separate incident
   links from merges, preserve adjudication provenance and initial-capture
   backfill, poll analysis within its deadline, and exercise execution faults.
   `src/oilbot` is now the single implementation; `polybot.oil` delegates to it.
2. **Market and broker integration.** Select the target instrument and broker.
   Implement contract discovery, account/entitlement checks, timestamped streaming
   quotes, sequence/reset semantics, reconnect recovery, and session calendars.
   Keep the event adapter independent of strategy and execution.
3. **Forward paper service.** Feed new decisions and quotes into the durable paper
   engine, add continuously running supervision, monitoring and clock ticks,
   expose halt/status controls, and compare simulated fills against broker paper
   fills. Broker paper orders require a separate adapter; none is implemented yet.
4. **Strategy validation.** Assemble time-aligned, rights-qualified news and market
   data; assign incident episodes; freeze strategy and evaluation rules; compare
   against the existing baselines after realistic costs on unseen periods.
   Candidate direction alone is not evidence of predictive value. Evaluate gaps,
   reversals, spread spikes, and corrections before selecting a strategy.
5. **Broker order management.** Add durable client order IDs, acknowledged versus
   uncertain submissions, broker reconciliation before retry, partial fills,
   cancellations, reconnect handling, broker-held protection, and startup checks
   for pre-existing positions. Duplicate delivery must never duplicate orders.
6. **Explicit live configuration.** Bind the selected account, capital/exposure
   limits, loss budget, allowed contracts, rollover policy and deployment host.
   Enable live routing only after the broker/risk fault tests and separate live
   activation. The observation configuration still rejects broker execution.

Unresolved choices are the target broker/instrument, market-data provider,
deployment host, and eventual live risk budget. No credentials, subscriptions,
remote account changes, live orders, or services are created by this milestone.

## Verification

```bash
python -m pytest -q
```

The suite includes synthetic long/short P&L, delayed fills, spread/stale-data
rejections, partial exits, deadline/session restrictions, gap/manual/loss halts,
transaction rollback, duplicate delivery, restart recovery, and a complete
snapshot-to-paper-report CLI exercise. No test calls a broker or model service.
