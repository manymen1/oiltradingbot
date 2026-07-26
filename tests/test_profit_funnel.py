from __future__ import annotations

import json

import pytest

from polybot.discovery.config import DiscoveryConfig, ScoringConfig
from polybot.discovery.profit_funnel import (
    _replay_profit_flags,
    _stage,
    build_profit_funnel,
)
from polybot.discovery.scorer import grade_market
from polybot.discovery.store import DiscoveryStore
from test_discovery import _analyzed_context, _binary_event


def _write_json(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_profit_funnel_reconciles_every_stage_and_preserves_zeroes(
    tmp_path,
) -> None:
    config = DiscoveryConfig(data_dir=tmp_path)
    store = DiscoveryStore(tmp_path)
    market = grade_market(
        _analyzed_context(_binary_event(slug="funnel-market")),
        ScoringConfig(allow_fixture_analysis_live=True),
    )
    store.save_context(market)
    _write_json(
        tmp_path / "discovery_scan.json",
        {
            "enumerated_event_keys": ["event-1"],
            "candidate_event_keys": ["event-1"],
            "candidate_market_ids": [market.market_id],
            "rejection_by_event": {},
            "scan_sha256": "scan",
        },
    )

    report = build_profit_funnel(
        config,
        store,
        generated_at="2026-07-25T00:00:00+00:00",
    )

    assert report["paper_only"] is True
    assert report["model_pricing_funnel"]["included_in_rules_first_counts"] is False
    assert report["stages"][2]["numerator"] == 1
    assert report["stages"][3]["numerator"] == 0
    for stage in report["stages"]:
        assert stage["numerator"] + sum(
            stage["primary_loss_reasons"].values()
        ) == stage["eligible_denominator"]


def test_profit_funnel_hash_excludes_report_generation_time(tmp_path) -> None:
    config = DiscoveryConfig(data_dir=tmp_path)
    store = DiscoveryStore(tmp_path)
    _write_json(
        tmp_path / "discovery_scan.json",
        {
            "enumerated_event_keys": [],
            "candidate_event_keys": [],
            "candidate_market_ids": [],
            "rejection_by_event": {},
            "scan_sha256": "empty",
        },
    )
    first = build_profit_funnel(
        config,
        store,
        generated_at="2026-07-25T00:00:00+00:00",
    )
    second = build_profit_funnel(
        config,
        store,
        generated_at="2026-07-25T01:00:00+00:00",
    )
    assert first["report_sha256"] == second["report_sha256"]
    assert first["generated_at"] != second["generated_at"]


def test_profit_funnel_stage_rejects_unattributed_losses() -> None:
    with pytest.raises(ValueError, match="does not reconcile"):
        _stage(
            "bad",
            denominator=3,
            numerator=1,
            loss_reasons={"only_one_loss": 1},
            unit="market",
            independent_event_clusters=1,
        )


def test_replay_profit_flags_use_only_unique_forward_or_frozen_runs(
    tmp_path,
) -> None:
    base = {
        "kind": "rules_first_replay",
        "market_id": "market-1",
        "event_slug": "event-1",
        "policy_partition_sha256": "partition-a",
        "result_sha256": "result-1",
        "settlement": {
            "complete": True,
            "traded": True,
            "cost_adjusted_pnl_usd": 1.0,
            "one_cent_stress_pnl_usd": 0.5,
        },
    }
    _write_json(
        tmp_path / "rule_replays" / "a" / "summary.json",
        {**base, "run_id": "run-1", "dataset_role": "forward"},
    )
    _write_json(
        tmp_path / "rule_replays" / "b" / "summary.json",
        {**base, "run_id": "run-1", "dataset_role": "forward"},
    )
    _write_json(
        tmp_path / "rule_replays" / "dev" / "summary.json",
        {
            **base,
            "run_id": "dev-run",
            "result_sha256": "dev",
            "dataset_role": "development",
            "settlement": {
                **base["settlement"],
                "cost_adjusted_pnl_usd": -100.0,
            },
        },
    )

    assert _replay_profit_flags(tmp_path) == {
        "market-1": {
            "resolved": True,
            "positive_after_cost": True,
            "positive_after_one_cent": True,
        }
    }
    assert _replay_profit_flags(
        tmp_path,
        required_partitions={"market-1": "different-partition"},
    ) == {}
