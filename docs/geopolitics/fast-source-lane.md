# Fast source and latency lane

This tranche is a paper-only measurement path. It reduces news-discovery
latency and records the data required to decide whether terminal-confirmation
trading is economically viable. It does not add maker orders, authenticated
execution, or a new live-family permission.

## Source routing

Each current `SourcePlan` has two discovery surfaces:

- `feed_urls`: RSS/Atom and aggregator queries, polled by the existing central
  feed service.
- `poll_urls`: required named settlement sources in the curated publisher or
  official-domain registry without a usable feed, polled by the central
  direct-source service.

An arbitrary URL copied from untrusted market rule text never becomes an
outbound polling target. Unknown named sources retain their semantic identity
and aggregator discovery, but require an explicit registry review before
direct requests are enabled. Sitemap and JSON adapters also reject item URLs
outside the polled publisher host or its parent/subdomains.

Direct-source polling is still one request stream per unique URL across the
fleet. It uses conditional requests and the existing per-domain 403/429
backoff. An endpoint with no newly inserted items backs off exponentially
from the two-second armed cadence to 30 seconds; a new item immediately
restores the fast cadence. A site failure therefore cannot turn one source
into a per-market request storm.

When several direct endpoints on one domain are due simultaneously, their
Tranche 7 monitoring score breaks the contention tie. Least-recently-attempted
ordering still guarantees coverage, so priority cannot permanently starve a
cold-start source.

The direct-source service recognizes:

- RSS/Atom;
- XML sitemap URL sets and one sitemap-index level;
- JSON listings with URL/title/date/body fields;
- HTML release/news listings.

Items discovered from JSON, sitemaps, and HTML are promoted to the publisher
page through the central single-flight promotion cache before model evidence
extraction. Discovery does not grant authority: settlement and confirmation
roles still come only from the immutable `SourcePlan`.

## Deterministic evidence boundary

Arbitrary HTML, RSS text, and JSON never become deterministic evidence.

The fast evidence lane accepts only an internal source-adapter envelope:

```json
{
  "schema_version": 1,
  "market_id": "exact-market-id",
  "rule_spec_sha256": "exact-current-rulespec-hash",
  "fact": {
    "target_outcome": "Yes",
    "assertion": "PREDICATE_SATISFIED",
    "predicate_matches": true,
    "temporal_relation": "UNKNOWN",
    "event_at": "2026-07-25T12:00:00+00:00",
    "observed_value": "",
    "observed_value_upper": "",
    "observed_unit": "",
    "supporting_quote": "Verbatim source text",
    "clauses_satisfied": ["exact qualifying condition"],
    "clauses_violated": []
  }
}
```

A source-specific normalizer must emit this as `polybot_claim`. The runtime
then independently verifies:

- exact market and RuleSpec binding;
- an enabled objective rule family;
- exact source identity and required `SETTLEMENT`/`CONFIRMATION` role;
- publication timestamp and RuleSpec window relation;
- the existing closed fact schema;
- a verbatim quote present in the captured payload;
- immutable claim identity.

Malformed or stale envelopes fail closed and do not fall back to the model
lane. Valid envelopes use
`deterministic:official-claim-envelope-v1`, one extraction pass, and zero model
budget. All other articles remain on the existing two-pass extractor.

The evidence-policy hash includes the deterministic adapter version, enabled
families, model provider/model/passes, prompt version, and direct-source
adapter set. Forward results from a different evidence policy are not pooled
by the profit-priority reader.

## Point-in-time latency stages

Forward articles now preserve:

- `published_at`: publisher timestamp;
- `discovered_at`: first appearance in a feed/listing/API;
- `fetch_started_at`: publisher-page request start;
- `fetched_at`: publisher response received;
- `parsed_at`: full text ready;
- `first_observed_at`: market worker recorded the article;
- extraction start/completion;
- decision start/completion;
- simulated submission start/completion.

`forward-completeness` reports p50/p95/max for:

- publisher → discovery;
- discovery → publisher fetch start;
- publisher fetch duration;
- parse duration;
- parse → worker observation;
- deterministic and model extraction separately;
- publisher/discovery → simulated submission;
- decision → simulated submission;
- simulated submission round trip;
- book/trade source → receive.

The direct publisher-to-submission percentile is used when available. The
older sum-of-stage-p95 estimate remains only as a fallback for legacy rows.

## Trade prints

The market WebSocket `last_trade_price` message is stored as a first-class
trade print with:

- token and market;
- exchange and receive timestamps;
- price and size;
- exchange-reported side;
- fee rate;
- transaction hash;
- raw event hash.

Trade prints are included as `TRADE` events in content-addressed forward
timelines. Current taker replay validates and preserves these events but does
not infer fills from them. Maker simulation remains gated on a separate,
pessimistic queue model using displayed depth ahead, cancellations, prints
through the posted level, and an explicit queue-position haircut.

## Operator command

```bash
make economics
```

The report shows each family and market's terminal-event observations,
publisher-to-submission p50/p95, decision-to-submission p95, executable quote
half-life, stressed-opportunity count, conservative net value per 1,000
monitored market-hours, blockers, and the current primary bottleneck.

The report changes monitoring judgment only. It does not alter execution
eligibility.

## Go/no-go rule

Continue the taker-confirmation family only when forward observations show a
valid terminal decision with positive stressed edge at the actual simulated
submission time. If discovery or parsing latency routinely exceeds the
available edge window, stop that family or improve the named source; do not
weaken price, source, or terminality gates.
