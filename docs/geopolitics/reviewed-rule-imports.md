# Reviewed RuleSpec Imports

Reviewed imports resolve a compiler disagreement without weakening two-pass
consensus. They are an explicit operator path, not a third autonomous compiler
pass and not a way to make a market live-eligible.

## Safety model

- Only markets in `rule_compiler.reviewed_rule_market_ids` may use the path.
  The maintained config requires those markets to also be explicit priority
  markets.
- Candidate preparation starts from one exact normalized compiler-pass SHA-256
  stored for the current market and verbatim rule-text hash.
- Deterministic compiler preflight still runs. Missing topology, instrument,
  deadline, or rule bindings cannot be supplied by review prose.
- Import validates the complete RuleSpec schema, every clause reference, the
  configured deadline-authority policy, and the current persisted market,
  condition, outcome, and token binding.
- The operator must repeat the candidate's exact execution-spec SHA-256 on the
  import command. Editing any execution field changes that hash.
- SQLite stores the RuleSpec and its approval atomically. The approval records
  reviewer, note, approval time, exact spec hash, current rule-text hash, and
  the SHA-256 of the imported file bytes.
- A reviewed-only spec has a permanent
  `reviewed_rule_spec_paper_only` live blocker. Import also demotes the context
  to `RULES_REVIEW_REQUIRED` until a new SourcePlan is derived and the market
  is graded again.

Reviewer identity is an audit label, not a cryptographic signature. Protect
shell and filesystem access accordingly.

## Workflow

Inspect the failed passes and choose the exact normalized pass used as the
starting point:

```bash
make inspect-rule MARKET=<market-id>
make prepare-rule-review \
  MARKET=<market-id> \
  PASS_SHA256=<normalized-output-sha256> \
  OUT=reviews/<market-id>.json
```

Review and, if necessary, edit only the semantic fields in the generated full
RuleSpec. Validate its closed schema and obtain the resulting execution hash:

```bash
make validate-rule SPEC=reviews/<market-id>.json
```

The reviewer must compare the exact file, current verbatim rules, clause IDs,
source policy, predicate, time window, topology, and outcomes before approving
that hash:

```bash
make import-reviewed-rule \
  MARKET=<market-id> \
  SPEC=reviews/<market-id>.json \
  REVIEWER=<operator-id> \
  NOTE='<what was checked and why this interpretation is correct>' \
  APPROVE_SPEC_SHA256=<exact-spec-sha256>
```

Then build sources and regrade. Neither step is automatic:

```bash
make plan-sources MARKET=<market-id>
make grade-markets
make inspect-rule MARKET=<market-id>
```

`inspect-rule` includes the immutable approval records. A changed verbatim
rule hash makes the imported spec non-current just like a compiler-generated
spec.
