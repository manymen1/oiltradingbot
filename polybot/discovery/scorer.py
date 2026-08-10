from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from polybot.rules.contracts import (
    RuleSpec,
    VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY,
)

from .config import ScoringConfig
from .types import MarketContext, SourcePlan


def correlation_group(context: MarketContext) -> str:
    """Concentration key: markets deciding on the same actors move together.
    Two markets about the same parties are one risk, not two."""
    analysis = context.rule_analysis
    if analysis and analysis.parties:
        return "|".join(sorted(analysis.parties))
    if analysis and analysis.locations:
        return "|".join(sorted(analysis.locations))
    return "uncategorized"


def grade_market(
    context: MarketContext,
    scoring: ScoringConfig,
    *,
    now: datetime | None = None,
    group_counts: dict[str, int] | None = None,
    spread: float | None = None,
    rule_spec: RuleSpec | None = None,
    source_plan: SourcePlan | None = None,
    require_rule_spec: bool = False,
    paper_families: set[str] | None = None,
    live_confirmation_families: set[str] | None = None,
) -> MarketContext:
    """Compute per-dimension scores, apply the hard safety rules, and assign
    the market state. Pure function: returns an updated copy of the context.

    `group_counts` is the number of OTHER tradeable markets in each
    correlation group (exclude the market being graded)."""
    now = now or datetime.now(timezone.utc)
    group_counts = group_counts or {}
    reasons: list[str] = []
    scores: dict[str, float] = {}
    analysis = context.rule_analysis
    group = correlation_group(context)

    effective_deadline = _effective_deadline_iso(
        context,
        rule_spec=rule_spec,
    )
    days_left = _days_to_deadline(effective_deadline, now)
    scores["liquidity"] = context.liquidity
    scores["volume"] = context.volume
    if days_left is not None:
        scores["time_horizon_days"] = round(days_left, 2)
    if spread is not None:
        scores["spread"] = spread
    scores["correlation_group_count"] = float(group_counts.get(group, 0))

    # Hard terminal conditions first.
    if context.closed or not context.active or not context.accepting_orders:
        return _finalize(context, "CLOSED", ["market_closed_or_not_accepting_orders"], scores, group)
    if days_left is not None and days_left <= 0:
        return _finalize(context, "CLOSED", ["deadline_passed"], scores, group)

    if len(context.rule_text.strip()) < scoring.min_rule_text_chars:
        reasons.append("missing_or_short_resolution_rules")
        return _finalize(context, "RULES_REVIEW_REQUIRED", reasons, scores, group)
    if not context.outcomes or not all(
        o.yes_token_id and o.no_token_id and o.condition_id
        for o in context.outcomes
    ):
        return _finalize(context, "RULES_REVIEW_REQUIRED", ["unverified_token_mapping"], scores, group)
    if analysis is None:
        return _finalize(context, "RULES_REVIEW_REQUIRED", ["rule_analysis_missing"], scores, group)

    family = ""
    compiler_is_fixture = False
    compiler_is_reviewed = False
    deadline_mismatch_names: list[str] = []
    if require_rule_spec:
        if rule_spec is None:
            return _finalize(
                context,
                "RULES_REVIEW_REQUIRED",
                ["valid_rule_spec_missing"],
                scores,
                group,
            )
        try:
            rule_spec.validate_context_binding(context)
        except ValueError as exc:
            return _finalize(
                context,
                "RULES_REVIEW_REQUIRED",
                [f"stale_or_invalid_rule_spec:{exc}"],
                scores,
                group,
            )
        family = rule_spec.semantics.rule_family
        if (
            rule_spec.deadline_authority_policy
            == VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY
        ):
            deadline_mismatch_names = sorted(
                outcome.name
                for outcome in context.outcomes
                if (
                    outcome.active
                    and not outcome.closed
                    and outcome.deadline_consistency == "MISMATCH"
                )
            )
            if deadline_mismatch_names:
                scores["rule_deadline_paper_override"] = 1.0
        scores["rule_spec_valid"] = 1.0
        scores["rule_spec_family_supported"] = float(
            family in (paper_families or set())
        )
        compiler_is_fixture = rule_spec.compiler_model == "fixture"
        compiler_is_reviewed = rule_spec.compiler_model.startswith("reviewed:")
        if family == "SUBJECTIVE_DISCRETIONARY":
            return _finalize(
                context,
                "MONITOR_ONLY",
                ["subjective_rule_family"],
                scores,
                group,
            )
        if family not in (paper_families or set()):
            return _finalize(
                context,
                "MONITOR_ONLY",
                [f"unsupported_rule_family:{family}"],
                scores,
                group,
            )
        if source_plan is None:
            return _finalize(
                context,
                "RULES_REVIEW_REQUIRED",
                ["rule_spec_source_plan_missing"],
                scores,
                group,
            )
        if source_plan.missing_required_source_refs:
            return _finalize(
                context,
                "MONITOR_ONLY",
                [
                    "required_rule_source_unresolved:"
                    + ",".join(source_plan.missing_required_source_refs)
                ],
                scores,
                group,
            )
        try:
            from .sources import validate_source_plan_freshness

            validate_source_plan_freshness(
                context,
                source_plan,
                rule_spec,
            )
        except ValueError as exc:
            return _finalize(
                context,
                "RULES_REVIEW_REQUIRED",
                [f"stale_or_invalid_source_plan:{exc}"],
                scores,
                group,
            )
        scores["source_plan_valid"] = 1.0

    scores.update(
        {
            "rule_clarity": analysis.rule_clarity,
            "evidence_observability": analysis.evidence_observability,
            "resolution_risk": analysis.resolution_risk,
            "automation_suitability": analysis.automation_suitability,
        }
    )

    # Book-aware sizing recommendation: liquidity sizes orders, it does not
    # (by default) gate eligibility -- thin markets are the niche.
    if scoring.small_live_enabled:
        scores["recommended_max_order_usd"] = round(
            max(scoring.small_live_min_order_usd, context.liquidity * scoring.small_live_liquidity_fraction), 2
        )

    discretionary_paper_only = False
    if analysis.discretionary:
        if not scoring.allow_discretionary_paper:
            return _finalize(context, "MONITOR_ONLY", ["discretionary_rules"], scores, group)
        # Paper only, always: a judgment word in the rules bars live trading
        # forever, but must not bar us from LEARNING whether the classifier
        # reads that market correctly. Enforced below as a live blocker that
        # no other score can clear.
        discretionary_paper_only = True
    if analysis.rule_clarity < scoring.min_clarity_paper:
        return _finalize(context, "MONITOR_ONLY", [f"rule_clarity_below_paper_threshold:{analysis.rule_clarity}"], scores, group)
    if analysis.evidence_observability < scoring.min_observability_paper:
        return _finalize(context, "MONITOR_ONLY", [f"evidence_observability_below_paper_threshold:{analysis.evidence_observability}"], scores, group)
    if scoring.min_liquidity_paper > 0 and context.liquidity < scoring.min_liquidity_paper:
        return _finalize(context, "MONITOR_ONLY", [f"liquidity_below_paper_threshold:{context.liquidity:g}"], scores, group)

    live_blockers: list[str] = []
    if discretionary_paper_only:
        # Unconditional: discretionary rules never reach live, no matter how
        # strong every other score is.
        live_blockers.append("discretionary_rules_paper_only")
    if analysis.rule_clarity < scoring.min_clarity_live:
        live_blockers.append(f"rule_clarity_below_live_threshold:{analysis.rule_clarity}")
    if analysis.evidence_observability < scoring.min_observability_live:
        live_blockers.append(f"evidence_observability_below_live_threshold:{analysis.evidence_observability}")
    if analysis.automation_suitability < scoring.min_automation_live:
        live_blockers.append(f"automation_suitability_below_live_threshold:{analysis.automation_suitability}")
    if analysis.resolution_risk > scoring.max_resolution_risk_live:
        live_blockers.append(f"resolution_risk_above_live_threshold:{analysis.resolution_risk}")
    if scoring.min_liquidity_live > 0 and context.liquidity < scoring.min_liquidity_live:
        live_blockers.append(f"liquidity_below_live_threshold:{context.liquidity:g}")
    if spread is not None and spread > scoring.max_spread_live:
        live_blockers.append(f"spread_above_live_threshold:{spread}")
    if days_left is not None and days_left > scoring.max_days_to_deadline_live:
        live_blockers.append(f"time_horizon_above_live_threshold:{days_left:.0f}d")
    if group_counts.get(group, 0) >= scoring.max_markets_per_correlation_group:
        live_blockers.append(f"correlation_group_limit:{group}")
    if require_rule_spec and family not in (
        live_confirmation_families or set()
    ):
        live_blockers.append(f"rule_family_not_live_promoted:{family}")
    if compiler_is_fixture:
        live_blockers.append("fixture_rule_spec_not_live_eligible")
    if compiler_is_reviewed:
        live_blockers.append("reviewed_rule_spec_paper_only")
    if deadline_mismatch_names:
        live_blockers.append(
            "gamma_rule_deadline_mismatch_paper_only:"
            + ",".join(deadline_mismatch_names)
        )

    if analysis.model == "fixture" and not scoring.allow_fixture_analysis_live:
        # The offline heuristic analyzer is a test fixture, not a rule reader:
        # a config mistake must not let fixture-graded markets trade live.
        return _finalize(context, "PAPER_ELIGIBLE", ["fixture_analysis_not_live_eligible"] + live_blockers, scores, group)

    if live_blockers:
        # Back-compat bypass for operators who configured explicit liquidity
        # floors: when liquidity is the only failed live gate, stay live at
        # book-absorbable size instead of demoting to paper.
        if scoring.small_live_enabled and all(b.startswith("liquidity_below_live_threshold") for b in live_blockers):
            recommended = scores.get("recommended_max_order_usd", scoring.small_live_min_order_usd)
            return _finalize(
                context,
                "LIVE_CONFIRMATION_ELIGIBLE",
                [f"small_size_live:recommended_max_order_usd={recommended}"] + live_blockers,
                scores,
                group,
            )
        return _finalize(context, "PAPER_ELIGIBLE", live_blockers, scores, group)
    return _finalize(context, "LIVE_CONFIRMATION_ELIGIBLE", ["all_live_gates_passed"], scores, group)


def _finalize(context: MarketContext, state: str, reasons: list[str], scores: dict[str, float], group: str) -> MarketContext:
    payload: dict[str, Any] = {
        **context.as_dict(),
        "state": state,
        "state_reasons": reasons,
        "scores": scores,
        "correlation_group": group,
    }
    return MarketContext.from_dict(payload)


def _days_to_deadline(deadline_iso: str, now: datetime) -> float | None:
    if not deadline_iso:
        return None
    text = deadline_iso.strip().replace("Z", "+00:00")
    try:
        deadline = datetime.fromisoformat(text)
    except ValueError:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return (deadline - now).total_seconds() / 86400.0


def _effective_deadline_iso(
    context: MarketContext,
    *,
    rule_spec: RuleSpec | None,
) -> str:
    """Use the last active leg for grouped-market lifecycle decisions.

    Gamma parent events can retain an obsolete endDate while newer child legs
    remain open.  A reviewed verbatim-deadline spec is authoritative for its
    bound legs; otherwise the immutable child Gamma deadlines are used.
    """

    if context.kind != "grouped":
        return context.deadline_iso
    active_ids = {
        outcome.condition_id
        for outcome in context.outcomes
        if outcome.active and not outcome.closed and outcome.accepting_orders
    }
    if not active_ids:
        return context.deadline_iso
    candidates: list[str] = []
    if (
        rule_spec is not None
        and rule_spec.deadline_authority_policy
        == VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY
    ):
        candidates = [
            outcome.deadline_iso
            for outcome in rule_spec.outcomes
            if outcome.condition_id in active_ids and outcome.deadline_iso
        ]
    if not candidates:
        candidates = [
            outcome.deadline_iso
            for outcome in context.outcomes
            if outcome.condition_id in active_ids and outcome.deadline_iso
        ]
    parsed = [
        (value, _deadline_datetime(value))
        for value in candidates
    ]
    valid = [(value, stamp) for value, stamp in parsed if stamp is not None]
    if not valid:
        return context.deadline_iso
    return max(valid, key=lambda item: item[1])[0]


def _deadline_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
