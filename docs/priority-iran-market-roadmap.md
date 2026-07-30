# Priority Iran Market Semantic Roadmap

Snapshot date: 2026-07-31

## Objective

Make the thirteen operator-selected events the first semantic canary cohort
without allowing priority to weaken any scope, rule-agreement, source,
paper/live, or execution gate. Raw book capture remains independent of semantic
readiness.

The maintained configuration lists the stable parent event slugs under
`rule_compiler.priority_market_ids`. After monitored contexts, open pins
reserve recorder capacity before volume-filled extras. A pin also grants one
early bounded compiler attempt only. After an attempt, ordinary
fair/profit-priority scheduling resumes so a difficult market cannot starve
the universe.

## Live Mapping

| # | Parent event / selected leg | Required topology | Predicate family | Resolution-source policy | Immediate blocker |
|---|---|---|---|---|---|
| 1 | [US-Iran effective ceasefire](https://polymarket.com/event/us-x-iran-effective-ceasfire-byptptpt-2-week-pause-20260715194822042/us-x-iran-effective-ceasfire-by-august-31-20260715194822047) / Aug 31 | `MONOTONE_DEADLINE_LADDER` | `DURATION_REQUIREMENT` with qualifying-strike reset | Official US and Iranian government/military information plus credible reporting; conflicts use the rules' three-day totality procedure | Per-leg deadline/window and duration-reset semantics |
| 2 | [Israel-Iran ceasefire continues](https://polymarket.com/event/israel-x-iran-ceasefire-continues-throughptptpt-20260716224448963/israel-x-iran-ceasefire-continues-through-july-28) / Jul 28 | `MONOTONE_DEADLINE_LADDER` | `STATUS_AT_DEADLINE` with qualifying-breach rules | Official Israeli and Iranian government/military information plus credible reporting; conflicts use the rules' three-day totality procedure | Selected date has passed but Gamma still reports the leg open; per-leg dispute window is required |
| 3 | [Iran leader at end of 2026](https://polymarket.com/event/iran-leader-end-of-2026/will-there-be-no-head-of-state-in-iran-end-of-2026) / No Head of State | `EXCLUSIVE_ONE_OF_N` | `STATUS_AT_DEADLINE` | Consensus of credible reporting about de facto governing authority | Event has 30 currently open named legs plus inactive placeholders; only active real outcomes may bind |
| 4 | [US-Iran final nuclear deal](https://polymarket.com/event/us-iran-final-nuclear-deal-by-20260621201254412/us-iran-final-nuclear-deal-by-december-31-2026) / Dec 31 | `MONOTONE_DEADLINE_LADDER` | `OCCURRENCE_BEFORE_DEADLINE` for a qualifying written instrument | Official US or Iranian government communications or authorized representatives | Gamma's selected-leg/end-date metadata is inconsistent; deadline must be parsed and cross-checked against the leg rules |
| 5 | [US announces end of Iranian blockade](https://polymarket.com/event/us-announces-end-of-iranian-blockade-byptptpt-20260713152715080) | `MONOTONE_DEADLINE_LADDER` | `SOURCE_LOCKED_ANNOUNCEMENT` | Official US government sources, including the President, Defense, State, and CENTCOM | Per-leg deadline plus structured official-source alternatives |
| 6 | [Bab el-Mandeb effectively closed](https://polymarket.com/event/bab-el-mandeb-strait-effectively-closed-by) | `MONOTONE_DEADLINE_LADDER` | `NUMERIC_THRESHOLD` (`7-day moving average <= 10`) | IMF PortWatch is the required settlement source | Source-specific data adapter, revision cutoff, 14-day missing-data rule, and bad Gamma dates |
| 7 | [Next US-Iran peace talks by](https://polymarket.com/event/next-round-of-us-iran-peace-talks-byptptpt-20260623022722982) | `MONOTONE_DEADLINE_LADDER` | `OCCURRENCE_BEFORE_DEADLINE` | Official US and Iranian information plus consensus credible reporting | Per-leg deadline and exact definition of a qualifying round |
| 8 | [Iran military action against a Gulf state](https://polymarket.com/event/iran-military-action-against-a-gulf-state-onptptpt-20260708212328295/iran-military-action-against-a-gulf-state-on-july-31-20260708212322956) / Jul 31 | `INDEPENDENT_MULTI` daily bins | `OCCURRENCE_ON_DATE` | Official Iran/relevant Gulf-state government and military information plus consensus credible reporting | Current evaluator would inspect only the first outcome; every date needs an independent window |
| 9 | [Hamas agrees to disarm](https://polymarket.com/event/will-hamaz-disarm-by-december-31) / Dec 31, 2026 | `MONOTONE_DEADLINE_LADDER` (one open leg, historical siblings retained) | `SOURCE_LOCKED_ANNOUNCEMENT` | Hamas leadership statement, with wide credible-reporting consensus as an alternative qualifying path | `PRIMARY_WITH_FALLBACK`/alternative source policy and per-leg rules |
| 10 | [Location of next US-Iran talks](https://polymarket.com/event/where-will-the-next-next-round-of-us-iran-peace-talks-beptptpt-20260623023740663/will-the-next-diplomatic-us-iran-meeting-be-in-switzerland-by-september-30-2026-20260622185050768) / Switzerland | `EXCLUSIVE_ONE_OF_N` | `CATEGORICAL_EXCLUSIVE` | Official US and Iranian information plus consensus credible reporting | Correct exclusive binding for 19 outcomes, including no-meeting and regional catch-alls |
| 11 | [US military action against Cuba](https://polymarket.com/event/us-strike-on-cuba-by) | `MONOTONE_DEADLINE_LADDER` (one open leg) | `OCCURRENCE_BEFORE_DEADLINE` | Consensus of credible reporting; Trump/US claims qualify subject to the rule | Two-day post-deadline confirmation window and historical siblings |
| 12 | [Iran successfully targets shipping](https://polymarket.com/event/iran-successfully-targets-shipping-onptptpt-20260729163314520/iran-successfully-targets-shipping-on-august-23-2026-20260729163314543) / Aug 23 | `INDEPENDENT_MULTI` daily bins | `OCCURRENCE_ON_DATE` | Consensus of credible reporting | 31 independent date windows; no categorical or ladder inference is valid |
| 13 | [Iran charges Hormuz fees](https://polymarket.com/event/iran-charges-hormuz-fees-byptptpt-20260625175035466/iran-charges-hormuz-fees-by-october-31) / Oct 31 | `MONOTONE_DEADLINE_LADDER` | compound occurrence: official announcement **and** collection begins | Official Iranian announcement and consensus credible reporting | Compound predicate, `ALL_OF` source/evidence policy, and per-leg deadline |

## Confirmed Discovery Defect

Five selected events were present in the complete Gamma enumeration but were
discarded before context persistence:

- five because the excluded term `nfl` matched the substring in `conflict`;
- one because `stock` matched the substring in `stockpile`.

Exclusion matching now uses alphanumeric term boundaries. Exact sports/finance
terms remain excluded, while words containing those character sequences do not.

## Required Implementation Order

### Phase 0 — Safe prioritization

1. Keep the thirteen stable parent event slugs in
   `rule_compiler.priority_market_ids`.
2. Validate the list as unique non-empty strings.
3. Put never-attempted pins ahead of ordinary compiler work.
4. Remove the special advantage after the first attempt so failures cannot
   monopolize the per-cycle budget.
5. Reserve book-capture capacity for open pins after monitored contexts and
   before volume-fill extras, without exceeding the configured cap unless the
   monitored superset itself already exceeds it.
6. Keep every existing scope, agreement, source, paper/live, and execution
   check unchanged.

### Phase 1 — Preserve per-leg truth

1. Extend `OutcomeRecord` with immutable leg rule text/hash, deadline,
   timezone, and post-deadline resolution window.
2. Build those fields from each Gamma market object and the verbatim leg
   rules, not from the parent event deadline.
3. Cross-check rule-derived and Gamma deadlines. Any mismatch becomes an
   explicit semantic blocker rather than selecting either silently.
4. Filter inactive placeholder outcomes from semantic bindings while retaining
   them in raw discovery audit data.
5. Version the context payload deliberately so old capture data is associated
   only through an explicit compatible migration.

### Phase 2 — RuleSpec v2

1. Separate `predicate_family` from `outcome_topology`.
2. Add at least:
   `EXCLUSIVE_ONE_OF_N`, `INDEPENDENT_MULTI`,
   `MONOTONE_DEADLINE_LADDER`, and per-leg windows.
3. Represent shared event semantics once and bind deterministic leg overrides
   for date, timezone, and selected label.
4. Add structured source policies:
   `ANY_OF`, `ALL_OF`, `QUORUM`, `PRIMARY_WITH_FALLBACK`, and
   `CONDITIONAL_FALLBACK`.
5. Make compiler passes select canonical clause/source/topology IDs. Continue
   blocking actual source, deadline, predicate, and topology disagreements;
   do not use fuzzy prose agreement.

### Phase 3 — Evaluators

1. Evaluate every bound outcome; remove every non-categorical use of
   `spec.outcomes[0]`.
2. Add independent daily-bin evaluation for markets 8 and 12.
3. Add monotone-ladder consistency checks without inferring a terminal state
   merely from prices.
4. Add resettable-duration state for market 1.
5. Add source-data revision and missing-publication handling for IMF PortWatch.
6. Add compound announcement-and-collection evaluation for market 13.
7. Continue to emit `AMBIGUOUS` with blockers for conflicting or insufficient
   evidence.

### Phase 4 — Sources

1. Build canonical adapters for named official US entities and IMF PortWatch.
2. Model Iranian, Israeli, Gulf-state, and Hamas official sources as roles,
   with explicit availability/fallback state; do not pretend a generic news
   feed is an official source.
3. Deduplicate credible reporting by organization/independence group.
4. Enforce the exact per-market source policy before terminal evaluation.
5. Fix source-reference parsing so prose URLs resolve correctly and tokens
   such as `e.g.` can never become domains.

### Phase 5 — Cohort acceptance

For each event, fixtures must cover every open leg, the selected URL leg,
deadline/timezone boundaries, exclusions, source-policy success/failure,
conflicts, and terminal YES/NO. Acceptance requires:

- two compiler passes agree on canonical semantic IDs;
- replay fixtures produce the expected state for every leg;
- semantic reads can use only compatible capture rows;
- paper soak records claims/evaluations/proofs with no live execution;
- no unresolved metadata, source, topology, or deadline blocker remains.

Promotion is per topology and source adapter, never for the cohort as a whole.
The maintained configuration keeps `live_confirmation_families` empty until
replay and forward evidence justify a separate operator decision.
