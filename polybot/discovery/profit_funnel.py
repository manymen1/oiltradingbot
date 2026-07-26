from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from polybot.rules.contracts import sha256_json
from polybot.rules.store import RuleStore

from .config import (
    DiscoveryConfig,
    forward_recorder_db_path,
    load_discovery_config,
    rule_store_db_path,
)
from .profit_priority import (
    OBJECTIVE_FAMILIES,
    ProfitPriorityRecord,
    load_priority_snapshot,
)
from .sources import source_plan_sha256
from .store import DiscoveryStore
from .types import MarketContext

PROFIT_FUNNEL_SCHEMA_VERSION = 1


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 8) if denominator else None


def _stage(
    name: str,
    *,
    denominator: int,
    numerator: int,
    loss_reasons: dict[str, int],
    unit: str,
    independent_event_clusters: int,
) -> dict[str, Any]:
    clean = {
        str(reason): int(count)
        for reason, count in sorted(loss_reasons.items())
        if int(count) > 0
    }
    if numerator > denominator:
        raise ValueError(f"profit funnel stage {name} exceeds denominator")
    if sum(clean.values()) != denominator - numerator:
        raise ValueError(
            f"profit funnel stage {name} does not reconcile: "
            f"{numerator}+{sum(clean.values())}!={denominator}"
        )
    return {
        "stage": name,
        "unit": unit,
        "eligible_denominator": denominator,
        "numerator": numerator,
        "conversion_rate": _ratio(numerator, denominator),
        "independent_event_clusters": independent_event_clusters,
        "primary_loss_reasons": clean,
    }


def _context_reason(context: MarketContext, fallback: str) -> str:
    return (
        str(context.state_reasons[0]).split(":", 1)[0]
        if context.state_reasons
        else fallback
    )


def build_profit_funnel(
    config: DiscoveryConfig,
    store: DiscoveryStore,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Reconstruct a market-level terminal-confirmation funnel.

    Every stage uses a single unit and assigns every loss exactly one primary
    reason. Observation totals are reported separately so repeated decisions
    cannot inflate independent market/event conversion counts.
    """

    generated = generated_at or datetime.now(timezone.utc).isoformat()
    contexts = {
        context.market_id: context for context in store.all_contexts()
    }
    discovery_scan = _read_json(config.data_dir / "discovery_scan.json")
    coverage = store.load_coverage_manifest() or {}
    enumerated_keys = set(discovery_scan.get("enumerated_event_keys", []))
    candidate_event_keys = set(
        discovery_scan.get("candidate_event_keys", [])
    )
    candidate_market_ids = set(
        discovery_scan.get("candidate_market_ids", [])
    )
    rejection_by_event = discovery_scan.get("rejection_by_event", {})
    rejection_by_event = (
        rejection_by_event if isinstance(rejection_by_event, dict) else {}
    )

    rule_store = RuleStore(rule_store_db_path(config))
    current_specs: dict[str, Any] = {}
    valid_plans: set[str] = set()
    objective: set[str] = set()
    family_by_market: dict[str, str] = {}
    source_by_market: dict[str, str] = {}
    for market_id, context in contexts.items():
        spec = rule_store.load_spec(market_id, context.rule_text_sha256)
        if spec is None:
            continue
        current_specs[market_id] = spec
        family_by_market[market_id] = spec.semantics.rule_family
        plan = store.load_source_plan(market_id)
        if (
            plan is None
            or plan.rule_text_sha256 != context.rule_text_sha256
            or plan.rule_spec_sha256 != spec.spec_sha256
            or plan.missing_required_source_refs
        ):
            continue
        valid_plans.add(market_id)
        source_by_market[market_id] = source_plan_sha256(plan)
        if (
            spec.semantics.rule_family in OBJECTIVE_FAMILIES
            and spec.semantics.rule_family
            in {
                item.strip().upper()
                for item in config.rule_runner.paper_execution_families
            }
        ):
            objective.add(market_id)

    priorities = load_priority_snapshot(config.data_dir)
    forward = _forward_market_metrics(
        config,
        required_bindings={
            market_id: str(
                record.component_values.get("forward_binding_sha256")
                or ""
            )
            for market_id, record in priorities.items()
        },
    )
    replay = _replay_profit_flags(config.data_dir)

    stages: list[dict[str, Any]] = []
    active_denominator = len(enumerated_keys)
    active_numerator = active_denominator
    stages.append(
        _stage(
            "active_events_enumerated",
            denominator=active_denominator,
            numerator=active_numerator,
            loss_reasons={},
            unit="event",
            independent_event_clusters=active_numerator,
        )
    )
    candidate_losses = Counter(
        str(rejection_by_event.get(key) or "not_geopolitical")
        for key in enumerated_keys - candidate_event_keys
    )
    stages.append(
        _stage(
            "geopolitical_candidates",
            denominator=len(enumerated_keys),
            numerator=len(candidate_event_keys),
            loss_reasons=dict(candidate_losses),
            unit="event",
            independent_event_clusters=len(candidate_event_keys),
        )
    )
    unparseable_count = max(
        0,
        len(candidate_event_keys) - len(candidate_market_ids),
    )
    stages.append(
        _stage(
            "parseable_market_contexts",
            denominator=len(candidate_event_keys),
            numerator=min(
                len(candidate_event_keys),
                len(candidate_market_ids),
            ),
            loss_reasons=(
                {"unparseable_event": unparseable_count}
                if unparseable_count
                else {}
            ),
            unit="market_context",
            independent_event_clusters=len(
                {
                    contexts[market_id].event_slug
                    for market_id in candidate_market_ids
                    if market_id in contexts
                }
            ),
        )
    )

    parseable_contexts = {
        market_id
        for market_id in candidate_market_ids
        if market_id in contexts
    }
    stages.extend(
        _market_stage_sequence(
            contexts=contexts,
            initial=parseable_contexts,
            current_specs=set(current_specs),
            valid_plans=valid_plans,
            objective=objective,
            forward=forward,
            priorities=priorities,
            replay=replay,
        )
    )

    partitions: dict[str, dict[str, Any]] = {}
    grouped: dict[
        tuple[str, str, str],
        list[ProfitPriorityRecord],
    ] = {}
    for record in priorities.values():
        key = (
            record.rule_family,
            record.source_adapter_id,
            record.policy_partition_sha256,
        )
        grouped.setdefault(key, []).append(record)
    for (family, source, partition), records in sorted(grouped.items()):
        market_ids = {record.market_id for record in records}
        partition_priorities = {
            record.market_id: record for record in records
        }
        replay_partitions = {
            record.market_id: str(
                record.component_values.get(
                    "replay_policy_partition_sha256",
                )
                or ""
            )
            for record in records
        }
        partition_replay = _replay_profit_flags(
            config.data_dir,
            required_partitions=replay_partitions,
        )
        key = sha256_json(
            {
                "rule_family": family,
                "source_adapter_id": source,
                "policy_partition_sha256": partition,
            }
        )
        partitions[key] = {
            "rule_family": family,
            "source_adapter_id": source,
            "policy_partition_sha256": partition,
            "replay_policy_partition_sha256s": sorted(
                {
                    value
                    for value in replay_partitions.values()
                    if value
                }
            ),
            "data_cutoff_at": max(
                (
                    record.data_cutoff_at
                    for record in records
                    if record.data_cutoff_at
                ),
                default="",
            ),
            "market_count": len(market_ids),
            "independent_event_clusters": len(
                {
                    contexts[market_id].event_slug
                    for market_id in market_ids
                    if market_id in contexts
                }
            ),
            "forward_sessions": sum(
                1
                for market_id in market_ids
                if forward.get(market_id, {}).get("sessions", 0) > 0
            ),
            "terminal_observations": sum(
                int(forward.get(market_id, {}).get("terminal_proofs", 0))
                for market_id in market_ids
            ),
            "human_labels": sum(
                record.component_sample_sizes.get("human_labels", 0)
                for record in records
            ),
            "stressed_fill_samples": sum(
                record.component_sample_sizes.get(
                    "stressed_fill_trials",
                    0,
                )
                for record in records
            ),
            "positive_after_cost_clusters": len(
                {
                    contexts[market_id].event_slug
                    for market_id in market_ids
                    if market_id in contexts
                    and partition_replay.get(market_id, {}).get(
                        "positive_after_cost",
                        False,
                    )
                }
            ),
            "positive_after_one_cent_shock_clusters": len(
                {
                    contexts[market_id].event_slug
                    for market_id in market_ids
                    if market_id in contexts
                    and partition_replay.get(market_id, {}).get(
                        "positive_after_one_cent",
                        False,
                    )
                }
            ),
            "stages": _market_stage_sequence(
                contexts=contexts,
                initial=market_ids & parseable_contexts,
                current_specs=set(current_specs),
                valid_plans=valid_plans,
                objective=objective,
                forward=forward,
                priorities=partition_priorities,
                replay=partition_replay,
            ),
        }

    stable = {
        "schema_version": PROFIT_FUNNEL_SCHEMA_VERSION,
        "kind": "rules_first_profit_funnel",
        "paper_only": True,
        "coverage_status": (
            coverage.get("coverage_status")
            or discovery_scan.get("coverage_status")
            or "MISSING"
        ),
        "coverage_complete": bool(
            coverage.get(
                "coverage_complete",
                discovery_scan.get("coverage_status") == "COMPLETE",
            )
        ),
        "coverage_sha256": coverage.get("coverage_sha256"),
        "discovery_scan_sha256": discovery_scan.get("scan_sha256"),
        "stages": stages,
        "partitions": partitions,
        "observation_totals": {
            "forward_sessions": sum(
                int(item.get("sessions", 0)) for item in forward.values()
            ),
            "terminal_evidence_observations": sum(
                int(item.get("terminal_proofs", 0))
                for item in forward.values()
            ),
            "fresh_quote_anchors": sum(
                int(item.get("fresh_quotes", 0))
                for item in forward.values()
            ),
            "survived_quote_samples": sum(
                int(item.get("survived_quotes", 0))
                for item in forward.values()
            ),
            "stressed_fill_samples": sum(
                int(item.get("stressed_fills", 0))
                for item in forward.values()
            ),
        },
        "model_pricing_funnel": {
            "separate": True,
            "included_in_rules_first_counts": False,
        },
    }
    return {
        **stable,
        "data_cutoff_at": _data_cutoff(
            coverage,
            discovery_scan,
            priorities.values(),
        ),
        "generated_at": generated,
        "report_sha256": sha256_json(stable),
    }


def _market_stage_sequence(
    *,
    contexts: dict[str, MarketContext],
    initial: set[str],
    current_specs: set[str],
    valid_plans: set[str],
    objective: set[str],
    forward: dict[str, dict[str, Any]],
    priorities: dict[str, ProfitPriorityRecord],
    replay: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    stages: list[dict[str, Any]] = []

    def append_stage(
        name: str,
        previous: set[str],
        current: set[str],
        reason,
    ) -> set[str]:
        current = previous & current
        losses = Counter(reason(market_id) for market_id in previous - current)
        stages.append(
            _stage(
                name,
                denominator=len(previous),
                numerator=len(current),
                loss_reasons=dict(losses),
                unit="market",
                independent_event_clusters=len(
                    {
                        contexts[market_id].event_slug
                        for market_id in current
                        if market_id in contexts
                    }
                ),
            )
        )
        return current

    current = append_stage(
        "valid_current_rule_specs",
        initial,
        current_specs,
        lambda market_id: _context_reason(
            contexts[market_id],
            "valid_current_rule_spec_missing",
        ),
    )
    current = append_stage(
        "valid_current_source_plans",
        current,
        valid_plans,
        lambda _market_id: "valid_current_source_plan_missing",
    )
    current = append_stage(
        "objective_execution_supported_families",
        current,
        objective,
        lambda market_id: (
            f"unsupported_family:{priorities[market_id].rule_family}"
            if market_id in priorities
            else "unsupported_or_unranked_family"
        ),
    )
    current = append_stage(
        "forward_sessions",
        current,
        {
            market_id
            for market_id, metrics in forward.items()
            if int(metrics.get("sessions", 0)) > 0
        },
        lambda _market_id: "no_forward_session",
    )
    current = append_stage(
        "terminal_evidence_observations",
        current,
        {
            market_id
            for market_id, metrics in forward.items()
            if int(metrics.get("terminal_proofs", 0)) > 0
        },
        lambda _market_id: "no_terminal_evidence",
    )
    current = append_stage(
        "human_correct_terminal_decisions",
        current,
        {
            market_id
            for market_id, record in priorities.items()
            if record.component_sample_sizes.get("human_labels", 0) > 0
            and "false_terminal_or_source_violation_observed"
            not in record.blockers
        },
        lambda _market_id: "missing_or_incorrect_human_label",
    )
    current = append_stage(
        "fresh_executable_quotes",
        current,
        {
            market_id
            for market_id, metrics in forward.items()
            if int(metrics.get("fresh_quotes", 0)) > 0
        },
        lambda _market_id: "fresh_executable_quote_missing",
    )
    current = append_stage(
        "quotes_surviving_measured_p95_latency",
        current,
        {
            market_id
            for market_id, metrics in forward.items()
            if int(metrics.get("survived_quotes", 0)) > 0
            and (
                market_id not in priorities
                or "measured_p95_latency_exceeds_survival_horizon"
                not in priorities[market_id].blockers
            )
        },
        lambda market_id: (
            "measured_p95_latency_exceeds_survival_horizon"
            if market_id in priorities
            and "measured_p95_latency_exceeds_survival_horizon"
            in priorities[market_id].blockers
            else "quote_not_survived_or_sample_unavailable"
        ),
    )
    current = append_stage(
        "stressed_simulated_fills",
        current,
        {
            market_id
            for market_id, metrics in forward.items()
            if int(metrics.get("stressed_fills", 0)) > 0
        },
        lambda _market_id: "stressed_fill_unavailable",
    )
    current = append_stage(
        "resolved_independent_event_clusters",
        current,
        {
            market_id
            for market_id, flags in replay.items()
            if flags.get("resolved", False)
        },
        lambda _market_id: "unresolved_trade",
    )
    current = append_stage(
        "positive_after_cost_trades",
        current,
        {
            market_id
            for market_id, flags in replay.items()
            if flags.get("positive_after_cost", False)
        },
        lambda _market_id: "after_cost_pnl_not_positive",
    )
    append_stage(
        "positive_after_one_cent_shock_trades",
        current,
        {
            market_id
            for market_id, flags in replay.items()
            if flags.get("positive_after_one_cent", False)
        },
        lambda _market_id: "one_cent_shock_pnl_not_positive",
    )
    return stages


def _forward_market_metrics(
    config: DiscoveryConfig,
    *,
    required_bindings: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    path = forward_recorder_db_path(config)
    if not path.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        bindings = connection.execute(
            """
            SELECT binding_sha256, market_id FROM bindings
            ORDER BY created_at, binding_sha256
            """
        ).fetchall()
        for binding in bindings:
            binding_sha = str(binding["binding_sha256"])
            market_id = str(binding["market_id"])
            if required_bindings is not None and (
                not required_bindings.get(market_id)
                or required_bindings[market_id] != binding_sha
            ):
                continue
            target = result.setdefault(
                market_id,
                {
                    "sessions": 0,
                    "terminal_proofs": 0,
                    "fresh_quotes": 0,
                    "survived_quotes": 0,
                    "stressed_fills": 0,
                },
            )
            target["sessions"] += int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM sessions
                    WHERE binding_sha256=?
                    """,
                    (binding_sha,),
                ).fetchone()[0]
            )
            target["terminal_proofs"] += int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT proof_sha256)
                    FROM decision_proofs
                    WHERE binding_sha256=? AND terminal=1
                    """,
                    (binding_sha,),
                ).fetchone()[0]
            )
            target["fresh_quotes"] += int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT anchor_id)
                    FROM quote_anchors
                    WHERE binding_sha256=? AND anchor_executable_usd>0
                    """,
                    (binding_sha,),
                ).fetchone()[0]
            )
            sample = connection.execute(
                """
                SELECT COUNT(DISTINCT CASE
                           WHEN s.status='OK' AND s.sample_lag_ms<=?
                            AND s.quote_survived=1
                           THEN s.anchor_id || ':' || s.horizon_ms END),
                       COUNT(DISTINCT CASE
                           WHEN s.status='OK' AND s.sample_lag_ms<=?
                            AND s.quote_survived_one_cent=1
                            AND s.executable_usd_at_one_cent_shock>0
                           THEN s.anchor_id || ':' || s.horizon_ms END)
                FROM quote_samples s
                JOIN quote_anchors a ON a.anchor_id=s.anchor_id
                WHERE a.binding_sha256=?
                """,
                (
                    config.forward_recorder.max_sample_lag_ms,
                    config.forward_recorder.max_sample_lag_ms,
                    binding_sha,
                ),
            ).fetchone()
            target["survived_quotes"] += int(sample[0] or 0)
            target["stressed_fills"] += int(sample[1] or 0)
    return result


def _replay_profit_flags(
    data_dir: Path,
    *,
    required_partitions: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    root = data_dir / "rule_replays"
    if not root.exists():
        return {}
    runs: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("summary.json")):
        raw = _read_json(path)
        if (
            raw.get("kind") != "rules_first_replay"
            or raw.get("dataset_role") not in {"frozen_oos", "forward"}
        ):
            continue
        run_id = str(raw.get("run_id") or "")
        result_sha = str(raw.get("result_sha256") or "")
        if not run_id or not result_sha:
            continue
        existing = runs.get(run_id)
        if existing is not None and existing.get("result_sha256") != result_sha:
            raise ValueError(f"conflicting replay run {run_id}")
        runs[run_id] = raw
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw in runs.values():
        market_id = str(raw.get("market_id") or "")
        partition = str(raw.get("policy_partition_sha256") or "")
        if not market_id or not partition:
            continue
        grouped.setdefault((market_id, partition), []).append(raw)

    selected: dict[str, list[dict[str, Any]]] = {}
    if required_partitions is not None:
        for market_id, partition in required_partitions.items():
            if partition:
                matching = grouped.get(
                    (market_id, partition),
                    [],
                )
                if matching:
                    selected[market_id] = matching
    else:
        markets = {market_id for market_id, _partition in grouped}
        for market_id in markets:
            options = [
                (partition, items)
                for (candidate, partition), items in grouped.items()
                if candidate == market_id
            ]
            if not options:
                continue
            _partition, items = max(
                options,
                key=lambda option: (
                    max(
                        str(item.get("generated_at") or "")
                        for item in option[1]
                    ),
                    option[0],
                ),
            )
            selected[market_id] = items

    by_market: dict[str, dict[str, Any]] = {}
    for market_id, summaries in selected.items():
        resolved = False
        after_cost_values: list[float] = []
        one_cent_values: list[float] = []
        for raw in summaries:
            settlement = raw.get("settlement", {})
            if not isinstance(settlement, dict):
                continue
            if settlement.get("complete") and settlement.get("traded"):
                resolved = True
                pnl = settlement.get("cost_adjusted_pnl_usd")
                stress = settlement.get("one_cent_stress_pnl_usd")
                if isinstance(pnl, (int, float)) and not isinstance(
                    pnl,
                    bool,
                ):
                    after_cost_values.append(float(pnl))
                if isinstance(stress, (int, float)) and not isinstance(
                    stress,
                    bool,
                ):
                    one_cent_values.append(float(stress))
        by_market[market_id] = {
            "resolved": resolved,
            "positive_after_cost": (
                bool(after_cost_values)
                and sum(after_cost_values) / len(after_cost_values) > 0
            ),
            "positive_after_one_cent": (
                bool(one_cent_values)
                and sum(one_cent_values) / len(one_cent_values) > 0
            ),
        }
    return by_market


def _data_cutoff(
    coverage: dict[str, Any],
    discovery_scan: dict[str, Any],
    records: Iterable[ProfitPriorityRecord],
) -> str:
    candidates = [
        str(coverage.get("scan_ended_at") or ""),
        str(discovery_scan.get("generated_at") or ""),
        *[record.data_cutoff_at for record in records],
    ]
    return max((item for item in candidates if item), default="")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def profit_funnel_command(config_path: Path) -> int:
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    report = build_profit_funnel(config, store)
    output = config.data_dir / "profit_funnel.json"
    from polybot.core.holdings import _atomic_json_write

    _atomic_json_write(output, report)
    print(
        json.dumps(
            {**report, "output_path": str(output)},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


__all__ = [
    "PROFIT_FUNNEL_SCHEMA_VERSION",
    "build_profit_funnel",
    "profit_funnel_command",
]
