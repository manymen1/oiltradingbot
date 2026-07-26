from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from polybot.discovery.config import ScoringConfig
from polybot.discovery.registry import (
    independent_source_count,
    resolve_source_reference,
    source_identity,
)
from polybot.discovery.scorer import grade_market
from polybot.discovery.sources import (
    build_source_plan,
    source_plan_sha256,
    validate_source_plan_freshness,
)
from polybot.rules.compiler import fixture_semantics
from polybot.rules.contracts import RuleSpec, SourceRequirement
from test_rule_contracts import _golden_rules, context_for_case


def _spec(context, semantics=None, *, model: str = "anthropic:test"):
    return RuleSpec.from_context(
        context,
        semantics or fixture_semantics(context),
        compiler_model=model,
        compiled_at="2026-07-25T00:00:00+00:00",
    )


def test_rule_spec_source_plan_includes_named_settlement_source() -> None:
    case = next(
        item for item in _golden_rules()
        if item["id"] == "source-locked-05"
    )
    context = context_for_case(case, strong_analysis=True)
    spec = _spec(context)
    plan = build_source_plan(context, spec)
    assert plan.rule_spec_sha256 == spec.spec_sha256
    reuters = [
        item for item in plan.source_records
        if item.organization_id == "reuters"
    ]
    assert reuters
    assert reuters[0].required is True
    assert "SETTLEMENT" in reuters[0].roles
    assert reuters[0].poll_urls == ["https://reuters.com/"]
    assert "https://reuters.com/" in plan.poll_urls
    assert "reuters.com" in plan.auto_trade_domains
    assert plan.missing_required_source_refs == []


def test_unresolved_required_named_source_forces_monitor_only() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    base = fixture_semantics(context)
    semantics = replace(
        base,
        source_requirements=[
            SourceRequirement(
                source_ref="Secret Gazette Print Edition",
                roles=["SETTLEMENT"],
                required=True,
                rationale="sole resolution source named by the rules",
            )
        ],
    )
    spec = _spec(context, semantics)
    plan = build_source_plan(context, spec)
    assert plan.missing_required_source_refs == [
        "Secret Gazette Print Edition"
    ]
    graded = grade_market(
        context,
        ScoringConfig(
            allow_fixture_analysis_live=True,
            min_rule_text_chars=1,
        ),
        rule_spec=spec,
        source_plan=plan,
        require_rule_spec=True,
        paper_families={semantics.rule_family},
        live_confirmation_families={semantics.rule_family},
    )
    assert graded.state == "MONITOR_ONLY"
    assert graded.state_reasons[0].startswith(
        "required_rule_source_unresolved"
    )


def test_untrusted_literal_source_is_not_an_outbound_poll_target() -> None:
    sources = resolve_source_reference(
        "http://127.0.0.1/latest",
        roles=["SETTLEMENT"],
        required=True,
    )

    assert len(sources) == 1
    assert sources[0].domain == "127.0.0.1"
    assert sources[0].poll_urls == []


def test_unpromoted_family_is_paper_only_then_can_be_promoted() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    spec = _spec(context)
    plan = build_source_plan(context, spec)
    scoring = ScoringConfig(allow_fixture_analysis_live=True)
    paper = grade_market(
        context,
        scoring,
        rule_spec=spec,
        source_plan=plan,
        require_rule_spec=True,
        paper_families={spec.semantics.rule_family},
        live_confirmation_families=set(),
    )
    assert paper.state == "PAPER_ELIGIBLE"
    assert any(
        reason.startswith("rule_family_not_live_promoted")
        for reason in paper.state_reasons
    )
    live = grade_market(
        context,
        scoring,
        rule_spec=spec,
        source_plan=plan,
        require_rule_spec=True,
        paper_families={spec.semantics.rule_family},
        live_confirmation_families={spec.semantics.rule_family},
    )
    assert live.state == "LIVE_CONFIRMATION_ELIGIBLE"


def test_subjective_family_is_always_monitor_only() -> None:
    case = {
        "id": "subjective",
        "kind": "binary",
        "question": "Will there be a major escalation?",
        "rule_text": (
            "Resolves Yes if a major escalation occurs, as determined in the "
            "resolution source's sole discretion."
        ),
        "resolution_source": "",
    }
    context = context_for_case(case, strong_analysis=True)
    spec = _spec(context)
    assert spec.semantics.rule_family == "SUBJECTIVE_DISCRETIONARY"
    plan = build_source_plan(context, spec)
    graded = grade_market(
        context,
        ScoringConfig(
            allow_fixture_analysis_live=True,
            min_rule_text_chars=1,
        ),
        rule_spec=spec,
        source_plan=plan,
        require_rule_spec=True,
        paper_families={"SUBJECTIVE_DISCRETIONARY"},
        live_confirmation_families=set(),
    )
    assert graded.state == "MONITOR_ONLY"
    assert graded.state_reasons == ["subjective_rule_family"]


def test_reuters_mirrors_count_as_one_independent_source() -> None:
    evidence = [
        {
            "domain": "reuters.com",
            "origin_organization": "Reuters",
        },
        {
            "domain": "finance.yahoo.com",
            "origin_organization": "Reuters",
        },
        {
            "domain": "aol.com",
            "byline": "Reporting by Reuters",
        },
    ]
    assert independent_source_count(evidence) == 1
    assert source_identity(
        "finance.yahoo.com",
        origin_organization="Reuters",
    ) == ("reuters", "reuters")
    assert source_identity(
        "finance.yahoo.com",
        origin_organization="Yahoo Finance",
        byline="Reporting by Reuters",
    ) == ("reuters", "reuters")
    assert independent_source_count(
        evidence + [{"domain": "apnews.com"}]
    ) == 2


def test_source_plan_hash_covers_roles_and_rule_spec() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    spec = _spec(context)
    plan = build_source_plan(context, spec)
    changed_record = replace(
        plan.source_records[0],
        roles=sorted(set(plan.source_records[0].roles) | {"SETTLEMENT"}),
    )
    changed = replace(
        plan,
        source_records=[changed_record] + plan.source_records[1:],
    )
    assert source_plan_sha256(plan) != source_plan_sha256(changed)
    changed_spec = replace(plan, rule_spec_sha256="0" * 64)
    assert source_plan_sha256(plan) != source_plan_sha256(changed_spec)


def test_stale_source_plan_is_refused_after_rule_change() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    spec = _spec(context)
    plan = build_source_plan(context, spec)
    changed = replace(
        context,
        rule_text=context.rule_text + " Amended.",
        rule_text_sha256="f" * 64,
        rule_version=2,
    )
    with pytest.raises(ValueError, match="stale|binding changed"):
        validate_source_plan_freshness(changed, plan, spec)


def test_source_plan_freshness_accepts_exact_semantic_version() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    spec = _spec(context)
    plan = build_source_plan(context, spec)
    validate_source_plan_freshness(context, plan, spec)
