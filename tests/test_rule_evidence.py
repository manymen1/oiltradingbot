from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from polybot.core.budget import ClassifierBudgetStore
from polybot.core.config import ClassifierConfig
from polybot.core.execution import PaperTradingAdapter
from polybot.core.portfolio import (
    PortfolioAllocator,
    PortfolioConfig,
    PortfolioLink,
)
from polybot.core.types import Article
from polybot.discovery.config import OpportunityConfig
from polybot.discovery.sources import build_source_plan
from polybot.rules.compiler import fixture_semantics
from polybot.rules.contracts import (
    EVIDENCE_CLAIM_SCHEMA_VERSION,
    EvidenceClaim,
    RuleSpec,
)
from polybot.rules.decision import (
    ConfirmationDecisionEngine,
    ConfirmationPolicy,
)
from polybot.rules.evaluators import evaluate_rule
from polybot.rules.evidence import EvidenceExtractor
from polybot.rules.fast_evidence import OFFICIAL_CLAIM_ADAPTER_VERSION
from polybot.rules.store import RuleStore
from test_rule_contracts import _golden_rules, context_for_case


def _case(family: str) -> dict:
    return next(
        item
        for item in _golden_rules()
        if item["expected_family"] == family
    )


def _spec(family: str):
    context = context_for_case(_case(family), strong_analysis=True)
    spec = RuleSpec.from_context(
        context,
        fixture_semantics(context),
        compiler_model="anthropic:test",
        compiled_at="2026-07-25T00:00:00+00:00",
    )
    return context, spec


def _multi_spec(
    family: str,
    topology: str,
) -> RuleSpec:
    _context, base = _spec(family)
    first = replace(
        base.outcomes[0],
        name="july_25",
        label="July 25",
        market_slug=f"{base.market_id}-july-25",
        condition_id=f"{base.market_id}-july-25-condition",
        yes_token_id=f"{base.market_id}-july-25-yes",
        no_token_id=f"{base.market_id}-july-25-no",
        deadline_iso="2026-07-25T23:59:59+00:00",
        gamma_deadline_iso="2026-07-25T23:59:59+00:00",
        start_iso="2026-07-01T00:00:00+00:00",
    )
    second = replace(
        base.outcomes[0],
        name="august_31",
        label="August 31",
        market_slug=f"{base.market_id}-august-31",
        condition_id=f"{base.market_id}-august-31-condition",
        yes_token_id=f"{base.market_id}-august-31-yes",
        no_token_id=f"{base.market_id}-august-31-no",
        deadline_iso="2026-08-31T23:59:59+00:00",
        gamma_deadline_iso="2026-08-31T23:59:59+00:00",
        start_iso="2026-07-01T00:00:00+00:00",
    )
    semantics = replace(
        base.semantics,
        window=replace(
            base.semantics.window,
            end_iso="2026-08-31T23:59:59+00:00",
        ),
    )
    return RuleSpec.from_dict(
        replace(
            base,
            kind="grouped",
            outcome_topology=topology,
            outcomes=[first, second],
            semantics=semantics,
        ).as_dict()
    )


def test_codex_cli_evidence_extractor_accepts_schema_json(
    tmp_path: Path,
) -> None:
    context, spec = _spec("OCCURRENCE_BEFORE_DEADLINE")
    plan = build_source_plan(context, spec)
    article = _article(
        "codex-article",
        "Officials scheduled talks next month.",
    )
    payload = {
        "target_outcome": spec.outcomes[0].name,
        "assertion": "SCHEDULED",
        "predicate_matches": True,
        "temporal_relation": "IN_WINDOW",
        "event_at": "",
        "observed_value": "",
        "observed_value_upper": "",
        "observed_unit": "",
        "supporting_quote": article.raw_text,
        "clauses_satisfied": [],
        "clauses_violated": [],
    }
    extractor = EvidenceExtractor(
        ClassifierConfig(
            provider="codex_cli",
            model="gpt-5.5",
            cli_binary="codex",
        ),
        RuleStore(tmp_path / "rules.sqlite3"),
        cli_runner=lambda _prompt: json.dumps(payload),
    )

    result = extractor.extract(
        context=context,
        spec=spec,
        source_plan=plan,
        article=article,
    )

    assert result.status == "EXTRACTED"
    assert result.claim is not None
    assert result.claim.rule_spec_sha256 == spec.spec_sha256


def _claim(
    spec: RuleSpec,
    *,
    article_id: str,
    assertion: str,
    group: str,
    roles: list[str] | None = None,
    target: str | None = None,
    matches: bool = True,
    temporal: str = "IN_WINDOW",
    value: str = "",
    value_upper: str = "",
    unit: str = "",
    event_at: str = "",
) -> EvidenceClaim:
    return EvidenceClaim.from_dict(
        EvidenceClaim(
            schema_version=EVIDENCE_CLAIM_SCHEMA_VERSION,
            market_id=spec.market_id,
            rule_spec_sha256=spec.spec_sha256,
            article_id=article_id,
            source_domain=f"{group}.example",
            source_organization_id=group,
            origin_organization_id=group,
            independence_group=group,
            source_roles=roles or ["CONFIRMATION"],
            published_at="2026-07-25T00:00:00+00:00",
            extracted_at="2026-07-25T00:01:00+00:00",
            target_outcome=(
                target
                if target is not None
                else spec.outcomes[0].name
            ),
            assertion=assertion,
            predicate_matches=matches,
            temporal_relation=temporal,
            event_at=event_at,
            observed_value=value,
            observed_value_upper=value_upper,
            observed_unit=unit,
            supporting_quote=f"supported quote {article_id}",
            clauses_satisfied=(
                list(spec.semantics.qualifying_clause_ids[:1])
                if matches
                else []
            ),
            clauses_violated=(
                []
                if matches
                else list(spec.semantics.exclusion_clause_ids[:1])
            ),
            model="anthropic:test",
            extraction_passes=2,
        ).as_dict()
    )


def _article(
    article_id: str,
    text: str,
    *,
    domain: str = "reuters.com",
    origin: str = "",
) -> Article:
    return Article(
        url=f"https://{domain}/{article_id}",
        domain=domain,
        title=text,
        published_at="2026-07-25T00:00:00+00:00",
        fetched_at="2026-07-25T00:00:10+00:00",
        raw_text=text,
        hash=article_id,
        source_kind="article",
        origin_organization=origin,
    )


def _envelope(payload: dict) -> str:
    return json.dumps(
        {
            "type": "result",
            "is_error": False,
            "structured_output": payload,
        }
    )


def test_extractor_binds_source_and_instrument_and_caches(
    tmp_path: Path,
) -> None:
    context, spec = _spec("OCCURRENCE_BEFORE_DEADLINE")
    plan = build_source_plan(context, spec)
    store = RuleStore(tmp_path / "rules.sqlite3")
    store.save_spec(spec)
    article = _article(
        "article-1",
        "Both senior delegations entered the room and talks began.",
    )
    extractor = EvidenceExtractor(
        ClassifierConfig(provider="rule_based"),
        store,
        passes=2,
    )
    result = extractor.extract(
        context=context,
        spec=spec,
        source_plan=plan,
        article=article,
    )
    assert result.status == "EXTRACTED"
    assert result.claim is not None
    assert result.claim.market_id == spec.market_id
    assert result.claim.rule_spec_sha256 == spec.spec_sha256
    assert result.claim.assertion == "PREDICATE_SATISFIED"
    assert result.claim.source_organization_id == "reuters"
    assert result.claim.independence_group == "reuters"
    assert "CONFIRMATION" in result.claim.source_roles
    assert len(store.extraction_passes(spec.spec_sha256, article.hash)) == 2

    cached = extractor.extract(
        context=context,
        spec=spec,
        source_plan=plan,
        article=article,
    )
    assert cached.status == "CACHED"
    assert cached.claim == result.claim
    assert len(store.extraction_passes(spec.spec_sha256, article.hash)) == 2


def test_extractor_rejects_trade_fields_and_fabricated_quote(
    tmp_path: Path,
) -> None:
    context, spec = _spec("OCCURRENCE_BEFORE_DEADLINE")
    plan = build_source_plan(context, spec)
    article = _article("article-1", "Officials scheduled talks next month.")
    valid = {
        "target_outcome": spec.outcomes[0].name,
        "assertion": "SCHEDULED",
        "predicate_matches": True,
        "temporal_relation": "IN_WINDOW",
        "event_at": "",
        "observed_value": "",
        "observed_value_upper": "",
        "observed_unit": "",
        "supporting_quote": "Officials scheduled talks next month.",
        "clauses_satisfied": list(
            spec.semantics.qualifying_clause_ids[:1]
        ),
        "clauses_violated": [],
    }
    calls = {"count": 0}

    def injected(_prompt: str) -> str:
        calls["count"] += 1
        return _envelope({**valid, "trade_action": "ENTER_YES"})

    result = EvidenceExtractor(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "injected.sqlite3"),
        cli_runner=injected,
    ).extract(
        context=context,
        spec=spec,
        source_plan=plan,
        article=article,
    )
    assert result.status == "INVALID"
    assert "unknown keys" in result.reason
    assert calls["count"] == 2

    fabricated = dict(valid)
    fabricated["supporting_quote"] = "This quote is not in the article."
    result = EvidenceExtractor(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "quote.sqlite3"),
        cli_runner=lambda _prompt: _envelope(fabricated),
    ).extract(
        context=context,
        spec=spec,
        source_plan=plan,
        article=article,
    )
    assert result.status == "INVALID"
    assert "not present" in result.reason


def test_extractor_rejects_invented_rule_clause_id(tmp_path: Path) -> None:
    context, spec = _spec("OCCURRENCE_BEFORE_DEADLINE")
    plan = build_source_plan(context, spec)
    article = _article("article-1", "Officials scheduled talks next month.")
    payload = {
        "target_outcome": spec.outcomes[0].name,
        "assertion": "SCHEDULED",
        "predicate_matches": True,
        "temporal_relation": "IN_WINDOW",
        "event_at": "",
        "observed_value": "",
        "observed_value_upper": "",
        "observed_unit": "",
        "supporting_quote": article.raw_text,
        "clauses_satisfied": ["clause_invented_by_model"],
        "clauses_violated": [],
    }

    result = EvidenceExtractor(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "rules.sqlite3"),
        cli_runner=lambda _prompt: _envelope(payload),
    ).extract(
        context=context,
        spec=spec,
        source_plan=plan,
        article=article,
    )

    assert result.status == "INVALID"
    assert "unknown rule clause ids" in result.reason


def test_extractor_requires_pass_agreement_and_atomic_budget(
    tmp_path: Path,
) -> None:
    context, spec = _spec("OCCURRENCE_BEFORE_DEADLINE")
    plan = build_source_plan(context, spec)
    article = _article("article-1", "Officials scheduled talks next month.")
    payload = {
        "target_outcome": spec.outcomes[0].name,
        "assertion": "SCHEDULED",
        "predicate_matches": True,
        "temporal_relation": "IN_WINDOW",
        "event_at": "",
        "observed_value": "",
        "observed_value_upper": "",
        "observed_unit": "",
        "supporting_quote": article.raw_text,
        "clauses_satisfied": [],
        "clauses_violated": [],
    }

    def disagree(prompt: str) -> str:
        changed = dict(payload)
        if "pass: 2 of 2" in prompt:
            changed["assertion"] = "PATHWAY_SUPPORT"
        return _envelope(changed)

    store = RuleStore(tmp_path / "rules.sqlite3")
    result = EvidenceExtractor(
        ClassifierConfig(provider="claude_cli"),
        store,
        cli_runner=disagree,
    ).extract(
        context=context,
        spec=spec,
        source_plan=plan,
        article=article,
    )
    assert result.status == "DISAGREEMENT"
    assert store.load_claim(spec.spec_sha256, article.hash) is None

    limits = ClassifierConfig(
        provider="claude_cli",
        max_escalations_per_hour=1,
        max_escalations_per_day=1,
    )
    calls = {"count": 0}
    budget = ClassifierBudgetStore(
        tmp_path,
        tmp_path / "budget.sqlite3",
    )
    result = EvidenceExtractor(
        limits,
        RuleStore(tmp_path / "budget-rules.sqlite3"),
        budget_store=budget,
        budget_limits=limits,
        cli_runner=lambda _prompt: calls.__setitem__(
            "count", calls["count"] + 1
        )
        or _envelope(payload),
    ).extract(
        context=context,
        spec=spec,
        source_plan=plan,
        article=article,
    )
    assert result.status == "BUDGET_BLOCKED"
    assert calls["count"] == 0


def test_stale_rule_or_plan_blocks_extraction(tmp_path: Path) -> None:
    context, spec = _spec("OCCURRENCE_BEFORE_DEADLINE")
    plan = build_source_plan(context, spec)
    changed = replace(
        context,
        rule_text=context.rule_text + " amended",
        rule_text_sha256="f" * 64,
        rule_version=2,
    )
    with pytest.raises(ValueError, match="stale|binding changed"):
        EvidenceExtractor(
            ClassifierConfig(provider="rule_based"),
            RuleStore(tmp_path / "rules.sqlite3"),
        ).extract(
            context=changed,
            spec=spec,
            source_plan=plan,
            article=_article("a", "talks began"),
        )


def test_occurrence_requires_independent_authorized_confirmations() -> None:
    _context, spec = _spec("OCCURRENCE_BEFORE_DEADLINE")
    one = _claim(
        spec,
        article_id="a",
        assertion="PREDICATE_SATISFIED",
        group="reuters",
    )
    mirror = replace(
        _claim(
            spec,
            article_id="b",
            assertion="PREDICATE_SATISFIED",
            group="reuters",
        ),
        source_domain="finance.yahoo.com",
    )
    evaluation = evaluate_rule(spec, [one, mirror])[0]
    assert evaluation.evidence_state == "AMBIGUOUS"
    assert evaluation.terminal is False
    assert "insufficient_independent_confirmations:1/2" in evaluation.blockers

    independent = _claim(
        spec,
        article_id="c",
        assertion="PREDICATE_SATISFIED",
        group="associated_press",
    )
    evaluation = evaluate_rule(spec, [one, mirror, independent])[0]
    assert evaluation.evidence_state == "TERMINAL_YES"
    assert evaluation.terminal is True
    assert evaluation.independent_confirmations == 2


def test_terminal_evidence_requires_timestamp_and_foreclosure_source() -> None:
    _context, spec = _spec("OCCURRENCE_BEFORE_DEADLINE")
    unknown_time = [
        replace(
            _claim(
                spec,
                article_id=article_id,
                assertion="PREDICATE_SATISFIED",
                group=group,
            ),
            published_at="",
        )
        for article_id, group in (
            ("a", "reuters"),
            ("b", "associated_press"),
        )
    ]
    evaluation = evaluate_rule(spec, unknown_time)[0]
    assert evaluation.evidence_state == "AMBIGUOUS"
    assert evaluation.terminal is False
    assert "terminal_claim_timestamp_unknown" in evaluation.blockers

    _context, source_locked = _spec("SOURCE_LOCKED_ANNOUNCEMENT")
    foreclosure = _claim(
        source_locked,
        article_id="foreclosed",
        assertion="PREDICATE_FORECLOSED",
        group="reuters",
        roles=["CONFIRMATION"],
    )
    semantics = replace(
        source_locked.semantics,
        resolution_policy=replace(
            source_locked.semantics.resolution_policy,
            terminal_no_monotonic=True,
        ),
    )
    source_locked = replace(source_locked, semantics=semantics)
    foreclosure = replace(
        foreclosure,
        rule_spec_sha256=source_locked.spec_sha256,
    )
    evaluation = evaluate_rule(source_locked, [foreclosure])[0]
    assert evaluation.evidence_state == "AMBIGUOUS"
    assert evaluation.terminal is False
    assert (
        "terminal_foreclosure_missing_settlement_source"
        in evaluation.blockers
    )


def test_scheduled_cancelled_and_excluded_are_nonterminal() -> None:
    _context, spec = _spec("OCCURRENCE_BEFORE_DEADLINE")
    for assertion, expected in (
        ("SCHEDULED", "STRONG_YES"),
        ("CANCELLED", "STRONG_NO"),
        ("EXCLUDED_ACTIVITY", "RULE_IRRELEVANT"),
    ):
        claim = _claim(
            spec,
            article_id=assertion,
            assertion=assertion,
            group="reuters",
            matches=assertion != "EXCLUDED_ACTIVITY",
        )
        evaluated = evaluate_rule(spec, [claim])[0]
        assert evaluated.evidence_state == expected
        assert evaluated.terminal is False


def test_passed_deadline_without_resolution_evidence_is_nonterminal() -> None:
    _context, spec = _spec("OCCURRENCE_BEFORE_DEADLINE")
    evaluated = evaluate_rule(
        spec,
        [],
        as_of=datetime(2027, 1, 2, tzinfo=timezone.utc),
    )[0]
    assert evaluated.evidence_state == "AMBIGUOUS"
    assert evaluated.terminal is False
    assert evaluated.blockers == [
        "awaiting_explicit_resolution_evidence",
        "deadline_silence_is_not_terminal_no",
    ]


def test_source_locked_requires_named_settlement_source() -> None:
    _context, spec = _spec("SOURCE_LOCKED_ANNOUNCEMENT")
    wrong = _claim(
        spec,
        article_id="wrong",
        assertion="PREDICATE_SATISFIED",
        group="associated_press",
        roles=["CONFIRMATION"],
    )
    evaluation = evaluate_rule(spec, [wrong])[0]
    assert evaluation.evidence_state == "AMBIGUOUS"
    assert "terminal_claim_missing_settlement_source" in evaluation.blockers

    named = _claim(
        spec,
        article_id="named",
        assertion="PREDICATE_SATISFIED",
        group="government:united_states",
        roles=["SETTLEMENT", "CONFIRMATION"],
    )
    evaluation = evaluate_rule(spec, [named])[0]
    assert evaluation.evidence_state == "TERMINAL_YES"
    assert evaluation.terminal is True


def test_categorical_winner_deterministically_forecloses_other_legs() -> None:
    _context, spec = _spec("CATEGORICAL_EXCLUSIVE")
    winner = _claim(
        spec,
        article_id="a",
        assertion="PREDICATE_SATISFIED",
        group="reuters",
        target=spec.outcomes[0].name,
    )
    second = _claim(
        spec,
        article_id="b",
        assertion="PREDICATE_SATISFIED",
        group="associated_press",
        target=spec.outcomes[0].name,
    )
    evaluations = evaluate_rule(spec, [winner, second])
    assert [item.evidence_state for item in evaluations] == [
        "TERMINAL_YES",
        "TERMINAL_NO",
    ]
    assert all(item.terminal for item in evaluations)


def test_independent_multi_evaluates_only_explicitly_targeted_outcome() -> None:
    spec = _multi_spec("OCCURRENCE_BEFORE_DEADLINE", "INDEPENDENT_MULTI")
    first_claims = [
        _claim(
            spec,
            article_id=article_id,
            assertion="PREDICATE_SATISFIED",
            group=group,
            target=spec.outcomes[0].name,
        )
        for article_id, group in (
            ("first-wire", "reuters"),
            ("first-independent", "associated_press"),
        )
    ]
    evaluations = evaluate_rule(spec, first_claims)
    assert [item.evidence_state for item in evaluations] == [
        "TERMINAL_YES",
        "AMBIGUOUS",
    ]

    untargeted = _claim(
        spec,
        article_id="untargeted",
        assertion="PREDICATE_SATISFIED",
        group="reuters",
        target="",
    )
    with pytest.raises(ValueError, match="target_outcome"):
        evaluate_rule(spec, [untargeted])


def test_deadline_ladder_uses_each_outcome_deadline() -> None:
    spec = _multi_spec("STATUS_AT_DEADLINE", "MONOTONE_DEADLINE_LADDER")
    claims = [
        _claim(
            spec,
            article_id=f"status-{outcome.name}",
            assertion="STATUS_OBSERVED",
            group="reuters",
            target=outcome.name,
            temporal=(
                "AT_DEADLINE"
                if outcome == spec.outcomes[0]
                else "UNKNOWN"
            ),
        )
        for outcome in spec.outcomes
    ]
    evaluations = evaluate_rule(
        spec,
        claims,
        as_of=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )
    assert [item.evidence_state for item in evaluations] == [
        "TERMINAL_YES",
        "STRONG_YES",
    ]
    assert [item.terminal for item in evaluations] == [True, False]


def test_per_leg_window_rejects_event_after_target_deadline() -> None:
    spec = _multi_spec(
        "OCCURRENCE_BEFORE_DEADLINE",
        "MONOTONE_DEADLINE_LADDER",
    )
    late_claims = [
        _claim(
            spec,
            article_id=article_id,
            assertion="PREDICATE_SATISFIED",
            group=group,
            target=spec.outcomes[0].name,
            event_at="2026-08-01T00:00:00+00:00",
        )
        for article_id, group in (
            ("late-wire", "reuters"),
            ("late-independent", "associated_press"),
        )
    ]
    evaluation = evaluate_rule(
        spec,
        late_claims,
        as_of=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )[0]
    assert evaluation.evidence_state == "AMBIGUOUS"
    assert evaluation.terminal is False
    assert "no_decisive_rule_bound_claim" in evaluation.blockers


def test_status_report_before_leg_deadline_never_becomes_terminal_by_aging() -> None:
    spec = _multi_spec("STATUS_AT_DEADLINE", "MONOTONE_DEADLINE_LADDER")
    report = _claim(
        spec,
        article_id="early-status",
        assertion="STATUS_OBSERVED",
        group="reuters",
        target=spec.outcomes[0].name,
        temporal="IN_WINDOW",
    )
    evaluation = evaluate_rule(
        spec,
        [report],
        as_of=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )[0]
    assert evaluation.evidence_state == "STRONG_YES"
    assert evaluation.terminal is False


def test_exclusive_topology_forecloses_other_legs_for_occurrence_family() -> None:
    spec = _multi_spec("OCCURRENCE_BEFORE_DEADLINE", "EXCLUSIVE_ONE_OF_N")
    winner_claims = [
        _claim(
            spec,
            article_id=article_id,
            assertion="PREDICATE_SATISFIED",
            group=group,
            target=spec.outcomes[0].name,
        )
        for article_id, group in (
            ("winner-wire", "reuters"),
            ("winner-independent", "associated_press"),
        )
    ]
    evaluations = evaluate_rule(spec, winner_claims)
    assert [item.evidence_state for item in evaluations] == [
        "TERMINAL_YES",
        "TERMINAL_NO",
    ]
    assert all(item.terminal for item in evaluations)


def test_numeric_duration_and_status_evaluators() -> None:
    _context, numeric = _spec("NUMERIC_THRESHOLD")
    count = _claim(
        numeric,
        article_id="count",
        assertion="COUNT_OBSERVED",
        group="reuters",
        value="11",
        unit=numeric.semantics.predicate.unit,
    )
    evaluated = evaluate_rule(numeric, [count])[0]
    assert evaluated.evidence_state == "TERMINAL_YES"
    assert evaluated.terminal is True

    uncertain = replace(
        count,
        article_id="range",
        observed_value="8",
        observed_value_upper="12",
    )
    evaluated = evaluate_rule(numeric, [uncertain])[0]
    assert evaluated.evidence_state == "AMBIGUOUS"
    assert "numeric_range_straddles_threshold" in evaluated.blockers

    _context, duration = _spec("DURATION_REQUIREMENT")
    progress = _claim(
        duration,
        article_id="progress",
        assertion="DURATION_OBSERVED",
        group="reuters",
        value="6",
        unit="hours",
    )
    assert evaluate_rule(duration, [progress])[0].evidence_state == "PATHWAY_YES"
    complete = replace(
        progress,
        article_id="complete",
        observed_value="8",
        observed_unit="days",
    )
    assert evaluate_rule(duration, [complete])[0].evidence_state == "TERMINAL_YES"
    breach = _claim(
        duration,
        article_id="breach",
        assertion="QUALIFYING_BREACH",
        group="reuters",
        event_at="2026-07-26T00:00:00+00:00",
    )
    assert evaluate_rule(duration, [complete, breach])[0].evidence_state == "STRONG_NO"

    _context, status = _spec("STATUS_AT_DEADLINE")
    observed = _claim(
        status,
        article_id="status",
        assertion="STATUS_OBSERVED",
        group="reuters",
        temporal="AT_DEADLINE",
    )
    evaluated = evaluate_rule(status, [observed])[0]
    assert evaluated.evidence_state == "TERMINAL_YES"
    assert evaluated.terminal is True


class _Quotes:
    def __init__(self, snapshots: dict[str, dict]):
        self.snapshots = snapshots

    def quote_snapshot(self, token_id: str) -> dict:
        return dict(self.snapshots[token_id])


def _paper_adapter(
    tmp_path: Path,
    spec: RuleSpec,
    *,
    yes_ask: float = 0.80,
) -> PaperTradingAdapter:
    yes = spec.outcomes[0].yes_token_id
    no = spec.outcomes[0].no_token_id
    snapshots = {
        yes: {
            "asks": [[yes_ask, 1000]],
            "bids": [[max(0.01, yes_ask - 0.02), 1000]],
            "staleness": 0.0,
            "revision": "yes-r1",
        },
        no: {
            "asks": [[round(1 - yes_ask + 0.02, 4), 1000]],
            "bids": [[round(1 - yes_ask, 4), 1000]],
            "staleness": 0.0,
            "revision": "no-r1",
        },
    }
    return PaperTradingAdapter(
        state_path=tmp_path / "paper.json",
        quote_provider=_Quotes(snapshots),
        token_pairs=[(yes, no)],
        fee_bps=0,
        slippage_bps=25,
        max_book_age_seconds=10,
    )


def _portfolio(tmp_path: Path, context) -> PortfolioLink:
    ledger = tmp_path / "allocations.json"
    from polybot.core.portfolio import AllocatorConfig

    PortfolioAllocator(ledger, AllocatorConfig()).write_caps()
    return PortfolioLink(
        PortfolioConfig(
            ledger_path=str(ledger),
            market_id=context.market_id,
            event_slug=context.event_slug,
            correlation_group="test",
            deadline_iso=context.deadline_iso,
        )
    )


def test_strict_official_envelope_uses_zero_call_deterministic_lane(
    tmp_path: Path,
) -> None:
    context, spec = _spec("SOURCE_LOCKED_ANNOUNCEMENT")
    plan = build_source_plan(context, spec)
    quote = "White House officially announces recognition."
    envelope = {
        "schema_version": 1,
        "market_id": context.market_id,
        "rule_spec_sha256": spec.spec_sha256,
        "fact": {
            "target_outcome": spec.outcomes[0].name,
            "assertion": "PREDICATE_SATISFIED",
            "predicate_matches": True,
            "temporal_relation": "UNKNOWN",
            "event_at": "2026-07-25T00:00:00+00:00",
            "observed_value": "",
            "observed_value_upper": "",
            "observed_unit": "",
            "supporting_quote": quote,
            "clauses_satisfied": list(
                spec.semantics.qualifying_clause_ids[:1]
            ),
            "clauses_violated": [],
        },
    }
    article = Article(
        url="https://www.whitehouse.gov/briefing-room/release",
        domain="whitehouse.gov",
        title=quote,
        published_at="2026-07-25T00:00:00+00:00",
        fetched_at="2026-07-25T00:00:00.100000+00:00",
        raw_text=json.dumps(envelope, sort_keys=True, separators=(",", ":")),
        hash="official-envelope",
        source_kind="official_claim_json",
        discovered_at="2026-07-25T00:00:00.050000+00:00",
        parsed_at="2026-07-25T00:00:00.100000+00:00",
        source_endpoint="https://www.whitehouse.gov/api/releases",
        source_adapter="official_claim_json_v1",
    )
    store = RuleStore(tmp_path / "rules.sqlite3")
    extractor = EvidenceExtractor(
        ClassifierConfig(
            provider="claude_cli",
            model="must-not-run",
        ),
        store,
        passes=2,
        deterministic_enabled=True,
        deterministic_families={"SOURCE_LOCKED_ANNOUNCEMENT"},
        deterministic_policy_version=OFFICIAL_CLAIM_ADAPTER_VERSION,
        cli_runner=lambda _prompt: (_ for _ in ()).throw(
            AssertionError("model lane must not run")
        ),
    )

    result = extractor.extract(
        context=context,
        spec=spec,
        source_plan=plan,
        article=article,
        extracted_at="2026-07-25T00:00:00.110000+00:00",
    )

    assert result.status == "EXTRACTED"
    assert result.calls_reserved == 0
    assert result.lane == "deterministic"
    assert result.adapter_id == OFFICIAL_CLAIM_ADAPTER_VERSION
    assert result.claim is not None
    assert result.claim.extraction_passes == 1
    assert result.claim.model == (
        f"deterministic:{OFFICIAL_CLAIM_ADAPTER_VERSION}"
    )
    assert result.claim.temporal_relation == "IN_WINDOW"


def test_invalid_official_envelope_fails_closed_without_model_fallback(
    tmp_path: Path,
) -> None:
    context, spec = _spec("SOURCE_LOCKED_ANNOUNCEMENT")
    plan = build_source_plan(context, spec)
    article = Article(
        url="https://www.whitehouse.gov/briefing-room/release",
        domain="whitehouse.gov",
        title="Official result",
        published_at="2026-07-25T00:00:00+00:00",
        fetched_at="2026-07-25T00:00:00.100000+00:00",
        raw_text=json.dumps(
            {
                "schema_version": 1,
                "market_id": context.market_id,
                "rule_spec_sha256": "f" * 64,
                "fact": {},
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        hash="bad-official-envelope",
        source_kind="official_claim_json",
        source_adapter="official_claim_json_v1",
    )
    extractor = EvidenceExtractor(
        ClassifierConfig(provider="claude_cli", model="must-not-run"),
        RuleStore(tmp_path / "rules.sqlite3"),
        passes=2,
        deterministic_enabled=True,
        deterministic_families={"SOURCE_LOCKED_ANNOUNCEMENT"},
        deterministic_policy_version=OFFICIAL_CLAIM_ADAPTER_VERSION,
        cli_runner=lambda _prompt: (_ for _ in ()).throw(
            AssertionError("model lane must not run")
        ),
    )

    result = extractor.extract(
        context=context,
        spec=spec,
        source_plan=plan,
        article=article,
        extracted_at="2026-07-25T00:00:00.110000+00:00",
    )

    assert result.status == "INVALID"
    assert result.lane == "deterministic"
    assert result.reason == "official_claim_rulespec_binding_mismatch"


def test_official_envelope_with_bad_event_time_fails_closed(
    tmp_path: Path,
) -> None:
    context, spec = _spec("SOURCE_LOCKED_ANNOUNCEMENT")
    plan = build_source_plan(context, spec)
    quote = "White House officially announces recognition."
    article = Article(
        url="https://www.whitehouse.gov/briefing-room/release",
        domain="whitehouse.gov",
        title=quote,
        published_at="2026-07-25T00:00:00+00:00",
        fetched_at="2026-07-25T00:00:00.100000+00:00",
        raw_text=json.dumps(
            {
                "schema_version": 1,
                "market_id": context.market_id,
                "rule_spec_sha256": spec.spec_sha256,
                "fact": {
                    "target_outcome": spec.outcomes[0].name,
                    "assertion": "PREDICATE_SATISFIED",
                    "predicate_matches": True,
                    "temporal_relation": "UNKNOWN",
                    "event_at": "not-a-timestamp",
                    "observed_value": "",
                    "observed_value_upper": "",
                    "observed_unit": "",
                    "supporting_quote": quote,
                    "clauses_satisfied": list(
                        spec.semantics.qualifying_clause_ids[:1]
                    ),
                    "clauses_violated": [],
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        hash="bad-official-time",
        source_kind="official_claim_json",
        source_adapter="official_claim_json_v1",
    )
    extractor = EvidenceExtractor(
        ClassifierConfig(provider="claude_cli", model="must-not-run"),
        RuleStore(tmp_path / "rules.sqlite3"),
        passes=2,
        deterministic_enabled=True,
        deterministic_families={"SOURCE_LOCKED_ANNOUNCEMENT"},
        deterministic_policy_version=OFFICIAL_CLAIM_ADAPTER_VERSION,
        cli_runner=lambda _prompt: (_ for _ in ()).throw(
            AssertionError("model lane must not run")
        ),
    )

    result = extractor.extract(
        context=context,
        spec=spec,
        source_plan=plan,
        article=article,
        extracted_at="2026-07-25T00:00:00.110000+00:00",
    )

    assert result.status == "INVALID"
    assert result.lane == "deterministic"
    assert result.reason.startswith("official_claim_fact_invalid:")


def test_terminal_decision_enters_once_and_writes_complete_proof(
    tmp_path: Path,
) -> None:
    context, spec = _spec("OCCURRENCE_BEFORE_DEADLINE")
    plan = build_source_plan(context, spec)
    claims = [
        _claim(
            spec,
            article_id="a",
            assertion="PREDICATE_SATISFIED",
            group="reuters",
        ),
        _claim(
            spec,
            article_id="b",
            assertion="PREDICATE_SATISFIED",
            group="associated_press",
        ),
    ]
    evaluation = evaluate_rule(spec, claims)[0]
    adapter = _paper_adapter(tmp_path, spec)
    engine = ConfirmationDecisionEngine(
        context=context,
        spec=spec,
        adapter=adapter,
        portfolio=_portfolio(tmp_path, context),
        policy=ConfirmationPolicy(
            min_edge=0.05,
            max_entry_price=0.90,
            requested_usd=25.0,
        ),
    )
    decision_at = "2026-07-25T00:02:00+00:00"
    draft = engine.decide(evaluation, created_at=decision_at)
    assert draft.intent.action == "ENTER_YES"
    result = engine.execute(draft)
    assert result["executed"] is True
    proof = engine.proof(
        source_plan=plan,
        evaluation=evaluation,
        draft=draft,
        claims=claims,
        execution_result=result,
    )
    assert proof.paper_only is True
    assert proof.executed is True
    assert proof.rule_spec_sha256 == spec.spec_sha256
    assert proof.evaluation_sha256 == evaluation.evaluation_sha256
    assert proof.claim_sha256s == sorted(
        claim.claim_sha256 for claim in claims
    )
    assert proof.article_ids == ["a", "b"]
    assert proof.source_domains == [
        "associated_press.example",
        "reuters.example",
    ]
    assert proof.independence_groups == ["associated_press", "reuters"]
    assert proof.supporting_quotes == [
        "supported quote a",
        "supported quote b",
    ]
    assert proof.clauses_satisfied == list(
        spec.semantics.qualifying_clause_ids[:1]
    )
    assert proof.clauses_violated == []
    assert proof.executable_ask == 0.80
    assert proof.net_edge is not None and proof.net_edge > 0.05

    duplicate = engine.decide(evaluation, created_at=decision_at)
    assert duplicate.intent.action == "HOLD"
    assert engine.execute(duplicate)["executed"] is False


def test_confirmation_edge_and_portfolio_gate_fail_closed(
    tmp_path: Path,
) -> None:
    context, spec = _spec("OCCURRENCE_BEFORE_DEADLINE")
    evaluation = evaluate_rule(
        spec,
        [
            _claim(
                spec,
                article_id="a",
                assertion="PREDICATE_SATISFIED",
                group="reuters",
            ),
            _claim(
                spec,
                article_id="b",
                assertion="PREDICATE_SATISFIED",
                group="associated_press",
            ),
        ],
    )[0]
    engine = ConfirmationDecisionEngine(
        context=context,
        spec=spec,
        adapter=_paper_adapter(tmp_path, spec, yes_ask=0.95),
        portfolio=_portfolio(tmp_path, context),
        policy=ConfirmationPolicy(),
    )
    draft = engine.decide(evaluation)
    assert draft.intent.action == "NO_ACTION"
    assert any(
        blocker.startswith("price_above_cap")
        for blocker in draft.intent.blockers
    )


def test_unpromoted_family_evaluates_but_cannot_execute(
    tmp_path: Path,
) -> None:
    context, spec = _spec("STATUS_AT_DEADLINE")
    evaluation = evaluate_rule(
        spec,
        [
            _claim(
                spec,
                article_id="status",
                assertion="STATUS_OBSERVED",
                group="reuters",
                temporal="AT_DEADLINE",
            )
        ],
    )[0]
    assert evaluation.terminal is True
    engine = ConfirmationDecisionEngine(
        context=context,
        spec=spec,
        adapter=_paper_adapter(tmp_path, spec),
        portfolio=_portfolio(tmp_path, context),
        policy=ConfirmationPolicy(),
        execution_enabled=False,
    )
    draft = engine.decide(evaluation)
    assert draft.intent.action == "NO_ACTION"
    assert draft.intent.blockers == [
        "rule_family_paper_execution_disabled:STATUS_AT_DEADLINE"
    ]
    assert engine.execute(draft)["executed"] is False
