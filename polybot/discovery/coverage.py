from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.core.holdings import _atomic_json_write
from polybot.core.central_feed import CentralFeedStore
from polybot.rules.contracts import sha256_json
from polybot.rules.store import RuleStore

from .config import (
    DiscoveryConfig,
    central_feed_db_path,
    forward_recorder_db_path,
    rule_store_db_path,
)
from .fleet import FleetManager, recorded_book_contexts
from .scope import market_scope_decision
from .sources import source_plan_sha256, validate_source_plan_freshness
from .store import DiscoveryStore
from .types import (
    TRADEABLE_STATES,
    MarketContext,
    SourcePlan,
)


def build_semantic_coverage(
    config: DiscoveryConfig,
    *,
    persist: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    store = DiscoveryStore(config.data_dir)
    contexts = store.all_contexts()
    manager = FleetManager(
        config,
        store,
        live=False,
        per_order_usd=config.allocator.per_order_usd,
        ledger_path=str(config.data_dir / "allocations.json"),
    )
    monitored = manager.desired_markets(contexts)
    recorded = recorded_book_contexts(
        config,
        contexts,
        monitored,
    )
    monitored_ids = {context.market_id for context in monitored}
    recorded_ids = {context.market_id for context in recorded}

    rule_store = RuleStore(rule_store_db_path(config))
    capture = _capture_inventory(forward_recorder_db_path(config))
    plans = {
        context.market_id: store.load_source_plan(context.market_id)
        for context in contexts
    }
    feed_health = _feed_health_inventory(
        config,
        [
            plan
            for plan in plans.values()
            if plan is not None
        ],
        now,
    )
    rows = [
        _coverage_row(
            config,
            rule_store,
            context,
            scope=market_scope_decision(context, config.universe),
            monitored=context.market_id in monitored_ids,
            recorded=context.market_id in recorded_ids,
            capture=capture.get(context.market_id, {}),
            plan=plans.get(context.market_id),
            feed_health=feed_health,
            now=now,
        )
        for context in contexts
    ]
    recorded_rows = [row for row in rows if row["recorded"]]
    summary = {
        "contexts": len(rows),
        "recorded": len(recorded_rows),
        "monitored": len(monitored_ids),
        "scope": dict(Counter(row["scope_status"] for row in rows)),
        "recorded_states": dict(
            Counter(row["state"] for row in recorded_rows)
        ),
        "book_capture_ready": sum(
            bool(row["book_capture_ready"]) for row in recorded_rows
        ),
        "rule_ready": sum(bool(row["rule_ready"]) for row in recorded_rows),
        "source_plan_ready": sum(
            bool(row["source_plan_ready"]) for row in recorded_rows
        ),
        "source_health_ready": sum(
            bool(row["source_health_ready"]) for row in recorded_rows
        ),
        "legacy_source_plans": sum(
            row["source_plan_status"] == "LEGACY_PRE_RULESPEC"
            for row in rows
        ),
        "evidence_ready": sum(
            bool(row["evidence_ready"]) for row in recorded_rows
        ),
        "paper_execution_ready": sum(
            bool(row["paper_execution_ready"]) for row in recorded_rows
        ),
        "rule_families": dict(
            Counter(
                row["rule_family"]
                for row in recorded_rows
                if row["rule_family"]
            )
        ),
    }
    report = {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "summary": summary,
        "markets": sorted(
            rows,
            key=lambda row: (
                not row["recorded"],
                not row["monitored"],
                -float(row["volume"]),
                row["market_id"],
            ),
        ),
    }
    stable = {
        key: value for key, value in report.items() if key != "generated_at"
    }
    report["report_sha256"] = sha256_json(stable)
    if persist:
        _atomic_json_write(
            config.data_dir / "semantic_coverage.json",
            report,
        )
    return report


def _coverage_row(
    config: DiscoveryConfig,
    rule_store: RuleStore,
    context: MarketContext,
    *,
    scope,
    monitored: bool,
    recorded: bool,
    capture: dict[str, Any],
    plan: SourcePlan | None,
    feed_health: dict[str, dict[str, object]],
    now: datetime,
) -> dict[str, Any]:
    blockers: list[str] = []
    spec_error = ""
    try:
        spec = rule_store.load_spec(
            context.market_id,
            context.rule_text_sha256,
        )
    except (TypeError, ValueError) as exc:
        spec = None
        spec_error = f"{type(exc).__name__}: {exc}"
        blockers.append(f"current_rule_spec_invalid:{spec_error}")
    rule_ready = spec is not None
    if not rule_ready and not spec_error:
        blockers.append("current_rule_spec_missing")

    plan_status = plan.semantic_status if plan is not None else "MISSING"
    source_ready = False
    source_error = ""
    if spec is not None and plan is not None:
        try:
            validate_source_plan_freshness(context, plan, spec)
            source_ready = True
        except ValueError as exc:
            source_error = str(exc)
    if not source_ready:
        if plan is None:
            blockers.append("current_source_plan_missing")
        elif spec is None:
            blockers.append(
                f"source_plan_not_current:{plan.semantic_status}"
            )
        else:
            blockers.append(f"source_plan_invalid:{source_error}")
    source_health = _source_health(plan, feed_health)
    source_health_ready = bool(
        source_ready and source_health["ready"]
    )
    if source_ready and not source_health_ready:
        blockers.extend(
            str(item) for item in source_health["blockers"]
        )

    book_age = _age_seconds(capture.get("latest_book_at"), now)
    book_fresh = (
        book_age is not None
        and book_age
        <= config.rule_runner.paper_max_book_age_seconds
    )
    book_ready = bool(
        recorded
        and capture.get("capture_binding_sha256")
        and capture.get("latest_book_at")
    )
    if recorded and not book_ready:
        blockers.append("capture_book_missing")

    fee_blockers = _fee_blockers(config, context, now)
    if fee_blockers:
        blockers.append(
            f"fee_readiness_failed:{len(fee_blockers)}"
        )
    family = spec.semantics.rule_family if spec is not None else ""
    family_ready = family in {
        item.strip().upper()
        for item in config.rule_runner.paper_execution_families
    }
    if rule_ready and not family_ready:
        blockers.append(f"paper_family_not_enabled:{family}")

    activity = (
        rule_store.market_activity_status(
            context.market_id,
            spec.spec_sha256,
        )
        if spec is not None
        else {
            "evidence_claims": 0,
            "extraction_passes": 0,
            "evaluations": 0,
            "decision_proofs": 0,
        }
    )
    evidence_ready = rule_ready and source_ready and source_health_ready
    paper_ready = bool(
        context.state in TRADEABLE_STATES
        and evidence_ready
        and family_ready
        and not fee_blockers
        and book_fresh
    )
    return {
        "market_id": context.market_id,
        "title": context.event_title or context.question,
        "state": context.state,
        "state_reasons": context.state_reasons,
        "volume": context.volume,
        "liquidity": context.liquidity,
        "deadline_iso": context.deadline_iso,
        "scope_status": scope.status,
        "scope_reason": scope.reason,
        "monitored": monitored,
        "recorded": recorded,
        "context_sha256": sha256_json(_context_identity(context)),
        "capture_binding_sha256": str(
            capture.get("capture_binding_sha256") or ""
        ),
        "semantic_binding_sha256": str(
            capture.get("semantic_binding_sha256") or ""
        ),
        "capture_semantic_match": bool(
            capture.get("capture_semantic_match")
        ),
        "latest_book_at": str(capture.get("latest_book_at") or ""),
        "book_age_seconds": (
            round(book_age, 3) if book_age is not None else None
        ),
        "book_capture_ready": book_ready,
        "book_fresh": book_fresh,
        "rule_ready": rule_ready,
        "rule_spec_error": spec_error,
        "rule_spec_sha256": spec.spec_sha256 if spec is not None else "",
        "rule_family": family,
        "source_plan_status": plan_status,
        "source_plan_ready": source_ready,
        "source_health_ready": source_health_ready,
        "source_health": source_health,
        "source_plan_sha256": (
            source_plan_sha256(plan) if plan is not None else ""
        ),
        "source_records": len(plan.source_records) if plan is not None else 0,
        "feed_urls": len(plan.feed_urls) if plan is not None else 0,
        "poll_urls": len(plan.poll_urls) if plan is not None else 0,
        "missing_required_sources": (
            plan.missing_required_source_refs if plan is not None else []
        ),
        "evidence_ready": evidence_ready,
        "paper_execution_ready": paper_ready,
        "fee_ready": not fee_blockers,
        "fee_blocker_count": len(fee_blockers),
        "fee_blockers": fee_blockers,
        **activity,
        "blockers": blockers,
    }


def _feed_health_inventory(
    config: DiscoveryConfig,
    plans: list[SourcePlan],
    now: datetime,
) -> dict[str, dict[str, object]]:
    urls = {
        url
        for plan in plans
        for source in plan.source_records
        for url in (*source.feed_urls, *source.poll_urls)
        if url
    }
    path = central_feed_db_path(config)
    if not config.central_feed.enabled or not path.exists() or not urls:
        return {}
    maximum_age = max(
        config.central_feed.stale_after_seconds,
        config.central_feed.direct_poll_seconds * 3.0,
        config.central_feed.aggregator_poll_seconds * 3.0,
    )
    try:
        return CentralFeedStore(path).feed_health(
            urls,
            stale_after_seconds=maximum_age,
            now=now,
        )
    except (OSError, sqlite3.Error):
        return {}


def _source_health(
    plan: SourcePlan | None,
    feed_health: dict[str, dict[str, object]],
) -> dict[str, Any]:
    if plan is None:
        return {
            "ready": False,
            "healthy_confirmation_groups": 0,
            "required_confirmation_groups": 0,
            "sources": [],
            "blockers": ["source_plan_missing"],
        }
    sources: list[dict[str, Any]] = []
    healthy_groups: set[str] = set()
    policy = dict(plan.source_policy or {})
    policy_ids = {
        str(item)
        for item in policy.get("requirement_ids", [])
    }
    healthy_requirement_ids: set[str] = set()
    healthy_requirement_groups: dict[str, set[str]] = {}
    blockers: list[str] = []
    for source in plan.source_records:
        endpoints = list(
            dict.fromkeys([*source.feed_urls, *source.poll_urls])
        )
        endpoint_states = {
            url: str(feed_health.get(url, {}).get("status") or "NOT_POLLED")
            for url in endpoints
        }
        healthy_endpoints = [
            url
            for url in endpoints
            if bool(feed_health.get(url, {}).get("healthy"))
        ]
        semantic = bool(
            set(source.roles) & {"CONFIRMATION", "SETTLEMENT"}
        ) and source.source_tier not in {
            "aggregator",
            "state_affiliated_press",
        }
        if semantic and healthy_endpoints:
            healthy_groups.add(source.independence_group)
            healthy_requirement_ids.update(source.requirement_ids)
            for requirement_id in source.requirement_ids:
                healthy_requirement_groups.setdefault(
                    requirement_id,
                    set(),
                ).add(source.independence_group)
        if (
            source.required
            and not healthy_endpoints
            and not (set(source.requirement_ids) & policy_ids)
        ):
            blockers.append(
                f"required_source_unhealthy:{source.source_id}"
            )
        sources.append(
            {
                "source_id": source.source_id,
                "domain": source.domain,
                "roles": source.roles,
                "required": source.required,
                "requirement_ids": source.requirement_ids,
                "endpoint_statuses": endpoint_states,
                "healthy_endpoints": len(healthy_endpoints),
            }
        )
    policy_type = str(policy.get("policy_type") or "")
    quorum = int(policy.get("quorum") or 0)
    healthy_policy_ids = healthy_requirement_ids & policy_ids
    if not policy_type or not policy_ids or quorum < 1:
        blockers.append("source_policy_missing_or_invalid")
    elif policy_type == "ANY_OF":
        if not healthy_policy_ids:
            blockers.append("source_policy_any_of_unsatisfied")
    elif policy_type == "ALTERNATIVE_QUORUM":
        branches = policy.get("branches", [])
        branch_ready = False
        if not isinstance(branches, list) or len(branches) < 2:
            blockers.append("source_policy_alternative_quorum_invalid")
        else:
            for branch in branches:
                if not isinstance(branch, dict):
                    continue
                branch_ids = {
                    str(item)
                    for item in branch.get("requirement_ids", [])
                }
                requirement_quorum = int(
                    branch.get("requirement_quorum") or 0
                )
                source_minimum = int(
                    branch.get("minimum_independent_sources") or 0
                )
                groups = {
                    group
                    for requirement_id in branch_ids
                    for group in healthy_requirement_groups.get(
                        requirement_id,
                        set(),
                    )
                }
                if (
                    len(branch_ids & healthy_requirement_ids)
                    >= requirement_quorum
                    and len(groups) >= source_minimum
                ):
                    branch_ready = True
                    break
            if not branch_ready:
                blockers.append(
                    "source_policy_alternative_quorum_unsatisfied"
                )
    elif policy_type == "ALL_OF":
        missing = sorted(policy_ids - healthy_policy_ids)
        if missing:
            blockers.append(
                "source_policy_all_of_unsatisfied:" + ",".join(missing)
            )
    elif policy_type == "QUORUM":
        if len(healthy_policy_ids) < quorum:
            blockers.append(
                "source_policy_quorum_unsatisfied:"
                f"{len(healthy_policy_ids)}<{quorum}"
            )
    elif policy_type in {
        "PRIMARY_WITH_FALLBACK",
        "CONDITIONAL_FALLBACK",
    }:
        primary = {
            str(item)
            for item in policy.get("primary_requirement_ids", [])
        }
        fallback = {
            str(item)
            for item in policy.get("fallback_requirement_ids", [])
        }
        if len(healthy_requirement_ids & primary) < quorum:
            condition = str(policy.get("fallback_condition") or "")
            if condition != "SOURCE_UNAVAILABLE":
                blockers.append(
                    "source_policy_fallback_condition_unverified:"
                    + (condition or "missing")
                )
            elif len(healthy_requirement_ids & fallback) < quorum:
                blockers.append("source_policy_fallback_unsatisfied")
    else:
        blockers.append(f"source_policy_unsupported:{policy_type}")
    required_groups = int(plan.minimum_independent_confirmations)
    if len(healthy_groups) < required_groups:
        blockers.append(
            "healthy_confirmation_groups_below_minimum:"
            f"{len(healthy_groups)}<{required_groups}"
        )
    return {
        "ready": not blockers,
        "healthy_confirmation_groups": len(healthy_groups),
        "required_confirmation_groups": required_groups,
        "source_policy_type": policy_type,
        "healthy_policy_requirements": len(healthy_policy_ids),
        "required_policy_requirements": len(policy_ids),
        "sources": sources,
        "blockers": blockers,
    }


def _fee_blockers(
    config: DiscoveryConfig,
    context: MarketContext,
    now: datetime,
) -> list[str]:
    blockers: list[str] = []
    for outcome in context.outcomes:
        if outcome.fee_schedule is None:
            blockers.append(
                f"fee_schedule_missing:{outcome.name}"
            )
            continue
        blockers.extend(
            f"{item}:{outcome.name}"
            for item in outcome.fee_schedule.entry_blockers(
                as_of=now,
                max_age_hours=(
                    config.rule_runner.max_fee_schedule_age_hours
                ),
            )
        )
    return blockers


def _context_identity(context: MarketContext) -> dict[str, Any]:
    raw = context.as_dict()
    for key in (
        "state",
        "state_reasons",
        "scores",
        "correlation_group",
        "discovered_at",
        "updated_at",
        "volume",
        "liquidity",
    ):
        raw.pop(key, None)
    return raw


def _capture_inventory(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    connection = sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        bindings = connection.execute(
            """
            SELECT binding_sha256, binding_kind, market_id, context_json,
                   recorder_policy_sha256, created_at
            FROM bindings
            ORDER BY created_at, binding_sha256
            """
        ).fetchall()
        inventory: dict[str, dict[str, Any]] = {}
        for row in bindings:
            market_id = str(row["market_id"])
            item = inventory.setdefault(market_id, {})
            prefix = (
                "capture"
                if str(row["binding_kind"]) == "BOOK_CAPTURE"
                else "semantic"
            )
            item[f"{prefix}_binding_sha256"] = str(
                row["binding_sha256"]
            )
            item[f"{prefix}_context_json"] = str(row["context_json"])
            item[f"{prefix}_policy"] = str(
                row["recorder_policy_sha256"]
            )
        for market_id, item in inventory.items():
            capture_binding = item.get("capture_binding_sha256")
            if capture_binding:
                latest = connection.execute(
                    """
                    SELECT MAX(received_at) AS latest
                    FROM book_events WHERE binding_sha256=?
                    """,
                    (capture_binding,),
                ).fetchone()
                item["latest_book_at"] = str(latest["latest"] or "")
            item["capture_semantic_match"] = bool(
                item.get("capture_context_json")
                and item.get("capture_context_json")
                == item.get("semantic_context_json")
                and item.get("capture_policy")
                == item.get("semantic_policy")
            )
        return inventory
    finally:
        connection.close()


def _age_seconds(raw: Any, now: datetime) -> float | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - parsed).total_seconds())


__all__ = ["build_semantic_coverage"]
