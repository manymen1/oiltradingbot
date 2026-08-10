from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from polybot.core.article import article_age_hours
from polybot.core.types import Article
from polybot.discovery.sources import build_source_plan
from polybot.discovery.store import DiscoveryStore
from polybot.rules.compiler import fixture_semantics
from polybot.rules.contracts import (
    RuleSpec,
    SourcePolicy,
    SourceRequirement,
    sha256_json,
    source_requirement_id,
)
from polybot.rules.promotion import (
    PromotionPolicy,
    build_promotion_report,
    load_replay_summaries,
)
from polybot.rules.replay import (
    load_rule_replay_timeline,
    replay_rule_market,
)
from polybot.rules.store import RuleStore
from test_rule_contracts import _golden_rules, context_for_case


def _setup(tmp_path: Path) -> tuple[Path, object, RuleSpec]:
    case = next(
        item
        for item in _golden_rules()
        if item["expected_family"] == "OCCURRENCE_BEFORE_DEADLINE"
    )
    context = replace(
        context_for_case(case, strong_analysis=True),
        state="PAPER_ELIGIBLE",
    )
    spec = RuleSpec.from_context(
        context,
        fixture_semantics(context),
        compiler_model="fixture",
        compiled_at="2026-07-25T00:00:00+00:00",
    )
    plan = build_source_plan(context, spec)
    data_dir = tmp_path / "data"
    store = DiscoveryStore(data_dir)
    store.save_context(context)
    store.save_source_plan(plan)
    RuleStore(data_dir / "rules.sqlite3").save_spec(spec)
    config_path = tmp_path / "discovery.yaml"
    config_path.write_text(
        f"""
classifier:
  provider: rule_based
rule_compiler:
  enabled: true
rule_runner:
  enabled: true
  extraction_passes: 2
  paper_fee_bps: 0
  paper_slippage_bps: 25
fleet:
  position_mode: alert_only
data_dir: {data_dir}
logs_dir: {tmp_path / 'logs'}
""",
        encoding="utf-8",
    )
    return config_path, context, spec


def _setup_blockade(tmp_path: Path) -> tuple[Path, object, RuleSpec]:
    rule_text = """This market resolves Yes if the United States government, or an authorized representative, publicly and officially announces the end, termination, lifting, or suspension of the United States naval blockade on Iranian ships before the deadline. A qualifying announcement must clearly communicate a present and decided general end or suspension through official channels. A limited or partial change, a specific-vessel exemption, a prospective, contingent, probable, or conditional end, an anonymous or leaked statement, an informal comment, a statement by someone not authorized to speak, or a fabricated, hacked, or impersonated communication does not qualify. Resolution will be based on official information from the United States government. Once a qualifying announcement is made, the result remains Yes even if it is later reversed. Otherwise this market resolves No."""
    case = {
        "id": "reviewed-us-iran-blockade",
        "kind": "binary",
        "question": "Will the US officially announce an end to the Iranian blockade?",
        "rule_text": rule_text,
        "resolution_source": "https://www.whitehouse.gov/",
        "expected_family": "SOURCE_LOCKED_ANNOUNCEMENT",
        "expected_comparator": "ANNOUNCED",
    }
    context = replace(
        context_for_case(case, strong_analysis=True),
        state="PAPER_ELIGIBLE",
    )
    semantics = fixture_semantics(context)
    source_ref = "united states government"
    requirement_id = source_requirement_id(
        source_ref,
        ["SETTLEMENT"],
        True,
    )
    source_clause = next(
        item.clause_id
        for item in RuleSpec.from_context(
            context,
            semantics,
            compiler_model="fixture",
            compiled_at="2026-07-25T00:00:00+00:00",
        ).rule_clauses
        if "resolution will be based" in item.text.casefold()
    )
    semantics = replace(
        semantics,
        source_requirements=[
            SourceRequirement(
                requirement_id=requirement_id,
                source_ref=source_ref,
                roles=["SETTLEMENT"],
                required=True,
                rationale="reviewed blockade requires official US information",
                clause_ids=[source_clause],
            )
        ],
        source_policy=SourcePolicy(
            policy_type="ALL_OF",
            requirement_ids=[requirement_id],
            quorum=1,
        ),
    )
    spec = RuleSpec.from_context(
        context,
        semantics,
        compiler_model="reviewed:test",
        compiled_at="2026-07-25T00:00:00+00:00",
    )
    plan = build_source_plan(context, spec)
    data_dir = tmp_path / "data"
    store = DiscoveryStore(data_dir)
    store.save_context(context)
    store.save_source_plan(plan)
    RuleStore(data_dir / "rules.sqlite3").save_spec(spec)
    config_path = tmp_path / "discovery.yaml"
    config_path.write_text(
        f"""
classifier:
  provider: rule_based
rule_compiler:
  enabled: true
rule_runner:
  enabled: true
  extraction_passes: 2
  paper_fee_bps: 0
  paper_slippage_bps: 25
fleet:
  position_mode: alert_only
data_dir: {data_dir}
logs_dir: {tmp_path / 'logs'}
""",
        encoding="utf-8",
    )
    return config_path, context, spec


def _setup_cuba_strike(tmp_path: Path) -> tuple[Path, object, RuleSpec]:
    rule_text = """This market will resolve to \"Yes\" if a US-initiated drone, missile, or air strike on the soil of Cuba is announced or credibly reported to have occurred by the listed date ET.

For the purposes of this market, a qualifying \"strike\" is defined as the use of aerial bombs, drones, or missiles launched by any United States operatives that physically impact ground territory within Cuba. A strike on terrestrial territory, including rivers, lakes, and ports, counts, but territorial sea does not. Missiles or drones that are intercepted before impact and surface-to-air missile strikes will not be sufficient. Artillery fire, small arms fire, ground incursions, naval shelling, and cyberattacks will not qualify.

Any strike during the timeframe claimed by either Donald Trump or the U.S. government will qualify. The primary resolution source will be a consensus of credible reporting. Otherwise this market resolves No."""
    case = {
        "id": "us-strike-on-cuba-by",
        "kind": "binary",
        "question": "US military action against Cuba by...?",
        "rule_text": rule_text,
        "resolution_source": "consensus of credible reporting",
        "expected_family": "OCCURRENCE_BEFORE_DEADLINE",
        "expected_comparator": "OCCURRED",
    }
    context = replace(
        context_for_case(case, strong_analysis=True),
        state="PAPER_ELIGIBLE",
    )
    base = RuleSpec.from_context(
        context,
        fixture_semantics(context),
        compiler_model="fixture",
        compiled_at="2026-07-25T00:00:00+00:00",
    )
    consensus_id = source_requirement_id(
        "consensus of credible reporting",
        ["SETTLEMENT"],
        True,
    )
    trump_id = source_requirement_id(
        "donald trump",
        ["SETTLEMENT"],
        False,
    )
    government_id = source_requirement_id(
        "u.s. government",
        ["SETTLEMENT"],
        False,
    )
    consensus_clauses = [
        item.clause_id
        for item in base.rule_clauses
        if "resolution source" in item.text.casefold()
    ]
    official_clauses = [
        item.clause_id
        for item in base.rule_clauses
        if "claimed by either" in item.text.casefold()
    ]
    semantics = replace(
        base.semantics,
        source_requirements=[
            SourceRequirement(
                requirement_id=consensus_id,
                source_ref="consensus of credible reporting",
                roles=["SETTLEMENT"],
                required=True,
                rationale="one approved credible publisher is terminal",
                clause_ids=consensus_clauses,
            ),
            SourceRequirement(
                requirement_id=trump_id,
                source_ref="donald trump",
                roles=["SETTLEMENT"],
                required=False,
                rationale="explicit official alternative",
                clause_ids=official_clauses,
            ),
            SourceRequirement(
                requirement_id=government_id,
                source_ref="u.s. government",
                roles=["SETTLEMENT"],
                required=False,
                rationale="explicit official alternative",
                clause_ids=official_clauses,
            ),
        ],
        source_policy=SourcePolicy(
            policy_type="ANY_OF",
            requirement_ids=[consensus_id, trump_id, government_id],
            quorum=1,
        ),
        resolution_policy=replace(
            base.semantics.resolution_policy,
            independent_confirmation_sources=1,
        ),
    )
    spec = RuleSpec.from_context(
        context,
        semantics,
        compiler_model="reviewed:test",
        compiled_at="2026-07-25T00:00:00+00:00",
    )
    plan = build_source_plan(context, spec)
    assert not plan.missing_required_source_refs
    data_dir = tmp_path / "data"
    store = DiscoveryStore(data_dir)
    store.save_context(context)
    store.save_source_plan(plan)
    RuleStore(data_dir / "rules.sqlite3").save_spec(spec)
    config_path = tmp_path / "discovery.yaml"
    config_path.write_text(
        f"""
classifier:
  provider: rule_based
rule_compiler:
  enabled: true
rule_runner:
  enabled: true
  extraction_passes: 2
  paper_fee_bps: 0
  paper_slippage_bps: 25
fleet:
  position_mode: alert_only
data_dir: {data_dir}
logs_dir: {tmp_path / 'logs'}
""",
        encoding="utf-8",
    )
    return config_path, context, spec


def _book(
    at: str,
    outcome: str,
    *,
    yes_bid: float,
    yes_ask: float,
) -> dict:
    return {
        "type": "BOOK",
        "at": at,
        "outcome": outcome,
        "yes": {
            "bids": [[yes_bid, 1000]],
            "asks": [[yes_ask, 1000]],
        },
        "no": {
            "bids": [[round(1 - yes_ask, 4), 1000]],
            "asks": [[round(1 - yes_bid, 4), 1000]],
        },
    }


def _article(
    at: str,
    article_id: str,
    domain: str,
    text: str = "Both senior delegations entered the room and talks began.",
) -> dict:
    return {
        "type": "ARTICLE",
        "at": at,
        "article": {
            "url": f"https://{domain}/{article_id}",
            "domain": domain,
            "title": text,
            "published_at": at,
            "fetched_at": at,
            "raw_text": text,
            "hash": article_id,
            "source_kind": "article",
        },
    }


def _timeline(path: Path, spec: RuleSpec) -> Path:
    outcome = spec.outcomes[0].name
    events = [
        _book(
            "2026-07-25T10:00:00+00:00",
            outcome,
            yes_bid=0.93,
            yes_ask=0.95,
        ),
        _article(
            "2026-07-25T10:00:01+00:00",
            "reuters-1",
            "reuters.com",
        ),
        _article(
            "2026-07-25T10:00:02+00:00",
            "ap-1",
            "apnews.com",
        ),
        _book(
            "2026-07-25T10:00:03+00:00",
            outcome,
            yes_bid=0.79,
            yes_ask=0.80,
        ),
        _book(
            "2026-07-25T10:05:03+00:00",
            outcome,
            yes_bid=0.86,
            yes_ask=0.87,
        ),
        {
            "type": "RESOLUTION",
            "at": "2026-09-30T23:59:59+00:00",
            "outcome": outcome,
            "resolved_yes": True,
        },
    ]
    path.write_text(
        "\n".join(json.dumps(item, sort_keys=True) for item in events),
        encoding="utf-8",
    )
    return path


def test_rules_first_replay_reprices_after_terminal_evidence_and_is_stable(
    tmp_path: Path,
) -> None:
    config_path, context, spec = _setup(tmp_path)
    timeline = _timeline(tmp_path / "timeline.jsonl", spec)

    first = replay_rule_market(config_path, context.market_id, timeline)
    second = replay_rule_market(config_path, context.market_id, timeline)

    assert first["result_sha256"] == second["result_sha256"]
    assert first["timeline_sha256"] == second["timeline_sha256"]
    assert first["classifier"]["wall_clock_free"] is True
    assert first["extraction"]["agreement_rate"] == 1.0
    assert first["decisions"]["executed_entries"] == 1
    assert first["decisions"]["duplicate_entries"] == 0
    assert first["proofs"]["completeness_rate"] == 1.0
    assert first["markouts"]["300"]["samples"] == 1
    assert first["markouts"]["300"]["mean_net_clv"] > 0
    assert first["settlement"]["complete"] is True
    assert first["settlement"]["cost_adjusted_pnl_usd"] > 0
    # The two confirming articles arrive against a 95c ask; entry becomes
    # possible only on the later 80c book event.
    executed = [
        cycle
        for cycle in first["cycles"]
        if cycle.get("executed")
    ]
    assert len(executed) == 1
    assert executed[0]["trigger"] == "BOOK"
    assert executed[0]["at"] == "2026-07-25T10:00:03+00:00"

    changed_config = tmp_path / "changed.yaml"
    changed_config.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "paper_slippage_bps: 25",
            "paper_slippage_bps: 50",
        ),
        encoding="utf-8",
    )
    changed = replay_rule_market(changed_config, context.market_id, timeline)
    assert changed["run_id"] != first["run_id"]
    assert changed["replay_policy_sha256"] != first["replay_policy_sha256"]


def test_blockade_replay_rejects_near_misses_before_official_announcement(
    tmp_path: Path,
) -> None:
    config_path, context, spec = _setup_blockade(tmp_path)
    outcome = spec.outcomes[0].name
    events = [
        _book(
            "2026-07-25T10:00:00+00:00",
            outcome,
            yes_bid=0.78,
            yes_ask=0.80,
        ),
        _article(
            "2026-07-25T10:00:01+00:00",
            "partial-exemption",
            "whitehouse.gov",
            "A limited or partial change created a specific vessel exemption.",
        ),
        _article(
            "2026-07-25T10:00:02+00:00",
            "conditional-preview",
            "whitehouse.gov",
            "The administration described a conditional end and plans to announce later.",
        ),
        _article(
            "2026-07-25T10:00:03+00:00",
            "leaked-draft",
            "reuters.com",
            "An anonymous leaked statement described a draft suspension.",
        ),
        _article(
            "2026-07-25T10:00:04+00:00",
            "unauthorized-comment",
            "whitehouse.gov",
            "An adviser not authorized to speak made an informal comment.",
        ),
        _article(
            "2026-07-25T10:00:05+00:00",
            "official-final",
            "whitehouse.gov",
            "The White House formally announced the present general end of the blockade.",
        ),
        {
            "type": "RESOLUTION",
            "at": "2026-12-31T23:59:59-05:00",
            "outcome": outcome,
            "resolved_yes": True,
        },
    ]
    timeline = tmp_path / "blockade-timeline.jsonl"
    timeline.write_text(
        "\n".join(json.dumps(item, sort_keys=True) for item in events),
        encoding="utf-8",
    )

    result = replay_rule_market(config_path, context.market_id, timeline)

    executed = [cycle for cycle in result["cycles"] if cycle.get("executed")]
    assert len(executed) == 1
    assert executed[0]["trigger"] == "ARTICLE"
    assert executed[0]["at"] == "2026-07-25T10:00:05+00:00"
    assert result["decisions"]["executed_entries"] == 1
    assert result["safety"]["false_terminal_actions"] == 0
    assert result["safety"]["settlement_source_violations"] == 0


def test_cuba_replay_uses_first_credible_source_but_rejects_exclusions(
    tmp_path: Path,
) -> None:
    config_path, context, spec = _setup_cuba_strike(tmp_path)
    outcome = spec.outcomes[0].name
    events = [
        _book(
            "2026-07-25T10:00:00+00:00",
            outcome,
            yes_bid=0.68,
            yes_ask=0.70,
        ),
        _article(
            "2026-07-25T10:00:01+00:00",
            "artillery",
            "whitehouse.gov",
            "The U.S. government confirmed artillery fire near Cuba.",
        ),
        _article(
            "2026-07-25T10:00:02+00:00",
            "intercepted",
            "reuters.com",
            "Reuters reported that a U.S. missile was intercepted before impact.",
        ),
        _article(
            "2026-07-25T10:00:03+00:00",
            "naval",
            "reuters.com",
            "Reuters reported naval shelling in Cuban territorial sea.",
        ),
        _article(
            "2026-07-25T10:00:04+00:00",
            "qualifying-reuters",
            "reuters.com",
            "Reuters credibly reported that a U.S. missile physically impacted Cuban ground territory.",
        ),
        {
            "type": "RESOLUTION",
            "at": "2026-12-31T23:59:59-05:00",
            "outcome": outcome,
            "resolved_yes": True,
        },
    ]
    timeline = tmp_path / "cuba-strike-timeline.jsonl"
    timeline.write_text(
        "\n".join(json.dumps(item, sort_keys=True) for item in events),
        encoding="utf-8",
    )

    result = replay_rule_market(config_path, context.market_id, timeline)

    executed = [cycle for cycle in result["cycles"] if cycle.get("executed")]
    assert len(executed) == 1
    assert executed[0]["trigger"] == "ARTICLE"
    assert executed[0]["at"] == "2026-07-25T10:00:04+00:00"
    assert result["decisions"]["executed_entries"] == 1
    assert result["safety"]["false_terminal_actions"] == 0
    assert result["safety"]["settlement_source_violations"] == 0


def test_historical_article_age_uses_replay_clock() -> None:
    article = Article(
        url="https://reuters.com/old",
        domain="reuters.com",
        title="",
        published_at="2020-01-01T00:00:00+00:00",
        fetched_at="2020-01-01T01:00:00+00:00",
        raw_text="full text",
        hash="old",
    )
    from datetime import datetime, timezone

    age = article_age_hours(
        article,
        as_of=datetime(2020, 1, 1, 1, tzinfo=timezone.utc),
    )
    assert age == 1.0


def test_timeline_rejects_lookahead_crossed_books_and_unknown_fields(
    tmp_path: Path,
) -> None:
    _config_path, _context, spec = _setup(tmp_path)
    outcome = spec.outcomes[0].name
    path = tmp_path / "bad.jsonl"
    future = _article(
        "2026-07-25T10:00:00+00:00",
        "future",
        "reuters.com",
    )
    future["article"]["published_at"] = "2026-07-25T10:00:01+00:00"
    path.write_text(json.dumps(future), encoding="utf-8")
    with pytest.raises(ValueError, match="before published_at"):
        load_rule_replay_timeline(path, spec)

    feed_summary = _article(
        "2026-07-25T10:00:00+00:00",
        "feed",
        "reuters.com",
    )
    feed_summary["article"]["source_kind"] = "feed"
    path.write_text(json.dumps(feed_summary), encoding="utf-8")
    with pytest.raises(ValueError, match="full text"):
        load_rule_replay_timeline(path, spec)

    crossed = _book(
        "2026-07-25T10:00:00+00:00",
        outcome,
        yes_bid=0.80,
        yes_ask=0.79,
    )
    path.write_text(json.dumps(crossed), encoding="utf-8")
    with pytest.raises(ValueError, match="crossed or locked"):
        load_rule_replay_timeline(path, spec)

    first = _book(
        "2026-07-25T10:00:02+00:00",
        outcome,
        yes_bid=0.70,
        yes_ask=0.71,
    )
    second = _book(
        "2026-07-25T10:00:01+00:00",
        outcome,
        yes_bid=0.70,
        yes_ask=0.71,
    )
    path.write_text(
        "\n".join([json.dumps(first), json.dumps(second)]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="out of order"):
        load_rule_replay_timeline(path, spec)

    first["future_quote"] = 0.9
    path.write_text(json.dumps(first), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown keys"):
        load_rule_replay_timeline(path, spec)


def test_resolution_is_required_for_open_position_pnl(tmp_path: Path) -> None:
    config_path, context, spec = _setup(tmp_path)
    timeline = _timeline(tmp_path / "timeline.jsonl", spec)
    lines = timeline.read_text(encoding="utf-8").splitlines()
    timeline.write_text("\n".join(lines[:-1]), encoding="utf-8")
    result = replay_rule_market(config_path, context.market_id, timeline)
    assert result["settlement"]["complete"] is False
    assert result["settlement"]["cost_adjusted_pnl_usd"] is None
    assert result["settlement"]["unresolved_open_outcomes"] == [
        spec.outcomes[0].name
    ]


def _rehash(summary: dict) -> dict:
    result = dict(summary)
    result.pop("output_path", None)
    result.pop("result_sha256", None)
    result["run_id"] = hashlib.sha256(
        (
            f"{result['rule_spec_sha256']}:{result['timeline_sha256']}:"
            f"{result['replay_policy_sha256']}"
        ).encode("utf-8")
    ).hexdigest()[:20]
    result["result_sha256"] = sha256_json(result)
    return result


def test_promotion_report_clusters_events_and_never_changes_configuration(
    tmp_path: Path,
) -> None:
    config_path, context, spec = _setup(tmp_path)
    base = replay_rule_market(
        config_path,
        context.market_id,
        _timeline(tmp_path / "timeline.jsonl", spec),
        dataset_role="frozen_oos",
    )
    summaries = []
    for index in range(100):
        item = {
            **base,
            "market_id": f"market-{index:02d}",
            "event_slug": f"event-{index:02d}",
            "rule_spec_sha256": sha256_json(
                {"fixture_spec": index}
            ),
            "timeline_sha256": sha256_json(
                {"fixture_timeline": index}
            ),
            "human_labels": {
                **base["human_labels"],
                "provided": 1,
                "matched": 1,
                "terminal_decisions": 1,
                "disagreements": 0,
                "unknown_evaluations": 0,
                "agreement_rate": 1.0,
                "records": [
                    {
                        "evaluation_sha256": hashlib.sha256(
                            f"evaluation-{index}".encode("utf-8")
                        ).hexdigest(),
                        "expected_state": "TERMINAL_YES",
                        "labeler_id": "reviewer-01",
                        "rationale": "Fixture terminal decision.",
                        "matched": True,
                        "actual_state": "TERMINAL_YES",
                        "actual_terminal": True,
                        "agrees": True,
                    }
                ],
            },
        }
        summaries.append(_rehash(item))
    report = build_promotion_report(summaries)
    family = report["families"]["OCCURRENCE_BEFORE_DEADLINE"]
    assert family["status"] == "PASS"
    assert family["metrics"]["independent_events"] == 100
    assert family["metrics"]["resolved_events"] == 100
    assert family["metrics"]["episode_ev_bootstrap_lcb_usd"] > 0
    assert report["eligible_for_manual_canary_review"] == [
        "OCCURRENCE_BEFORE_DEADLINE"
    ]
    assert report["configuration_changed"] is False
    assert report["live_enabled"] is False

    repeated_label = json.loads(json.dumps(summaries))
    repeated_label[1]["human_labels"]["records"] = [
        dict(repeated_label[0]["human_labels"]["records"][0])
    ]
    repeated_label[1] = _rehash(repeated_label[1])
    repeated_report = build_promotion_report(repeated_label)
    repeated_family = repeated_report["families"][
        "OCCURRENCE_BEFORE_DEADLINE"
    ]
    assert repeated_family["status"] == "FAIL"
    assert (
        repeated_family["metrics"]["human_labeled_terminal_decisions"]
        == 99
    )
    assert repeated_family["metrics"]["duplicate_human_label_records"] == 1

    conflicting_label = json.loads(json.dumps(summaries))
    conflict_record = conflicting_label[0]["human_labels"]["records"][0]
    conflicting_label[1]["human_labels"]["records"] = [
        {
            **conflict_record,
            "expected_state": "TERMINAL_NO",
            "agrees": False,
        }
    ]
    conflicting_label[1]["human_labels"]["disagreements"] = 1
    conflicting_label[1]["human_labels"]["agreement_rate"] = 0.0
    conflicting_label[1] = _rehash(conflicting_label[1])
    conflict_report = build_promotion_report(conflicting_label)
    conflict_family = conflict_report["families"][
        "OCCURRENCE_BEFORE_DEADLINE"
    ]
    assert conflict_family["status"] == "FAIL"
    assert conflict_family["metrics"]["human_label_conflicts"] == 1

    deduplicated = build_promotion_report(
        [summaries[0], summaries[0]],
        policy=PromotionPolicy(
            min_replay_runs=1,
            min_independent_events=1,
            min_evidence_articles=1,
            min_terminal_opportunities=1,
            min_positive_edge_opportunities=1,
            min_resolved_events=1,
            min_markout_5m_samples=1,
            min_markout_5m_events=1,
            min_human_labeled_terminal_decisions=0,
        ),
    )
    assert (
        deduplicated["families"]["OCCURRENCE_BEFORE_DEADLINE"]["metrics"][
            "replay_runs"
        ]
        == 1
    )

    duplicated = [dict(item) for item in summaries]
    duplicated[0] = _rehash(
        {
            **duplicated[0],
            "decisions": {
                **duplicated[0]["decisions"],
                "duplicate_entries": 1,
            },
        }
    )
    failed = build_promotion_report(duplicated)
    assert failed["families"]["OCCURRENCE_BEFORE_DEADLINE"]["status"] == "FAIL"


def test_promotion_report_separates_unsupported_family_and_verifies_hash(
    tmp_path: Path,
) -> None:
    config_path, context, spec = _setup(tmp_path)
    base = replay_rule_market(
        config_path,
        context.market_id,
        _timeline(tmp_path / "timeline.jsonl", spec),
        dataset_role="frozen_oos",
    )
    status_partition = {
        **base["policy_partition"],
        "rule_family": "STATUS_AT_DEADLINE",
    }
    status = _rehash(
        {
            **base,
            "rule_family": "STATUS_AT_DEADLINE",
            "policy_partition": status_partition,
            "policy_partition_sha256": sha256_json(status_partition),
        }
    )
    policy = PromotionPolicy(
        min_replay_runs=1,
        min_independent_events=1,
        min_evidence_articles=1,
        min_terminal_opportunities=1,
        min_positive_edge_opportunities=1,
        min_resolved_events=1,
        min_markout_5m_samples=1,
        min_markout_5m_events=1,
        min_human_labeled_terminal_decisions=0,
    )
    report = build_promotion_report([status], policy=policy)
    assert report["families"]["STATUS_AT_DEADLINE"]["status"] == "FAIL"
    gate = next(
        item
        for item in report["families"]["STATUS_AT_DEADLINE"]["gates"]
        if item["name"] == "family_is_confirmation_executable"
    )
    assert gate["passed"] is False

    path = tmp_path / "summary.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    loaded = load_replay_summaries(path)
    assert loaded[0]["run_id"] == base["run_id"]
    tampered = dict(base)
    tampered["market_id"] = "tampered"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="hash verification"):
        load_replay_summaries(path)


def test_promotion_rejects_development_mixed_policy_and_zero_ev(
    tmp_path: Path,
) -> None:
    config_path, context, spec = _setup(tmp_path)
    timeline = _timeline(tmp_path / "timeline.jsonl", spec)
    development = replay_rule_market(
        config_path,
        context.market_id,
        timeline,
    )
    low_sample_policy = PromotionPolicy(
        min_replay_runs=1,
        min_independent_events=1,
        min_evidence_articles=1,
        min_terminal_opportunities=1,
        min_positive_edge_opportunities=1,
        min_resolved_events=1,
        min_markout_5m_samples=1,
        min_markout_5m_events=1,
        min_human_labeled_terminal_decisions=0,
    )
    report = build_promotion_report(
        [development],
        policy=low_sample_policy,
    )
    family = report["families"]["OCCURRENCE_BEFORE_DEADLINE"]
    gate = next(
        item
        for item in family["gates"]
        if item["name"] == "dataset_role_is_promotion_eligible"
    )
    assert gate["passed"] is False

    oos = replay_rule_market(
        config_path,
        context.market_id,
        timeline,
        dataset_role="frozen_oos",
    )
    changed_partition = {
        **oos["policy_partition"],
        "execution_policy_sha256": sha256_json(
            {"different_execution_policy": True}
        ),
    }
    mixed = _rehash(
        {
            **oos,
            "timeline_sha256": sha256_json({"other": "timeline"}),
            "policy_partition": changed_partition,
            "policy_partition_sha256": sha256_json(changed_partition),
        }
    )
    family = build_promotion_report(
        [oos, mixed],
        policy=low_sample_policy,
    )["families"]["OCCURRENCE_BEFORE_DEADLINE"]
    assert family["status"] == "FAIL"
    assert family["metrics"]["policy_partitions"] == 2

    zero = _rehash(
        {
            **oos,
            "settlement": {
                **oos["settlement"],
                "cost_adjusted_pnl_usd": 0.0,
                "one_cent_stress_pnl_usd": 0.0,
                "two_cent_stress_pnl_usd": 0.0,
            },
        }
    )
    family = build_promotion_report(
        [zero],
        policy=low_sample_policy,
    )["families"]["OCCURRENCE_BEFORE_DEADLINE"]
    strict_gates = {
        item["name"]: item["passed"]
        for item in family["gates"]
        if item["name"]
        in {
            "total_cost_adjusted_pnl_usd",
            "one_cent_stress_pnl_usd",
            "episode_ev_bootstrap_lcb_usd",
        }
    }
    assert strict_gates == {
        "total_cost_adjusted_pnl_usd": False,
        "one_cent_stress_pnl_usd": False,
        "episode_ev_bootstrap_lcb_usd": False,
    }
