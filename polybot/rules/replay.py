from __future__ import annotations

import hashlib
import json
import math
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.core.execution import PaperTradingAdapter
from polybot.core.fees import (
    FEE_POLICY_VERSION,
    fee_schedule_map,
    fee_schedules_sha256,
)
from polybot.core.holdings import _atomic_json_write
from polybot.core.portfolio import (
    PortfolioAllocator,
    PortfolioConfig,
    PortfolioLink,
)
from polybot.core.types import Article
from polybot.discovery.config import load_discovery_config, rule_store_db_path
from polybot.discovery.sources import (
    source_plan_sha256,
    validate_source_plan_freshness,
)
from polybot.discovery.store import DiscoveryStore
from polybot.discovery.types import MarketContext, SourcePlan, market_dir_slug

from .contracts import DecisionProof, RuleEvaluation, RuleSpec, sha256_json
from .evidence import (
    EVIDENCE_EXTRACTOR_VERSION,
    EVIDENCE_PROMPT_VERSION,
    EvidenceExtractor,
)
from .evaluators import EVALUATOR_VERSION
from .runner import GenericRuleMarketRunner
from .store import RuleStore

RULE_REPLAY_SCHEMA_VERSION = 3
RULE_REPLAY_ENGINE_VERSION = "rules-replay-v3-trade-prints"
REPLAY_MARKOUT_HORIZONS_SECONDS = (60, 300, 1800)
REPLAY_MARKOUT_MAX_LAG_SECONDS = {60: 60, 300: 120, 1800: 300}
_EVENT_TYPES = {"ARTICLE", "BOOK", "RESOLUTION", "TRADE"}
REPLAY_DATASET_ROLES = {"development", "frozen_oos", "forward"}
_ARTICLE_FIELDS = {
    "url",
    "domain",
    "title",
    "published_at",
    "fetched_at",
    "raw_text",
    "hash",
    "source_kind",
    "byline",
    "origin_organization",
    "discovered_at",
    "fetch_started_at",
    "parsed_at",
    "source_endpoint",
    "source_adapter",
}


@dataclass(frozen=True)
class ReplayEvent:
    event_type: str
    at: datetime
    payload: dict[str, Any]
    line_number: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.event_type,
            "at": self.at.isoformat(),
            **self.payload,
        }


class ReplayQuoteProvider:
    """Point-in-time quote source; it cannot expose a future snapshot."""

    def __init__(self, spec: RuleSpec):
        self.spec = spec
        self.now: datetime | None = None
        self._snapshots: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._bindings = {item.name: item for item in spec.outcomes}

    def set_now(self, at: datetime) -> None:
        self.now = _aware_utc(at)

    def update(
        self,
        *,
        outcome: str,
        yes: dict[str, Any],
        no: dict[str, Any],
        at: datetime,
        revision: str,
    ) -> None:
        try:
            binding = self._bindings[outcome]
        except KeyError as exc:
            raise ValueError(f"unknown replay outcome {outcome!r}") from exc
        stamp = _aware_utc(at)
        self.set_now(stamp)
        self._snapshots[binding.yes_token_id] = (
            stamp,
            {**yes, "token_id": binding.yes_token_id, "revision": f"{revision}:yes"},
        )
        self._snapshots[binding.no_token_id] = (
            stamp,
            {**no, "token_id": binding.no_token_id, "revision": f"{revision}:no"},
        )

    def quote_snapshot(self, token_id: str) -> dict[str, Any]:
        if self.now is None or token_id not in self._snapshots:
            return {}
        at, snapshot = self._snapshots[token_id]
        staleness = max(0.0, (self.now - at).total_seconds())
        return {**snapshot, "staleness": staleness}


def load_rule_replay_timeline(path: Path, spec: RuleSpec) -> list[ReplayEvent]:
    events: list[ReplayEvent] = []
    previous_at: datetime | None = None
    seen_article_ids: set[str] = set()
    resolutions: dict[str, bool] = {}
    outcome_names = {item.name for item in spec.outcomes}
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"timeline line {line_number} is invalid JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError(f"timeline line {line_number} must be an object")
        event_type = _choice(raw.get("type"), _EVENT_TYPES, f"line {line_number}.type")
        at = _iso_at(raw.get("at"), f"line {line_number}.at")
        if previous_at is not None and at < previous_at:
            raise ValueError(
                f"timeline line {line_number} is out of order; replay input "
                "must be nondecreasing to prevent look-ahead"
            )
        previous_at = at
        if event_type == "ARTICLE":
            _known(raw, {"type", "at", "article"}, line_number)
            article = _article_payload(raw.get("article"), at, line_number)
            article_id = str(article["hash"])
            if article_id in seen_article_ids:
                raise ValueError(
                    f"timeline line {line_number} repeats article hash {article_id!r}"
                )
            seen_article_ids.add(article_id)
            payload = {"article": article}
        elif event_type == "BOOK":
            _known(raw, {"type", "at", "outcome", "yes", "no"}, line_number)
            outcome = _text(raw.get("outcome"), f"line {line_number}.outcome")
            if outcome not in outcome_names:
                raise ValueError(
                    f"timeline line {line_number} has unknown outcome {outcome!r}"
                )
            payload = {
                "outcome": outcome,
                "yes": _book(raw.get("yes"), line_number, "yes"),
                "no": _book(raw.get("no"), line_number, "no"),
            }
        elif event_type == "TRADE":
            _known(
                raw,
                {
                    "type",
                    "at",
                    "outcome",
                    "outcome_side",
                    "token_id",
                    "price",
                    "size",
                    "reported_side",
                    "fee_rate_bps",
                    "transaction_hash",
                },
                line_number,
            )
            outcome = _text(
                raw.get("outcome"),
                f"line {line_number}.outcome",
            )
            if outcome not in outcome_names:
                raise ValueError(
                    f"timeline line {line_number} has unknown outcome "
                    f"{outcome!r}"
                )
            outcome_side = _choice(
                raw.get("outcome_side"),
                {"YES", "NO"},
                f"line {line_number}.outcome_side",
            )
            reported_side = _choice(
                raw.get("reported_side"),
                {"BUY", "SELL"},
                f"line {line_number}.reported_side",
            )
            price = _float(raw.get("price"))
            size = _float(raw.get("size"))
            fee_rate_bps = _float(raw.get("fee_rate_bps"))
            if price is None or not 0 < price < 1:
                raise ValueError(
                    f"line {line_number}.price must be between 0 and 1"
                )
            if size is None or size <= 0:
                raise ValueError(
                    f"line {line_number}.size must be positive"
                )
            if fee_rate_bps is None or fee_rate_bps < 0:
                raise ValueError(
                    f"line {line_number}.fee_rate_bps must be non-negative"
                )
            payload = {
                "outcome": outcome,
                "outcome_side": outcome_side,
                "token_id": _text(
                    raw.get("token_id"),
                    f"line {line_number}.token_id",
                ),
                "price": price,
                "size": size,
                "reported_side": reported_side,
                "fee_rate_bps": fee_rate_bps,
                "transaction_hash": str(
                    raw.get("transaction_hash") or ""
                ),
            }
        else:
            _known(
                raw,
                {"type", "at", "outcome", "resolved_yes"},
                line_number,
            )
            outcome = _text(raw.get("outcome"), f"line {line_number}.outcome")
            if outcome not in outcome_names:
                raise ValueError(
                    f"timeline line {line_number} has unknown outcome {outcome!r}"
                )
            resolved_yes = raw.get("resolved_yes")
            if not isinstance(resolved_yes, bool):
                raise ValueError(
                    f"line {line_number}.resolved_yes must be a boolean"
                )
            if outcome in resolutions and resolutions[outcome] != resolved_yes:
                raise ValueError(
                    f"timeline line {line_number} conflicts with an earlier "
                    f"resolution for {outcome!r}"
                )
            resolutions[outcome] = resolved_yes
            payload = {"outcome": outcome, "resolved_yes": resolved_yes}
        events.append(
            ReplayEvent(
                event_type=event_type,
                at=at,
                payload=payload,
                line_number=line_number,
            )
        )
    if not events:
        raise ValueError(f"replay timeline {path} has no events")
    return events


def load_human_labels(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid human-label file {path}") from exc
    if not isinstance(raw, list):
        raise ValueError("human-label file must contain a JSON array")
    labels: list[dict[str, str]] = []
    seen: set[str] = set()
    required = {
        "evaluation_sha256",
        "expected_state",
        "labeler_id",
        "rationale",
    }
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"human label {index} must be an object")
        unknown = sorted(set(item) - required)
        missing = sorted(required - set(item))
        if unknown or missing:
            detail = []
            if missing:
                detail.append("missing=" + ",".join(missing))
            if unknown:
                detail.append("unknown=" + ",".join(unknown))
            raise ValueError(
                f"human label {index} has invalid fields: "
                + " ".join(detail)
            )
        evaluation_sha256 = str(item["evaluation_sha256"]).strip().lower()
        if (
            len(evaluation_sha256) != 64
            or any(
                char not in "0123456789abcdef"
                for char in evaluation_sha256
            )
        ):
            raise ValueError(
                f"human label {index}.evaluation_sha256 is invalid"
            )
        if evaluation_sha256 in seen:
            raise ValueError(
                f"duplicate human label for {evaluation_sha256}"
            )
        expected_state = str(item["expected_state"]).strip().upper()
        if expected_state not in {
            "TERMINAL_YES",
            "TERMINAL_NO",
            "NONTERMINAL",
        }:
            raise ValueError(
                f"human label {index}.expected_state is invalid"
            )
        labeler_id = str(item["labeler_id"]).strip()
        rationale = str(item["rationale"]).strip()
        if not labeler_id or not rationale:
            raise ValueError(
                f"human label {index} requires labeler_id and rationale"
            )
        seen.add(evaluation_sha256)
        labels.append(
            {
                "evaluation_sha256": evaluation_sha256,
                "expected_state": expected_state,
                "labeler_id": labeler_id,
                "rationale": rationale,
            }
        )
    return sorted(labels, key=lambda item: item["evaluation_sha256"])


def replay_rule_market(
    config_path: Path,
    market_id: str,
    timeline_path: Path,
    *,
    out: Path | None = None,
    dataset_role: str = "development",
    labels_path: Path | None = None,
) -> dict[str, Any]:
    dataset_role = dataset_role.strip().casefold()
    if dataset_role not in REPLAY_DATASET_ROLES:
        raise ValueError(
            "dataset_role must be one of "
            + ", ".join(sorted(REPLAY_DATASET_ROLES))
        )
    config = load_discovery_config(config_path)
    if not config.rule_runner.enabled:
        raise SystemExit("rule_runner.enabled is false; rules-first replay is unavailable")
    if config.fleet.position_mode == "live":
        raise SystemExit("rules-first replay refuses fleet.position_mode=live")

    discovery_store = DiscoveryStore(config.data_dir)
    context = discovery_store.load_context(market_id)
    if context is None:
        raise SystemExit(f"unknown market_id {market_id!r}")
    rule_store = RuleStore(rule_store_db_path(config))
    spec = rule_store.load_spec(market_id, context.rule_text_sha256)
    if spec is None:
        raise SystemExit(f"market {market_id!r} has no current RuleSpec")
    plan = discovery_store.load_source_plan(market_id)
    if plan is None:
        raise SystemExit(f"market {market_id!r} has no source plan")
    spec.validate_context_binding(context)
    validate_source_plan_freshness(context, plan, spec)
    events = load_rule_replay_timeline(timeline_path, spec)
    human_labels = load_human_labels(labels_path)

    timeline_sha256 = sha256_json([event.as_dict() for event in events])
    replay_policy = {
        "engine_version": RULE_REPLAY_ENGINE_VERSION,
        "dataset_role": dataset_role,
        "human_labels_sha256": sha256_json(human_labels),
        "classifier": {
            "provider": config.classifier.provider,
            "model": config.classifier.model,
        },
        "rule_runner": asdict(config.rule_runner),
        "confirmation_policy": {
            "min_edge": config.opportunity.min_edge,
            "max_entry_price": config.opportunity.max_entry_price,
            "slippage_buffer": config.opportunity.slippage_buffer,
            "resolution_risk_buffer": config.opportunity.resolution_risk_buffer,
            "resolution_risk_scale": config.opportunity.resolution_risk_scale,
            "requested_usd": config.allocator.per_order_usd,
        },
        "fee_policy": {
            "version": FEE_POLICY_VERSION,
            "fee_schedules_sha256": fee_schedules_sha256(context),
            "max_age_hours": (
                config.rule_runner.max_fee_schedule_age_hours
            ),
        },
        "allocator": asdict(config.allocator),
        "context": {
            "market_id": context.market_id,
            "event_slug": context.event_slug,
            "state": context.state,
            "deadline_iso": context.deadline_iso,
            "correlation_group": context.correlation_group,
            "resolution_risk": (
                context.rule_analysis.resolution_risk
                if context.rule_analysis is not None
                else None
            ),
        },
    }
    policy_partition = {
        "rule_family": spec.semantics.rule_family,
        "model_sha256": sha256_json(
            {
                "provider": config.classifier.provider,
                "model": config.classifier.model,
                "passes": config.rule_runner.extraction_passes,
            }
        ),
        "prompt_sha256": sha256_json(
            {
                "extractor_version": EVIDENCE_EXTRACTOR_VERSION,
                "prompt_version": EVIDENCE_PROMPT_VERSION,
            }
        ),
        "evaluator_sha256": sha256_json(
            {"evaluator_version": EVALUATOR_VERSION}
        ),
        "source_adapter_sha256": sha256_json(
            {
                "kind": "rules_replay_timeline",
                "version": "point-in-time-jsonl-v1",
            }
        ),
        "fee_policy_sha256": sha256_json(
            {
                "version": FEE_POLICY_VERSION,
                "curves": sorted(
                    {
                        json.dumps(
                            {
                                "fees_enabled": schedule.fees_enabled,
                                "rate": schedule.rate,
                                "exponent": schedule.exponent,
                                "taker_only": schedule.taker_only,
                                "rebate_rate": schedule.rebate_rate,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        for schedule in fee_schedule_map(context).values()
                    }
                ),
            }
        ),
        "execution_policy_sha256": sha256_json(
            {
                "rule_runner": replay_policy["rule_runner"],
                "confirmation_policy": replay_policy[
                    "confirmation_policy"
                ],
                "allocator": replay_policy["allocator"],
            }
        ),
    }
    policy_partition_sha256 = sha256_json(policy_partition)
    replay_policy_sha256 = sha256_json(replay_policy)
    run_id = hashlib.sha256(
        (
            f"{spec.spec_sha256}:{timeline_sha256}:"
            f"{replay_policy_sha256}"
        ).encode("utf-8")
    ).hexdigest()[:20]
    replay_root = (
        config.data_dir
        / "rule_replays"
        / market_dir_slug(market_id)
        / run_id
    )
    summary_path = replay_root / "summary.json"
    cached = _load_cached_replay(
        summary_path,
        run_id=run_id,
        timeline_sha256=timeline_sha256,
        replay_policy_sha256=replay_policy_sha256,
        rule_spec_sha256=spec.spec_sha256,
    )
    if cached is not None:
        if out is not None:
            _atomic_json_write(Path(out), cached)
            return {**cached, "output_path": str(Path(out))}
        return {**cached, "output_path": str(summary_path)}
    _reset_replay_root(replay_root, config.data_dir)
    replay_store = DiscoveryStore(replay_root / "discovery")
    replay_store.save_context(context)
    replay_store.save_source_plan(plan)
    replay_rule_store = RuleStore(replay_root / "rules.sqlite3")
    replay_rule_store.save_spec(spec)

    quote_provider = ReplayQuoteProvider(spec)
    adapter = PaperTradingAdapter(
        state_path=replay_root / "paper_broker.json",
        quote_provider=quote_provider,
        token_pairs=[
            (item.yes_token_id, item.no_token_id)
            for item in spec.outcomes
        ],
        fee_schedules=fee_schedule_map(context),
        slippage_bps=config.rule_runner.paper_slippage_bps,
        max_book_age_seconds=config.rule_runner.paper_max_book_age_seconds,
    )
    ledger_path = replay_root / "allocations.json"
    PortfolioAllocator(ledger_path, config.allocator).write_caps()
    portfolio = PortfolioLink(
        PortfolioConfig(
            ledger_path=str(ledger_path),
            market_id=context.market_id,
            event_slug=context.event_slug,
            correlation_group=context.correlation_group or "uncategorized",
            region="global",
            deadline_iso=context.deadline_iso,
        )
    )
    runner = GenericRuleMarketRunner(
        config=config,
        context=context,
        spec=spec,
        source_plan=plan,
        rule_store=replay_rule_store,
        extractor=EvidenceExtractor(
            config.classifier,
            replay_rule_store,
            passes=config.rule_runner.extraction_passes,
            deterministic_enabled=(
                config.rule_runner.deterministic_evidence_enabled
            ),
            deterministic_families={
                item.strip().upper()
                for item in (
                    config.rule_runner.deterministic_evidence_families
                )
            },
            deterministic_policy_version=(
                config.rule_runner.deterministic_evidence_policy_version
            ),
        ),
        feed_reader=None,
        promotion_cache=None,
        adapter=adapter,
        portfolio=portfolio,
        data_dir=replay_root / "runner",
    )

    cycles: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    open_entries: set[tuple[str, str]] = set()
    duplicate_entries = 0
    resolutions: dict[str, bool] = {}
    event_counts = {name: 0 for name in sorted(_EVENT_TYPES)}
    for event_index, event in enumerate(events):
        event_counts[event.event_type] += 1
        quote_provider.set_now(event.at)
        if event.event_type == "BOOK":
            quote_provider.update(
                outcome=str(event.payload["outcome"]),
                yes=dict(event.payload["yes"]),
                no=dict(event.payload["no"]),
                at=event.at,
                revision=f"event-{event_index}",
            )
            _record_markouts(entries, adapter, event.at)
            if replay_rule_store.claims_for_spec(spec.spec_sha256):
                cycle = runner.run_once(articles=[], as_of=event.at)
                cycles.append(_cycle_record(event, cycle))
                duplicate_entries += _record_executions(
                    cycle,
                    event.at,
                    entries,
                    open_entries,
                )
        elif event.event_type == "ARTICLE":
            article = Article(**dict(event.payload["article"]))
            cycle = runner.run_once(articles=[article], as_of=event.at)
            cycles.append(_cycle_record(event, cycle))
            duplicate_entries += _record_executions(
                cycle,
                event.at,
                entries,
                open_entries,
            )
        elif event.event_type == "TRADE":
            # Persisted for future pessimistic maker/queue replay. The current
            # taker confirmation engine deliberately does not infer a fill
            # from a public print.
            continue
        else:
            outcome = str(event.payload["outcome"])
            resolutions[outcome] = bool(event.payload["resolved_yes"])

    proofs = replay_rule_store.proofs_for_market(market_id)
    evaluations = {
        item.evaluation_sha256: item
        for item in replay_rule_store.evaluations_for_spec(spec.spec_sha256)
    }
    claims = {
        item.claim_sha256: item
        for item in replay_rule_store.claims_for_spec(spec.spec_sha256)
    }
    proof_audits = [
        _audit_proof(
            proof,
            context=context,
            spec=spec,
            plan=plan,
            evaluation=evaluations.get(proof.evaluation_sha256),
            claims=claims,
        )
        for proof in proofs
    ]
    extraction = _extraction_metrics(cycles)
    decision_metrics = _decision_metrics(cycles)
    settlement = _settlement(
        adapter.snapshot(),
        spec,
        resolutions,
        entries,
    )
    safety = _safety_metrics(
        proofs=proofs,
        evaluations=evaluations,
        claims=claims,
        spec=spec,
        resolutions=resolutions,
    )
    label_metrics = _human_label_metrics(
        human_labels,
        evaluations=evaluations,
    )
    markouts = _markout_metrics(entries)
    result: dict[str, Any] = {
        "schema_version": RULE_REPLAY_SCHEMA_VERSION,
        "kind": "rules_first_replay",
        "run_id": run_id,
        "timeline_sha256": timeline_sha256,
        "replay_policy_sha256": replay_policy_sha256,
        "replay_policy": replay_policy,
        "market_id": market_id,
        "event_slug": context.event_slug,
        "rule_family": spec.semantics.rule_family,
        "dataset_role": dataset_role,
        "policy_partition": policy_partition,
        "policy_partition_sha256": policy_partition_sha256,
        "rule_text_sha256": spec.rule_text_sha256,
        "rule_spec_sha256": spec.spec_sha256,
        "source_plan_sha256": source_plan_sha256(plan),
        "classifier": {
            "provider": config.classifier.provider,
            "model": config.classifier.model,
            "passes": config.rule_runner.extraction_passes,
            "wall_clock_free": True,
            "model_reproducible": (
                config.classifier.provider.strip().lower() == "rule_based"
            ),
        },
        "events": {"total": len(events), **event_counts},
        "extraction": extraction,
        "decisions": {
            **decision_metrics,
            "duplicate_entries": duplicate_entries,
        },
        "proofs": {
            "total": len(proofs),
            "complete": sum(1 for item in proof_audits if item["complete"]),
            "completeness_rate": _ratio(
                sum(1 for item in proof_audits if item["complete"]),
                len(proofs),
            ),
            "audits": proof_audits,
            "records": [
                {"proof_sha256": item.proof_sha256, **item.as_dict()}
                for item in proofs
            ],
        },
        "trades": entries,
        "markouts": markouts,
        "settlement": settlement,
        "safety": safety,
        "human_labels": label_metrics,
        "cycles": cycles,
        "generated_at": events[-1].at.isoformat(),
    }
    result["result_sha256"] = sha256_json(result)
    _atomic_json_write(summary_path, result)
    if out is not None:
        _atomic_json_write(Path(out), result)
        result["output_path"] = str(Path(out))
    else:
        result["output_path"] = str(summary_path)
    return result


def replay_rule_market_command(
    config_path: Path,
    market_id: str,
    timeline_path: Path,
    *,
    out: Path | None = None,
    dataset_role: str = "development",
    labels_path: Path | None = None,
) -> int:
    result = replay_rule_market(
        config_path,
        market_id,
        timeline_path,
        out=out,
        dataset_role=dataset_role,
        labels_path=labels_path,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _cycle_record(event: ReplayEvent, cycle: dict[str, Any]) -> dict[str, Any]:
    return {
        "at": event.at.isoformat(),
        "trigger": event.event_type,
        "line_number": event.line_number,
        **cycle,
    }


def _record_executions(
    cycle: dict[str, Any],
    at: datetime,
    entries: list[dict[str, Any]],
    open_entries: set[tuple[str, str]],
) -> int:
    duplicates = 0
    for action in cycle.get("actions", []):
        if not isinstance(action, dict) or not action.get("executed"):
            continue
        name = str(action.get("action") or "")
        outcome = str(action.get("outcome") or "")
        side = str(action.get("side") or "")
        key = (outcome, side)
        if name.startswith("ENTER_"):
            if key in open_entries:
                duplicates += 1
            open_entries.add(key)
            execution = action.get("execution_result")
            execution = execution if isinstance(execution, dict) else {}
            shares = _float(execution.get("filled_shares")) or 0.0
            total_cost = _float(execution.get("total_cost_usd"))
            execution_price = _float(execution.get("execution_price"))
            gross_cost = _float(execution.get("gross_cost_usd"))
            fee_usd = _float(execution.get("fee_usd"))
            raw_level_fills = execution.get("level_fills", [])
            level_fills = (
                raw_level_fills
                if isinstance(raw_level_fills, list)
                else []
            )
            slippage_loss = sum(
                max(
                    0.0,
                    (
                        (_float(level.get("execution_price")) or 0.0)
                        - (_float(level.get("book_price")) or 0.0)
                    )
                    * (_float(level.get("filled_shares")) or 0.0),
                )
                for level in level_fills
                if isinstance(level, dict)
            )
            entries.append(
                {
                    "at": at.isoformat(),
                    "outcome": outcome,
                    "side": side,
                    "token_id": str(action.get("token_id") or ""),
                    "proof_sha256": str(action.get("proof_sha256") or ""),
                    "execution_price": execution_price,
                    "filled_shares": shares,
                    "gross_cost_usd": gross_cost,
                    "fee_usd": fee_usd,
                    "modeled_slippage_loss_usd": round(
                        slippage_loss,
                        8,
                    ),
                    "total_cost_usd": total_cost,
                    "net_cost_per_share": (
                        round(total_cost / shares, 8)
                        if total_cost is not None and shares > 0
                        else None
                    ),
                    "markouts": {},
                }
            )
        elif name.startswith("EXIT_"):
            open_entries.discard(key)
    return duplicates


def _record_markouts(
    entries: list[dict[str, Any]],
    adapter: PaperTradingAdapter,
    at: datetime,
) -> None:
    for entry in entries:
        entry_at = _iso_at(entry["at"], "trade.at")
        elapsed = (at - entry_at).total_seconds()
        side = str(entry["side"])
        token_id = str(entry["token_id"])
        bid = (
            adapter.yes_best_bid(token_id)
            if side == "YES"
            else adapter.no_best_bid(token_id)
        )
        if bid is None:
            continue
        for horizon in REPLAY_MARKOUT_HORIZONS_SECONDS:
            key = str(horizon)
            max_lag = REPLAY_MARKOUT_MAX_LAG_SECONDS[horizon]
            if (
                elapsed < horizon
                or elapsed > horizon + max_lag
                or key in entry["markouts"]
            ):
                continue
            execution_price = _float(entry.get("execution_price"))
            net_cost = _float(entry.get("net_cost_per_share"))
            entry["markouts"][key] = {
                "at": at.isoformat(),
                "elapsed_seconds": round(elapsed, 3),
                "executable_bid": bid,
                "raw_clv": (
                    round(bid - execution_price, 8)
                    if execution_price is not None
                    else None
                ),
                "net_clv": (
                    round(bid - net_cost, 8)
                    if net_cost is not None
                    else None
                ),
            }


def _markout_metrics(entries: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for horizon in REPLAY_MARKOUT_HORIZONS_SECONDS:
        values = [
            _float(entry.get("markouts", {}).get(str(horizon), {}).get("net_clv"))
            for entry in entries
        ]
        clean = [value for value in values if value is not None]
        result[str(horizon)] = {
            "samples": len(clean),
            "mean_net_clv": (
                round(sum(clean) / len(clean), 8) if clean else None
            ),
            "positive_fraction": _ratio(
                sum(1 for value in clean if value > 0),
                len(clean),
            ),
        }
    return result


def _settlement(
    broker: dict[str, Any],
    spec: RuleSpec,
    resolutions: dict[str, bool],
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    traded = bool(entries)
    balances = broker.get("balances")
    balances = balances if isinstance(balances, dict) else {}
    payout = 0.0
    unresolved_open: list[str] = []
    for binding in spec.outcomes:
        yes_shares = _float(balances.get(binding.yes_token_id)) or 0.0
        no_shares = _float(balances.get(binding.no_token_id)) or 0.0
        if yes_shares <= 0 and no_shares <= 0:
            continue
        if binding.name not in resolutions:
            unresolved_open.append(binding.name)
            continue
        payout += yes_shares if resolutions[binding.name] else no_shares
    complete = not unresolved_open
    net_cash = _float(broker.get("net_cash_usd")) or 0.0
    base_pnl = net_cash + payout if complete and traded else None
    invested = sum(
        _float(item.get("total_cost_usd")) or 0.0
        for item in entries
    )
    entry_shares = sum(
        _float(item.get("filled_shares")) or 0.0
        for item in entries
    )
    one_cent_stress = (
        base_pnl - 0.01 * entry_shares
        if base_pnl is not None
        else None
    )
    two_cent_stress = (
        base_pnl - 0.02 * entry_shares
        if base_pnl is not None
        else None
    )
    return {
        "traded": traded,
        "complete": complete,
        "resolutions": dict(sorted(resolutions.items())),
        "unresolved_open_outcomes": sorted(unresolved_open),
        "net_cash_usd": round(net_cash, 8),
        "resolution_payout_usd": round(payout, 8) if complete else None,
        "cost_adjusted_pnl_usd": (
            round(base_pnl, 8) if base_pnl is not None else None
        ),
        "one_cent_stress_pnl_usd": (
            round(one_cent_stress, 8)
            if one_cent_stress is not None
            else None
        ),
        "two_cent_stress_pnl_usd": (
            round(two_cent_stress, 8)
            if two_cent_stress is not None
            else None
        ),
        "invested_usd": round(invested, 8),
        "entry_shares": round(entry_shares, 8),
        "return_per_dollar": (
            round(base_pnl / invested, 8)
            if base_pnl is not None and invested > 0
            else None
        ),
        "fees_usd": round(_float(broker.get("fees_usd")) or 0.0, 8),
        "modeled_slippage_loss_usd": round(
            sum(
                _float(item.get("modeled_slippage_loss_usd")) or 0.0
                for item in entries
            ),
            8,
        ),
        "balances": balances,
    }


def _extraction_metrics(cycles: list[dict[str, Any]]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    for cycle in cycles:
        for result in cycle.get("extraction_results", []):
            if not isinstance(result, dict):
                continue
            status = str(result.get("status") or "UNKNOWN")
            statuses[status] = statuses.get(status, 0) + 1
    attempted = sum(
        count
        for status, count in statuses.items()
        if status not in {"CACHED", "STALE", "PROMOTION_FAILED"}
    )
    agreed = statuses.get("EXTRACTED", 0)
    disagreements = statuses.get("DISAGREEMENT", 0)
    invalid = statuses.get("INVALID", 0)
    return {
        "attempted_articles": attempted,
        "agreed": agreed,
        "disagreements": disagreements,
        "invalid": invalid,
        "agreement_rate": _ratio(agreed, agreed + disagreements),
        "valid_extraction_rate": _ratio(agreed, attempted),
        "invalid_rate": _ratio(invalid, attempted),
        "statuses": dict(sorted(statuses.items())),
    }


def _decision_metrics(cycles: list[dict[str, Any]]) -> dict[str, Any]:
    terminal = 0
    quote_available = 0
    tradable = 0
    executed_entries = 0
    executed_exits = 0
    for cycle in cycles:
        states = cycle.get("states", [])
        actions = cycle.get("actions", [])
        for index, state in enumerate(states):
            if not isinstance(state, dict) or not state.get("terminal"):
                continue
            action = actions[index] if index < len(actions) else {}
            if not isinstance(action, dict) or action.get("action") == "HOLD":
                continue
            terminal += 1
            if action.get("executable_ask") is not None:
                quote_available += 1
            edge = _float(action.get("net_edge"))
            minimum = _float(action.get("minimum_edge"))
            if (
                edge is not None
                and minimum is not None
                and edge + 1e-12 >= minimum
            ):
                tradable += 1
        for action in actions:
            if not isinstance(action, dict) or not action.get("executed"):
                continue
            name = str(action.get("action") or "")
            if name.startswith("ENTER_"):
                executed_entries += 1
            elif name.startswith("EXIT_"):
                executed_exits += 1
    return {
        "terminal_opportunities": terminal,
        "quote_available": quote_available,
        "quote_availability_rate": _ratio(quote_available, terminal),
        "positive_net_edge": tradable,
        "positive_net_edge_rate": _ratio(tradable, terminal),
        "executed_entries": executed_entries,
        "executed_exits": executed_exits,
    }


def _safety_metrics(
    *,
    proofs: list[DecisionProof],
    evaluations: dict[str, RuleEvaluation],
    claims: dict[str, Any],
    spec: RuleSpec,
    resolutions: dict[str, bool],
) -> dict[str, int]:
    false_terminal_actions = 0
    settlement_source_violations = 0
    deadline_no_inference_actions = 0
    fee_schedule_violations = 0
    for proof in proofs:
        if not proof.executed or proof.action not in {
            "ENTER_YES",
            "ENTER_NO",
        }:
            continue
        evaluation = evaluations.get(proof.evaluation_sha256)
        if evaluation is None:
            false_terminal_actions += 1
            continue
        resolved = resolutions.get(proof.outcome_name)
        if resolved is not None:
            expected_side = "YES" if resolved else "NO"
            if proof.side != expected_side:
                false_terminal_actions += 1
        if (
            evaluation.evidence_state == "TERMINAL_NO"
            and not evaluation.claim_sha256s
        ):
            deadline_no_inference_actions += 1
        if spec.semantics.rule_family == "SOURCE_LOCKED_ANNOUNCEMENT":
            relevant = [
                claims[item]
                for item in evaluation.claim_sha256s
                if item in claims
            ]
            if not relevant or not all(
                "SETTLEMENT" in item.source_roles for item in relevant
            ):
                settlement_source_violations += 1
        if (
            proof.fee_schedule_status != "VERIFIED"
            or not proof.fee_schedule_sha256
        ):
            fee_schedule_violations += 1
    return {
        "false_terminal_actions": false_terminal_actions,
        "settlement_source_violations": settlement_source_violations,
        "deadline_no_inference_actions": deadline_no_inference_actions,
        "fee_schedule_violations": fee_schedule_violations,
    }


def _human_label_metrics(
    labels: list[dict[str, str]],
    *,
    evaluations: dict[str, RuleEvaluation],
) -> dict[str, Any]:
    matched = 0
    terminal_labels = 0
    disagreements = 0
    unknown_evaluations = 0
    records: list[dict[str, Any]] = []
    for label in labels:
        evaluation = evaluations.get(label["evaluation_sha256"])
        if evaluation is None:
            unknown_evaluations += 1
            records.append({**label, "matched": False, "agrees": False})
            continue
        matched += 1
        expected = label["expected_state"]
        if expected in {"TERMINAL_YES", "TERMINAL_NO"}:
            terminal_labels += 1
            agrees = (
                evaluation.terminal
                and evaluation.evidence_state == expected
            )
        else:
            agrees = not evaluation.terminal
        if not agrees:
            disagreements += 1
        records.append(
            {
                **label,
                "matched": True,
                "actual_state": evaluation.evidence_state,
                "actual_terminal": evaluation.terminal,
                "agrees": agrees,
            }
        )
    return {
        "provided": len(labels),
        "matched": matched,
        "terminal_decisions": terminal_labels,
        "disagreements": disagreements,
        "unknown_evaluations": unknown_evaluations,
        "agreement_rate": _ratio(
            matched - disagreements,
            matched,
        ),
        "records": records,
    }


def _audit_proof(
    proof: DecisionProof,
    *,
    context: MarketContext,
    spec: RuleSpec,
    plan: SourcePlan,
    evaluation: RuleEvaluation | None,
    claims: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    if proof.market_id != context.market_id:
        errors.append("market_id_mismatch")
    if proof.rule_text_sha256 != context.rule_text_sha256:
        errors.append("rule_text_hash_mismatch")
    if proof.rule_spec_sha256 != spec.spec_sha256:
        errors.append("rule_spec_hash_mismatch")
    if proof.source_plan_sha256 != source_plan_sha256(plan):
        errors.append("source_plan_hash_mismatch")
    if evaluation is None:
        errors.append("evaluation_missing")
    else:
        if set(proof.claim_sha256s) != set(evaluation.claim_sha256s):
            errors.append("evaluation_claim_set_mismatch")
    missing_claims = sorted(set(proof.claim_sha256s) - set(claims))
    if missing_claims:
        errors.append("claims_missing")
    relevant = [claims[item] for item in proof.claim_sha256s if item in claims]
    if sorted({item.article_id for item in relevant}) != proof.article_ids:
        errors.append("article_ids_not_reconstructable")
    if sorted({item.source_domain for item in relevant}) != proof.source_domains:
        errors.append("source_domains_not_reconstructable")
    if sorted({item.independence_group for item in relevant}) != proof.independence_groups:
        errors.append("independence_groups_not_reconstructable")
    if sorted({item.supporting_quote for item in relevant}) != proof.supporting_quotes:
        errors.append("supporting_quotes_not_reconstructable")
    if sorted(
        {
            clause
            for item in relevant
            for clause in item.clauses_satisfied
        }
    ) != proof.clauses_satisfied:
        errors.append("satisfied_clauses_not_reconstructable")
    if sorted(
        {
            clause
            for item in relevant
            for clause in item.clauses_violated
        }
    ) != proof.clauses_violated:
        errors.append("violated_clauses_not_reconstructable")
    binding = next(
        (item for item in spec.outcomes if item.name == proof.outcome_name),
        None,
    )
    context_outcome = next(
        (
            item
            for item in context.outcomes
            if item.name == proof.outcome_name
        ),
        None,
    )
    if context_outcome is None:
        errors.append("fee_schedule_outcome_not_bound")
    elif context_outcome.fee_schedule is None:
        if proof.fee_schedule_status == "VERIFIED":
            errors.append("fee_schedule_unexpectedly_verified")
    else:
        expected_fee_hash = context_outcome.fee_schedule.schedule_sha256
        if proof.fee_schedule_sha256 != expected_fee_hash:
            errors.append("fee_schedule_hash_mismatch")
        if proof.fee_schedule != context_outcome.fee_schedule.as_dict():
            errors.append("fee_schedule_not_reconstructable")
    if proof.action in {"ENTER_YES", "ENTER_NO", "EXIT_YES", "EXIT_NO"}:
        if binding is None:
            errors.append("trade_outcome_not_bound")
        else:
            expected_token = (
                binding.yes_token_id
                if proof.side == "YES"
                else binding.no_token_id
            )
            if proof.token_id != expected_token:
                errors.append("trade_token_not_bound")
    if (
        proof.executed
        and proof.action in {"ENTER_YES", "ENTER_NO"}
        and proof.fee_schedule_status != "VERIFIED"
    ):
        errors.append("executed_entry_fee_schedule_unverified")
    if proof.executed:
        shares = _float(proof.execution_result.get("filled_shares"))
        if shares is None or shares <= 0:
            errors.append("executed_without_fill")
        if proof.action.startswith("ENTER_") and proof.executable_ask is None:
            errors.append("executed_entry_without_quote")
        if proof.action.startswith("EXIT_") and proof.executable_bid is None:
            errors.append("executed_exit_without_quote")
    return {
        "proof_sha256": proof.proof_sha256,
        "complete": not errors,
        "errors": errors,
    }


def _article_payload(value: Any, at: datetime, line_number: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"timeline line {line_number}.article must be an object")
    unknown = sorted(set(value) - _ARTICLE_FIELDS)
    if unknown:
        raise ValueError(
            f"timeline line {line_number}.article contains unknown keys: "
            + ", ".join(unknown)
        )
    required = {"url", "domain", "raw_text", "hash", "fetched_at"}
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(
            f"timeline line {line_number}.article is missing: "
            + ", ".join(missing)
        )
    payload = {
        "url": _text(value.get("url"), f"line {line_number}.article.url"),
        "domain": _text(value.get("domain"), f"line {line_number}.article.domain"),
        "title": str(value.get("title") or ""),
        "published_at": value.get("published_at"),
        "fetched_at": _text(
            value.get("fetched_at"),
            f"line {line_number}.article.fetched_at",
        ),
        "raw_text": _text(
            value.get("raw_text"),
            f"line {line_number}.article.raw_text",
        ),
        "hash": _text(value.get("hash"), f"line {line_number}.article.hash"),
        "source_kind": str(value.get("source_kind") or "article"),
        "byline": str(value.get("byline") or ""),
        "origin_organization": str(value.get("origin_organization") or ""),
        "discovered_at": str(value.get("discovered_at") or ""),
        "fetch_started_at": str(value.get("fetch_started_at") or ""),
        "parsed_at": str(value.get("parsed_at") or ""),
        "source_endpoint": str(value.get("source_endpoint") or ""),
        "source_adapter": str(value.get("source_adapter") or ""),
    }
    if payload["source_kind"] in {"feed", "feed_item"}:
        raise ValueError(
            f"timeline line {line_number}.article must contain promoted "
            "full text, not a feed summary"
        )
    fetched = _iso_at(payload["fetched_at"], f"line {line_number}.article.fetched_at")
    if fetched > at:
        raise ValueError(
            f"timeline line {line_number} exposes an article before fetched_at"
        )
    if payload["published_at"]:
        published = _iso_at(
            payload["published_at"],
            f"line {line_number}.article.published_at",
        )
        if published > at:
            raise ValueError(
                f"timeline line {line_number} exposes an article before published_at"
            )
        payload["published_at"] = published.isoformat()
    payload["fetched_at"] = fetched.isoformat()
    for name in ("discovered_at", "fetch_started_at", "parsed_at"):
        if not payload[name]:
            continue
        stamp = _iso_at(
            payload[name],
            f"line {line_number}.article.{name}",
        )
        if stamp > at:
            raise ValueError(
                f"timeline line {line_number} exposes an article before "
                f"{name}"
            )
        payload[name] = stamp.isoformat()
    return payload


def _book(value: Any, line_number: int, side_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(
            f"timeline line {line_number}.{side_name} must be an object"
        )
    unknown = sorted(set(value) - {"bids", "asks"})
    if unknown:
        raise ValueError(
            f"timeline line {line_number}.{side_name} contains unknown keys: "
            + ", ".join(unknown)
        )
    bids = _levels(value.get("bids"), line_number, f"{side_name}.bids")
    asks = _levels(value.get("asks"), line_number, f"{side_name}.asks")
    bids.sort(key=lambda item: item[0], reverse=True)
    asks.sort(key=lambda item: item[0])
    if bids and asks and bids[0][0] >= asks[0][0]:
        raise ValueError(
            f"timeline line {line_number}.{side_name} is crossed or locked"
        )
    return {"bids": bids, "asks": asks}


def _levels(value: Any, line_number: int, name: str) -> list[list[float]]:
    if not isinstance(value, list):
        raise ValueError(f"timeline line {line_number}.{name} must be a list")
    result: list[list[float]] = []
    for index, level in enumerate(value):
        if not isinstance(level, (list, tuple)) or len(level) != 2:
            raise ValueError(
                f"timeline line {line_number}.{name}[{index}] must be [price, size]"
            )
        price = _finite(level[0], f"line {line_number}.{name}[{index}].price")
        size = _finite(level[1], f"line {line_number}.{name}[{index}].size")
        if price <= 0 or price >= 1:
            raise ValueError(
                f"line {line_number}.{name}[{index}].price must be between 0 and 1"
            )
        if size <= 0:
            raise ValueError(
                f"line {line_number}.{name}[{index}].size must be positive"
            )
        result.append([price, size])
    return result


def _reset_replay_root(path: Path, data_dir: Path) -> None:
    target = path.resolve()
    root = (data_dir / "rule_replays").resolve()
    if root not in target.parents or len(target.parts) <= len(root.parts) + 1:
        raise ValueError(f"unsafe replay root {target}")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)


def _load_cached_replay(
    path: Path,
    *,
    run_id: str,
    timeline_sha256: str,
    replay_policy_sha256: str,
    rule_spec_sha256: str,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt cached replay summary {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"invalid cached replay summary {path}")
    expected = str(raw.get("result_sha256") or "")
    payload = dict(raw)
    payload.pop("result_sha256", None)
    payload.pop("output_path", None)
    if not expected or sha256_json(payload) != expected:
        raise ValueError(f"cached replay summary {path} failed hash verification")
    bindings = {
        "run_id": run_id,
        "timeline_sha256": timeline_sha256,
        "replay_policy_sha256": replay_policy_sha256,
        "rule_spec_sha256": rule_spec_sha256,
    }
    mismatches = [
        name for name, value in bindings.items() if raw.get(name) != value
    ]
    if mismatches:
        raise ValueError(
            f"cached replay summary {path} has stale bindings: "
            + ",".join(mismatches)
        )
    return raw


def _known(raw: dict[str, Any], allowed: set[str], line_number: int) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(
            f"timeline line {line_number} contains unknown keys: "
            + ", ".join(unknown)
        )


def _choice(value: Any, allowed: set[str], name: str) -> str:
    text = _text(value, name).upper()
    if text not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}")
    return text


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _iso_at(value: Any, name: str) -> datetime:
    text = _text(value, name)
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 datetime") from exc
    if stamp.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return stamp.astimezone(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("replay datetimes must include a timezone")
    return value.astimezone(timezone.utc)


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


__all__ = [
    "RULE_REPLAY_SCHEMA_VERSION",
    "RULE_REPLAY_ENGINE_VERSION",
    "REPLAY_DATASET_ROLES",
    "ReplayEvent",
    "ReplayQuoteProvider",
    "load_rule_replay_timeline",
    "load_human_labels",
    "replay_rule_market",
    "replay_rule_market_command",
]
