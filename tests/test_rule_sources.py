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
from polybot.discovery.store import DiscoveryStore
from polybot.discovery.types import SOURCE_PLAN_LEGACY
from polybot.rules.compiler import fixture_semantics
from polybot.rules.contracts import (
    RuleSpec,
    SourcePolicy,
    SourcePolicyBranch,
    SourceRequirement,
    source_requirement_id,
)
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
    assert plan.source_policy == spec.semantics.source_policy.as_dict()
    reuters = [
        item for item in plan.source_records
        if item.organization_id == "reuters"
    ]
    assert reuters
    assert reuters[0].required is True
    assert reuters[0].requirement_ids == [
        spec.semantics.source_requirements[0].requirement_id
    ]
    assert "SETTLEMENT" in reuters[0].roles
    assert reuters[0].poll_urls == ["https://reuters.com/"]
    assert "https://reuters.com/" in plan.poll_urls
    assert "reuters.com" in plan.auto_trade_domains
    assert plan.missing_required_source_refs == []


def test_unresolved_required_named_source_forces_monitor_only() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    base = fixture_semantics(context)
    requirement_id = source_requirement_id(
        "Secret Gazette Print Edition",
        ["SETTLEMENT"],
        True,
    )
    semantics = replace(
        base,
        source_requirements=[
            SourceRequirement(
                requirement_id=requirement_id,
                source_ref="Secret Gazette Print Edition",
                roles=["SETTLEMENT"],
                required=True,
                rationale="sole resolution source named by the rules",
            )
        ],
        source_policy=SourcePolicy(
            policy_type="ANY_OF",
            requirement_ids=[requirement_id],
            quorum=1,
        ),
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


@pytest.mark.parametrize(
    ("source_ref", "expected_domains"),
    [
        ("Donald Trump", {"whitehouse.gov"}),
        (
            "U.S. government",
            {
                "state.gov",
                "whitehouse.gov",
                "defense.gov",
                "war.gov",
                "centcom.mil",
            },
        ),
        (
            "United States government",
            {
                "state.gov",
                "whitehouse.gov",
                "defense.gov",
                "war.gov",
                "centcom.mil",
            },
        ),
        ("President of the United States", {"whitehouse.gov"}),
        ("United States Department of State", {"state.gov"}),
        (
            "United States Department of Defense",
            {"defense.gov", "war.gov"},
        ),
        ("United States Central Command (CENTCOM)", {"centcom.mil"}),
        (
            "official information from the United States government",
            {
                "state.gov",
                "whitehouse.gov",
                "defense.gov",
                "war.gov",
                "centcom.mil",
            },
        ),
        (
            "government of the United States",
            {
                "state.gov",
                "whitehouse.gov",
                "defense.gov",
                "war.gov",
                "centcom.mil",
            },
        ),
    ],
)
def test_named_us_official_source_alternatives_resolve_without_fake_independence(
    source_ref: str,
    expected_domains: set[str],
) -> None:
    sources = resolve_source_reference(
        source_ref,
        roles=["SETTLEMENT", "CONFIRMATION"],
        required=True,
    )

    assert {item.domain for item in sources} == expected_domains
    assert {item.independence_group for item in sources} == {
        "government:united_states"
    }
    assert all(item.required for item in sources)


def test_named_iran_government_source_resolves_to_one_official_group() -> None:
    sources = resolve_source_reference(
        "government of Iran",
        roles=["SETTLEMENT", "CONFIRMATION"],
        required=False,
    )

    assert {item.domain for item in sources} == {"mfa.gov.ir"}
    assert {item.independence_group for item in sources} == {
        "government:iran"
    }


@pytest.mark.parametrize(
    "source_ref",
    [
        "credible reporting",
        "consensus of credible reporting",
        "wide consensus of credible reporting",
        "major news agencies of record",
    ],
)
def test_credible_reporting_aliases_resolve_policy_requirements(
    source_ref: str,
) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    base = fixture_semantics(context)
    requirement_id = source_requirement_id(
        source_ref,
        ["SETTLEMENT"],
        False,
    )
    semantics = replace(
        base,
        source_requirements=[
            SourceRequirement(
                requirement_id=requirement_id,
                source_ref=source_ref,
                roles=["SETTLEMENT"],
                required=False,
                rationale="publisher evidence branch named by the rules",
            )
        ],
        source_policy=SourcePolicy(
            policy_type="ANY_OF",
            requirement_ids=[requirement_id],
            quorum=1,
        ),
    )
    spec = _spec(context, semantics)
    plan = build_source_plan(context, spec)

    assert any(
        requirement_id in item.requirement_ids
        for item in plan.source_records
    )
    validate_source_plan_freshness(context, plan, spec)


def test_unresolved_optional_alternative_does_not_disable_resolved_path() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    base = fixture_semantics(context)
    unknown_id = source_requirement_id(
        "Official Channel Without Endpoint",
        ["SETTLEMENT"],
        False,
    )
    reporting_id = source_requirement_id(
        "credible reporting",
        ["SETTLEMENT"],
        False,
    )
    semantics = replace(
        base,
        source_requirements=[
            SourceRequirement(
                requirement_id=unknown_id,
                source_ref="Official Channel Without Endpoint",
                roles=["SETTLEMENT"],
                required=False,
            ),
            SourceRequirement(
                requirement_id=reporting_id,
                source_ref="credible reporting",
                roles=["SETTLEMENT"],
                required=False,
            ),
        ],
        source_policy=SourcePolicy(
            policy_type="ALTERNATIVE_QUORUM",
            requirement_ids=[unknown_id, reporting_id],
            quorum=1,
            branches=[
                SourcePolicyBranch(
                    requirement_ids=[unknown_id],
                    requirement_quorum=1,
                    minimum_independent_sources=1,
                ),
                SourcePolicyBranch(
                    requirement_ids=[reporting_id],
                    requirement_quorum=1,
                    minimum_independent_sources=1,
                ),
            ],
        ),
    )
    spec = _spec(context, semantics)
    plan = build_source_plan(context, spec)

    assert unknown_id not in {
        requirement_id
        for source in plan.source_records
        for requirement_id in source.requirement_ids
    }
    validate_source_plan_freshness(context, plan, spec)


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


def test_aggregator_cannot_gain_confirmation_or_auto_trade_authority() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    spec = _spec(context)
    plan = build_source_plan(context, spec)
    google_index = next(
        index
        for index, source in enumerate(plan.source_records)
        if source.source_id == "aggregator:google_news"
    )
    records = list(plan.source_records)
    records[google_index] = replace(
        records[google_index],
        roles=["CONFIRMATION"],
    )
    elevated = replace(
        plan,
        source_records=records,
        auto_trade_domains=[
            *plan.auto_trade_domains,
            "news.google.com",
        ],
    )

    with pytest.raises(ValueError, match="aggregator|authority"):
        validate_source_plan_freshness(context, elevated, spec)


def test_required_source_without_endpoint_is_not_current() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    spec = _spec(context)
    plan = build_source_plan(context, spec)
    required = replace(
        plan.source_records[0],
        required=True,
        feed_urls=[],
        poll_urls=[],
    )
    records = [required, *plan.source_records[1:]]
    invalid = replace(plan, source_records=records)

    with pytest.raises(ValueError, match="no usable endpoint"):
        validate_source_plan_freshness(context, invalid, spec)


def test_legacy_plan_is_quarantined_and_replacement_is_versioned(
    tmp_path,
) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    store = DiscoveryStore(tmp_path)
    legacy = build_source_plan(context)
    assert legacy.semantic_status == SOURCE_PLAN_LEGACY
    store.save_source_plan(legacy)

    assert store.quarantine_legacy_source_plans() == 1
    assert store.quarantine_legacy_source_plans() == 0
    assert len(list(store.legacy_plans_dir.glob("*.json"))) == 1

    current = build_source_plan(context, _spec(context))
    store.save_source_plan(current)
    loaded = store.load_source_plan(context.market_id)
    assert loaded is not None
    assert loaded.semantic_status == "CURRENT"
    assert len(list(store.plan_history_dir.glob("*.json"))) == 1
