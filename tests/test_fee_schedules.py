from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from polybot.core.execution import PaperTradingAdapter
from polybot.core.fees import (
    FEE_POLICY_VERSION,
    FeeScheduleSnapshot,
    explicit_zero_fee_schedule,
)
from polybot.discovery.sources import build_source_plan
from polybot.rules.compiler import fixture_semantics
from polybot.rules.contracts import RuleSpec
from polybot.rules.decision import (
    ConfirmationDecisionEngine,
    ConfirmationPolicy,
)
from polybot.rules.evaluators import evaluate_rule
from test_rule_contracts import _golden_rules, context_for_case
from test_rule_evidence import _claim, _portfolio, _Quotes


def _occurrence():
    case = next(
        item
        for item in _golden_rules()
        if item["expected_family"] == "OCCURRENCE_BEFORE_DEADLINE"
    )
    context = context_for_case(case, strong_analysis=True)
    spec = RuleSpec.from_context(
        context,
        fixture_semantics(context),
        compiler_model="fixture",
        compiled_at="2026-07-25T00:00:00+00:00",
    )
    evaluation = evaluate_rule(
        spec,
        [
            _claim(
                spec,
                article_id="reuters",
                assertion="PREDICATE_SATISFIED",
                group="reuters",
            ),
            _claim(
                spec,
                article_id="ap",
                assertion="PREDICATE_SATISFIED",
                group="associated_press",
            ),
        ],
        as_of=datetime(2026, 7, 25, 1, tzinfo=timezone.utc),
    )[0]
    return context, spec, evaluation


def _adapter(
    tmp_path: Path,
    spec: RuleSpec,
    schedule: FeeScheduleSnapshot,
    *,
    ask: float = 0.85,
) -> PaperTradingAdapter:
    binding = spec.outcomes[0]
    quotes = _Quotes(
        {
            binding.yes_token_id: {
                "asks": [[ask, 1000]],
                "bids": [[ask - 0.02, 1000]],
                "staleness": 0.0,
                "revision": "yes",
            },
            binding.no_token_id: {
                "asks": [[1 - ask + 0.02, 1000]],
                "bids": [[1 - ask, 1000]],
                "staleness": 0.0,
                "revision": "no",
            },
        }
    )
    return PaperTradingAdapter(
        state_path=tmp_path / "paper.json",
        quote_provider=quotes,
        token_pairs=[(binding.yes_token_id, binding.no_token_id)],
        fee_schedules={
            binding.yes_token_id: schedule,
            binding.no_token_id: schedule,
        },
        slippage_bps=0,
        max_book_age_seconds=10,
    )


def test_fee_schedule_formula_and_explicit_zero_declaration() -> None:
    observed = "2026-07-25T00:00:00+00:00"
    zero = explicit_zero_fee_schedule(observed)
    assert zero.fees_enabled is False
    assert zero.fee_for_fill(100, 0.50) == 0.0

    politics = FeeScheduleSnapshot.from_gamma(
        fees_enabled=True,
        fee_schedule={
            "rate": 0.04,
            "exponent": 1,
            "takerOnly": True,
            "rebateRate": 0.25,
        },
        observed_at=observed,
    )
    assert politics.fee_for_fill(100, 0.50) == 1.0
    assert politics.fee_for_fill(100, 0.85) == 0.51
    assert FeeScheduleSnapshot.from_dict(
        politics.as_dict()
    ).schedule_sha256 == politics.schedule_sha256


def test_dynamic_fee_is_in_edge_fill_and_decision_proof(
    tmp_path: Path,
) -> None:
    context, spec, evaluation = _occurrence()
    schedule = FeeScheduleSnapshot.from_gamma(
        fees_enabled=True,
        fee_schedule={
            "rate": 0.04,
            "exponent": 1,
            "takerOnly": True,
            "rebateRate": 0.25,
        },
        observed_at="2026-07-25T00:00:00+00:00",
    )
    context = replace(
        context,
        outcomes=[replace(context.outcomes[0], fee_schedule=schedule)],
    )
    engine = ConfirmationDecisionEngine(
        context=context,
        spec=spec,
        adapter=_adapter(tmp_path, spec, schedule),
        portfolio=_portfolio(tmp_path, context),
        policy=ConfirmationPolicy(requested_usd=25),
    )
    draft = engine.decide(
        evaluation,
        created_at="2026-07-25T01:00:00+00:00",
    )
    assert draft.intent.action == "ENTER_YES"
    assert draft.fee_buffer == 0.0051
    result = engine.execute(draft)
    assert result["executed"] is True
    assert result["fee_usd"] > 0
    proof = engine.proof(
        source_plan=build_source_plan(context, spec),
        evaluation=evaluation,
        draft=draft,
        claims=[],
        execution_result=result,
        created_at="2026-07-25T01:00:01+00:00",
    )
    assert proof.fee_policy_version == FEE_POLICY_VERSION
    assert proof.fee_schedule_status == "VERIFIED"
    assert proof.fee_schedule_sha256 == schedule.schedule_sha256


def test_missing_or_stale_fee_schedule_blocks_entry(
    tmp_path: Path,
) -> None:
    context, spec, evaluation = _occurrence()
    missing = replace(
        context,
        outcomes=[
            replace(
                context.outcomes[0],
                fee_schedule=None,
                fee_schedule_error="feesEnabled is missing",
            )
        ],
    )
    engine = ConfirmationDecisionEngine(
        context=missing,
        spec=spec,
        adapter=_adapter(
            tmp_path,
            spec,
            explicit_zero_fee_schedule(
                "2026-07-25T00:00:00+00:00"
            ),
        ),
        portfolio=_portfolio(tmp_path, missing),
        policy=ConfirmationPolicy(),
    )
    draft = engine.decide(
        evaluation,
        created_at="2026-07-25T01:00:00+00:00",
    )
    assert draft.intent.action == "NO_ACTION"
    assert any(
        item.startswith("fee_schedule_unavailable")
        for item in draft.intent.blockers
    )

    stale_schedule = explicit_zero_fee_schedule(
        "2026-07-20T00:00:00+00:00"
    )
    stale = replace(
        context,
        outcomes=[
            replace(context.outcomes[0], fee_schedule=stale_schedule)
        ],
    )
    engine = ConfirmationDecisionEngine(
        context=stale,
        spec=spec,
        adapter=_adapter(tmp_path, spec, stale_schedule),
        portfolio=_portfolio(tmp_path, stale),
        policy=ConfirmationPolicy(max_fee_schedule_age_hours=24),
    )
    draft = engine.decide(
        evaluation,
        created_at="2026-07-25T01:00:00+00:00",
    )
    assert draft.intent.action == "NO_ACTION"
    assert any(
        item.startswith("fee_schedule_stale")
        for item in draft.intent.blockers
    )
