from __future__ import annotations

from dataclasses import replace

from polybot.discovery.coverage import _source_health
from polybot.discovery.types import PlannedSource, SourcePlan


def _source(
    source_id: str,
    requirement_id: str,
    url: str,
) -> PlannedSource:
    return PlannedSource(
        source_id=source_id,
        organization_id=source_id,
        independence_group=source_id,
        domain=f"{source_id}.example",
        source_tier="wire",
        feed_urls=[url],
        roles=["CONFIRMATION"],
        requirement_ids=[requirement_id],
    )


def _plan(policy: dict) -> SourcePlan:
    return SourcePlan(
        market_id="market",
        rule_text_sha256="a" * 64,
        rule_spec_sha256="b" * 64,
        source_records=[
            _source("primary", "source_primary", "https://primary.example/rss"),
            _source("fallback", "source_fallback", "https://fallback.example/rss"),
        ],
        minimum_independent_confirmations=1,
        source_policy=policy,
    )


def test_source_health_distinguishes_any_of_from_all_of() -> None:
    health = {
        "https://primary.example/rss": {"healthy": True, "status": "LIVE"},
        "https://fallback.example/rss": {"healthy": False, "status": "DEAD"},
    }
    any_of = _plan(
        {
            "policy_type": "ANY_OF",
            "requirement_ids": ["source_primary", "source_fallback"],
            "quorum": 1,
            "primary_requirement_ids": [],
            "fallback_requirement_ids": [],
            "fallback_condition": "",
        }
    )
    all_of = replace(
        any_of,
        source_policy={
            **any_of.source_policy,
            "policy_type": "ALL_OF",
            "quorum": 2,
        },
    )

    assert _source_health(any_of, health)["ready"] is True
    blocked = _source_health(all_of, health)
    assert blocked["ready"] is False
    assert blocked["blockers"] == [
        "source_policy_all_of_unsatisfied:source_fallback"
    ]


def test_source_health_uses_only_verifiable_fallback_conditions() -> None:
    health = {
        "https://primary.example/rss": {"healthy": False, "status": "DEAD"},
        "https://fallback.example/rss": {"healthy": True, "status": "LIVE"},
    }
    policy = {
        "policy_type": "PRIMARY_WITH_FALLBACK",
        "requirement_ids": ["source_primary", "source_fallback"],
        "quorum": 1,
        "primary_requirement_ids": ["source_primary"],
        "fallback_requirement_ids": ["source_fallback"],
        "fallback_condition": "SOURCE_UNAVAILABLE",
    }
    plan = _plan(policy)

    assert _source_health(plan, health)["ready"] is True

    conditional = replace(
        plan,
        source_policy={
            **policy,
            "policy_type": "CONDITIONAL_FALLBACK",
            "fallback_condition": "CONFLICT_UNRESOLVED",
        },
    )
    blocked = _source_health(conditional, health)
    assert blocked["ready"] is False
    assert blocked["blockers"] == [
        "source_policy_fallback_condition_unverified:CONFLICT_UNRESOLVED"
    ]
