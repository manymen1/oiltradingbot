from __future__ import annotations

from dataclasses import replace

import pytest

from test_discovery import _analyzed_context, _binary_event
from test_forward_recorder import _setup as _forward_setup

from polybot.discovery.config import (
    DiscoveryConfig,
    ProfitPriorityConfig,
    forward_recorder_db_path,
    load_discovery_config,
)
from polybot.discovery.profit_priority import (
    PriorityEvidence,
    ProfitPriorityRecord,
    _sampling_horizon,
    collect_priority_evidence,
    score_profit_priority,
)
from polybot.discovery.sources import source_plan_sha256
from polybot.rules.forward import (
    ForwardRecorderStore,
    _evidence_policy_payload,
)


def _context():
    context = _analyzed_context(_binary_event(slug="priority-market"))
    return context.from_dict(
        {
            **context.as_dict(),
            "state": "PAPER_ELIGIBLE",
        }
    )


def _policy(**changes):
    return replace(
        ProfitPriorityConfig(
            min_terminal_observations=20,
            min_human_labels=20,
            min_quote_samples=20,
            min_stressed_fill_samples=20,
            min_resolved_trades=5,
        ),
        **changes,
    )


def _evidence(**changes):
    base = PriorityEvidence(
        monitored_market_hours=100.0,
        terminal_opportunities=100,
        human_labels=100,
        quote_trials=100,
        quote_survivals=90,
        stressed_fill_trials=100,
        stressed_fills=80,
        fillable_notionals_usd=tuple(float(i) for i in range(1, 101)),
        one_cent_shocked_edges=(0.08,) * 25,
        resolved_trade_pnls_usd=(1.0,) * 10,
        p95_submission_latency_ms=1_000.0,
        quote_half_life_ms=5_000.0,
        observation_sha256s=("a", "b"),
        data_cutoff_at="2026-07-25T00:00:00+00:00",
    )
    return replace(base, **changes)


def _score(evidence=None, *, family="SOURCE_LOCKED_ANNOUNCEMENT", **kwargs):
    return score_profit_priority(
        context=_context(),
        rule_family=family,
        source_adapter_id="official-source-v1",
        policy_partition_sha256="partition-a",
        evidence=evidence or _evidence(),
        policy=kwargs.pop("policy", _policy()),
        computed_at=kwargs.pop(
            "computed_at",
            "2026-07-25T01:00:00+00:00",
        ),
        execution_supported=kwargs.pop("execution_supported", True),
    )


def test_identical_inputs_reproduce_score_hash_across_computation_times() -> None:
    first = _score(computed_at="2026-07-25T01:00:00+00:00")
    second = _score(computed_at="2026-07-25T02:00:00+00:00")
    assert first.score_sha256 == second.score_sha256
    assert first.computed_at != second.computed_at


def test_future_dated_priority_evidence_is_rejected() -> None:
    with pytest.raises(ValueError, match="future-dated"):
        _score(
            _evidence(data_cutoff_at="2026-07-26T00:00:00+00:00"),
            computed_at="2026-07-25T01:00:00+00:00",
        )


def test_missing_labels_create_error_reserve_and_zero_execution() -> None:
    record = _score(_evidence(human_labels=0))
    assert record.execution_priority == 0
    assert record.component_values["terminal_error_rate_ucb"] == 0.05
    assert any(
        blocker.startswith("insufficient_human_labels")
        for blocker in record.blockers
    )


def test_false_terminal_action_materially_lowers_priority() -> None:
    clean = _score()
    dirty = _score(_evidence(false_terminal_actions=5))
    assert (
        dirty.component_values["terminal_error_reserve_usd"]
        > clean.component_values["terminal_error_reserve_usd"]
    )
    assert (
        dirty.component_values["conservative_priority_value"]
        < clean.component_values["conservative_priority_value"]
    )
    assert "false_terminal_or_source_violation_observed" in dirty.blockers


def test_one_cent_negative_edge_cannot_create_execution_priority() -> None:
    record = _score(
        _evidence(one_cent_shocked_edges=(-0.03,) * 25)
    )
    assert record.execution_priority == 0
    assert "one_cent_shocked_edge_not_positive" in record.blockers


def test_fillable_notional_uses_conservative_p25() -> None:
    record = _score(
        _evidence(fillable_notionals_usd=(10.0, 20.0, 30.0, 1000.0))
    )
    assert record.component_values["fillable_notional_p25_usd"] == 17.5


def test_cold_start_has_zero_execution_but_remains_explorable() -> None:
    record = _score(PriorityEvidence())
    assert record.cold_start is True
    assert record.exploration_eligible is True
    assert record.execution_priority == 0
    assert record.monitor_priority > 0


def test_subjective_family_has_no_execution_or_exploration_priority() -> None:
    record = _score(
        family="SUBJECTIVE_DISCRETIONARY",
        execution_supported=False,
    )
    assert record.execution_priority == 0
    assert record.exploration_eligible is False
    assert record.monitor_priority == 0


def test_p95_beyond_recorder_horizon_blocks_exploitation() -> None:
    record = _score(
        _evidence(p95_submission_latency_ms=10_001.0),
        policy=_policy(max_supported_latency_ms=10_000),
    )
    assert record.execution_priority == 0
    assert (
        "measured_p95_latency_exceeds_survival_horizon"
        in record.blockers
    )


def test_quote_sampling_uses_first_horizon_at_or_after_measured_p95() -> None:
    config = DiscoveryConfig()
    assert _sampling_horizon(config, 1_200.0) == 2_000
    assert _sampling_horizon(config, None) == 10_000
    assert _sampling_horizon(config, 10_001.0) == 10_000


def test_unavailable_quote_samples_do_not_become_survival() -> None:
    record = _score(
        _evidence(
            quote_trials=0,
            quote_survivals=0,
            stressed_fill_trials=0,
            stressed_fills=0,
            fillable_notionals_usd=(),
        )
    )
    assert record.component_values["quote_survival_lcb"] == 0
    assert record.component_values["stressed_fill_lcb"] == 0
    assert record.execution_priority == 0


def test_integrity_blocker_is_bound_into_score() -> None:
    record = _score(
        _evidence(
            integrity_blockers=(
                "time_integrity:decisions_before_evidence",
            )
        )
    )
    assert (
        "time_integrity:decisions_before_evidence" in record.blockers
    )
    assert record.execution_priority == 0


def test_priority_record_rejects_unknown_fields() -> None:
    raw = _score().as_dict()
    raw["future_field"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        ProfitPriorityRecord.from_dict(raw)


def test_priority_record_hash_is_reconstructable() -> None:
    record = _score()
    loaded = ProfitPriorityRecord.from_dict(record.as_dict())
    assert loaded == record
    raw = record.as_dict()
    raw["monitor_priority"] += 1
    with pytest.raises(ValueError, match="hash"):
        ProfitPriorityRecord.from_dict(raw)


def test_duplicate_observation_hashes_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        _score(_evidence(observation_sha256s=("same", "same")))


def test_forward_evidence_uses_exact_recorder_source_and_context_binding(
    tmp_path,
) -> None:
    config_path, context, spec, plan = _forward_setup(tmp_path)
    config = load_discovery_config(config_path)
    recorder = ForwardRecorderStore(forward_recorder_db_path(config))
    binding = recorder.ensure_binding(
        context,
        spec,
        plan,
        config.forward_recorder,
        evidence_policy=_evidence_policy_payload(config),
        created_at="2026-07-25T00:00:00+00:00",
    )

    evidence = collect_priority_evidence(
        config,
        context,
        plan,
        spec_sha256=spec.spec_sha256,
        source_identity=source_plan_sha256(plan),
        computed_at="2026-07-25T01:00:00+00:00",
    )

    assert binding in evidence.observation_sha256s
    assert evidence.data_cutoff_at == "2026-07-25T00:00:00+00:00"


def test_forward_evidence_does_not_pool_incompatible_recorder_policy(
    tmp_path,
) -> None:
    config_path, context, spec, plan = _forward_setup(tmp_path)
    config = load_discovery_config(config_path)
    recorder = ForwardRecorderStore(forward_recorder_db_path(config))
    recorder.ensure_binding(
        context,
        spec,
        plan,
        replace(config.forward_recorder, max_book_levels=19),
        created_at="2026-07-25T00:00:00+00:00",
    )

    evidence = collect_priority_evidence(
        config,
        context,
        plan,
        spec_sha256=spec.spec_sha256,
        source_identity=source_plan_sha256(plan),
        computed_at="2026-07-25T01:00:00+00:00",
    )

    assert evidence == PriorityEvidence()


def test_forward_evidence_does_not_pool_incompatible_evidence_policy(
    tmp_path,
) -> None:
    config_path, context, spec, plan = _forward_setup(tmp_path)
    config = load_discovery_config(config_path)
    recorder = ForwardRecorderStore(forward_recorder_db_path(config))
    recorder.ensure_binding(
        context,
        spec,
        plan,
        config.forward_recorder,
        evidence_policy={
            "policy_version": "obsolete-extractor-policy",
        },
        created_at="2026-07-25T00:00:00+00:00",
    )

    evidence = collect_priority_evidence(
        config,
        context,
        plan,
        spec_sha256=spec.spec_sha256,
        source_identity=source_plan_sha256(plan),
        computed_at="2026-07-25T01:00:00+00:00",
    )

    assert evidence == PriorityEvidence()
