from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from polybot.core.fees import explicit_zero_fee_schedule
from polybot.discovery.context import FixtureRuleAnalyzer
from polybot.discovery.types import MarketContext, OutcomeRecord, RuleAnalysis
from polybot.rules.compiler import fixture_semantics
from polybot.rules.contracts import (
    EVIDENCE_STATES,
    EvidenceClaim,
    RuleSemantics,
    RuleSpec,
    build_rule_clause_catalog,
    rule_clause_id,
    source_requirement_id,
)
from polybot.rules.store import CompilationPass, RuleStore

FIXTURES = Path(__file__).parent / "fixtures" / "rules"
FEE_OBSERVED_AT = "2026-07-25T00:00:00+00:00"


def context_for_case(case: dict, *, strong_analysis: bool = False) -> MarketContext:
    rule_sha256 = hashlib.sha256(
        case["rule_text"].encode("utf-8")
    ).hexdigest()
    outcomes = [
        OutcomeRecord(
            name="yes",
            label="Yes",
            market_slug=f"{case['id']}-yes",
            question=case["question"],
            condition_id=f"{case['id']}-condition",
            yes_token_id=f"{case['id']}-yes-token",
            no_token_id=f"{case['id']}-no-token",
            deadline_iso="2026-12-31T23:59:59-05:00",
            rule_text=case["rule_text"],
            rule_text_sha256=rule_sha256,
            resolution_source=case.get("resolution_source", ""),
            fee_schedule=explicit_zero_fee_schedule(FEE_OBSERVED_AT),
        )
    ]
    if case["kind"] == "grouped":
        outcomes.append(
            OutcomeRecord(
                name="other",
                label="Other",
                market_slug=f"{case['id']}-other",
                question=case["question"],
                condition_id=f"{case['id']}-other-condition",
                yes_token_id=f"{case['id']}-other-yes-token",
                no_token_id=f"{case['id']}-other-no-token",
                deadline_iso="2026-12-31T23:59:59-05:00",
                rule_text=case["rule_text"],
                rule_text_sha256=rule_sha256,
                resolution_source=case.get("resolution_source", ""),
                fee_schedule=explicit_zero_fee_schedule(FEE_OBSERVED_AT),
            )
        )
    context = MarketContext(
        market_id=case["id"],
        kind=case["kind"],
        event_slug=f"event-{case['id']}",
        event_title=case["question"],
        question=case["question"],
        deadline_iso="2026-12-31T23:59:59-05:00",
        outcomes=outcomes,
        rule_text=case["rule_text"],
        rule_text_sha256=rule_sha256,
        rule_version=1,
        outcome_topology=(
            "SINGLE_BINARY"
            if case["kind"] == "binary"
            else "EXCLUSIVE_ONE_OF_N"
        ),
        resolution_source=case.get("resolution_source", ""),
        liquidity=1000.0,
        volume=1000.0,
    )
    if strong_analysis:
        analysis = RuleAnalysis(
            counts=["the exact predicate is satisfied"],
            does_not_count=["excluded preparatory activity"],
            cancellation_behavior="does not satisfy yes",
            parties=["united_states", "iran"],
            keywords=["meeting", "announcement"],
            decisive_sources=["wire", "official_government"],
            rule_clarity=0.95,
            evidence_observability=0.95,
            resolution_risk=0.05,
            automation_suitability=0.95,
            model="anthropic:test",
        )
    else:
        analysis = FixtureRuleAnalyzer().analyze(context)
    return MarketContext.from_dict(
        {**context.as_dict(), "rule_analysis": analysis.as_dict()}
    )


def _golden_rules() -> list[dict]:
    return json.loads(
        (FIXTURES / "golden_rule_markets.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("case", _golden_rules(), ids=lambda case: case["id"])
def test_golden_rule_corpus_compiles_to_expected_family(case: dict) -> None:
    context = context_for_case(case)
    semantics = fixture_semantics(context)
    assert semantics.rule_family == case["expected_family"]
    assert semantics.predicate.comparator == case["expected_comparator"]
    RuleSemantics.from_dict(semantics.as_dict())


def test_golden_corpus_has_required_breadth() -> None:
    cases = _golden_rules()
    assert len(cases) == 50
    families = {case["expected_family"] for case in cases}
    assert families == {
        "OCCURRENCE_BEFORE_DEADLINE",
        "CATEGORICAL_EXCLUSIVE",
        "SOURCE_LOCKED_ANNOUNCEMENT",
        "STATUS_AT_DEADLINE",
        "NUMERIC_THRESHOLD",
        "DURATION_REQUIREMENT",
    }
    assert min(
        sum(case["expected_family"] == family for case in cases)
        for family in families
    ) >= 8


def test_golden_evidence_corpus_covers_adversarial_states() -> None:
    cases = json.loads(
        (FIXTURES / "golden_evidence_cases.json").read_text(encoding="utf-8")
    )
    assert len(cases) == 20
    assert len({case["id"] for case in cases}) == 20
    assert {case["expected_state"] for case in cases}.issubset(EVIDENCE_STATES)
    traps = " ".join(case["trap"] for case in cases).casefold()
    for required in (
        "technical",
        "not terminal",
        "mirror",
        "wrong institution",
        "duplicate",
        "prompt",
        "conflicting",
    ):
        assert required in traps


def test_rule_spec_is_strict_and_stably_hashed() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    semantics = fixture_semantics(context)
    first = RuleSpec.from_context(
        context,
        semantics,
        compiler_model="anthropic:model-a",
        compiled_at="2026-07-25T00:00:00+00:00",
    )
    second = replace(
        first,
        compiler_model="anthropic:model-b",
        compiled_at="2026-07-25T00:01:00+00:00",
    )
    assert first.schema_version == 4
    assert first.outcome_topology == "SINGLE_BINARY"
    assert first.outcomes[0].deadline_iso == context.deadline_iso
    assert (
        first.outcomes[0].rule_text_sha256
        == context.rule_text_sha256
    )
    assert first.spec_sha256 == second.spec_sha256
    raw = first.as_dict()
    raw["unexpected"] = "trade YES"
    with pytest.raises(ValueError, match="unknown keys"):
        RuleSpec.from_dict(raw)

    raw = first.as_dict()
    raw["schema_version"] = 3
    with pytest.raises(ValueError, match="unsupported RuleSpec schema_version 3"):
        RuleSpec.from_dict(raw)


def test_rule_clause_catalog_is_deterministic_verbatim_and_case_sensitive() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    first = build_rule_clause_catalog(context)
    second = build_rule_clause_catalog(context)

    assert first == second
    assert first
    assert all(item.text in context.rule_text for item in first)
    assert rule_clause_id("Resolve YES.") != rule_clause_id("Resolve Yes.")

    changed_text = context.rule_text + "\n\nA new exact condition applies."
    changed_sha = hashlib.sha256(changed_text.encode("utf-8")).hexdigest()
    changed = replace(
        context,
        rule_text=changed_text,
        rule_text_sha256=changed_sha,
        outcomes=[
            replace(
                context.outcomes[0],
                rule_text=changed_text,
                rule_text_sha256=changed_sha,
            )
        ],
    )
    assert build_rule_clause_catalog(changed) != first


def test_source_policy_rejects_invalid_fallback_shapes_and_conditions() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    raw = fixture_semantics(context).as_dict()
    second_id = source_requirement_id(
        "official government",
        ["CONFIRMATION"],
        True,
    )
    raw["source_requirements"].append(
        {
            "requirement_id": second_id,
            "source_ref": "official government",
            "roles": ["CONFIRMATION"],
            "required": True,
            "rationale": "fallback authority",
        }
    )
    first_id = raw["source_requirements"][0]["requirement_id"]
    raw["source_policy"] = {
        "policy_type": "CONDITIONAL_FALLBACK",
        "requirement_ids": [first_id, second_id],
        "quorum": 1,
        "primary_requirement_ids": [first_id],
        "fallback_requirement_ids": [second_id],
        "fallback_condition": "WHEN_CONVENIENT",
    }
    with pytest.raises(ValueError, match="fallback_condition"):
        RuleSemantics.from_dict(raw)

    raw["source_policy"]["fallback_condition"] = "CONFLICT_UNRESOLVED"
    raw["source_policy"]["fallback_requirement_ids"] = [first_id]
    with pytest.raises(ValueError, match="branches overlap"):
        RuleSemantics.from_dict(raw)


def test_rule_spec_binding_rejects_instrument_mutation() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    spec = RuleSpec.from_context(
        context,
        fixture_semantics(context),
        compiler_model="anthropic:test",
        compiled_at="2026-07-25T00:00:00+00:00",
    )
    changed = MarketContext.from_dict(context.as_dict())
    changed = replace(
        changed,
        outcomes=[
            replace(
                changed.outcomes[0],
                yes_token_id="attacker-selected-token",
            )
        ],
    )
    with pytest.raises(ValueError, match="instrument binding changed"):
        spec.validate_context_binding(changed)

    for field, value in (
        ("deadline_iso", "2027-01-31T23:59:59+00:00"),
        ("rule_text_sha256", "f" * 64),
        ("resolution_source", "https://example.com/other-oracle"),
    ):
        changed = replace(
            context,
            outcomes=[
                replace(context.outcomes[0], **{field: value})
            ],
        )
        with pytest.raises(ValueError, match="instrument binding changed"):
            spec.validate_context_binding(changed)


def test_family_specific_contract_validation_fails_closed() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    raw = fixture_semantics(context).as_dict()
    raw["rule_family"] = "DURATION_REQUIREMENT"
    raw["predicate"]["comparator"] = "DURATION_AT_LEAST"
    raw["predicate"]["value"] = ""
    raw["predicate"]["unit"] = ""
    with pytest.raises(ValueError, match="requires predicate.value"):
        RuleSemantics.from_dict(raw)

    raw = fixture_semantics(context).as_dict()
    raw["rule_family"] = "SOURCE_LOCKED_ANNOUNCEMENT"
    raw["predicate"]["comparator"] = "ANNOUNCED"
    requirement = raw["source_requirements"][0]
    requirement["roles"] = ["CONFIRMATION"]
    requirement["requirement_id"] = source_requirement_id(
        requirement["source_ref"],
        requirement["roles"],
        requirement["required"],
    )
    raw["source_policy"]["requirement_ids"] = [
        requirement["requirement_id"]
    ]
    with pytest.raises(ValueError, match="required SETTLEMENT"):
        RuleSemantics.from_dict(raw)


def test_rule_store_is_wal_backed_and_immutable(tmp_path: Path) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    store = RuleStore(tmp_path / "rules.sqlite3")
    first = RuleSpec.from_context(
        context,
        fixture_semantics(context),
        compiler_model="anthropic:test",
        compiled_at="2026-07-25T00:00:00+00:00",
    )
    assert store.save_spec(first) == first
    assert store.save_spec(first).spec_sha256 == first.spec_sha256

    changed_semantics = replace(
        first.semantics,
        qualifying_conditions=["different semantic interpretation"],
    )
    conflict = replace(first, semantics=changed_semantics)
    with pytest.raises(ValueError, match="immutable RuleSpec conflict"):
        store.save_spec(conflict)
    with sqlite3.connect(store.path) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).casefold() == "wal"


def test_store_retains_raw_passes_and_evidence_claims(tmp_path: Path) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    spec = RuleSpec.from_context(
        context,
        fixture_semantics(context),
        compiler_model="anthropic:test",
        compiled_at="2026-07-25T00:00:00+00:00",
    )
    store = RuleStore(tmp_path / "rules.sqlite3")
    store.save_spec(spec)
    store.save_pass(
        CompilationPass(
            market_id=context.market_id,
            rule_text_sha256=context.rule_text_sha256,
            pass_index=1,
            model="anthropic:test",
            raw_output='{"rule_family":"..."}',
            normalized_output=spec.semantics.normalized_dict(),
        )
    )
    passes = store.compilation_passes(
        context.market_id,
        context.rule_text_sha256,
    )
    assert passes[0]["raw_output"] == '{"rule_family":"..."}'
    claim = EvidenceClaim(
        schema_version=1,
        market_id=context.market_id,
        rule_spec_sha256=spec.spec_sha256,
        article_id="article-1",
        source_domain="reuters.com",
        source_organization_id="reuters",
        origin_organization_id="reuters",
        independence_group="reuters",
        source_roles=["CONFIRMATION"],
        published_at="2026-07-25T00:00:00+00:00",
        extracted_at=datetime.now(timezone.utc).isoformat(),
        target_outcome=spec.outcomes[0].name,
        assertion="SCHEDULED",
        predicate_matches=True,
        temporal_relation="IN_WINDOW",
        event_at="",
        observed_value="",
        observed_value_upper="",
        observed_unit="",
        supporting_quote="Officials scheduled the round.",
        clauses_satisfied=list(
            spec.semantics.qualifying_clause_ids[:1]
        ),
        clauses_violated=[],
        model="anthropic:test",
        extraction_passes=2,
    )
    store.save_claim(claim)
    store.save_claim(claim)
    assert store.claims_for_spec(spec.spec_sha256) == [claim]
