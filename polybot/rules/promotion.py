from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from polybot.core.holdings import _atomic_json_write
from polybot.discovery.config import load_discovery_config

from .contracts import sha256_json
from .replay import RULE_REPLAY_SCHEMA_VERSION

PROMOTION_REPORT_SCHEMA_VERSION = 2
PAPER_EXECUTION_FAMILIES = {
    "OCCURRENCE_BEFORE_DEADLINE",
    "CATEGORICAL_EXCLUSIVE",
    "SOURCE_LOCKED_ANNOUNCEMENT",
}


@dataclass(frozen=True)
class PromotionPolicy:
    """Minimum evidence required before a family may be considered for canary.

    Passing is advisory: the report never edits configuration or enables live
    execution. Samples are clustered by event slug so several rungs or replay
    variants of one geopolitical event cannot manufacture independence.
    """

    min_replay_runs: int = 30
    min_independent_events: int = 30
    min_evidence_articles: int = 40
    min_extraction_agreement_rate: float = 0.98
    max_invalid_extraction_rate: float = 0.02
    min_proof_completeness_rate: float = 1.0
    min_terminal_opportunities: int = 20
    min_quote_availability_rate: float = 0.25
    min_positive_edge_opportunities: int = 5
    min_resolved_events: int = 30
    min_markout_5m_samples: int = 10
    min_markout_5m_events: int = 20
    min_human_labeled_terminal_decisions: int = 100
    max_human_label_disagreements: int = 0
    max_human_label_conflicts: int = 0
    max_false_terminal_actions: int = 0
    max_settlement_source_violations: int = 0
    max_deadline_no_inference_actions: int = 0
    max_fee_schedule_violations: int = 0
    min_mean_5m_net_clv: float = 0.0
    min_total_cost_adjusted_pnl_usd: float = 0.0
    min_one_cent_stress_pnl_usd: float = 0.0
    min_episode_ev_lcb_usd: float = 0.0
    max_duplicate_entries: int = 0
    bootstrap_samples: int = 4000
    bootstrap_confidence: float = 0.95
    bootstrap_seed: int = 207
    allowed_dataset_roles: tuple[str, ...] = ("frozen_oos", "forward")

    def validate(self) -> None:
        integers = {
            "min_replay_runs": self.min_replay_runs,
            "min_independent_events": self.min_independent_events,
            "min_evidence_articles": self.min_evidence_articles,
            "min_terminal_opportunities": self.min_terminal_opportunities,
            "min_positive_edge_opportunities": self.min_positive_edge_opportunities,
            "min_resolved_events": self.min_resolved_events,
            "min_markout_5m_samples": self.min_markout_5m_samples,
            "min_markout_5m_events": self.min_markout_5m_events,
            "min_human_labeled_terminal_decisions": (
                self.min_human_labeled_terminal_decisions
            ),
            "max_human_label_disagreements": (
                self.max_human_label_disagreements
            ),
            "max_human_label_conflicts": self.max_human_label_conflicts,
            "max_false_terminal_actions": self.max_false_terminal_actions,
            "max_settlement_source_violations": (
                self.max_settlement_source_violations
            ),
            "max_deadline_no_inference_actions": (
                self.max_deadline_no_inference_actions
            ),
            "max_fee_schedule_violations": (
                self.max_fee_schedule_violations
            ),
            "max_duplicate_entries": self.max_duplicate_entries,
            "bootstrap_samples": self.bootstrap_samples,
        }
        for name, value in integers.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.bootstrap_samples <= 0:
            raise ValueError("bootstrap_samples must be positive")
        probabilities = {
            "min_extraction_agreement_rate": self.min_extraction_agreement_rate,
            "max_invalid_extraction_rate": self.max_invalid_extraction_rate,
            "min_proof_completeness_rate": self.min_proof_completeness_rate,
            "min_quote_availability_rate": self.min_quote_availability_rate,
            "bootstrap_confidence": self.bootstrap_confidence,
        }
        for name, value in probabilities.items():
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < 0
                or float(value) > 1
            ):
                raise ValueError(f"{name} must be between 0 and 1")
        if self.bootstrap_confidence <= 0 or self.bootstrap_confidence >= 1:
            raise ValueError("bootstrap_confidence must be strictly between 0 and 1")
        for name in (
            "min_mean_5m_net_clv",
            "min_total_cost_adjusted_pnl_usd",
            "min_one_cent_stress_pnl_usd",
            "min_episode_ev_lcb_usd",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"{name} must be finite")
        allowed_roles = {"frozen_oos", "forward"}
        if (
            not isinstance(self.allowed_dataset_roles, tuple)
            or not self.allowed_dataset_roles
            or any(
                not isinstance(item, str) or item not in allowed_roles
                for item in self.allowed_dataset_roles
            )
        ):
            raise ValueError(
                "allowed_dataset_roles must contain frozen_oos and/or forward"
            )


def load_replay_summaries(path: Path) -> list[dict[str, Any]]:
    root = Path(path)
    candidates = [root] if root.is_file() else sorted(root.rglob("*.json"))
    summaries: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict) or raw.get("kind") != "rules_first_replay":
            continue
        _validate_replay_summary(raw, candidate)
        run_id = str(raw["run_id"])
        existing = summaries.get(run_id)
        if existing is not None and existing["result_sha256"] != raw["result_sha256"]:
            raise ValueError(f"conflicting replay summaries for run_id {run_id}")
        summaries[run_id] = raw
    if not summaries:
        raise ValueError(f"no rules-first replay summaries found under {root}")
    return [summaries[key] for key in sorted(summaries)]


def build_promotion_report(
    summaries: list[dict[str, Any]],
    *,
    policy: PromotionPolicy | None = None,
) -> dict[str, Any]:
    policy = policy or PromotionPolicy()
    policy.validate()
    if not summaries:
        raise ValueError("promotion report requires at least one replay summary")
    for index, summary in enumerate(summaries):
        _validate_replay_summary(summary, Path(f"<summary:{index}>"))
    by_run: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        run_id = str(summary["run_id"])
        existing = by_run.get(run_id)
        if (
            existing is not None
            and existing["result_sha256"] != summary["result_sha256"]
        ):
            raise ValueError(f"conflicting replay summaries for run_id {run_id}")
        by_run[run_id] = summary
    summaries = [by_run[key] for key in sorted(by_run)]

    families: dict[str, Any] = {}
    for family in sorted({str(item["rule_family"]) for item in summaries}):
        scoped = [item for item in summaries if item["rule_family"] == family]
        families[family] = _family_report(family, scoped, policy)
    eligible = sorted(
        family
        for family, report in families.items()
        if report["status"] == "PASS"
    )
    generated_at = max(str(item["generated_at"]) for item in summaries)
    result: dict[str, Any] = {
        "schema_version": PROMOTION_REPORT_SCHEMA_VERSION,
        "kind": "rules_first_promotion_report",
        "policy": asdict(policy),
        "source_runs": sorted(str(item["run_id"]) for item in summaries),
        "families": families,
        "eligible_for_manual_canary_review": eligible,
        "configuration_changed": False,
        "live_enabled": False,
        "generated_at": generated_at,
    }
    result["result_sha256"] = sha256_json(result)
    return result


def promotion_report_command(
    config_path: Path,
    runs_path: Path,
    *,
    out: Path | None = None,
) -> int:
    config = load_discovery_config(config_path)
    summaries = load_replay_summaries(runs_path)
    report = build_promotion_report(summaries)
    output = out or config.data_dir / "promotion" / "rules_first.json"
    _atomic_json_write(output, report)
    print(json.dumps({**report, "output_path": str(output)}, indent=2, sort_keys=True))
    return 0


def _family_report(
    family: str,
    summaries: list[dict[str, Any]],
    policy: PromotionPolicy,
) -> dict[str, Any]:
    partition_ids = sorted(
        {str(item["policy_partition_sha256"]) for item in summaries}
    )
    if len(partition_ids) != 1:
        return {
            "status": "FAIL",
            "metrics": {
                "replay_runs": len(summaries),
                "policy_partitions": len(partition_ids),
            },
            "gates": [
                _gate(
                    "single_policy_partition",
                    False,
                    len(partition_ids),
                    1,
                )
            ],
            "run_ids": sorted(str(item["run_id"]) for item in summaries),
            "event_clusters": [],
            "policy_partition_sha256s": partition_ids,
        }
    dataset_roles = sorted(
        {str(item["dataset_role"]) for item in summaries}
    )
    extraction_attempts = sum(
        _integer(item["extraction"].get("attempted_articles"))
        for item in summaries
    )
    agreed = sum(_integer(item["extraction"].get("agreed")) for item in summaries)
    disagreements = sum(
        _integer(item["extraction"].get("disagreements"))
        for item in summaries
    )
    invalid = sum(_integer(item["extraction"].get("invalid")) for item in summaries)
    proof_total = sum(_integer(item["proofs"].get("total")) for item in summaries)
    proof_complete = sum(
        _integer(item["proofs"].get("complete")) for item in summaries
    )
    terminal = sum(
        _integer(item["decisions"].get("terminal_opportunities"))
        for item in summaries
    )
    quote_available = sum(
        _integer(item["decisions"].get("quote_available"))
        for item in summaries
    )
    positive_edge = sum(
        _integer(item["decisions"].get("positive_net_edge"))
        for item in summaries
    )
    duplicate_entries = sum(
        _integer(item["decisions"].get("duplicate_entries"))
        for item in summaries
    )
    human_label_metrics = _unique_human_label_metrics(summaries)
    human_terminal = human_label_metrics["terminal_decisions"]
    human_disagreements = human_label_metrics["disagreements"]
    human_conflicts = human_label_metrics["conflicts"]
    false_terminal_actions = sum(
        _integer(item["safety"].get("false_terminal_actions"))
        for item in summaries
    )
    settlement_source_violations = sum(
        _integer(item["safety"].get("settlement_source_violations"))
        for item in summaries
    )
    deadline_no_inference_actions = sum(
        _integer(item["safety"].get("deadline_no_inference_actions"))
        for item in summaries
    )
    fee_schedule_violations = sum(
        _integer(item["safety"].get("fee_schedule_violations"))
        for item in summaries
    )
    event_groups: dict[str, list[dict[str, Any]]] = {}
    for item in summaries:
        key = str(item.get("event_slug") or item["market_id"])
        event_groups.setdefault(key, []).append(item)

    resolved_event_pnl: list[float] = []
    one_cent_stress_event_pnl: list[float] = []
    two_cent_stress_event_pnl: list[float] = []
    for group in event_groups.values():
        values = [
            _number(item["settlement"].get("cost_adjusted_pnl_usd"))
            for item in group
            if item["settlement"].get("complete")
            and item["settlement"].get("traded")
            and item["settlement"].get("cost_adjusted_pnl_usd") is not None
        ]
        clean = [value for value in values if value is not None]
        if clean:
            # Multiple timelines or market rungs for one event remain one
            # clustered observation and therefore receive one bootstrap vote.
            resolved_event_pnl.append(sum(clean) / len(clean))
        for field_name, target in (
            ("one_cent_stress_pnl_usd", one_cent_stress_event_pnl),
            ("two_cent_stress_pnl_usd", two_cent_stress_event_pnl),
        ):
            stress_values = [
                _number(item["settlement"].get(field_name))
                for item in group
                if item["settlement"].get("complete")
                and item["settlement"].get("traded")
                and item["settlement"].get(field_name) is not None
            ]
            stress_clean = [
                value for value in stress_values if value is not None
            ]
            if stress_clean:
                target.append(sum(stress_clean) / len(stress_clean))

    five_min_by_event: list[float] = []
    five_min_samples = 0
    for group in event_groups.values():
        values: list[float] = []
        for item in group:
            for trade in item.get("trades", []):
                if not isinstance(trade, dict):
                    continue
                markout = trade.get("markouts", {}).get("300", {})
                value = _number(markout.get("net_clv")) if isinstance(markout, dict) else None
                if value is not None:
                    values.append(value)
        five_min_samples += len(values)
        if values:
            five_min_by_event.append(sum(values) / len(values))

    agreement_rate = _ratio(agreed, agreed + disagreements)
    invalid_rate = _ratio(invalid, extraction_attempts)
    proof_rate = _ratio(proof_complete, proof_total)
    quote_rate = _ratio(quote_available, terminal)
    total_pnl = sum(resolved_event_pnl)
    one_cent_stress_pnl = sum(one_cent_stress_event_pnl)
    two_cent_stress_pnl = sum(two_cent_stress_event_pnl)
    mean_pnl = (
        total_pnl / len(resolved_event_pnl)
        if resolved_event_pnl
        else None
    )
    ev_lcb = _bootstrap_mean_lcb(
        resolved_event_pnl,
        samples=policy.bootstrap_samples,
        confidence=policy.bootstrap_confidence,
        seed=policy.bootstrap_seed,
    )
    mean_five_min_clv = (
        sum(five_min_by_event) / len(five_min_by_event)
        if five_min_by_event
        else None
    )
    metrics = {
        "replay_runs": len(summaries),
        "policy_partitions": len(partition_ids),
        "dataset_roles": dataset_roles,
        "unique_markets": len({str(item["market_id"]) for item in summaries}),
        "independent_events": len(event_groups),
        "evidence_articles": extraction_attempts,
        "extraction_agreement_rate": agreement_rate,
        "invalid_extraction_rate": invalid_rate,
        "proofs": proof_total,
        "proof_completeness_rate": proof_rate,
        "terminal_opportunities": terminal,
        "quote_availability_rate": quote_rate,
        "positive_net_edge_opportunities": positive_edge,
        "duplicate_entries": duplicate_entries,
        "human_labeled_terminal_decisions": human_terminal,
        "human_label_disagreements": human_disagreements,
        "human_label_conflicts": human_conflicts,
        "duplicate_human_label_records": human_label_metrics["duplicates"],
        "false_terminal_actions": false_terminal_actions,
        "settlement_source_violations": settlement_source_violations,
        "deadline_no_inference_actions": deadline_no_inference_actions,
        "fee_schedule_violations": fee_schedule_violations,
        "resolved_events": len(resolved_event_pnl),
        "total_cost_adjusted_pnl_usd": round(total_pnl, 8),
        "one_cent_stress_pnl_usd": round(one_cent_stress_pnl, 8),
        "two_cent_stress_pnl_usd": round(two_cent_stress_pnl, 8),
        "mean_cost_adjusted_pnl_per_event_usd": (
            round(mean_pnl, 8) if mean_pnl is not None else None
        ),
        "episode_ev_bootstrap_lcb_usd": (
            round(ev_lcb, 8) if ev_lcb is not None else None
        ),
        "markout_5m_samples": five_min_samples,
        "markout_5m_events": len(five_min_by_event),
        "mean_5m_net_clv": (
            round(mean_five_min_clv, 8)
            if mean_five_min_clv is not None
            else None
        ),
    }
    gates = [
        _gate(
            "family_is_confirmation_executable",
            family in PAPER_EXECUTION_FAMILIES,
            family,
            sorted(PAPER_EXECUTION_FAMILIES),
        ),
        _gate(
            "single_policy_partition",
            len(partition_ids) == 1,
            len(partition_ids),
            1,
        ),
        _gate(
            "dataset_role_is_promotion_eligible",
            bool(dataset_roles)
            and set(dataset_roles).issubset(
                set(policy.allowed_dataset_roles)
            ),
            dataset_roles,
            list(policy.allowed_dataset_roles),
        ),
        _at_least("replay_runs", metrics["replay_runs"], policy.min_replay_runs),
        _at_least(
            "independent_events",
            metrics["independent_events"],
            policy.min_independent_events,
        ),
        _at_least(
            "evidence_articles",
            metrics["evidence_articles"],
            policy.min_evidence_articles,
        ),
        _at_least(
            "extraction_agreement_rate",
            agreement_rate,
            policy.min_extraction_agreement_rate,
        ),
        _at_most(
            "invalid_extraction_rate",
            invalid_rate,
            policy.max_invalid_extraction_rate,
        ),
        _at_least(
            "proof_completeness_rate",
            proof_rate,
            policy.min_proof_completeness_rate,
        ),
        _at_least(
            "terminal_opportunities",
            terminal,
            policy.min_terminal_opportunities,
        ),
        _at_least(
            "quote_availability_rate",
            quote_rate,
            policy.min_quote_availability_rate,
        ),
        _at_least(
            "positive_net_edge_opportunities",
            positive_edge,
            policy.min_positive_edge_opportunities,
        ),
        _at_most(
            "duplicate_entries",
            duplicate_entries,
            policy.max_duplicate_entries,
        ),
        _at_least(
            "human_labeled_terminal_decisions",
            human_terminal,
            policy.min_human_labeled_terminal_decisions,
        ),
        _at_most(
            "human_label_disagreements",
            human_disagreements,
            policy.max_human_label_disagreements,
        ),
        _at_most(
            "human_label_conflicts",
            human_conflicts,
            policy.max_human_label_conflicts,
        ),
        _at_most(
            "false_terminal_actions",
            false_terminal_actions,
            policy.max_false_terminal_actions,
        ),
        _at_most(
            "settlement_source_violations",
            settlement_source_violations,
            policy.max_settlement_source_violations,
        ),
        _at_most(
            "deadline_no_inference_actions",
            deadline_no_inference_actions,
            policy.max_deadline_no_inference_actions,
        ),
        _at_most(
            "fee_schedule_violations",
            fee_schedule_violations,
            policy.max_fee_schedule_violations,
        ),
        _at_least(
            "resolved_events",
            len(resolved_event_pnl),
            policy.min_resolved_events,
        ),
        _at_least(
            "markout_5m_samples",
            five_min_samples,
            policy.min_markout_5m_samples,
        ),
        _at_least(
            "markout_5m_events",
            len(five_min_by_event),
            policy.min_markout_5m_events,
        ),
        _at_least(
            "mean_5m_net_clv",
            mean_five_min_clv,
            policy.min_mean_5m_net_clv,
        ),
        _strictly_greater(
            "total_cost_adjusted_pnl_usd",
            total_pnl if resolved_event_pnl else None,
            policy.min_total_cost_adjusted_pnl_usd,
        ),
        _strictly_greater(
            "one_cent_stress_pnl_usd",
            one_cent_stress_pnl
            if one_cent_stress_event_pnl
            else None,
            policy.min_one_cent_stress_pnl_usd,
        ),
        _strictly_greater(
            "episode_ev_bootstrap_lcb_usd",
            ev_lcb,
            policy.min_episode_ev_lcb_usd,
        ),
    ]
    return {
        "status": "PASS" if all(item["passed"] for item in gates) else "FAIL",
        "metrics": metrics,
        "gates": gates,
        "run_ids": sorted(str(item["run_id"]) for item in summaries),
        "event_clusters": sorted(event_groups),
        "policy_partition_sha256s": partition_ids,
    }


def _validate_replay_summary(raw: dict[str, Any], path: Path) -> None:
    if raw.get("schema_version") != RULE_REPLAY_SCHEMA_VERSION:
        raise ValueError(f"{path} has an unsupported replay schema")
    required = {
        "kind",
        "run_id",
        "result_sha256",
        "timeline_sha256",
        "replay_policy_sha256",
        "rule_spec_sha256",
        "market_id",
        "event_slug",
        "rule_family",
        "dataset_role",
        "policy_partition",
        "policy_partition_sha256",
        "generated_at",
        "extraction",
        "decisions",
        "proofs",
        "trades",
        "settlement",
        "safety",
        "human_labels",
    }
    missing = sorted(required - set(raw))
    if missing:
        raise ValueError(f"{path} is missing replay fields: {', '.join(missing)}")
    for name in (
        "extraction",
        "decisions",
        "proofs",
        "settlement",
        "safety",
        "human_labels",
        "policy_partition",
    ):
        if not isinstance(raw.get(name), dict):
            raise ValueError(f"{path}.{name} must be an object")
    human_labels = raw["human_labels"]
    records = human_labels.get("records")
    if not isinstance(records, list):
        raise ValueError(f"{path}.human_labels.records must be a list")
    reconstructed = {
        "provided": len(records),
        "matched": 0,
        "terminal_decisions": 0,
        "disagreements": 0,
        "unknown_evaluations": 0,
    }
    seen_label_evaluations: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(
                f"{path}.human_labels.records[{index}] must be an object"
            )
        evaluation_sha256 = str(
            record.get("evaluation_sha256") or ""
        ).strip().lower()
        if (
            len(evaluation_sha256) != 64
            or any(
                char not in "0123456789abcdef"
                for char in evaluation_sha256
            )
        ):
            raise ValueError(
                f"{path}.human_labels.records[{index}] has an invalid "
                "evaluation_sha256"
            )
        if evaluation_sha256 in seen_label_evaluations:
            raise ValueError(
                f"{path}.human_labels contains a duplicate evaluation label"
            )
        seen_label_evaluations.add(evaluation_sha256)
        expected_state = str(record.get("expected_state") or "")
        if expected_state not in {
            "TERMINAL_YES",
            "TERMINAL_NO",
            "NONTERMINAL",
        }:
            raise ValueError(
                f"{path}.human_labels.records[{index}] has an invalid "
                "expected_state"
            )
        matched = record.get("matched")
        agrees = record.get("agrees")
        if not isinstance(matched, bool) or not isinstance(agrees, bool):
            raise ValueError(
                f"{path}.human_labels.records[{index}] requires boolean "
                "matched and agrees fields"
            )
        if not str(record.get("labeler_id") or "").strip() or not str(
            record.get("rationale") or ""
        ).strip():
            raise ValueError(
                f"{path}.human_labels.records[{index}] requires a labeler "
                "and rationale"
            )
        if matched:
            reconstructed["matched"] += 1
            if expected_state in {"TERMINAL_YES", "TERMINAL_NO"}:
                reconstructed["terminal_decisions"] += 1
            if not agrees:
                reconstructed["disagreements"] += 1
        else:
            reconstructed["unknown_evaluations"] += 1
    for name, expected in reconstructed.items():
        if _integer(human_labels.get(name)) != expected:
            raise ValueError(
                f"{path}.human_labels.{name} is not reconstructable from "
                "its records"
            )
    if not isinstance(raw.get("trades"), list):
        raise ValueError(f"{path}.trades must be a list")
    expected = str(raw["result_sha256"])
    payload = dict(raw)
    payload.pop("result_sha256", None)
    payload.pop("output_path", None)
    if sha256_json(payload) != expected:
        raise ValueError(f"{path} failed replay result hash verification")
    if raw.get("dataset_role") not in {
        "development",
        "frozen_oos",
        "forward",
    }:
        raise ValueError(f"{path}.dataset_role is invalid")
    if sha256_json(raw["policy_partition"]) != str(
        raw["policy_partition_sha256"]
    ):
        raise ValueError(f"{path} failed policy partition hash verification")
    if raw["policy_partition"].get("rule_family") != raw["rule_family"]:
        raise ValueError(f"{path} policy partition family does not match")
    expected_run_id = hashlib.sha256(
        (
            f"{raw.get('rule_spec_sha256')}:{raw['timeline_sha256']}:"
            f"{raw['replay_policy_sha256']}"
        ).encode("utf-8")
    ).hexdigest()[:20]
    if raw["run_id"] != expected_run_id:
        raise ValueError(f"{path} failed replay run identity verification")


def _unique_human_label_metrics(
    summaries: list[dict[str, Any]],
) -> dict[str, int]:
    unique: dict[str, dict[str, Any]] = {}
    duplicates = 0
    conflicts = 0
    for summary in summaries:
        for record in summary["human_labels"]["records"]:
            evaluation_sha256 = str(record["evaluation_sha256"])
            existing = unique.get(evaluation_sha256)
            if existing is None:
                unique[evaluation_sha256] = record
                continue
            duplicates += 1
            if existing["expected_state"] != record["expected_state"]:
                conflicts += 1
    terminal = sum(
        1
        for item in unique.values()
        if item["matched"]
        and item["expected_state"] in {"TERMINAL_YES", "TERMINAL_NO"}
    )
    disagreements = sum(
        1
        for item in unique.values()
        if item["matched"] and not item["agrees"]
    )
    return {
        "terminal_decisions": terminal,
        "disagreements": disagreements,
        "duplicates": duplicates,
        "conflicts": conflicts,
    }


def _bootstrap_mean_lcb(
    values: list[float],
    *,
    samples: int,
    confidence: float,
    seed: int,
) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    rng = random.Random(seed)
    count = len(values)
    means = sorted(
        sum(values[rng.randrange(count)] for _ in range(count)) / count
        for _ in range(samples)
    )
    alpha = 1.0 - confidence
    index = max(0, min(len(means) - 1, int(math.floor(alpha * len(means)))))
    return means[index]


def _gate(
    name: str,
    passed: bool,
    actual: Any,
    required: Any,
) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "actual": actual,
        "required": required,
    }


def _at_least(name: str, actual: Any, required: float | int) -> dict[str, Any]:
    value = _number(actual)
    return _gate(
        name,
        value is not None and value + 1e-12 >= float(required),
        actual,
        f">={required}",
    )


def _at_most(name: str, actual: Any, required: float | int) -> dict[str, Any]:
    value = _number(actual)
    return _gate(
        name,
        value is not None and value <= float(required) + 1e-12,
        actual,
        f"<={required}",
    )


def _strictly_greater(
    name: str,
    actual: Any,
    required: float | int,
) -> dict[str, Any]:
    value = _number(actual)
    return _gate(
        name,
        value is not None and value > float(required) + 1e-12,
        actual,
        f">{required}",
    )


def _integer(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 8)


__all__ = [
    "PROMOTION_REPORT_SCHEMA_VERSION",
    "PAPER_EXECUTION_FAMILIES",
    "PromotionPolicy",
    "build_promotion_report",
    "load_replay_summaries",
    "promotion_report_command",
]
