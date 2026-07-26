from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from polybot.core.fees import fee_schedules_sha256
from polybot.core.budget import ClassifierBudgetStore
from polybot.core.holdings import _atomic_json_write
from polybot.core.storage import append_jsonl
from polybot.rules.contracts import canonical_json, sha256_json
from polybot.rules.store import RuleStore

from .config import (
    DiscoveryConfig,
    ProfitPriorityConfig,
    classifier_budget_db_path,
    forward_recorder_db_path,
    rule_store_db_path,
)
from .sources import source_plan_sha256
from .store import DiscoveryStore
from .types import MarketContext, SourcePlan

PRIORITY_SNAPSHOT_SCHEMA_VERSION = 1
OBJECTIVE_FAMILIES = {
    "SOURCE_LOCKED_ANNOUNCEMENT",
    "OCCURRENCE_BEFORE_DEADLINE",
    "CATEGORICAL_EXCLUSIVE",
}
PAPER_OBJECTIVE_FAMILIES = {
    *OBJECTIVE_FAMILIES,
    "STATUS_AT_DEADLINE",
    "NUMERIC_THRESHOLD",
    "DURATION_REQUIREMENT",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _finite(value: Any, *, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class PriorityEvidence:
    """Compatible point-in-time observations consumed by the pure scorer."""

    monitored_market_hours: float = 0.0
    terminal_opportunities: int = 0
    human_labels: int = 0
    false_terminal_actions: int = 0
    settlement_source_violations: int = 0
    quote_trials: int = 0
    quote_survivals: int = 0
    stressed_fill_trials: int = 0
    stressed_fills: int = 0
    model_calls: int = 0
    fillable_notionals_usd: tuple[float, ...] = ()
    one_cent_shocked_edges: tuple[float, ...] = ()
    resolved_trade_pnls_usd: tuple[float, ...] = ()
    p95_submission_latency_ms: float | None = None
    quote_half_life_ms: float | None = None
    quote_sampling_horizon_ms: int | None = None
    latency_stage_p50_ms: dict[str, float] = field(default_factory=dict)
    latency_stage_p95_ms: dict[str, float] = field(default_factory=dict)
    forward_binding_sha256: str = ""
    replay_policy_partition_sha256: str = ""
    observation_sha256s: tuple[str, ...] = ()
    integrity_blockers: tuple[str, ...] = ()
    data_cutoff_at: str = ""

    def validate(self) -> None:
        _finite(
            self.monitored_market_hours,
            name="monitored_market_hours",
        )
        if self.monitored_market_hours < 0:
            raise ValueError("monitored_market_hours must be non-negative")
        for name in (
            "terminal_opportunities",
            "human_labels",
            "false_terminal_actions",
            "settlement_source_violations",
            "quote_trials",
            "quote_survivals",
            "stressed_fill_trials",
            "stressed_fills",
            "model_calls",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.quote_survivals > self.quote_trials:
            raise ValueError("quote_survivals cannot exceed quote_trials")
        if self.stressed_fills > self.stressed_fill_trials:
            raise ValueError("stressed_fills cannot exceed stressed_fill_trials")
        for name in (
            "fillable_notionals_usd",
            "one_cent_shocked_edges",
            "resolved_trade_pnls_usd",
        ):
            for value in getattr(self, name):
                _finite(value, name=name)
        for name in ("p95_submission_latency_ms", "quote_half_life_ms"):
            value = getattr(self, name)
            if value is not None and _finite(value, name=name) < 0:
                raise ValueError(f"{name} must be non-negative")
        for name in ("latency_stage_p50_ms", "latency_stage_p95_ms"):
            stages = getattr(self, name)
            if (
                not isinstance(stages, dict)
                or any(
                    not isinstance(stage, str)
                    or not stage
                    or _finite(value, name=f"{name}.{stage}") < 0
                    for stage, value in stages.items()
                )
            ):
                raise ValueError(
                    f"{name} must map stage names to non-negative values"
                )
        if (
            self.quote_sampling_horizon_ms is not None
            and (
                not isinstance(self.quote_sampling_horizon_ms, int)
                or isinstance(self.quote_sampling_horizon_ms, bool)
                or self.quote_sampling_horizon_ms <= 0
            )
        ):
            raise ValueError(
                "quote_sampling_horizon_ms must be a positive integer"
            )
        if self.data_cutoff_at:
            _parse_at(self.data_cutoff_at)
        if len(self.observation_sha256s) != len(
            set(self.observation_sha256s)
        ):
            raise ValueError("observation_sha256s must be unique")
        if (
            not isinstance(self.replay_policy_partition_sha256, str)
        ):
            raise ValueError(
                "replay_policy_partition_sha256 must be text"
            )
        if not isinstance(self.forward_binding_sha256, str):
            raise ValueError("forward_binding_sha256 must be text")
        if any(
            not isinstance(item, str) or not item
            for item in self.integrity_blockers
        ):
            raise ValueError("integrity_blockers must contain non-empty text")


@dataclass(frozen=True)
class ProfitPriorityRecord:
    market_id: str
    event_slug: str
    rule_family: str
    source_adapter_id: str
    policy_partition_sha256: str
    priority_policy_version: str
    computed_at: str
    data_cutoff_at: str
    monitor_priority: float
    execution_priority: float
    confidence: float
    component_values: dict[str, Any]
    component_sample_sizes: dict[str, int]
    blockers: list[str]
    cold_start: bool
    exploration_eligible: bool
    score_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProfitPriorityRecord":
        if not isinstance(raw, dict):
            raise ValueError("profit priority record must be an object")
        unknown = sorted(set(raw) - set(cls.__dataclass_fields__))
        missing = sorted(set(cls.__dataclass_fields__) - set(raw))
        if unknown:
            raise ValueError(
                "profit priority record contains unknown fields: "
                + ", ".join(unknown)
            )
        if missing:
            raise ValueError(
                "profit priority record is missing fields: "
                + ", ".join(missing)
            )
        record = cls(**raw)
        if record.score_sha256 != _record_score_sha256(record):
            raise ValueError("profit priority score hash is not reconstructable")
        for name in (
            "market_id",
            "event_slug",
            "rule_family",
            "policy_partition_sha256",
            "priority_policy_version",
            "computed_at",
            "score_sha256",
        ):
            value = getattr(record, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"profit priority {name} must be non-empty text")
        if not isinstance(record.source_adapter_id, str):
            raise ValueError(
                "profit priority source_adapter_id must be text"
            )
        if not isinstance(record.component_values, dict):
            raise ValueError(
                "profit priority component_values must be an object"
            )
        if (
            not isinstance(record.component_sample_sizes, dict)
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                for value in record.component_sample_sizes.values()
            )
        ):
            raise ValueError(
                "profit priority sample sizes must be non-negative integers"
            )
        if (
            not isinstance(record.blockers, list)
            or any(
                not isinstance(value, str) or not value
                for value in record.blockers
            )
            or record.blockers != sorted(set(record.blockers))
        ):
            raise ValueError(
                "profit priority blockers must be sorted unique text"
            )
        if not isinstance(record.cold_start, bool) or not isinstance(
            record.exploration_eligible,
            bool,
        ):
            raise ValueError(
                "profit priority flags must be boolean"
            )
        for name in (
            "monitor_priority",
            "execution_priority",
            "confidence",
        ):
            _finite(getattr(record, name), name=name)
        if record.execution_priority < 0 or record.monitor_priority < 0:
            raise ValueError("priority values must be non-negative")
        if record.confidence < 0 or record.confidence > 1:
            raise ValueError("priority confidence must be between 0 and 1")
        computed = _parse_at(record.computed_at)
        if record.data_cutoff_at:
            if _parse_at(record.data_cutoff_at) > computed:
                raise ValueError(
                    "profit priority data cutoff is future-dated"
                )
        return record


def _record_score_payload(record: ProfitPriorityRecord) -> dict[str, Any]:
    # Observation/computation timestamps are audit metadata. They must not
    # manufacture a new strategy identity when the underlying evidence is
    # unchanged.
    return {
        "market_id": record.market_id,
        "event_slug": record.event_slug,
        "rule_family": record.rule_family,
        "source_adapter_id": record.source_adapter_id,
        "policy_partition_sha256": record.policy_partition_sha256,
        "priority_policy_version": record.priority_policy_version,
        "monitor_priority": record.monitor_priority,
        "execution_priority": record.execution_priority,
        "confidence": record.confidence,
        "component_values": record.component_values,
        "component_sample_sizes": record.component_sample_sizes,
        "blockers": record.blockers,
        "cold_start": record.cold_start,
        "exploration_eligible": record.exploration_eligible,
    }


def _record_score_sha256(record: ProfitPriorityRecord) -> str:
    return sha256_json(_record_score_payload(record))


def score_profit_priority(
    *,
    context: MarketContext,
    rule_family: str,
    source_adapter_id: str,
    policy_partition_sha256: str,
    evidence: PriorityEvidence,
    policy: ProfitPriorityConfig,
    computed_at: str | None = None,
    execution_supported: bool,
) -> ProfitPriorityRecord:
    """Compute a fail-closed monitoring priority from compatible evidence."""

    evidence.validate()
    family = str(rule_family).strip().upper()
    computed = computed_at or _now()
    _parse_at(computed)
    if evidence.data_cutoff_at and _parse_at(evidence.data_cutoff_at) > _parse_at(
        computed
    ):
        raise ValueError("priority evidence is future-dated")

    terminal_rate_lcb = _poisson_rate_lcb(
        evidence.terminal_opportunities,
        evidence.monitored_market_hours,
        policy.lower_bound_z,
    )
    quote_survival_lcb = _wilson_bound(
        evidence.quote_survivals,
        evidence.quote_trials,
        policy.lower_bound_z,
        upper=False,
    )
    stressed_fill_lcb = _wilson_bound(
        evidence.stressed_fills,
        evidence.stressed_fill_trials,
        policy.lower_bound_z,
        upper=False,
    )
    fillable_p25 = _percentile(
        [max(0.0, item) for item in evidence.fillable_notionals_usd],
        0.25,
    )
    edge_lcb = _mean_lcb(
        list(evidence.one_cent_shocked_edges),
        policy.lower_bound_z,
    )
    false_actions = (
        evidence.false_terminal_actions
        + evidence.settlement_source_violations
    )
    if evidence.human_labels:
        terminal_error_ucb = _wilson_bound(
            false_actions,
            evidence.human_labels,
            policy.lower_bound_z,
            upper=True,
        )
    else:
        terminal_error_ucb = policy.missing_label_error_rate
    terminal_error_reserve = (
        terminal_error_ucb * policy.false_terminal_loss_usd
    )
    gross = (
        terminal_rate_lcb
        * quote_survival_lcb
        * stressed_fill_lcb
        * fillable_p25
        * max(0.0, edge_lcb)
    )
    processing_cost_reserve = (
        policy.processing_cost_reserve_usd
        * max(1, evidence.model_calls)
    )
    priority_value = (
        gross
        - terminal_error_reserve
        - processing_cost_reserve
    )
    sample_floor = min(
        evidence.terminal_opportunities
        / max(1, policy.min_terminal_observations),
        evidence.human_labels / max(1, policy.min_human_labels),
        evidence.quote_trials / max(1, policy.min_quote_samples),
        evidence.stressed_fill_trials
        / max(1, policy.min_stressed_fill_samples),
        len(evidence.resolved_trade_pnls_usd)
        / max(1, policy.min_resolved_trades),
    )
    confidence = round(max(0.0, min(1.0, sample_floor)), 8)
    cold_start = (
        evidence.terminal_opportunities == 0
        and evidence.quote_trials == 0
        and not evidence.resolved_trade_pnls_usd
    )
    exploration_eligible = (
        family in PAPER_OBJECTIVE_FAMILIES
        and context.state not in {"REJECTED", "CLOSED"}
    )
    blockers: list[str] = []
    _minimum_blocker(
        blockers,
        "terminal_observations",
        evidence.terminal_opportunities,
        policy.min_terminal_observations,
    )
    _minimum_blocker(
        blockers,
        "human_labels",
        evidence.human_labels,
        policy.min_human_labels,
    )
    _minimum_blocker(
        blockers,
        "quote_samples",
        evidence.quote_trials,
        policy.min_quote_samples,
    )
    _minimum_blocker(
        blockers,
        "stressed_fill_samples",
        evidence.stressed_fill_trials,
        policy.min_stressed_fill_samples,
    )
    _minimum_blocker(
        blockers,
        "resolved_trades",
        len(evidence.resolved_trade_pnls_usd),
        policy.min_resolved_trades,
    )
    if evidence.p95_submission_latency_ms is None:
        blockers.append("measured_p95_submission_latency_missing")
    elif (
        evidence.p95_submission_latency_ms
        > policy.max_supported_latency_ms
    ):
        blockers.append("measured_p95_latency_exceeds_survival_horizon")
    if false_actions:
        blockers.append("false_terminal_or_source_violation_observed")
    if edge_lcb <= 0:
        blockers.append("one_cent_shocked_edge_not_positive")
    if priority_value <= 0:
        blockers.append("conservative_priority_value_not_positive")
    if not execution_supported or family not in OBJECTIVE_FAMILIES:
        blockers.append("family_not_execution_supported")
    blockers.extend(evidence.integrity_blockers)

    component_values: dict[str, Any] = {
        "terminal_opportunities_per_1000h_lcb": round(
            terminal_rate_lcb,
            10,
        ),
        "quote_survival_lcb": round(quote_survival_lcb, 10),
        "stressed_fill_lcb": round(stressed_fill_lcb, 10),
        "fillable_notional_p25_usd": round(fillable_p25, 8),
        "one_cent_shocked_edge_lcb": round(edge_lcb, 10),
        "terminal_error_rate_ucb": round(terminal_error_ucb, 10),
        "terminal_error_reserve_usd": round(
            terminal_error_reserve,
            8,
        ),
        "processing_cost_reserve_usd": round(
            processing_cost_reserve,
            8,
        ),
        "processing_cost_measured": False,
        "gross_opportunity_value": round(gross, 10),
        "conservative_priority_value": round(priority_value, 10),
        "p95_submission_latency_ms": (
            round(evidence.p95_submission_latency_ms, 3)
            if evidence.p95_submission_latency_ms is not None
            else None
        ),
        "quote_half_life_ms": (
            round(evidence.quote_half_life_ms, 3)
            if evidence.quote_half_life_ms is not None
            else None
        ),
        "quote_sampling_horizon_ms": (
            evidence.quote_sampling_horizon_ms
        ),
        "latency_stage_p50_ms": dict(
            sorted(evidence.latency_stage_p50_ms.items())
        ),
        "latency_stage_p95_ms": dict(
            sorted(evidence.latency_stage_p95_ms.items())
        ),
        "forward_binding_sha256": evidence.forward_binding_sha256,
        "replay_policy_partition_sha256": (
            evidence.replay_policy_partition_sha256
        ),
        "observation_sha256s": sorted(evidence.observation_sha256s),
        "integrity_blockers": sorted(evidence.integrity_blockers),
    }
    component_sample_sizes = {
        "monitored_market_hours": int(
            math.floor(evidence.monitored_market_hours)
        ),
        "terminal_opportunities": evidence.terminal_opportunities,
        "human_labels": evidence.human_labels,
        "quote_trials": evidence.quote_trials,
        "stressed_fill_trials": evidence.stressed_fill_trials,
        "model_calls": evidence.model_calls,
        "fillable_notionals": len(evidence.fillable_notionals_usd),
        "one_cent_shocked_edges": len(
            evidence.one_cent_shocked_edges
        ),
        "resolved_trades": len(evidence.resolved_trade_pnls_usd),
    }
    prior = _family_monitor_prior(family, source_adapter_id)
    monitor_priority = max(
        0.0,
        prior
        + (
            confidence * max(0.0, priority_value)
            if exploration_eligible
            else 0.0
        ),
    )
    execution_priority = (
        max(0.0, priority_value) if not blockers else 0.0
    )
    provisional = ProfitPriorityRecord(
        market_id=context.market_id,
        event_slug=context.event_slug,
        rule_family=family,
        source_adapter_id=source_adapter_id,
        policy_partition_sha256=policy_partition_sha256,
        priority_policy_version=policy.policy_version,
        computed_at=computed,
        data_cutoff_at=evidence.data_cutoff_at,
        monitor_priority=round(monitor_priority, 10),
        execution_priority=round(execution_priority, 10),
        confidence=confidence,
        component_values=component_values,
        component_sample_sizes=component_sample_sizes,
        blockers=sorted(set(blockers)),
        cold_start=cold_start,
        exploration_eligible=exploration_eligible,
        score_sha256="",
    )
    return ProfitPriorityRecord(
        **{
            **provisional.as_dict(),
            "score_sha256": _record_score_sha256(provisional),
        }
    )


def _minimum_blocker(
    blockers: list[str],
    name: str,
    observed: int,
    minimum: int,
) -> None:
    if observed < minimum:
        blockers.append(f"insufficient_{name}:{observed}<{minimum}")


def _family_monitor_prior(family: str, source_adapter_id: str) -> float:
    if not source_adapter_id:
        return 0.0
    return {
        "SOURCE_LOCKED_ANNOUNCEMENT": 1.0,
        "OCCURRENCE_BEFORE_DEADLINE": 0.8,
        "CATEGORICAL_EXCLUSIVE": 0.7,
        "STATUS_AT_DEADLINE": 0.3,
        "NUMERIC_THRESHOLD": 0.2,
        "DURATION_REQUIREMENT": 0.2,
    }.get(family, 0.0)


def _poisson_rate_lcb(count: int, hours: float, z: float) -> float:
    if count <= 0 or hours <= 0:
        return 0.0
    rate = count / hours * 1000.0
    standard_error = math.sqrt(count) / hours * 1000.0
    return max(0.0, rate - z * standard_error)


def _wilson_bound(
    successes: int,
    trials: int,
    z: float,
    *,
    upper: bool,
) -> float:
    if trials <= 0:
        return 0.0
    successes = min(max(0, successes), trials)
    p = successes / trials
    denominator = 1.0 + (z * z / trials)
    center = p + (z * z / (2.0 * trials))
    margin = z * math.sqrt(
        (p * (1.0 - p) / trials) + (z * z / (4.0 * trials * trials))
    )
    value = (center + margin if upper else center - margin) / denominator
    return max(0.0, min(1.0, value))


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _mean_lcb(values: list[float], z: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return min(0.0, values[0])
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (
        len(values) - 1
    )
    return mean - z * math.sqrt(variance / len(values))


def priority_snapshot_path(data_dir: Path) -> Path:
    return data_dir / "profit_priority.json"


def priority_history_path(data_dir: Path) -> Path:
    return data_dir / "profit_priority_history.jsonl"


def load_priority_snapshot(
    data_dir: Path,
    *,
    strict: bool = False,
) -> dict[str, ProfitPriorityRecord]:
    path = priority_snapshot_path(data_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("priority snapshot must be an object")
        allowed = {
            "schema_version",
            "kind",
            "priority_policy_version",
            "priority_policy_sha256",
            "computed_at",
            "data_cutoff_at",
            "records",
            "snapshot_sha256",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(
                "priority snapshot contains unknown fields: "
                + ", ".join(unknown)
            )
        if raw.get("schema_version") != PRIORITY_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("unsupported priority snapshot schema")
        if raw.get("kind") != "rules_first_profit_priority":
            raise ValueError("invalid priority snapshot kind")
        policy_version = str(raw.get("priority_policy_version") or "")
        policy_sha = str(raw.get("priority_policy_sha256") or "")
        if not policy_version or not policy_sha:
            raise ValueError("priority snapshot policy identity is missing")
        _parse_at(str(raw.get("computed_at") or ""))
        if raw.get("data_cutoff_at"):
            if _parse_at(str(raw["data_cutoff_at"])) > _parse_at(
                str(raw["computed_at"])
            ):
                raise ValueError(
                    "priority snapshot data cutoff is future-dated"
                )
        records_raw = raw.get("records")
        if not isinstance(records_raw, list):
            raise ValueError("priority snapshot records must be a list")
        reconstructed = dict(raw)
        supplied_hash = str(reconstructed.pop("snapshot_sha256", ""))
        if supplied_hash != sha256_json(
            _snapshot_hash_payload(reconstructed)
        ):
            raise ValueError("priority snapshot hash is not reconstructable")
        records = [
            ProfitPriorityRecord.from_dict(item)
            for item in records_raw
            if isinstance(item, dict)
        ]
        if len(records) != len(records_raw):
            raise ValueError("priority snapshot contains a non-object record")
        by_market = {record.market_id: record for record in records}
        if len(by_market) != len(records):
            raise ValueError("priority snapshot contains duplicate markets")
        if any(
            record.priority_policy_version != policy_version
            for record in records
        ):
            raise ValueError(
                "priority snapshot mixes policy versions"
            )
        return by_market
    except (OSError, json.JSONDecodeError, ValueError):
        if strict:
            raise
        return {}


def build_priority_snapshot(
    config: DiscoveryConfig,
    store: DiscoveryStore,
    *,
    computed_at: str | None = None,
) -> dict[str, Any]:
    from polybot.rules.forward import _evidence_policy_payload

    computed = computed_at or _now()
    rule_store = RuleStore(rule_store_db_path(config))
    records: list[ProfitPriorityRecord] = []
    cutoffs: list[str] = []
    contexts = sorted(
        store.all_contexts(),
        key=lambda item: item.market_id,
    )
    context_by_market = {
        context.market_id: context for context in contexts
    }
    evidence_policy_sha256 = sha256_json(
        _evidence_policy_payload(config)
    )
    for context in contexts:
        spec = rule_store.load_spec(
            context.market_id,
            context.rule_text_sha256,
        )
        plan = store.load_source_plan(context.market_id)
        if spec is None or plan is None:
            family = (
                spec.semantics.rule_family if spec is not None else "UNKNOWN"
            )
            source_identity = (
                source_plan_sha256(plan) if plan is not None else ""
            )
            partition = sha256_json(
                {
                    "rule_spec_sha256": (
                        spec.spec_sha256 if spec is not None else ""
                    ),
                    "source_plan_sha256": source_identity,
                    "fee_schedules_sha256": fee_schedules_sha256(context),
                    "evidence_policy_sha256": evidence_policy_sha256,
                }
            )
            evidence = PriorityEvidence()
        else:
            family = spec.semantics.rule_family
            source_identity = source_plan_sha256(plan)
            partition = sha256_json(
                {
                    "rule_spec_sha256": spec.spec_sha256,
                    "source_plan_sha256": source_identity,
                    "fee_schedules_sha256": fee_schedules_sha256(context),
                    "priority_policy_version": (
                        config.profit_priority.policy_version
                    ),
                    "evidence_policy_sha256": evidence_policy_sha256,
                }
            )
            evidence = collect_priority_evidence(
                config,
                context,
                plan,
                spec_sha256=spec.spec_sha256,
                source_identity=source_identity,
                computed_at=computed,
            )
        if evidence.data_cutoff_at:
            cutoffs.append(evidence.data_cutoff_at)
        records.append(
            score_profit_priority(
                context=context,
                rule_family=family,
                source_adapter_id=source_identity,
                policy_partition_sha256=partition,
                evidence=evidence,
                policy=config.profit_priority,
                computed_at=computed,
                execution_supported=(
                    family
                    in {
                        item.strip().upper()
                        for item in config.rule_runner.paper_execution_families
                    }
                ),
            )
        )
    policy_payload = asdict(config.profit_priority)
    stable = {
        "schema_version": PRIORITY_SNAPSHOT_SCHEMA_VERSION,
        "kind": "rules_first_profit_priority",
        "priority_policy_version": config.profit_priority.policy_version,
        "priority_policy_sha256": sha256_json(policy_payload),
        "records": [record.as_dict() for record in records],
    }
    snapshot_payload = {
        **stable,
        "computed_at": computed,
        "data_cutoff_at": max(cutoffs) if cutoffs else "",
    }
    snapshot = {
        **snapshot_payload,
        "snapshot_sha256": sha256_json(
            _snapshot_hash_payload(snapshot_payload)
        ),
    }
    previous = load_priority_snapshot(config.data_dir)
    previous_hashes = {
        market_id: record.score_sha256
        for market_id, record in previous.items()
    }
    _atomic_json_write(priority_snapshot_path(config.data_dir), snapshot)
    for record in records:
        if previous_hashes.get(record.market_id) == record.score_sha256:
            continue
        append_jsonl(
            priority_history_path(config.data_dir),
            {
                "changed_at": computed,
                **record.as_dict(),
            },
        )
    if config.classifier_budget.enabled:
        budget_path = classifier_budget_db_path(config)
        limits = replace(
            config.classifier,
            budget_db_path=str(budget_path),
            max_escalations_per_hour=(
                config.classifier_budget.max_escalations_per_hour
            ),
            max_escalations_per_day=(
                config.classifier_budget.max_escalations_per_day
            ),
            max_classifier_errors_per_hour=(
                config.classifier_budget.max_classifier_errors_per_hour
            ),
        )
        budget = ClassifierBudgetStore(
            config.data_dir,
            budget_path,
            priority_quotas=True,
            exploitation_fraction=(
                config.classifier_budget.exploitation_fraction
            ),
            exploration_fraction=(
                config.classifier_budget.exploration_fraction
            ),
            system_fraction=config.classifier_budget.system_fraction,
        )
        exploitation = [
            (record.market_id, record.score_sha256)
            for record in sorted(
                (
                    item
                    for item in records
                    if not item.cold_start
                    and item.exploration_eligible
                ),
                key=lambda item: (-item.monitor_priority, item.market_id),
            )
        ]
        exploration = [
            (record.market_id, record.score_sha256)
            for record in sorted(
                (
                    item
                    for item in records
                    if item.cold_start and item.exploration_eligible
                ),
                key=lambda item: (
                    context_by_market[item.market_id].discovered_at or "",
                    item.market_id,
                ),
            )
        ]
        budget.configure_priority_allocations(
            limits,
            exploitation=exploitation,
            exploration=exploration,
            calls_per_group=config.rule_runner.extraction_passes,
        )
    return snapshot


def _snapshot_hash_payload(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in raw.items()
        if key != "snapshot_sha256"
    }


def collect_priority_evidence(
    config: DiscoveryConfig,
    context: MarketContext,
    plan: SourcePlan,
    *,
    spec_sha256: str,
    source_identity: str,
    computed_at: str,
) -> PriorityEvidence:
    path = forward_recorder_db_path(config)
    if not path.exists():
        return PriorityEvidence()
    from polybot.rules.forward import (
        ForwardRecorderStore,
        _context_binding_payload,
        _evidence_policy_payload,
        _recorder_policy_payload,
        _source_plan_binding_payload,
    )

    ForwardRecorderStore(path)
    expected_context_json = canonical_json(_context_binding_payload(context))
    expected_source_binding_sha256 = sha256_json(
        _source_plan_binding_payload(plan)
    )
    expected_recorder_policy_sha256 = sha256_json(
        _recorder_policy_payload(config.forward_recorder)
    )
    expected_evidence_policy_sha256 = sha256_json(
        _evidence_policy_payload(config)
    )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        binding = connection.execute(
            """
            SELECT binding_sha256, created_at FROM bindings
            WHERE market_id=? AND rule_spec_sha256=?
              AND source_plan_sha256=?
              AND context_json=?
              AND recorder_policy_sha256=?
              AND evidence_policy_sha256=?
            ORDER BY created_at DESC, binding_sha256 DESC LIMIT 1
            """,
            (
                context.market_id,
                spec_sha256,
                expected_source_binding_sha256,
                expected_context_json,
                expected_recorder_policy_sha256,
                expected_evidence_policy_sha256,
            ),
        ).fetchone()
        if binding is None:
            return PriorityEvidence()
        binding_sha = str(binding["binding_sha256"])
        timestamps = [
            str(row["at"])
            for row in connection.execute(
                """
                SELECT started_at AS at FROM sessions WHERE binding_sha256=?
                UNION ALL SELECT COALESCE(ended_at, '') AS at
                    FROM sessions WHERE binding_sha256=?
                UNION ALL SELECT observed_at AS at FROM decision_proofs
                    WHERE binding_sha256=?
                UNION ALL SELECT sampled_at AS at FROM quote_samples s
                    JOIN quote_anchors a ON a.anchor_id=s.anchor_id
                    WHERE a.binding_sha256=?
                """,
                (binding_sha, binding_sha, binding_sha, binding_sha),
            ).fetchall()
            if str(row["at"])
        ]
        cutoff = max(timestamps) if timestamps else str(binding["created_at"])
        if _parse_at(cutoff) > _parse_at(computed_at):
            raise ValueError(
                f"future-dated forward observation for {context.market_id}"
            )
        sessions = connection.execute(
            """
            SELECT started_at, ended_at FROM sessions
            WHERE binding_sha256=?
            """,
            (binding_sha,),
        ).fetchall()
        monitored_seconds = 0.0
        cutoff_dt = _parse_at(cutoff)
        for session in sessions:
            start = _parse_at(str(session["started_at"]))
            end_raw = str(session["ended_at"] or "")
            end = _parse_at(end_raw) if end_raw else cutoff_dt
            monitored_seconds += max(0.0, (min(end, cutoff_dt) - start).total_seconds())
        proofs = connection.execute(
            """
            SELECT DISTINCT proof_sha256, evaluation_sha256, terminal,
                            observed_at
            FROM decision_proofs WHERE binding_sha256=?
            """,
            (binding_sha,),
        ).fetchall()
        terminal = sum(1 for row in proofs if int(row["terminal"]) == 1)
        extraction_rows = connection.execute(
            """
            SELECT extraction_sha256, duration_ms, completed_at, result_json
            FROM extractions WHERE binding_sha256=?
              AND status NOT IN ('CACHED', 'STALE')
            """,
            (binding_sha,),
        ).fetchall()
        sample_rows = connection.execute(
            """
            SELECT a.anchor_id, s.horizon_ms, s.status, s.sample_lag_ms,
                   s.quote_survived, s.quote_survived_one_cent,
                   s.executable_usd_at_one_cent_shock
            FROM quote_samples s
            JOIN quote_anchors a ON a.anchor_id=s.anchor_id
            WHERE a.binding_sha256=?
            """,
            (binding_sha,),
        ).fetchall()

    from polybot.rules.forward import ForwardRecorderStore

    completeness = ForwardRecorderStore(path).completeness(
        binding_sha256=binding_sha,
        token_ids=[
            token_id
            for outcome in context.outcomes
            for token_id in (outcome.yes_token_id, outcome.no_token_id)
        ],
        horizons_ms=config.forward_recorder.quote_survival_horizons_ms,
        max_sample_lag_ms=config.forward_recorder.max_sample_lag_ms,
    )
    latency_report = completeness.get("latency_ms", {})
    latency_stage_p50 = {
        str(name): float(summary["p50"])
        for name, summary in latency_report.items()
        if isinstance(summary, dict)
        and isinstance(summary.get("p50"), (int, float))
        and not isinstance(summary.get("p50"), bool)
        and float(summary["p50"]) >= 0
    }
    latency_stage_p95 = {
        str(name): float(summary["p95"])
        for name, summary in latency_report.items()
        if isinstance(summary, dict)
        and isinstance(summary.get("p95"), (int, float))
        and not isinstance(summary.get("p95"), bool)
        and float(summary["p95"]) >= 0
    }
    direct_p95 = latency_stage_p95.get("publisher_to_submission")
    latency_parts = [
        latency_report.get(name, {}).get("p95")
        for name in (
            "publisher_to_fetch",
            "fetch_to_first_observation",
            "two_pass_extraction",
            "latest_evidence_to_decision",
        )
    ]
    if direct_p95 is not None:
        p95_latency = direct_p95
    elif all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in latency_parts
    ):
        p95_latency = sum(max(0.0, float(value)) for value in latency_parts)
    else:
        p95_latency = None
    horizon = _sampling_horizon(config, p95_latency)
    valid_samples = [
        row
        for row in sample_rows
        if int(row["horizon_ms"]) == horizon
        and str(row["status"]) == "OK"
        and float(row["sample_lag_ms"])
        <= config.forward_recorder.max_sample_lag_ms
        and row["quote_survived"] is not None
    ]
    survivals = sum(
        int(row["quote_survived"]) for row in valid_samples
    )
    stressed_known = [
        row
        for row in valid_samples
        if row["quote_survived_one_cent"] is not None
    ]
    stressed_fills = sum(
        int(row["quote_survived_one_cent"])
        for row in stressed_known
    )
    fillable = tuple(
        max(0.0, float(row["executable_usd_at_one_cent_shock"]))
        for row in stressed_known
        if int(row["quote_survived_one_cent"]) == 1
    )
    observation_hashes = {
        binding_sha,
        *[str(row["proof_sha256"]) for row in proofs],
        *[str(row["extraction_sha256"]) for row in extraction_rows],
        *[
            sha256_json(
                {
                    "anchor_id": str(row["anchor_id"]),
                    "horizon_ms": int(row["horizon_ms"]),
                    "status": str(row["status"]),
                    "quote_survived": row["quote_survived"],
                    "quote_survived_one_cent": row[
                        "quote_survived_one_cent"
                    ],
                    "executable_usd_at_one_cent_shock": float(
                        row["executable_usd_at_one_cent_shock"]
                    ),
                }
            )
            for row in valid_samples
        ],
    }
    integrity_blockers = tuple(
        sorted(
            f"time_integrity:{name}"
            for name, value in completeness.get(
                "time_integrity",
                {},
            ).items()
            if isinstance(value, int) and value > 0
        )
    )
    (
        labels,
        false_actions,
        source_violations,
        edges,
        pnls,
        replay_hashes,
        replay_partition,
    ) = (
        _replay_economics(
            config.data_dir,
            context.market_id,
            rule_spec_sha256=spec_sha256,
            source_plan_sha256=source_identity,
            fee_schedules_sha256_value=fee_schedules_sha256(context),
        )
    )
    observation_hashes.update(replay_hashes)
    half_life = _quote_half_life(
        path,
        binding_sha,
        config.forward_recorder.max_sample_lag_ms,
    )
    return PriorityEvidence(
        monitored_market_hours=monitored_seconds / 3600.0,
        terminal_opportunities=terminal,
        human_labels=labels,
        false_terminal_actions=false_actions,
        settlement_source_violations=source_violations,
        quote_trials=len(valid_samples),
        quote_survivals=survivals,
        stressed_fill_trials=len(stressed_known),
        stressed_fills=stressed_fills,
        model_calls=sum(
            max(
                0,
                int(
                    (
                        json.loads(str(row["result_json"]))
                        if row["result_json"]
                        else {}
                    ).get("calls_reserved")
                    or 0
                ),
            )
            for row in extraction_rows
        ),
        fillable_notionals_usd=fillable,
        one_cent_shocked_edges=tuple(edges),
        resolved_trade_pnls_usd=tuple(pnls),
        p95_submission_latency_ms=p95_latency,
        quote_half_life_ms=half_life,
        quote_sampling_horizon_ms=horizon,
        latency_stage_p50_ms=latency_stage_p50,
        latency_stage_p95_ms=latency_stage_p95,
        forward_binding_sha256=binding_sha,
        replay_policy_partition_sha256=replay_partition,
        observation_sha256s=tuple(sorted(observation_hashes)),
        integrity_blockers=integrity_blockers,
        data_cutoff_at=cutoff,
    )


def _sampling_horizon(
    config: DiscoveryConfig,
    p95_latency_ms: float | None,
) -> int:
    horizons = config.forward_recorder.quote_survival_horizons_ms
    if p95_latency_ms is None:
        return max(horizons)
    for horizon in horizons:
        if horizon >= p95_latency_ms:
            return horizon
    return max(horizons)


def _quote_half_life(
    db_path: Path,
    binding_sha: str,
    max_lag_ms: int,
) -> float | None:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT s.horizon_ms, s.quote_survived
            FROM quote_samples s
            JOIN quote_anchors a ON a.anchor_id=s.anchor_id
            WHERE a.binding_sha256=? AND s.status='OK'
              AND s.sample_lag_ms<=?
              AND s.quote_survived IS NOT NULL
            """,
            (binding_sha, max_lag_ms),
        ).fetchall()
    for horizon in sorted({int(row["horizon_ms"]) for row in rows}):
        scoped = [
            int(row["quote_survived"])
            for row in rows
            if int(row["horizon_ms"]) == horizon
        ]
        if scoped and sum(scoped) / len(scoped) < 0.5:
            return float(horizon)
    return None


def _replay_economics(
    data_dir: Path,
    market_id: str,
    *,
    rule_spec_sha256: str,
    source_plan_sha256: str,
    fee_schedules_sha256_value: str,
) -> tuple[
    int,
    int,
    int,
    list[float],
    list[float],
    set[str],
    str,
]:
    root = data_dir / "rule_replays"
    if not root.exists():
        return 0, 0, 0, [], [], set(), ""
    summaries: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("summary.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        replay_policy = (
            raw.get("replay_policy", {})
            if isinstance(raw, dict)
            else {}
        )
        fee_policy = (
            replay_policy.get("fee_policy", {})
            if isinstance(replay_policy, dict)
            else {}
        )
        replay_fee_schedules_sha256 = (
            str(fee_policy.get("fee_schedules_sha256") or "")
            if isinstance(fee_policy, dict)
            else ""
        )
        if (
            not isinstance(raw, dict)
            or raw.get("kind") != "rules_first_replay"
            or str(raw.get("market_id")) != market_id
            or raw.get("dataset_role") not in {"frozen_oos", "forward"}
            or str(raw.get("rule_spec_sha256") or "")
            != rule_spec_sha256
            or str(raw.get("source_plan_sha256") or "")
            != source_plan_sha256
            or replay_fee_schedules_sha256 != fee_schedules_sha256_value
        ):
            continue
        run_id = str(raw.get("run_id") or "")
        result_hash = str(raw.get("result_sha256") or "")
        if not run_id or not result_hash:
            continue
        existing = summaries.get(run_id)
        if existing is not None and existing.get("result_sha256") != result_hash:
            raise ValueError(f"conflicting replay summary {run_id}")
        summaries[run_id] = raw
    if not summaries:
        return 0, 0, 0, [], [], set(), ""
    by_partition: dict[str, list[dict[str, Any]]] = {}
    for summary in summaries.values():
        by_partition.setdefault(
            str(summary.get("policy_partition_sha256") or ""),
            [],
        ).append(summary)
    chosen_partition, chosen = max(
        by_partition.items(),
        key=lambda partition_and_items: (
            max(
                str(item.get("generated_at") or "")
                for item in partition_and_items[1]
            ),
            partition_and_items[0],
        ),
    )
    label_records: dict[str, dict[str, Any]] = {}
    false_actions = 0
    source_violations = 0
    edges: list[float] = []
    pnl_by_event: dict[str, list[float]] = {}
    hashes: set[str] = set()
    for summary in chosen:
        hashes.add(str(summary["result_sha256"]))
        false_actions += int(
            summary.get("safety", {}).get("false_terminal_actions", 0)
        )
        source_violations += int(
            summary.get("safety", {}).get(
                "settlement_source_violations",
                0,
            )
        )
        for record in summary.get("human_labels", {}).get("records", []):
            if not isinstance(record, dict):
                continue
            evaluation = str(record.get("evaluation_sha256") or "")
            if not evaluation:
                continue
            existing = label_records.get(evaluation)
            if existing is not None and canonical_json(existing) != canonical_json(
                record
            ):
                raise ValueError(
                    f"conflicting human labels for {evaluation}"
                )
            label_records[evaluation] = record
        settlement = summary.get("settlement", {})
        if not isinstance(settlement, dict):
            continue
        pnl = settlement.get("one_cent_stress_pnl_usd")
        trades = [
            item
            for item in summary.get("trades", [])
            if isinstance(item, dict)
        ]
        total_cost = sum(
            float(item.get("total_cost_usd") or 0.0) for item in trades
        )
        if (
            settlement.get("complete")
            and settlement.get("traded")
            and isinstance(pnl, (int, float))
            and not isinstance(pnl, bool)
        ):
            event = str(summary.get("event_slug") or market_id)
            pnl_by_event.setdefault(event, []).append(float(pnl))
            if total_cost > 0:
                edges.append(float(pnl) / total_cost)
    pnls = [
        sum(values) / len(values)
        for _event, values in sorted(pnl_by_event.items())
        if values
    ]
    return (
        len(label_records),
        false_actions,
        source_violations,
        edges,
        pnls,
        hashes,
        chosen_partition,
    )


def priority_sort_key(
    context: MarketContext,
    priorities: dict[str, ProfitPriorityRecord],
) -> tuple[Any, ...]:
    record = priorities.get(context.market_id)
    if record is None:
        return (0, 0.0, context.discovered_at or "", context.market_id)
    return (
        -1,
        -record.monitor_priority,
        context.discovered_at or "",
        context.market_id,
    )


def priority_report_command(config_path: Path) -> int:
    from .config import load_discovery_config

    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    snapshot = build_priority_snapshot(config, store)
    records = [
        ProfitPriorityRecord.from_dict(item)
        for item in snapshot["records"]
    ]
    report = {
        **snapshot,
        "summary": {
            "markets": len(records),
            "cold_start": sum(1 for item in records if item.cold_start),
            "exploration_eligible": sum(
                1 for item in records if item.exploration_eligible
            ),
            "positive_execution_priority": sum(
                1 for item in records if item.execution_priority > 0
            ),
            "quote_half_life_ms_by_family": _half_life_by_family(records),
            "top_blockers": _blocker_counts(records),
        },
        "output_path": str(priority_snapshot_path(config.data_dir)),
        "paper_only": True,
        "execution_eligibility_changed": False,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def economics_report_command(config_path: Path) -> int:
    """Answer where the confirmation edge is currently being lost."""

    from .config import load_discovery_config

    config = load_discovery_config(config_path)
    snapshot = build_priority_snapshot(
        config,
        DiscoveryStore(config.data_dir),
    )
    records = [
        ProfitPriorityRecord.from_dict(item)
        for item in snapshot["records"]
    ]
    markets = [
        {
            "market_id": record.market_id,
            "event_slug": record.event_slug,
            "rule_family": record.rule_family,
            "terminal_observations": record.component_sample_sizes.get(
                "terminal_opportunities",
                0,
            ),
            "terminal_rate_lcb_per_1000_market_hours": (
                record.component_values.get(
                    "terminal_opportunities_per_1000h_lcb"
                )
            ),
            "publisher_to_submission_p50_ms": (
                record.component_values.get(
                    "latency_stage_p50_ms",
                    {},
                ).get("publisher_to_submission")
            ),
            "publisher_to_submission_p95_ms": (
                record.component_values.get(
                    "latency_stage_p95_ms",
                    {},
                ).get("publisher_to_submission")
            ),
            "decision_to_submission_p95_ms": (
                record.component_values.get(
                    "latency_stage_p95_ms",
                    {},
                ).get("decision_to_submission")
            ),
            "quote_half_life_ms": record.component_values.get(
                "quote_half_life_ms"
            ),
            "stressed_opportunities": record.component_sample_sizes.get(
                "one_cent_shocked_edges",
                0,
            ),
            "conservative_net_value_usd_per_1000_market_hours": (
                record.component_values.get(
                    "conservative_priority_value"
                )
            ),
            "primary_bottleneck": _primary_bottleneck(record),
            "blockers": record.blockers,
        }
        for record in records
    ]
    grouped: dict[str, list[ProfitPriorityRecord]] = {}
    for record in records:
        grouped.setdefault(record.rule_family, []).append(record)
    families = {
        family: {
            "markets": len(items),
            "terminal_observations": sum(
                item.component_sample_sizes.get(
                    "terminal_opportunities",
                    0,
                )
                for item in items
            ),
            "publisher_to_submission_p50_ms_market_median": (
                _component_median(
                    items,
                    "latency_stage_p50_ms",
                    "publisher_to_submission",
                )
            ),
            "publisher_to_submission_p95_ms_market_median": (
                _component_median(
                    items,
                    "latency_stage_p95_ms",
                    "publisher_to_submission",
                )
            ),
            "quote_half_life_ms_market_median": _component_median(
                items,
                "quote_half_life_ms",
            ),
            "stressed_opportunities": sum(
                item.component_sample_sizes.get(
                    "one_cent_shocked_edges",
                    0,
                )
                for item in items
            ),
            "positive_conservative_markets": sum(
                1
                for item in items
                if float(
                    item.component_values.get(
                        "conservative_priority_value"
                    )
                    or 0.0
                )
                > 0
            ),
            "primary_bottlenecks": _count_values(
                _primary_bottleneck(item) for item in items
            ),
        }
        for family, items in sorted(grouped.items())
    }
    report = {
        "schema_version": 1,
        "kind": "rules_first_operator_economics",
        "paper_only": True,
        "execution_eligibility_changed": False,
        "priority_snapshot_sha256": snapshot["snapshot_sha256"],
        "families": families,
        "markets": markets,
        "generated_at": _now(),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _primary_bottleneck(record: ProfitPriorityRecord) -> str:
    sizes = record.component_sample_sizes
    values = record.component_values
    if sizes.get("terminal_opportunities", 0) == 0:
        return "terminal_evidence_collection"
    p95 = values.get("latency_stage_p95_ms", {})
    if not isinstance(p95, dict) or not p95:
        return "latency_instrumentation"
    discovery = float(p95.get("publisher_to_discovery") or 0.0)
    model = float(p95.get("model_extraction") or 0.0)
    deterministic = float(
        p95.get("deterministic_extraction") or 0.0
    )
    if discovery >= max(300_000.0, model, deterministic):
        return "news_discovery"
    if max(model, deterministic) > max(1_000.0, discovery):
        return "evidence_parsing"
    if sizes.get("quote_trials", 0) == 0:
        return "quote_measurement"
    half_life = values.get("quote_half_life_ms")
    submission = p95.get("decision_to_submission")
    if (
        isinstance(half_life, (int, float))
        and isinstance(submission, (int, float))
        and float(submission) > float(half_life)
    ):
        return "liquidity_half_life"
    quote_survival = float(values.get("quote_survival_lcb") or 0.0)
    stressed_fill = float(values.get("stressed_fill_lcb") or 0.0)
    if quote_survival <= 0:
        return "liquidity_survival"
    if stressed_fill < quote_survival * 0.5:
        return "execution_fillability"
    if float(values.get("one_cent_shocked_edge_lcb") or 0.0) <= 0:
        return "after_cost_economics"
    if record.blockers:
        return "sample_size_or_safety"
    return "none_observed"


def _component_median(
    records: Iterable[ProfitPriorityRecord],
    field_name: str,
    nested_name: str | None = None,
) -> float | None:
    values: list[float] = []
    for record in records:
        value: Any = record.component_values.get(field_name)
        if nested_name is not None:
            value = (
                value.get(nested_name)
                if isinstance(value, dict)
                else None
            )
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    return _percentile(values, 0.5) if values else None


def _count_values(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(
        sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )


def _half_life_by_family(
    records: Iterable[ProfitPriorityRecord],
) -> dict[str, float | None]:
    grouped: dict[str, list[float]] = {}
    families: set[str] = set()
    for record in records:
        families.add(record.rule_family)
        value = record.component_values.get("quote_half_life_ms")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            grouped.setdefault(record.rule_family, []).append(float(value))
    return {
        family: (
            _percentile(grouped.get(family, []), 0.5)
            if grouped.get(family)
            else None
        )
        for family in sorted(families)
    }


def _blocker_counts(
    records: Iterable[ProfitPriorityRecord],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        for blocker in record.blockers:
            key = blocker.split(":", 1)[0]
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


__all__ = [
    "OBJECTIVE_FAMILIES",
    "PRIORITY_SNAPSHOT_SCHEMA_VERSION",
    "PriorityEvidence",
    "ProfitPriorityRecord",
    "build_priority_snapshot",
    "collect_priority_evidence",
    "load_priority_snapshot",
    "economics_report_command",
    "priority_history_path",
    "priority_report_command",
    "priority_snapshot_path",
    "priority_sort_key",
    "score_profit_priority",
]
