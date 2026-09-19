# polybot

**Want to just run it? See [QUICKSTART.md](QUICKSTART.md)** — `make setup`,
fill in `.env`, `make paper`. `make help` lists every command.

`polybot` now has two distinct product areas:

- geopolitics position bots under `polybot/iran/`, `polybot/location/`,
  `polybot/binary/`, and shared `polybot/core/` infrastructure.
  The location bot supports automated position ENTRY as well as protection:
  with `entry.enabled` and an empty `event.held_location` it starts flat,
  buys a configured entry target's YES when a trusted tier-one source
  confirms a qualifying senior round there, records the holding in
  `data_dir/holdings.json`, and then defends the entered leg with the same
  protection machinery (see `docs/geopolitics/location-bot-spec.md` and
  `configs/geopolitics/location-entry.example.yaml`).
  The legacy binary rule bot (`polybot/binary/`) handles occurrence-style
  single YES/NO markets: paste the market's verbatim resolution rules into
  config and the classifier judges every article strictly against them
  (see `configs/geopolitics/binary-entry.example.yaml`). It is not the
  generic executor for every possible geopolitical rule;
- a market-first discovery pipeline under `polybot/discovery/` that
  enumerates the live Polymarket universe, compiles verbatim rules into
  immutable two-pass `RuleSpec` contracts, grades tradeability and ambiguity,
  derives role-aware per-market source plans, extracts rule-bound
  `EvidenceClaim` facts with two-pass agreement, evaluates those facts with
  deterministic family code, and records self-contained `DecisionProof`s.
  Its generic market runner can enter or protect simulated positions for the
  first three paper-execution families; it has no live execution surface. A
  point-in-time replay interleaves archived evidence, executable depth, and
  resolutions, while the family-specific promotion report gates classifier
  agreement, proof completeness, quote latency, dynamic per-market fees, CLV,
  human-labelled terminal precision, stressed P&L, and clustered net EV
  without changing live configuration. Deadline silence never produces a
  tradeable terminal-NO state, and incompatible model/prompt/evaluator/source/
  fee/execution policies cannot be pooled for promotion.
  Its profit-first forward recorder uses sharded persistent public market
  WebSockets, full-depth delta reconstruction, exact first-seen articles,
  end-to-end decision latency, 100ms-10s quote-survival sampling, automatic
  resolution capture, completeness audits, and content-addressed replay
  timelines. Audited keyset enumeration covers the full active universe;
  conservative point-in-time profit scores allocate monitoring and classifier
  resources while retaining a cold-start exploration quota. A reconciled
  funnel attributes every loss from discovery through stressed resolved P&L.
  Centralized direct publisher polling, a strict zero-model-call evidence
  envelope, staged source-to-submission latency, and immutable public trade
  prints support the next paper-only economics campaign.
  Ranking grants no execution permission and the path remains paper-only with
  no authenticated order method (see
  `docs/geopolitics/profit-priority.md` and
  `docs/geopolitics/fast-source-lane.md`).
  The separate probability scanner still prices estimates against executable
  quotes, and the pipeline can emit legacy executor configs during migration
  (see `docs/geopolitics/market-first-pipeline.md` and
  `configs/discovery/geopolitics.example.yaml`);
- TypeScript valuation strategy tooling under `src/valuation/strategy/` and
  `src/valuation/cli.ts`.

Domain configs live under `configs/geopolitics/` and `configs/valuation/`.
Operational geopolitics wrappers live under `scripts/geopolitics/`, systemd
units under `services/`, and shared Polymarket TypeScript bridges under
`tools/polymarket-ts/`.

Autonomous entry is confirmation-only: full source text, agreeing structured
facts, deterministic rule evaluation, positive execution-adjusted edge,
verified market mappings, and reconciled holdings are all required. The
rules-first path currently executes through the persistent depth-aware paper
broker only. Existing live holdings continue to use the legacy binary/location
executors, which rebuild balances from chain state on startup and every cycle.

Optional anticipatory forecasting is implemented as a deterministic paper-only
layer: configured priors plus source/evidence likelihoods produce normalized
outcome probabilities, deduplicated observations, and simulated entry/exit
markouts. The classifier explicitly separates a forecast claim's target from
its direction, so denials reduce the named outcome instead of reinforcing it.
Dry-run forecasting uses public live books through a quote-only adapter,
rejects stale/wide quotes, simulates costs, and evaluates exits every polling
cycle. It cannot submit an exchange order.

After an event resolves, score its stored probability path with:

```bash
python -m polybot.geopolitics evaluate-location-forecast \
  --config configs/geopolitics/<event>.yaml \
  --resolved-outcome qatar
```

See `docs/valuation/runbook.md` for valuation commands and live-readiness
checks.

## Install

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Verified locally:

- `py-clob-client==0.34.6`
- `ClobClient(host, chain_id, key, creds, signature_type, funder)`
- `create_market_order(MarketOrderArgs, PartialCreateOrderOptions)`
- `post_order(order, OrderType.FAK)`
- `get_balance_allowance(BalanceAllowanceParams(...))`

Important: rechecked against current Polymarket trading docs on 2026-07-06.
The live Python path still uses `py-clob-client==0.34.6`; its public GitHub
repo is archived, so keep treating it as legacy. Its FAK market-order usage
matches current docs: FOK/FAK execute immediately against resting liquidity,
BUY amounts are dollars, and SELL amounts are shares. Prefer the maintained
`@polymarket/clob-client-v2` path for new execution work once the
deposit-wallet signer/API-key mismatch is resolved.

## Environment

Dry-run is the default:

```bash
POLYBOT_DRY_RUN=1
```

Live Iran execution requires all of:

1. `execution.dry_run: false` in the Iran YAML config
2. `python -m polybot.geopolitics run-iran --config <config.yaml> --live`
3. valid Polymarket credentials

The Iran classifier now calls the Anthropic API (`classifier.provider: anthropic`,
model `claude-opus-4-8`, two passes with required agreement and structured JSON
output). It needs a key in the bot's process environment — note the Python bot
does not read `.env`, so export it in the shell or run wrapper:

```bash
ANTHROPIC_API_KEY=
```

If the key is missing or the API is down, every escalated article degrades to
`ALERT_ONLY` and no trade is placed (`classifier.if_api_down`).

Trading-related variables:

```bash
POLYBOT_PRIVATE_KEY=
POLYBOT_CLOB_API_KEY=
POLYBOT_CLOB_SECRET=
POLYBOT_CLOB_PASSPHRASE=
POLYBOT_SIGNATURE_TYPE=
POLYBOT_FUNDER_ADDRESS=
```

Shared aliases are also accepted for compatibility:

```bash
PRIVATE_KEY=
CLOB_API_KEY=
CLOB_SECRET=
CLOB_PASS_PHRASE=
DEPOSIT_WALLET_ADDRESS=
FUNDER_ADDRESS=
CHAIN_ID=
CLOB_HOST=
```

Supported signature types are:

- `0` for EOA
- `1` for Polymarket proxy/Magic wallet
- `2` for browser-wallet Gnosis Safe proxy
- `3` for Polymarket deposit wallet / `POLY_1271`

If `DEPOSIT_WALLET_ADDRESS` is set and no explicit signature type is provided,
the bot defaults to `3`. If only `FUNDER_ADDRESS`/`POLYBOT_FUNDER_ADDRESS` is
set, it defaults to `1`.

## Commands

Inspect a Polymarket event:

```bash
.venv/bin/python -m polybot.main inspect <event-slug>
```

Inspect and verify an Iran config:

```bash
.venv/bin/python -m polybot.geopolitics inspect-iran --config configs/geopolitics/iran-july17-yes-protection.yaml
```

Preflight live readiness, including operator mode, config hash ack, credentials,
token mapping, and live balances:

```bash
.venv/bin/python -m polybot.geopolitics preflight-iran --config configs/geopolitics/iran-july17-yes-protection.yaml --live
```

Set the current position mode and acknowledge the exact config hash before live:

```bash
.venv/bin/python -m polybot.geopolitics set-iran-mode --config configs/geopolitics/iran-july17-yes-protection.yaml --mode live
.venv/bin/python -m polybot.geopolitics ack-iran-live --config configs/geopolitics/iran-july17-yes-protection.yaml --note "reviewed live config"
```

Read a portfolio-style position snapshot:

```bash
.venv/bin/python -m polybot.main positions --config configs/geopolitics/positions.example.yaml
.venv/bin/python -m polybot.main inspect-position iran-july17-yes --config configs/geopolitics/positions.example.yaml
```

Probe the TypeScript `clob-client-v2` deposit-wallet path without posting:

```bash
.venv/bin/python -m polybot.geopolitics probe-iran-clob-v2 --config configs/geopolitics/iran-july17-yes-protection.yaml --amount 5
```

Smoke the configured classifier without executing:

```bash
.venv/bin/python -m polybot.geopolitics smoke-iran-classifier --config configs/geopolitics/iran-july17-yes-protection.yaml --text "Reuters reports senior US and Iranian representatives scheduled a formal round of talks for July 14."
```

Live runs default to the Python CLOB adapter. The legacy TypeScript
`clob-client-v2` backend is still available for diagnostics, but the posted
probe exposed a Polymarket-side deposit-wallet signer/API-key mismatch:

```bash
POLYBOT_EXECUTION_BACKEND=clob_v2
```

The official beta SDK backend is also wired:

```bash
POLYBOT_EXECUTION_BACKEND=polymarket_beta
```

It requires Node 24. The bot has been migrated to the beta-derived deposit
wallet `0xf9021f4aa0cec3059a6b1da1083a68c9dc5fa267`, which is where on-chain
reconciliation shows the July 17 YES shares. The bridge refuses balance queries
and orders if `DEPOSIT_WALLET_ADDRESS` differs from the beta SDK's derived
wallet, so it cannot silently trade the wrong account.

Run the Iran bot:

```bash
.venv/bin/python -m polybot.geopolitics run-iran --config configs/geopolitics/iran-july17-yes-protection.yaml
```

Run live only after config and credential review:

```bash
.venv/bin/python -m polybot.geopolitics run-iran --config configs/geopolitics/iran-july17-yes-protection.yaml --live
```

## Iran Configs

- `configs/geopolitics/iran-july17-yes-protection.yaml`: protects a YES position on the July 17
  peace-talks leg.
- `configs/geopolitics/positions.example.yaml`: read-only portfolio snapshot config. It does not
  authorize live trading; operator mode files and config-hash acks still gate
  execution.

`sources.poll_urls` is for fixed, execution-grade article URLs. `sources.feed_urls`
is for discovery-grade RSS/Atom feeds. Feed items are processed through the same
keyword gate and classifier, but `allow_feed_auto_trade: false` keeps them from
placing trades by default. A trusted feed item can still record a scheduled-round
hold signal and pause a blind July 17 YES time-decay sale. If a feed item can be
resolved to a publisher URL but the full article fetch fails, it remains
`promoted_feed_summary`: useful for alerts/hold signals, never auto-trade.

## Safety Model

The Iran bot is a state machine, not a market-making system. It writes explicit
states below the configured `data_dir`, with dry-run state isolated under
`data_dir/dry_run`.

Important states include:

- `TRIGGER_DETECTED`
- `CANCELING_ORDERS`
- `SELLING_NO`
- `SELLING_YES`
- `NO_SOLD`
- `BUYING_YES`
- `BUYING_NO`
- `FLIPPED`
- `EXITED`
- `FLIP_INCOMPLETE`
- `NO_POSITION_UNCONFIRMED`
- `YES_POSITION_UNCONFIRMED`
- `TIME_DECAY_PRICE_FLOOR`
- `EXECUTION_ERROR`

Live execution cancels open market orders before sizing when
`safety.cancel_open_orders_first: true`. Transient zero-balance reads are
nonterminal, and unexpected execution exceptions write `EXECUTION_ERROR` and
keep the polling process alive.

The operator gate blocks live execution unless the position mode is `live` and
the current config hash has been acknowledged. The bot re-reads the mode files
every polling cycle; write `off` or `alert_only` to
`data/operator/positions/<config-stem>.mode` to stop live execution mid-run.
Live preflight also blocks missing Telegram credentials when degraded alerts are
required, and missing `ANTHROPIC_API_KEY`/`LLM_API_KEY` when the configured
classifier provider is `anthropic`.

The July 17 YES config includes calendar-decay brakes:

- trusted scheduled-round hold signals suspend time-decay selling temporarily
- `time_decay.min_trim_price` prevents dumping trims below a floor
- `time_decay.min_exit_price` prevents dumping full exits below a floor
- Reuters/AP discovery uses Google News RSS because stable public RSS is not
  available for those sources
- State Department discovery uses official State RSS feeds and safely no-ops in
  environments where State returns an HTML error page instead of RSS

IRNA is alert-only by default. It can surface information, but a single IRNA
item should not auto-trade.

## Logs

Primary logs are JSONL under `logs/`. Iran decisions are written to
`logs/decisions.jsonl`, article hashes to `logs/articles.jsonl`, and shared
runtime events to `logs/polybot.jsonl`.

Every log event is intended to explain why the bot acted or skipped.

## Go-Live Checklist

- Run the tests: `.venv/bin/python -m pytest -q -s`.
- Run `inspect-iran` and manually verify question, rule text, token IDs,
  `condition_id`, `tick_size`, `neg_risk`, and `accepting_orders`.
- Pin and review the rule-text SHA256.
- Add real `sources.poll_urls` if relying on news protection.
- Verify Polymarket access and legality in your jurisdiction.
- Confirm position caps match the actual position size you are willing to sell.
- Run at least one complete dry-run session and inspect state/log output.
- Fund only an amount you can lose entirely.

## Out Of Scope

No weather markets, no market making, no automatic position-size increases, no
generic sports/crypto/social-media adapters, and no UI.

## Oil observation pilot

The separate `polybot.oil` application captures oil news revisions, bounded Codex
extraction, incident history, fixture CL/MCL data and immutable replay reports.
It cannot submit orders. Start with `make oil-preflight` and the
[oil pilot guide](docs/oil-observation.md). Live market data, source processing
qualification and economic evidence remain explicit gates; no subscription or
legacy bot configuration is changed by the pilot.
