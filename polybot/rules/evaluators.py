from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import (
    EVIDENCE_STATES,
    RULE_EVALUATION_SCHEMA_VERSION,
    EvidenceClaim,
    OutcomeBinding,
    RuleEvaluation,
    RuleSpec,
)

EVALUATOR_VERSION = "rules-evaluator-v4"
SUPPORTED_OUTCOME_TOPOLOGIES = {
    "SINGLE_BINARY",
    "EXCLUSIVE_ONE_OF_N",
    "INDEPENDENT_MULTI",
    "MONOTONE_DEADLINE_LADDER",
}
SUPPORTED_EVALUATOR_FAMILIES = {
    "OCCURRENCE_BEFORE_DEADLINE",
    "CATEGORICAL_EXCLUSIVE",
    "SOURCE_LOCKED_ANNOUNCEMENT",
    "STATUS_AT_DEADLINE",
    "NUMERIC_THRESHOLD",
    "DURATION_REQUIREMENT",
}


def evaluate_rule(
    spec: RuleSpec,
    claims: list[EvidenceClaim],
    *,
    as_of: datetime | None = None,
) -> list[RuleEvaluation]:
    """Evaluate immutable extracted facts with family-specific code.

    Claims may describe facts, but they cannot supply the final evidence state.
    Every claim is rebound to the current spec before an evaluator can see it.
    """

    family = spec.semantics.rule_family
    if spec.outcome_topology not in SUPPORTED_OUTCOME_TOPOLOGIES:
        return [
            _evaluation(
                spec,
                outcome_name=outcome.name,
                state="AMBIGUOUS",
                terminal=False,
                claims=[],
                required=(
                    spec.semantics.resolution_policy
                    .independent_confirmation_sources
                ),
                blockers=[
                    "unsupported_outcome_topology:"
                    f"{spec.outcome_topology}"
                ],
                as_of=as_of,
            )
            for outcome in spec.outcomes
        ]
    if family not in SUPPORTED_EVALUATOR_FAMILIES:
        return [
            _evaluation(
                spec,
                outcome_name="",
                state="AMBIGUOUS",
                terminal=False,
                claims=[],
                required=spec.semantics.resolution_policy.independent_confirmation_sources,
                blockers=[f"unsupported_evaluator_family:{family}"],
                as_of=as_of,
            )
        ]
    for claim in claims:
        claim.validate_spec_binding(spec)
    unique = {claim.claim_sha256: claim for claim in claims}
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            _stamp(item.published_at) or _stamp(item.extracted_at),
            item.claim_sha256,
        ),
    )
    evaluator = FAMILY_EVALUATORS[family]
    evaluations = evaluator(spec, ordered, as_of=as_of)
    evaluations = _enforce_terminal_source_policy(
        spec,
        evaluations,
        ordered,
    )
    if (
        spec.outcome_topology == "EXCLUSIVE_ONE_OF_N"
        and family != "CATEGORICAL_EXCLUSIVE"
    ):
        return _enforce_exclusive_topology(
            spec,
            evaluations,
            ordered,
            as_of=as_of,
        )
    return evaluations


def _enforce_terminal_source_policy(
    spec: RuleSpec,
    evaluations: list[RuleEvaluation],
    claims: list[EvidenceClaim],
) -> list[RuleEvaluation]:
    """Fail closed unless terminal claims satisfy the exact source policy."""

    by_hash = {claim.claim_sha256: claim for claim in claims}
    guarded: list[RuleEvaluation] = []
    for evaluation in evaluations:
        if not evaluation.terminal:
            guarded.append(evaluation)
            continue
        decisive = [
            by_hash[item]
            for item in evaluation.claim_sha256s
            if item in by_hash
        ]
        blocker = _source_policy_blocker(spec, decisive)
        if not blocker:
            guarded.append(evaluation)
            continue
        guarded.append(
            replace(
                evaluation,
                evidence_state=(
                    "STRONG_YES"
                    if (
                        evaluation.evidence_state == "TERMINAL_YES"
                        and policy_is_incomplete_alternative(
                            spec,
                            blocker,
                        )
                    )
                    else "AMBIGUOUS"
                ),
                terminal=False,
                blockers=sorted({*evaluation.blockers, blocker}),
            )
        )
    return guarded


def policy_is_incomplete_alternative(
    spec: RuleSpec,
    blocker: str,
) -> bool:
    return (
        spec.semantics.source_policy.policy_type
        == "ALTERNATIVE_QUORUM"
        and blocker.startswith(
            "source_policy_alternative_quorum_unsatisfied:"
        )
    )


def _source_policy_blocker(
    spec: RuleSpec,
    claims: list[EvidenceClaim],
) -> str:
    policy = spec.semantics.source_policy
    matched = {
        requirement_id
        for claim in claims
        for requirement_id in claim.source_requirement_ids
    }
    allowed = set(policy.requirement_ids)
    matched &= allowed
    if policy.policy_type == "ANY_OF":
        satisfied = bool(matched)
        needed = 1
    elif policy.policy_type == "ALTERNATIVE_QUORUM":
        progress: list[tuple[int, int, int, int]] = []
        for branch in policy.branches:
            branch_ids = set(branch.requirement_ids)
            branch_claims = [
                claim
                for claim in claims
                if set(claim.source_requirement_ids) & branch_ids
            ]
            matched_ids = {
                requirement_id
                for claim in branch_claims
                for requirement_id in claim.source_requirement_ids
                if requirement_id in branch_ids
            }
            independent = _independent_count(branch_claims)
            if (
                len(matched_ids) >= branch.requirement_quorum
                and independent >= branch.minimum_independent_sources
            ):
                return ""
            progress.append(
                (
                    len(matched_ids),
                    branch.requirement_quorum,
                    independent,
                    branch.minimum_independent_sources,
                )
            )
        best = max(
            progress,
            key=lambda item: (
                item[0] / item[1],
                item[2] / item[3],
            ),
            default=(0, 1, 0, 1),
        )
        return (
            "source_policy_alternative_quorum_unsatisfied:"
            f"requirements={best[0]}/{best[1]},sources={best[2]}/{best[3]}"
        )
    elif policy.policy_type == "ALL_OF":
        satisfied = matched == allowed
        needed = len(allowed)
    elif policy.policy_type == "QUORUM":
        satisfied = len(matched) >= policy.quorum
        needed = policy.quorum
    else:
        primary = set(policy.primary_requirement_ids)
        satisfied = len(matched & primary) >= policy.quorum
        needed = policy.quorum
        if not satisfied:
            return (
                "source_policy_fallback_condition_unproven:"
                f"{policy.fallback_condition}"
            )
    if satisfied:
        return ""
    return f"source_policy_unsatisfied:{len(matched)}/{needed}"


def _occurrence(
    spec: RuleSpec,
    claims: list[EvidenceClaim],
    *,
    as_of: datetime | None,
) -> list[RuleEvaluation]:
    return [
        _event_evaluation(
            spec,
            outcome.name,
            _claims_for_outcome(spec, claims, outcome.name),
            as_of=as_of,
            require_settlement=False,
        )
        for outcome in spec.outcomes
    ]


def _source_locked(
    spec: RuleSpec,
    claims: list[EvidenceClaim],
    *,
    as_of: datetime | None,
) -> list[RuleEvaluation]:
    return [
        _event_evaluation(
            spec,
            outcome.name,
            _claims_for_outcome(spec, claims, outcome.name),
            as_of=as_of,
            require_settlement=True,
        )
        for outcome in spec.outcomes
    ]


def _categorical(
    spec: RuleSpec,
    claims: list[EvidenceClaim],
    *,
    as_of: datetime | None,
) -> list[RuleEvaluation]:
    terminal_by_outcome: dict[str, list[EvidenceClaim]] = {}
    for outcome in spec.outcomes:
        terminal_by_outcome[outcome.name] = _authorized_terminal_claims(
            _claims_for_outcome(spec, claims, outcome.name),
            require_settlement=False,
        )
    confirmed = [
        name
        for name, items in terminal_by_outcome.items()
        if _independent_count(items)
        >= spec.semantics.resolution_policy.independent_confirmation_sources
    ]
    if len(confirmed) > 1:
        conflict_claims = [
            claim
            for name in confirmed
            for claim in terminal_by_outcome[name]
        ]
        return [
            _evaluation(
                spec,
                outcome_name=outcome.name,
                state="AMBIGUOUS",
                terminal=False,
                claims=conflict_claims,
                required=spec.semantics.resolution_policy.independent_confirmation_sources,
                blockers=["multiple_categorical_outcomes_satisfied"],
                as_of=as_of,
            )
            for outcome in spec.outcomes
        ]
    if len(confirmed) == 1:
        winner = confirmed[0]
        decisive = terminal_by_outcome[winner]
        required = (
            spec.semantics.resolution_policy.independent_confirmation_sources
        )
        return [
            _evaluation(
                spec,
                outcome_name=outcome.name,
                state=(
                    "TERMINAL_YES"
                    if outcome.name == winner
                    else "TERMINAL_NO"
                ),
                terminal=True,
                claims=decisive,
                required=required,
                blockers=[],
                as_of=as_of,
            )
            for outcome in spec.outcomes
        ]
    return [
        _event_evaluation(
            spec,
            outcome.name,
            _claims_for_outcome(spec, claims, outcome.name),
            as_of=as_of,
            require_settlement=False,
        )
        for outcome in spec.outcomes
    ]


def _status(
    spec: RuleSpec,
    claims: list[EvidenceClaim],
    *,
    as_of: datetime | None,
) -> list[RuleEvaluation]:
    return [
        _status_outcome(
            spec,
            outcome.name,
            claims,
            as_of=as_of,
        )
        for outcome in spec.outcomes
    ]


def _status_outcome(
    spec: RuleSpec,
    outcome: str,
    claims: list[EvidenceClaim],
    *,
    as_of: datetime | None,
) -> RuleEvaluation:
    relevant = [
        claim
        for claim in _claims_for_outcome(spec, claims, outcome)
        if _claim_matches_outcome_window(spec, outcome, claim)
    ]
    observations = [
        claim
        for claim in relevant
        if claim.assertion == "STATUS_OBSERVED"
    ]
    if any(claim.assertion == "CONFLICTING" for claim in relevant):
        return _evaluation(
            spec,
            outcome_name=outcome,
            state="AMBIGUOUS",
            terminal=False,
            claims=relevant,
            required=1,
            blockers=["conflicting_status_evidence"],
            as_of=as_of,
        )
    breaches = [
        claim
        for claim in relevant
        if claim.assertion == "QUALIFYING_BREACH"
        and claim.predicate_matches
    ]
    if (
        breaches
        and spec.semantics.resolution_policy.terminal_no_monotonic
    ):
        latest_breach = breaches[-1]
        blockers = (
            []
            if _authorized_for_terminal([latest_breach])
            else ["status_source_not_authorized"]
        )
        return _evaluation(
            spec,
            outcome_name=outcome,
            state="TERMINAL_NO" if not blockers else "AMBIGUOUS",
            terminal=not blockers,
            claims=[latest_breach],
            required=1,
            blockers=blockers,
            as_of=as_of,
        )
    if not observations:
        return _fallback_evaluation(spec, outcome, relevant, as_of=as_of)
    latest = observations[-1]
    at_measurement = latest.temporal_relation == "AT_DEADLINE"
    monotonic_no = (
        spec.semantics.resolution_policy.terminal_no_monotonic
        and not latest.predicate_matches
    )
    state = (
        "TERMINAL_YES"
        if at_measurement and latest.predicate_matches
        else "TERMINAL_NO"
        if at_measurement or monotonic_no
        else "STRONG_YES"
        if latest.predicate_matches
        else "STRONG_NO"
    )
    authorized = _authorized_for_terminal([latest])
    blockers = [] if authorized else ["status_source_not_authorized"]
    terminal = state.startswith("TERMINAL_") and not blockers
    if blockers:
        state = "AMBIGUOUS"
    return _evaluation(
        spec,
        outcome_name=outcome,
        state=state,
        terminal=terminal,
        claims=[latest],
        required=1,
        blockers=blockers,
        as_of=as_of,
    )


def _numeric(
    spec: RuleSpec,
    claims: list[EvidenceClaim],
    *,
    as_of: datetime | None,
) -> list[RuleEvaluation]:
    return [
        _numeric_outcome(
            spec,
            outcome.name,
            claims,
            as_of=as_of,
        )
        for outcome in spec.outcomes
    ]


def _numeric_outcome(
    spec: RuleSpec,
    outcome: str,
    claims: list[EvidenceClaim],
    *,
    as_of: datetime | None,
) -> RuleEvaluation:
    relevant = [
        claim
        for claim in _claims_for_outcome(spec, claims, outcome)
        if _claim_matches_outcome_window(spec, outcome, claim)
    ]
    measurements = [
        claim
        for claim in relevant
        if claim.assertion == "COUNT_OBSERVED"
    ]
    if not measurements:
        return _fallback_evaluation(spec, outcome, relevant, as_of=as_of)
    latest = measurements[-1]
    lower = _decimal(latest.observed_value)
    upper = _decimal(latest.observed_value_upper) if latest.observed_value_upper else lower
    threshold = _decimal(spec.semantics.predicate.value)
    if lower is None or upper is None or threshold is None:
        return _evaluation(
            spec,
            outcome_name=outcome,
            state="AMBIGUOUS",
            terminal=False,
            claims=[latest],
            required=1,
            blockers=["numeric_value_invalid"],
            as_of=as_of,
        )
    comparator = spec.semantics.predicate.comparator
    low_result = _compare(lower, threshold, comparator)
    high_result = _compare(upper, threshold, comparator)
    if low_result != high_result:
        state = "AMBIGUOUS"
        terminal = False
        blockers = ["numeric_range_straddles_threshold"]
    else:
        after_deadline = latest.temporal_relation == "AT_DEADLINE"
        monotonic_yes = (
            spec.semantics.resolution_policy.terminal_yes_monotonic
        )
        if low_result and (monotonic_yes or after_deadline):
            state = "TERMINAL_YES"
            terminal = True
            blockers = []
        elif not low_result and after_deadline:
            state = "TERMINAL_NO"
            terminal = True
            blockers = []
        else:
            state = "STRONG_YES" if low_result else "STRONG_NO"
            terminal = False
            blockers = []
        if not _authorized_for_terminal([latest]):
            blockers.append(
                "numeric_source_or_timestamp_not_authorized"
            )
            state = "AMBIGUOUS"
            terminal = False
    return _evaluation(
        spec,
        outcome_name=outcome,
        state=state,
        terminal=terminal,
        claims=[latest],
        required=1,
        blockers=blockers,
        as_of=as_of,
    )


def _duration(
    spec: RuleSpec,
    claims: list[EvidenceClaim],
    *,
    as_of: datetime | None,
) -> list[RuleEvaluation]:
    return [
        _duration_outcome(
            spec,
            outcome,
            claims,
            as_of=as_of,
        )
        for outcome in spec.outcomes
    ]


def _duration_outcome(
    spec: RuleSpec,
    outcome: OutcomeBinding,
    claims: list[EvidenceClaim],
    *,
    as_of: datetime | None,
) -> RuleEvaluation:
    deadline = _outcome_deadline(spec, outcome.name)
    relevant = [
        claim
        for claim in _claims_for_outcome(spec, claims, outcome.name)
        if claim.temporal_relation != "BEFORE_WINDOW"
    ]
    breaches = [
        claim
        for claim in relevant
        if claim.assertion == "QUALIFYING_BREACH"
        and claim.predicate_matches
        and _claim_matches_outcome_window(spec, outcome.name, claim)
    ]
    measurements = [
        claim
        for claim in relevant
        if claim.assertion == "DURATION_OBSERVED"
        and claim.predicate_matches
        and _duration_interval_starts_in_window(claim, outcome, deadline)
    ]
    unknown_breaches = [claim for claim in breaches if not claim.event_at]
    if unknown_breaches:
        return _evaluation(
            spec,
            outcome_name=outcome.name,
            state="AMBIGUOUS",
            terminal=False,
            claims=unknown_breaches,
            required=1,
            blockers=["duration_breach_timestamp_missing"],
            as_of=as_of,
        )
    breach_times = [
        (stamp, claim)
        for claim in breaches
        if (stamp := _stamp(claim.event_at)) is not None
    ]
    required = _duration_hours(
        spec.semantics.predicate.value,
        spec.semantics.predicate.unit,
    )
    if required is None:
        return _evaluation(
            spec,
            outcome_name=outcome.name,
            state="AMBIGUOUS",
            terminal=False,
            claims=measurements,
            required=1,
            blockers=["duration_requirement_invalid"],
            as_of=as_of,
        )

    valid_progress: list[tuple[datetime, EvidenceClaim]] = []
    invalid_measurements: list[tuple[EvidenceClaim, str]] = []
    reset_measurements: list[tuple[datetime, EvidenceClaim, EvidenceClaim]] = []
    for measurement in measurements:
        start = _stamp(measurement.interval_start_at)
        end = _stamp(measurement.interval_end_at)
        if start is None or end is None:
            invalid_measurements.append(
                (measurement, "duration_interval_timestamps_missing")
            )
            continue
        observed = _duration_hours(
            measurement.observed_value,
            measurement.observed_unit,
        )
        if observed is None:
            invalid_measurements.append(
                (measurement, "duration_value_invalid")
            )
            continue
        covered = Decimal(str((end - start).total_seconds())) / Decimal("3600")
        if observed > covered:
            invalid_measurements.append(
                (measurement, "duration_observation_exceeds_interval")
            )
            continue
        interval_breaches = [
            (stamp, breach)
            for stamp, breach in breach_times
            if start <= stamp <= end
        ]
        if interval_breaches:
            latest_reset = max(interval_breaches, key=lambda item: item[0])
            reset_measurements.append((latest_reset[0], measurement, latest_reset[1]))
            continue
        if covered >= required and observed >= required:
            blockers = (
                []
                if _authorized_for_terminal([measurement])
                else ["duration_source_or_timestamp_not_authorized"]
            )
            return _evaluation(
                spec,
                outcome_name=outcome.name,
                state="TERMINAL_YES" if not blockers else "AMBIGUOUS",
                terminal=not blockers,
                claims=[measurement],
                required=1,
                blockers=blockers,
                as_of=as_of,
            )
        valid_progress.append((end, measurement))

    if reset_measurements:
        _stamp_value, measurement, breach = max(
            reset_measurements,
            key=lambda item: item[0],
        )
        return _evaluation(
            spec,
            outcome_name=outcome.name,
            state="STRONG_NO",
            terminal=False,
            claims=[measurement, breach],
            required=1,
            blockers=["duration_clock_reset"],
            as_of=as_of,
        )
    if valid_progress:
        end, latest_measurement = max(valid_progress, key=lambda item: item[0])
        later_breaches = [
            (stamp, claim)
            for stamp, claim in breach_times
            if stamp >= end
        ]
        if later_breaches:
            _stamp_value, breach = max(later_breaches, key=lambda item: item[0])
            return _evaluation(
                spec,
                outcome_name=outcome.name,
                state="STRONG_NO",
                terminal=False,
                claims=[breach],
                required=1,
                blockers=["duration_clock_reset"],
                as_of=as_of,
            )
        blockers = (
            []
            if _authorized_for_terminal([latest_measurement])
            else ["duration_source_or_timestamp_not_authorized"]
        )
        return _evaluation(
            spec,
            outcome_name=outcome.name,
            state="PATHWAY_YES" if not blockers else "AMBIGUOUS",
            terminal=False,
            claims=[latest_measurement],
            required=1,
            blockers=blockers,
            as_of=as_of,
        )
    if invalid_measurements:
        latest_measurement, blocker = max(
            invalid_measurements,
            key=lambda item: _claim_time(item[0]),
        )
        return _evaluation(
            spec,
            outcome_name=outcome.name,
            state="AMBIGUOUS",
            terminal=False,
            claims=[latest_measurement],
            required=1,
            blockers=[blocker],
            as_of=as_of,
        )
    if breach_times:
        _stamp_value, latest_breach = max(breach_times, key=lambda item: item[0])
        return _evaluation(
            spec,
            outcome_name=outcome.name,
            state="STRONG_NO",
            terminal=False,
            claims=[latest_breach],
            required=1,
            blockers=["duration_clock_reset"],
            as_of=as_of,
        )
    return _evaluation(
        spec,
        outcome_name=outcome.name,
        state="AMBIGUOUS",
        terminal=False,
        claims=relevant,
        required=1,
        blockers=["no_decisive_rule_bound_claim"],
        as_of=as_of,
    )


def _duration_interval_starts_in_window(
    claim: EvidenceClaim,
    outcome: OutcomeBinding,
    deadline: datetime | None,
) -> bool:
    start = _stamp(claim.interval_start_at)
    if start is None:
        # Retain opaque duration claims so the evaluator emits a specific,
        # fail-closed blocker instead of silently discarding them.
        return True
    outcome_start = _stamp(outcome.start_iso)
    if outcome_start is not None and start < outcome_start:
        return False
    return deadline is None or start <= deadline


def _enforce_exclusive_topology(
    spec: RuleSpec,
    evaluations: list[RuleEvaluation],
    claims: list[EvidenceClaim],
    *,
    as_of: datetime | None,
) -> list[RuleEvaluation]:
    terminal_yes = [
        evaluation
        for evaluation in evaluations
        if evaluation.terminal
        and evaluation.evidence_state == "TERMINAL_YES"
    ]
    required = (
        spec.semantics.resolution_policy.independent_confirmation_sources
    )
    if len(terminal_yes) > 1:
        conflict_claims = [
            claim
            for evaluation in terminal_yes
            for claim in _claims_for_outcome(
                spec,
                claims,
                evaluation.outcome_name,
            )
        ]
        return [
            _evaluation(
                spec,
                outcome_name=outcome.name,
                state="AMBIGUOUS",
                terminal=False,
                claims=conflict_claims,
                required=required,
                blockers=["multiple_exclusive_outcomes_satisfied"],
                as_of=as_of,
            )
            for outcome in spec.outcomes
        ]
    if len(terminal_yes) != 1:
        return evaluations
    winner = terminal_yes[0]
    decisive = _claims_for_outcome(spec, claims, winner.outcome_name)
    return [
        winner
        if evaluation.outcome_name == winner.outcome_name
        else _evaluation(
            spec,
            outcome_name=evaluation.outcome_name,
            state="TERMINAL_NO",
            terminal=True,
            claims=decisive,
            required=required,
            blockers=[],
            as_of=as_of,
        )
        for evaluation in evaluations
    ]


def _outcome_deadline(
    spec: RuleSpec,
    outcome_name: str,
) -> datetime | None:
    binding = next(
        (
            outcome
            for outcome in spec.outcomes
            if outcome.name == outcome_name
        ),
        None,
    )
    return _stamp(
        (
            binding.deadline_iso
            if binding is not None and binding.deadline_iso
            else spec.semantics.window.end_iso
        )
    )


def _event_evaluation(
    spec: RuleSpec,
    outcome: str,
    claims: list[EvidenceClaim],
    *,
    as_of: datetime | None,
    require_settlement: bool,
) -> RuleEvaluation:
    required = spec.semantics.resolution_policy.independent_confirmation_sources
    deadline = _outcome_deadline(spec, outcome)
    in_window_claims = [
        claim
        for claim in claims
        if _claim_matches_outcome_window(spec, outcome, claim)
    ]
    if any(claim.assertion == "CONFLICTING" for claim in claims):
        return _evaluation(
            spec,
            outcome_name=outcome,
            state="AMBIGUOUS",
            terminal=False,
            claims=claims,
            required=required,
            blockers=["conflicting_evidence"],
            as_of=as_of,
        )
    terminal_claims = _authorized_terminal_claims(
        in_window_claims,
        require_settlement=require_settlement,
    )
    terminal_groups = _independent_count(terminal_claims)
    if terminal_groups >= required:
        return _evaluation(
            spec,
            outcome_name=outcome,
            state="TERMINAL_YES",
            terminal=True,
            claims=terminal_claims,
            required=required,
            blockers=[],
            as_of=as_of,
        )
    raw_terminal = [
        claim
        for claim in in_window_claims
        if claim.assertion == "PREDICATE_SATISFIED"
        and claim.predicate_matches
        and claim.temporal_relation not in {"BEFORE_WINDOW", "AFTER_WINDOW"}
    ]
    if raw_terminal:
        blockers: list[str] = []
        if not terminal_claims:
            source_authorized = [
                claim
                for claim in raw_terminal
                if _terminal_source_authorized(
                    claim,
                    require_settlement=require_settlement,
                )
            ]
            if source_authorized:
                blockers.append("terminal_claim_timestamp_unknown")
            else:
                blockers.append(
                    "terminal_claim_missing_settlement_source"
                    if require_settlement
                    else "terminal_claim_source_not_authorized"
                )
        if terminal_groups < required:
            blockers.append(
                f"insufficient_independent_confirmations:{terminal_groups}/{required}"
            )
        return _evaluation(
            spec,
            outcome_name=outcome,
            state="AMBIGUOUS",
            terminal=False,
            claims=raw_terminal,
            required=required,
            blockers=blockers,
            as_of=as_of,
        )

    raw_foreclosure = [
        claim
        for claim in claims
        if claim.assertion == "PREDICATE_FORECLOSED"
        and claim.predicate_matches
    ]
    foreclosure = [
        claim
        for claim in raw_foreclosure
        if _terminal_source_authorized(
            claim,
            require_settlement=require_settlement,
        )
        and bool(claim.published_at or claim.event_at)
    ]
    foreclosure_groups = _independent_count(foreclosure)
    if (
        spec.semantics.resolution_policy.terminal_no_monotonic
        and foreclosure_groups >= required
    ):
        return _evaluation(
            spec,
            outcome_name=outcome,
            state="TERMINAL_NO",
            terminal=True,
            claims=foreclosure,
            required=required,
            blockers=[],
            as_of=as_of,
        )
    if (
        spec.semantics.resolution_policy.terminal_no_monotonic
        and raw_foreclosure
    ):
        blockers = []
        source_authorized = [
            claim
            for claim in raw_foreclosure
            if _terminal_source_authorized(
                claim,
                require_settlement=require_settlement,
            )
        ]
        if not foreclosure:
            blockers.append(
                "terminal_foreclosure_timestamp_unknown"
                if source_authorized
                else (
                    "terminal_foreclosure_missing_settlement_source"
                    if require_settlement
                    else "terminal_foreclosure_source_not_authorized"
                )
            )
        if foreclosure_groups < required:
            blockers.append(
                "insufficient_independent_foreclosure_confirmations:"
                f"{foreclosure_groups}/{required}"
            )
        return _evaluation(
            spec,
            outcome_name=outcome,
            state="AMBIGUOUS",
            terminal=False,
            claims=raw_foreclosure,
            required=required,
            blockers=blockers,
            as_of=as_of,
        )

    now = _aware(as_of or datetime.now(timezone.utc))
    if (
        spec.outcome_topology != "EXCLUSIVE_ONE_OF_N"
        and deadline is not None
        and now >= deadline
        and not terminal_claims
    ):
        return _evaluation(
            spec,
            outcome_name=outcome,
            state="AMBIGUOUS",
            terminal=False,
            claims=claims,
            required=required,
            blockers=[
                "awaiting_explicit_resolution_evidence",
                "deadline_silence_is_not_terminal_no",
            ],
            as_of=as_of,
        )
    return _fallback_evaluation(spec, outcome, claims, as_of=as_of)


def _fallback_evaluation(
    spec: RuleSpec,
    outcome: str,
    claims: list[EvidenceClaim],
    *,
    as_of: datetime | None,
) -> RuleEvaluation:
    required = spec.semantics.resolution_policy.independent_confirmation_sources
    precedence = [
        ("SCHEDULED", "STRONG_YES"),
        ("PATHWAY_SUPPORT", "PATHWAY_YES"),
        ("CANCELLED", "STRONG_NO"),
        ("PATHWAY_OBSTACLE", "PATHWAY_NO"),
        ("QUALIFYING_BREACH", "STRONG_NO"),
        ("EXCLUDED_ACTIVITY", "RULE_IRRELEVANT"),
        ("NONE", "RULE_IRRELEVANT"),
    ]
    for assertion, state in precedence:
        selected = [claim for claim in claims if claim.assertion == assertion]
        if selected:
            return _evaluation(
                spec,
                outcome_name=outcome,
                state=state,
                terminal=False,
                claims=selected,
                required=required,
                blockers=[],
                as_of=as_of,
            )
    return _evaluation(
        spec,
        outcome_name=outcome,
        state="AMBIGUOUS",
        terminal=False,
        claims=claims,
        required=required,
        blockers=["no_decisive_rule_bound_claim"],
        as_of=as_of,
    )


def _evaluation(
    spec: RuleSpec,
    *,
    outcome_name: str,
    state: str,
    terminal: bool,
    claims: list[EvidenceClaim],
    required: int,
    blockers: list[str],
    as_of: datetime | None,
) -> RuleEvaluation:
    if state not in EVIDENCE_STATES:
        raise ValueError(f"invalid evaluator state {state}")
    groups = sorted(
        {
            claim.independence_group
            for claim in claims
            if claim.independence_group
        }
    )
    evaluated = RuleEvaluation(
        schema_version=RULE_EVALUATION_SCHEMA_VERSION,
        market_id=spec.market_id,
        rule_spec_sha256=spec.spec_sha256,
        rule_family=spec.semantics.rule_family,
        outcome_name=outcome_name,
        evidence_state=state,
        terminal=terminal,
        claim_sha256s=sorted({claim.claim_sha256 for claim in claims}),
        independence_groups=groups,
        independent_confirmations=len(groups),
        required_confirmations=required,
        clauses_satisfied=sorted(
            {
                clause
                for claim in claims
                for clause in claim.clauses_satisfied
            }
        ),
        clauses_violated=sorted(
            {
                clause
                for claim in claims
                for clause in claim.clauses_violated
            }
        ),
        blockers=sorted(set(blockers)),
        evaluator_version=EVALUATOR_VERSION,
        evaluated_at=_aware(
            as_of or datetime.now(timezone.utc)
        ).isoformat(),
    )
    validated = RuleEvaluation.from_dict(evaluated.as_dict())
    validated.validate_spec_binding(spec)
    return validated


def _claims_for_outcome(
    spec: RuleSpec,
    claims: list[EvidenceClaim],
    outcome: str,
) -> list[EvidenceClaim]:
    if spec.outcome_topology == "MONOTONE_DEADLINE_LADDER":
        binding = next(item for item in spec.outcomes if item.name == outcome)
        return [
            claim
            for claim in claims
            if claim.target_outcome == outcome
            or (
                not claim.target_outcome
                and _ladder_claim_applies_to_leg(claim, binding)
            )
        ]
    return [
        claim
        for claim in claims
        if (
            claim.target_outcome == outcome
            or (
                len(spec.outcomes) == 1
                and not claim.target_outcome
            )
        )
    ]


def _ladder_claim_applies_to_leg(
    claim: EvidenceClaim,
    outcome: OutcomeBinding,
) -> bool:
    """One event-level claim (no target_outcome) propagates deterministically
    to every ladder leg it could have qualified: the leg must already have
    existed when the announcement happened. The upper deadline bound is
    enforced separately by _claim_matches_outcome_window."""

    announced_at = _stamp(claim.event_at) or _stamp(claim.published_at)
    if announced_at is None:
        return False
    leg_start = _stamp(outcome.start_iso)
    if leg_start is not None and announced_at < leg_start:
        return False
    return True


def _claim_matches_outcome_window(
    spec: RuleSpec,
    outcome_name: str,
    claim: EvidenceClaim,
) -> bool:
    if claim.temporal_relation in {"BEFORE_WINDOW", "AFTER_WINDOW"}:
        return False
    binding = next(
        (
            outcome
            for outcome in spec.outcomes
            if outcome.name == outcome_name
        ),
        None,
    )
    deadline = _outcome_deadline(spec, outcome_name)
    event_at = _stamp(claim.event_at)
    date_local = _uses_date_local_independent_windows(spec)
    if event_at is None:
        # Independent daily-bin markets require an occurrence timestamp.  A
        # model-selected target label is not proof that the event happened on
        # that label's local calendar day.
        return not date_local
    if binding is not None:
        start = _stamp(binding.start_iso)
        if start is not None:
            if claim.assertion == "COUNT_OBSERVED":
                try:
                    start_zone = ZoneInfo(
                        binding.deadline_timezone
                        or spec.semantics.window.timezone
                        or "UTC"
                    )
                except ZoneInfoNotFoundError:
                    return False
                if (
                    event_at.astimezone(start_zone).date()
                    < start.astimezone(start_zone).date()
                ):
                    return False
            elif event_at < start:
                return False
    if deadline is not None and event_at > deadline:
        return False
    if not date_local or binding is None or deadline is None:
        return True
    try:
        zone = ZoneInfo(
            binding.deadline_timezone
            or spec.semantics.window.timezone
            or "UTC"
        )
    except ZoneInfoNotFoundError:
        return False
    return event_at.astimezone(zone).date() == deadline.astimezone(zone).date()


def _uses_date_local_independent_windows(spec: RuleSpec) -> bool:
    if (
        spec.outcome_topology != "INDEPENDENT_MULTI"
        or spec.semantics.rule_family != "OCCURRENCE_BEFORE_DEADLINE"
        or len(spec.outcomes) < 2
    ):
        return False
    local_dates: set[tuple[str, str]] = set()
    for outcome in spec.outcomes:
        deadline = _stamp(outcome.deadline_iso)
        zone_name = (
            outcome.deadline_timezone
            or spec.semantics.window.timezone
            or "UTC"
        )
        if deadline is None:
            return False
        try:
            zone = ZoneInfo(zone_name)
        except ZoneInfoNotFoundError:
            return False
        local_dates.add((zone_name, deadline.astimezone(zone).date().isoformat()))
    return len(local_dates) == len(spec.outcomes)


def _authorized_terminal_claims(
    claims: list[EvidenceClaim],
    *,
    require_settlement: bool,
) -> list[EvidenceClaim]:
    return [
        claim
        for claim in claims
        if claim.assertion == "PREDICATE_SATISFIED"
        and claim.predicate_matches
        and claim.temporal_relation not in {"BEFORE_WINDOW", "AFTER_WINDOW"}
        and _terminal_source_authorized(
            claim,
            require_settlement=require_settlement,
        )
        and bool(claim.published_at or claim.event_at)
    ]


def _authorized(claims: list[EvidenceClaim]) -> bool:
    return bool(claims) and all(
        bool({"CONFIRMATION", "SETTLEMENT"} & set(claim.source_roles))
        for claim in claims
    )


def _authorized_for_terminal(claims: list[EvidenceClaim]) -> bool:
    return _authorized(claims) and all(
        bool(claim.published_at or claim.event_at)
        for claim in claims
    )


def _terminal_source_authorized(
    claim: EvidenceClaim,
    *,
    require_settlement: bool,
) -> bool:
    return (
        "SETTLEMENT" in claim.source_roles
        if require_settlement
        else _authorized([claim])
    )


def _independent_count(claims: list[EvidenceClaim]) -> int:
    return len(
        {
            claim.independence_group
            for claim in claims
            if claim.independence_group
        }
    )


def _compare(value: Decimal, threshold: Decimal, comparator: str) -> bool:
    return {
        "GREATER_THAN": value > threshold,
        "GREATER_THAN_OR_EQUAL": value >= threshold,
        "LESS_THAN": value < threshold,
        "LESS_THAN_OR_EQUAL": value <= threshold,
    }[comparator]


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _duration_hours(value: str, unit: str) -> Decimal | None:
    number = _decimal(value)
    normalized = unit.strip().casefold().rstrip("s")
    if number is None:
        return None
    multipliers = {
        "hour": Decimal("1"),
        "day": Decimal("24"),
        "week": Decimal("168"),
        "minute": Decimal("0.01666666666666666666666666667"),
    }
    multiplier = multipliers.get(normalized)
    return number * multiplier if multiplier is not None else None


def _claim_time(claim: EvidenceClaim) -> datetime:
    return (
        _stamp(claim.event_at)
        or _stamp(claim.published_at)
        or _stamp(claim.extracted_at)
        or datetime.min.replace(tzinfo=timezone.utc)
    )


def _stamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(parsed)


def _aware(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


FamilyEvaluator = Callable[..., list[RuleEvaluation]]
FAMILY_EVALUATORS: dict[str, FamilyEvaluator] = {
    "OCCURRENCE_BEFORE_DEADLINE": _occurrence,
    "CATEGORICAL_EXCLUSIVE": _categorical,
    "SOURCE_LOCKED_ANNOUNCEMENT": _source_locked,
    "STATUS_AT_DEADLINE": _status,
    "NUMERIC_THRESHOLD": _numeric,
    "DURATION_REQUIREMENT": _duration,
}


__all__ = [
    "EVALUATOR_VERSION",
    "FAMILY_EVALUATORS",
    "SUPPORTED_EVALUATOR_FAMILIES",
    "SUPPORTED_OUTCOME_TOPOLOGIES",
    "evaluate_rule",
]
