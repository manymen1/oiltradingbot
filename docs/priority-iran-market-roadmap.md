# Priority Iran Market Semantic Roadmap

Snapshot date: 2026-08-10

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

## Delivery Status

The first RuleSpec-v2 safety tranche is implemented:

- Gamma contexts persist deterministic topology plus each leg's deadline,
  verbatim rule hash, resolution-source reference, and metadata consistency.
- RuleSpec schema v2 binds those immutable fields and rejects stale context
  reuse.
- All supported evaluators route claims per outcome. Grouped claims require an
  explicit target, late events cannot satisfy an earlier leg, and wall-clock
  aging cannot turn a pre-deadline status report into a terminal result.
- Shared semantics compile only when all active legs have one common rule
  contract and one compatible settlement-source contract. Missing leg data,
  divergent rules/sources, unsupported topology, and active deadline
  mismatches fail before any model call.
- Source plans include every resolution source bound into the RuleSpec.
- Context construction now derives each leg's exact rule cutoff when the
  verbatim rules identify a clock, preserving the IANA timezone and any
  post-deadline resolution window. Gamma timestamps are compared as instants,
  not merely as calendar labels.
- Compiler-pass timestamps are canonicalized to UTC before agreement. Two
  offsets representing the same instant agree; different instants remain
  consensus-critical.

The follow-on RuleSpec-v3 clause/source tranche is also implemented:

- Every active verbatim leg rule is split into a deterministic,
  content-addressed `RuleClause` catalog. Compiler and evidence outputs cite
  those immutable clause IDs; model-written clause prose is discarded and
  rehydrated from the exact catalog text after two-pass agreement.
- Source requirements have deterministic IDs and a closed structured policy:
  `ANY_OF`, `ALL_OF`, `QUORUM`, `PRIMARY_WITH_FALLBACK`, or
  `CONDITIONAL_FALLBACK`, with explicit fallback conditions.
- SourcePlan schema v3 carries the exact RuleSpec policy and maps each planned
  publisher to the requirements it can satisfy. Freshness checks reject
  missing requirement mappings or policy drift.
- Semantic coverage evaluates requirement health according to the policy.
  Endpoint-unavailability fallback is automatic only when directly
  observable; conflict-driven fallback remains blocked until evidence proves
  its condition.
- Existing RuleSpec/source-plan versions fail closed and must be recompiled;
  raw book capture remains independent and uninterrupted.

The RuleSpec-v4 paper deadline-authority tranche is implemented:

- Strict Gamma/rule agreement remains the global default. A separate reviewed
  allowlist grants only these thirteen priority markets the versioned
  `VERBATIM_RULES_PAPER_ONLY_V1` policy.
- Every outcome binding retains the Gamma operational deadline, exact parsed
  rule deadline, semantic evaluation deadline, timezone, consistency result,
  and chosen authority. No mismatch is hidden or overwritten.
- A mismatched outcome compiles only when its rule clock and timezone are both
  exact. Bab el-Mandeb remains blocked because its September leg lacks that
  exact binding; the other twelve pass this deterministic preflight.
- Any compiled mismatch is unconditionally paper-only even if its rule family
  is later promoted. Gamma continues to govern order availability while the
  verbatim rule clock governs semantic evaluation.

The initial date-only 2026-07-31 Gamma preflight appeared to support eleven of
the thirteen events at this layer. Exact-instant replay corrected that result:
all thirteen have at least one active Gamma deadline that conflicts with the
verbatim rule clock. Common examples are Gamma `23:59Z` versus rule
`23:59 America/New_York`, daily Gamma cutoffs that roll into the following
IRST/AST calendar date, and Gamma `00:00Z` versus a rule's explicit ET time.
The final-nuclear-deal and Bab el-Mandeb events additionally retain their
previously detected wrong-date legs. Strict mode continues to fail all
thirteen. The reviewed v4 paper policy now permits twelve to proceed without
treating Gamma metadata as resolution truth; Bab el-Mandeb still fails before
a model call because its exact rule-deadline binding is incomplete.

The first real two-pass canary, the US-Iran effective-ceasefire event, failed
closed with `compiler_passes_disagree`. Both passes selected
`DURATION_REQUIREMENT`, the 14-calendar-day reset predicate, the same
exclusions, and the same source identities. They differed in the deadline
instant and in assigning conflict-resolution clauses between
qualifying conditions and the resolution policy. More precise review showed
that the deadline serializations were not equivalent:
`2026-08-31T23:59:00Z` is four hours earlier than
`2026-08-31T23:59:00-04:00`. No RuleSpec was saved. The stored canary is now a
regression example for fail-closed deadline disagreement; the clause-placement
variation still motivates canonical clause IDs and is not a reason to relax
exact agreement.

The second canary, the US announcement ending the Iranian blockade, initially
failed structural validation because both model passes returned the same
18-hex prefix of a 20-hex clause ID. Compiler-only binding now expands only an
18- or 19-hex prefix that identifies exactly one catalog clause; shorter,
ambiguous, invented, or duplicate-resolving IDs still fail. The retried passes
then reached semantic comparison and failed closed with
`compiler_passes_disagree`. One modeled the enumerated official US sources as
six `ANY_OF` requirements and cited both qualifying terminal-Yes clauses; the
other modeled one composite `ALL_OF` government-source requirement and cited
only the post-announcement terminal clause. This is a source-policy and clause
coverage disagreement, not formatting noise. No RuleSpec or SourcePlan was
saved, and the canary remains non-executable pending an explicit reviewed-spec
workflow.

That reviewed-spec workflow is now implemented. Candidate preparation is
bound to one stored normalized-pass hash and the current market/rule version;
import requires a separately repeated execution-spec hash, reviewer identity,
and substantive review note. The imported file hash and approval are stored
atomically with the immutable RuleSpec. Deterministic preflight, clause,
instrument, topology, deadline-authority, SourcePlan, grading, and execution
checks remain in force. Reviewed-only specs are unconditionally paper-only.
The first reviewed import, the next-round-of-US-Iran-peace-talks event, is now
stored with RuleSpec hash
`2f3109a3620e906bc295c89c2571f5b8b36add31554fd71f1ba6d526ec7fd917`
and a fresh 22-record SourcePlan. Its terminal policy conservatively accepts a
named US or Iranian government source; the credible-reporting alternative was
withheld because the current flat policy cannot express “one government OR two
independent publishers” without weakening the official path. Regrading now
correctly classifies this context `CLOSED`, so it is a resolution/replay canary
rather than a live paper-trading canary.

The first automatically agreed active canary is the US-military-action-against-
Cuba event, RuleSpec hash
`abc268b023c6cc4691554a38599676fa525d69fd90170e131d587dd9ac8d2f6e`.
Its three source-policy alternatives are fully resolved, its context grades
`PAPER_ELIGIBLE`, and a one-shot generic runner cycle produced three immutable
ambiguous proofs and zero executions. The registry now binds both “Donald
Trump” and “U.S. government” to canonical US-government identities; the latter
can no longer be misparsed as a synthetic `u.s` domain. This canary remains
paper-only because its rule family is not promoted, its rules are
discretionary, and Gamma's deadline differs from the exact verbatim-rule clock.

The blockade review exercise also exposed and fixed a deterministic source
registry gap. Its six named US-government alternatives now resolve to the
appropriate White House, State, Defense/War, and CENTCOM domains. Every one is
assigned to `government:united_states`, so multiple official endpoints cannot
be miscounted as independent confirmations. A second review selected the
newer `SOURCE_LOCKED_ANNOUNCEMENT` compiler pass, represented the named offices
as alternatives satisfying one US-government authority, and restored both
verbatim terminal-Yes clauses. The exact reviewed RuleSpec
`25837ae13fa6dc7b684afa6b06397bdff0d59912dde97f6105be937542214545`
is now imported with a fresh 21-record SourcePlan and grades
`PAPER_ELIGIBLE`. Its first generic paper cycle emitted 17 ambiguous per-leg
proofs and zero executions; terminal-positive replay fixtures and forward
observations remain required.

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

Six selected events were present in the complete Gamma enumeration but were
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

### Phase 2 — RuleSpec v4

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
