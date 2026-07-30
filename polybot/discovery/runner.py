from __future__ import annotations

import hashlib
import inspect
import json
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from polybot.core.holdings import _atomic_json_write
from polybot.log import log_event

from .allocator import PortfolioAllocator
from .config import (
    DiscoveryConfig,
    central_feed_db_path,
    classifier_budget_db_path,
    forward_recorder_db_path,
    load_discovery_config,
    opportunity_reachability,
    rule_store_db_path,
)
from .context import build_rule_analyzer
from .emit import emit_bot_config
from .gamma_universe import (
    context_from_event,
    enumerate_active_events,
    is_geopolitical_candidate,
    merge_refresh,
)
from .opportunity import QuoteProviderProtocol, scan_group_arbitrage, scan_opportunities
from .scorer import correlation_group, grade_market
from .sources import build_source_plan, validate_source_plan_freshness
from .store import DiscoveryStore
from .types import (
    SOURCE_PLAN_CURRENT,
    SOURCE_PLAN_LEGACY,
    MarketContext,
    TRADEABLE_STATES,
)


def _load(config_path: Path) -> tuple[DiscoveryConfig, DiscoveryStore, PortfolioAllocator]:
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    allocator = PortfolioAllocator(config.data_dir / "allocations.json", config.allocator)
    # Persist the caps into the ledger so executor-side PortfolioLinks enforce
    # exactly the limits this pipeline run was configured with.
    allocator.write_caps()
    return config, store, allocator


def discover_markets_command(
    config_path: Path,
    *,
    events_fetch: Callable[[str, dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> int:
    """Stage 1-2: enumerate the active universe, keep geopolitical candidates,
    and build/refresh a durable context record per market. A changed rule hash
    drops the old analysis and demotes the market to RULES_REVIEW_REQUIRED."""
    config, store, _ = _load(config_path)
    enumeration = enumerate_active_events(
        config.universe,
        fetch=events_fetch,
    )
    events = enumeration.events
    coverage_path = store.save_coverage_manifest(enumeration.manifest)
    rejected: Counter[str] = Counter()
    new_count = refreshed = 0
    candidates = 0
    enumerated_event_keys: list[str] = []
    candidate_event_keys: list[str] = []
    candidate_market_ids: list[str] = []
    observed_market_ids: list[str] = []
    rejection_by_event: dict[str, str] = {}
    for event in events:
        event_key = (
            f"id:{str(event.get('id') or '').strip()}"
            if str(event.get("id") or "").strip()
            else f"slug:{str(event.get('slug') or '').strip()}"
        )
        enumerated_event_keys.append(event_key)
        candidate, reason = is_geopolitical_candidate(event, config.universe)
        if not candidate:
            rejected[reason.split(":")[0]] += 1
            rejection_by_event[event_key] = reason.split(":")[0]
            continue
        candidates += 1
        candidate_event_keys.append(event_key)
        fresh = context_from_event(event)
        if fresh is None:
            rejected["unparseable_event"] += 1
            rejection_by_event[event_key] = "unparseable_event"
            continue
        candidate_market_ids.append(fresh.market_id)
        observed_market_ids.append(fresh.market_id)
        if fresh.liquidity < config.universe.min_liquidity and fresh.volume < config.universe.min_volume:
            rejected["below_liquidity_and_volume_floor"] += 1
            rejection_by_event[event_key] = (
                "below_liquidity_and_volume_floor"
            )
            continue
        existing = store.load_context(fresh.market_id)
        if existing is None:
            store.save_context(fresh)
            new_count += 1
        else:
            store.save_context(merge_refresh(existing, fresh))
            refreshed += 1
    stable_scan = {
        "coverage_sha256": enumeration.manifest["coverage_sha256"],
        "coverage_status": enumeration.manifest["coverage_status"],
        "raw_events": enumeration.manifest["raw_events"],
        "unique_events": enumeration.manifest["unique_events"],
        "candidate_events": candidates,
        "enumerated_event_keys": sorted(set(enumerated_event_keys)),
        "candidate_event_keys": sorted(set(candidate_event_keys)),
        "candidate_market_ids": sorted(set(candidate_market_ids)),
        "observed_market_ids": sorted(set(observed_market_ids)),
        "rejection_by_event": dict(sorted(rejection_by_event.items())),
        "new_contexts": new_count,
        "refreshed_contexts": refreshed,
        "rejected": dict(sorted(rejected.items())),
    }
    stable_scan["scan_sha256"] = hashlib.sha256(
        json.dumps(
            stable_scan,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    _atomic_json_write(config.data_dir / "discovery_scan.json", stable_scan)
    summary = {
        **stable_scan,
        "coverage_complete": enumeration.manifest["coverage_complete"],
        "coverage_manifest": str(coverage_path),
        "views": enumeration.manifest["views"],
        "truncated": enumeration.manifest["truncated"],
        "events_enumerated": len(events),
        "total_contexts": len(store.all_contexts()),
    }
    log_event("discovery_universe_scan", **summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    if (
        config.universe.fail_on_truncation
        and not enumeration.manifest["coverage_complete"]
    ):
        return 1
    return 0


def grade_markets_command(
    config_path: Path,
    *,
    analyzer=None,
    require_semantic_assets: bool | None = None,
    market_ids: set[str] | None = None,
) -> int:
    """Stage 3: run the rule/context analyzer where missing, then grade every
    market and assign its state. Two passes so correlation-group counts are
    computed over provisionally eligible markets."""
    config, store, _ = _load(config_path)
    if require_semantic_assets is None:
        require_semantic_assets = config.rule_compiler.enabled
    analyzer = analyzer or build_rule_analyzer(config.classifier)
    contexts = store.all_contexts()
    if market_ids is not None:
        contexts = [
            context
            for context in contexts
            if context.market_id in market_ids
        ]
    rule_store = None
    rule_store_error = ""
    if config.rule_compiler.enabled:
        try:
            from polybot.rules.store import RuleStore

            rule_store = RuleStore(rule_store_db_path(config))
        except Exception as exc:
            rule_store_error = str(exc)
            log_event("rule_store_unavailable_for_grading", error=str(exc))

    analyzed: list[MarketContext] = []
    analysis_failures = 0
    for context in contexts:
        if context.rule_analysis is None and len(context.rule_text.strip()) >= config.scoring.min_rule_text_chars:
            try:
                analysis = analyzer.analyze(context)
                context = MarketContext.from_dict({**context.as_dict(), "rule_analysis": analysis.as_dict()})
            except Exception as exc:
                analysis_failures += 1
                log_event("discovery_rule_analysis_failed", market_id=context.market_id, error=str(exc))
        analyzed.append(context)

    def grade(context: MarketContext, **kwargs: Any) -> MarketContext:
        rule_spec = None
        if rule_store is not None:
            try:
                rule_spec = rule_store.load_spec(
                    context.market_id,
                    context.rule_text_sha256,
                )
            except Exception as exc:
                log_event(
                    "rule_spec_load_failed_for_grading",
                    market_id=context.market_id,
                    error=str(exc),
                )
        plan = (
            store.load_source_plan(context.market_id)
            if config.rule_compiler.enabled
            else None
        )
        graded = grade_market(
            context,
            config.scoring,
            rule_spec=rule_spec,
            source_plan=plan,
            require_rule_spec=require_semantic_assets,
            paper_families={
                item.strip().upper()
                for item in config.rule_compiler.paper_families
            },
            live_confirmation_families={
                item.strip().upper()
                for item in config.rule_compiler.live_confirmation_families
            },
            **kwargs,
        )
        if rule_store_error and require_semantic_assets:
            graded = MarketContext.from_dict(
                {
                    **graded.as_dict(),
                    "state": "RULES_REVIEW_REQUIRED",
                    "state_reasons": [
                        f"rule_store_unavailable:{rule_store_error}"
                    ],
                }
            )
        return graded

    provisional = [grade(context) for context in analyzed]
    group_counts: Counter[str] = Counter(
        correlation_group(context) for context in provisional if context.state in TRADEABLE_STATES
    )
    selected_live_ids = _select_correlation_group_live_markets(
        provisional,
        config.scoring.max_markets_per_correlation_group,
    )
    selected_live_counts: Counter[str] = Counter(
        correlation_group(context)
        for context in provisional
        if context.market_id in selected_live_ids
    )
    provisional_states = {context.market_id: context.state for context in provisional}
    states: Counter[str] = Counter()
    for context in analyzed:
        # Correlation concentration is a deterministic selection, not an
        # all-or-nothing demotion. With a limit of two and three otherwise-live
        # markets, retain the two strongest and demote only the third.
        group = correlation_group(context)
        others = dict(selected_live_counts)
        if context.market_id in selected_live_ids:
            others[group] = max(0, others.get(group, 0) - 1)
        elif provisional_states.get(context.market_id) == "LIVE_CONFIRMATION_ELIGIBLE":
            others[group] = max(
                others.get(group, 0),
                config.scoring.max_markets_per_correlation_group,
            )
        graded = grade(context, group_counts=others)
        store.save_context(graded)
        states[graded.state] += 1
    summary = {"states": dict(states), "analysis_failures": analysis_failures, "correlation_groups": dict(group_counts)}
    log_event("discovery_grading", **summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def compile_rules_command(
    config_path: Path,
    market_id: str | None = None,
    *,
    compiler=None,
    market_ids: set[str] | None = None,
) -> int:
    """Compile current rule versions with two-pass agreement and strict binding."""

    config, store, _ = _load(config_path)
    if not config.rule_compiler.enabled:
        raise SystemExit(
            "rule compiler is disabled; set rule_compiler.enabled: true"
        )
    from polybot.core.budget import ClassifierBudgetStore
    from polybot.rules.compiler import RuleCompiler
    from polybot.rules.store import RuleStore

    rule_store = RuleStore(rule_store_db_path(config))
    if compiler is None:
        budget_store = None
        limits = config.classifier
        if config.classifier_budget.enabled:
            budget_path = classifier_budget_db_path(config)
            budget_store = ClassifierBudgetStore(
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
        compiler = RuleCompiler(
            config.classifier,
            rule_store,
            budget_store=budget_store,
            budget_limits=limits,
        )

    contexts = store.all_contexts()
    if market_id:
        contexts = [
            context for context in contexts
            if context.market_id == market_id
        ]
        if not contexts:
            raise SystemExit(f"unknown market_id {market_id!r}")
    elif market_ids is not None:
        contexts = [
            context
            for context in contexts
            if context.market_id in market_ids
        ]
    from .profit_priority import load_priority_snapshot, priority_sort_key

    priorities = load_priority_snapshot(config.data_dir)
    contexts = sorted(
        contexts,
        key=lambda item: priority_sort_key(item, priorities),
    )

    results: list[dict[str, Any]] = []
    new_compilations = 0
    for context in contexts:
        if len(context.rule_text.strip()) < config.scoring.min_rule_text_chars:
            _mark_rule_review_required(
                store,
                context,
                "missing_or_short_resolution_rules",
            )
            results.append(
                {
                    "market_id": context.market_id,
                    "status": "SKIPPED",
                    "reason": "missing_or_short_resolution_rules",
                }
            )
            continue
        try:
            cached = rule_store.load_spec(
                context.market_id,
                context.rule_text_sha256,
            )
        except Exception as exc:
            _mark_rule_review_required(
                store,
                context,
                f"rule_store_error:{exc}",
            )
            results.append(
                {
                    "market_id": context.market_id,
                    "status": "ERROR",
                    "reason": str(exc),
                }
            )
            continue
        if (
            cached is None
            and new_compilations >= config.rule_compiler.max_per_cycle
        ):
            results.append(
                {
                    "market_id": context.market_id,
                    "status": "DEFERRED",
                    "reason": "rule_compilation_deferred_cycle_limit",
                }
            )
            continue
        if cached is None:
            new_compilations += 1
        try:
            priority = priorities.get(context.market_id)
            compile_method = compiler.compile
            parameters = inspect.signature(compile_method).parameters
            accepts_budget_metadata = (
                "budget_purpose" in parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
            )
            if accepts_budget_metadata:
                result = compile_method(
                    context,
                    budget_purpose="system",
                    priority_score_sha256=(
                        priority.score_sha256 if priority is not None else ""
                    ),
                )
            else:
                result = compile_method(context)
            results.append(result.as_dict())
            if result.spec is None:
                _mark_rule_review_required(
                    store,
                    context,
                    f"rule_compilation_{result.status.casefold()}:{result.reason}",
                )
        except Exception as exc:
            _mark_rule_review_required(
                store,
                context,
                f"rule_compiler_error:{exc}",
            )
            results.append(
                {
                    "market_id": context.market_id,
                    "status": "ERROR",
                    "reason": str(exc),
                }
            )
            log_event(
                "rule_compilation_failed_closed",
                market_id=context.market_id,
                error=str(exc),
            )
    for item in results:
        selected_market_id = str(item.get("market_id") or "")
        priority = priorities.get(selected_market_id)
        item["selection_reason"] = (
            "cached_validation"
            if item.get("status") == "CACHED"
            else "profit_priority"
            if priority is not None
            else "deterministic_age_market_fallback"
        )
        item["monitor_priority"] = (
            priority.monitor_priority if priority is not None else None
        )
        item["priority_score_sha256"] = (
            priority.score_sha256 if priority is not None else None
        )
    summary = {
        "compiled": sum(
            1
            for item in results
            if item.get("status") == "COMPILED"
        ),
        "cached": sum(
            1 for item in results if item.get("status") == "CACHED"
        ),
        "deferred": sum(
            1 for item in results if item.get("status") == "DEFERRED"
        ),
        "failed": sum(
            1
            for item in results
            if item.get("status")
            in {"INVALID", "DISAGREEMENT", "BUDGET_BLOCKED", "ERROR"}
        ),
        "results": results,
    }
    log_event(
        "rule_compilation_cycle",
        compiled=summary["compiled"],
        cached=summary["cached"],
        deferred=summary["deferred"],
        failed=summary["failed"],
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def inspect_rule_command(config_path: Path, market_id: str) -> int:
    config, store, _ = _load(config_path)
    context = store.load_context(market_id)
    if context is None:
        raise SystemExit(f"unknown market_id {market_id!r}")
    from polybot.rules.store import RuleStore

    rule_store = RuleStore(rule_store_db_path(config))
    spec = rule_store.load_spec(market_id, context.rule_text_sha256)
    from .coverage import build_semantic_coverage

    readiness = next(
        (
            row
            for row in build_semantic_coverage(config)["markets"]
            if row["market_id"] == market_id
        ),
        {},
    )
    payload = {
        "market_id": market_id,
        "current_rule_text_sha256": context.rule_text_sha256,
        "spec_sha256": spec.spec_sha256 if spec else None,
        "spec": spec.as_dict() if spec else None,
        "passes": rule_store.compilation_passes(
            market_id,
            context.rule_text_sha256,
        ),
        "semantic_readiness": readiness,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def validate_rule_command(spec_path: Path) -> int:
    from polybot.rules.contracts import RuleSpec

    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    spec = RuleSpec.from_dict(raw)
    print(
        json.dumps(
            {
                "valid": True,
                "market_id": spec.market_id,
                "rule_family": spec.semantics.rule_family,
                "spec_sha256": spec.spec_sha256,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _mark_rule_review_required(
    store: DiscoveryStore,
    context: MarketContext,
    reason: str,
) -> None:
    store.save_context(
        MarketContext.from_dict(
            {
                **context.as_dict(),
                "state": "RULES_REVIEW_REQUIRED",
                "state_reasons": [reason],
            }
        )
    )


def _select_correlation_group_live_markets(
    provisional: list[MarketContext],
    limit: int,
) -> set[str]:
    """Select at most ``limit`` otherwise-live markets per correlation group.

    Rule quality determines the retained markets; market id is the stable
    tie-breaker. Liquidity is intentionally absent because book depth sizes
    orders elsewhere and is not the strategy-selection thesis.
    """
    grouped: dict[str, list[MarketContext]] = {}
    for context in provisional:
        if context.state != "LIVE_CONFIRMATION_ELIGIBLE":
            continue
        grouped.setdefault(correlation_group(context), []).append(context)
    selected: set[str] = set()
    for contexts in grouped.values():
        ranked = sorted(
            contexts,
            key=lambda context: (
                -(
                    context.scores.get("rule_clarity", 0.0)
                    + context.scores.get("evidence_observability", 0.0)
                    + context.scores.get("automation_suitability", 0.0)
                    - context.scores.get("resolution_risk", 0.0)
                ),
                context.market_id,
            ),
        )
        selected.update(context.market_id for context in ranked[: max(0, limit)])
    return selected


def plan_sources_command(config_path: Path, market_id: str | None = None) -> int:
    """Stage 4: derive a per-market source plan from the context package for
    every tradeable market (or one named market)."""
    config, store, _ = _load(config_path)
    rule_store = None
    if config.rule_compiler.enabled:
        from polybot.rules.store import RuleStore

        rule_store = RuleStore(rule_store_db_path(config))
    legacy_archived = (
        store.quarantine_legacy_source_plans()
        if config.rule_compiler.enabled
        else 0
    )
    contexts = store.all_contexts()
    if market_id:
        contexts = [c for c in contexts if c.market_id == market_id]
        if not contexts:
            raise SystemExit(f"unknown market_id {market_id!r}")
    planned, skipped = [], []
    for context in contexts:
        if (
            market_id is None
            and not config.rule_compiler.enabled
            and context.state not in TRADEABLE_STATES
        ):
            continue
        rule_spec = None
        if rule_store is not None:
            try:
                rule_spec = rule_store.load_spec(
                    context.market_id,
                    context.rule_text_sha256,
                )
            except Exception as exc:
                _mark_rule_review_required(
                    store,
                    context,
                    f"rule_store_error:{exc}",
                )
                skipped.append(
                    {
                        "market_id": context.market_id,
                        "reason": f"rule_store_error:{exc}",
                    }
                )
                continue
            if rule_spec is None:
                _mark_rule_review_required(
                    store,
                    context,
                    "valid_rule_spec_missing",
                )
                skipped.append(
                    {
                        "market_id": context.market_id,
                        "reason": "valid_rule_spec_missing",
                    }
                )
                continue
        try:
            plan = build_source_plan(context, rule_spec)
        except ValueError as exc:
            skipped.append({"market_id": context.market_id, "reason": str(exc)})
            if config.rule_compiler.enabled:
                _mark_rule_review_required(
                    store,
                    context,
                    f"source_plan_failed:{exc}",
                )
            continue
        store.save_source_plan(plan)
        planned.append(
            {
                "market_id": context.market_id,
                "rule_spec_sha256": plan.rule_spec_sha256 or None,
                "feeds": len(plan.feed_urls),
                "sources": len(plan.source_records),
                "auto_trade_domains": plan.auto_trade_domains,
                "missing_required_sources": (
                    plan.missing_required_source_refs
                ),
            }
        )
    print(
        json.dumps(
            {
                "legacy_plans_archived": legacy_archived,
                "planned": planned,
                "skipped": skipped,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def scan_opportunities_command(config_path: Path, *, quotes: QuoteProviderProtocol | None = None) -> int:
    """Stage 5-6: price every eligible outcome against its estimated
    probability, run the result through the portfolio allocator preview, and
    persist the scan for the funnel report."""
    config, store, allocator = _load(config_path)
    contexts = store.all_contexts()
    quotes = quotes or _live_quotes(contexts)
    from .calibration import CalibrationLog

    calibration = CalibrationLog(config.data_dir)
    forecast_calibrated = calibration.forecast_calibrated()
    opportunities = scan_opportunities(
        contexts,
        config.opportunity,
        quotes,
        allocator,
        forecast_calibrated=forecast_calibrated,
    )
    # Every scan feeds the calibration loop: what we believed, what the
    # market believed, same instant. Resolutions score both later.
    calibration.record_estimates(opportunities)
    group_arbitrage = scan_group_arbitrage(contexts, config.opportunity, quotes)
    payload = [item.as_dict() for item in opportunities]
    model_pricing = {
        **opportunity_reachability(config.opportunity),
        "forecast_calibrated": forecast_calibrated,
    }
    _atomic_json_write(
        config.data_dir / "opportunities.json",
        {
            "opportunities": payload,
            "group_arbitrage": group_arbitrage,
            "model_pricing": model_pricing,
        },
    )
    # opportunities.json is a snapshot (overwritten every cycle); the history
    # file is the time series -- how long edges persist, what spreads do
    # around events -- and is what post-soak analysis actually studies.
    from polybot.core.storage import append_jsonl

    scanned_at = datetime.now(timezone.utc).isoformat()
    history_path = config.data_dir / "scan_history.jsonl"
    for item in payload:
        append_jsonl(history_path, {"at": scanned_at, "kind": "opportunity", **item})
    for arb in group_arbitrage:
        append_jsonl(history_path, {"at": scanned_at, "kind": "group_arbitrage", **arb})
    executable = [item for item in opportunities if not item.blockers]
    print(
        json.dumps(
            {
                "scanned_outcomes": len(opportunities),
                "executable": [item.as_dict() for item in executable],
                "group_arbitrage": group_arbitrage,
                "model_pricing": model_pricing,
                "blocked": Counter(blocker.split(":")[0] for item in opportunities for blocker in item.blockers),
            },
            indent=2,
            sort_keys=True,
            default=dict,
        )
    )
    return 0


def emit_bot_config_command(config_path: Path, market_id: str, out: Path | None = None) -> int:
    """Stage 7 handoff: render a ready-to-review executor config (binary or
    location bot) for one eligible market. The existing engines remain the
    final execution component; nothing is armed by emission."""
    config, store, allocator = _load(config_path)
    context = store.load_context(market_id)
    if context is None:
        raise SystemExit(f"unknown market_id {market_id!r}")
    if context.state not in TRADEABLE_STATES:
        raise SystemExit(f"market {market_id} is {context.state}; only PAPER/LIVE-eligible markets can be emitted")
    plan = store.load_source_plan(market_id)
    if plan is None:
        raise SystemExit(f"market {market_id} has no source plan; run plan-sources first")
    if config.rule_compiler.enabled:
        from polybot.rules.store import RuleStore

        spec = RuleStore(rule_store_db_path(config)).load_spec(
            market_id,
            context.rule_text_sha256,
        )
        if spec is None:
            raise SystemExit(
                f"market {market_id} has no current RuleSpec; run compile-rules"
            )
        try:
            validate_source_plan_freshness(context, plan, spec)
        except ValueError as exc:
            raise SystemExit(
                f"market {market_id} has stale semantic assets: {exc}"
            ) from exc
    out = out or Path("configs/geopolitics/generated") / f"{_safe(market_id)}.yaml"
    entry_usd = allocator.config.per_order_usd
    recommended = context.scores.get("recommended_max_order_usd")
    if recommended:
        entry_usd = min(entry_usd, float(recommended))
    path = emit_bot_config(
        context,
        plan,
        entry_usd=entry_usd,
        out_path=out,
        ledger_path=str(allocator.state_path),
        classifier_provider=config.classifier.provider if config.classifier.provider != "rule_based" else "anthropic",
        classifier_model=config.classifier.model,
        classifier_cli_binary=config.classifier.cli_binary,
        classifier_cli_timeout_seconds=config.classifier.cli_timeout_seconds,
        classifier_budget_db=(
            str(classifier_budget_db_path(config))
            if config.classifier_budget.enabled
            else ""
        ),
        classifier_max_escalations_per_hour=config.classifier_budget.max_escalations_per_hour,
        classifier_max_escalations_per_day=config.classifier_budget.max_escalations_per_day,
        classifier_max_errors_per_hour=config.classifier_budget.max_classifier_errors_per_hour,
        central_feed_db=str(central_feed_db_path(config)) if config.central_feed.enabled else "",
        central_feed_stale_after_seconds=config.central_feed.stale_after_seconds,
    )
    print(json.dumps({"written": str(path), "kind": context.kind, "state": context.state}, indent=2))
    return 0


def reconcile_ledger_command(config_path: Path) -> int:
    """Ledger hygiene: prune stale daily buckets and free position slots for
    markets whose holdings files show flat."""
    _, _, allocator = _load(config_path)
    from .fleet import FleetManager  # is_holding path logic lives there

    from .config import load_discovery_config

    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    manager = FleetManager(config, store, live=False, per_order_usd=allocator.config.per_order_usd, ledger_path=str(allocator.state_path))
    state = allocator.reconcile(is_open=manager.is_holding)
    print(json.dumps({"open_positions": state.get("open_positions", []), "realized_net": state.get("realized_net", 0.0)}, indent=2, sort_keys=True))
    return 0


def record_resolution_command(config_path: Path, market_id: str, outcome: str, resolved: str) -> int:
    """Feed the calibration loop: record how one outcome actually resolved.
    Every resolution scores every probability source that ever priced it."""
    from .calibration import CalibrationLog

    config = load_discovery_config(config_path)
    if resolved.lower() not in {"yes", "no"}:
        raise SystemExit("--resolved must be 'yes' or 'no'")
    CalibrationLog(config.data_dir).record_resolution(market_id, outcome, resolved.lower() == "yes")
    print(json.dumps({"recorded": {"market_id": market_id, "outcome": outcome, "resolved_yes": resolved.lower() == "yes"}}, indent=2))
    return 0


def calibration_report_command(config_path: Path) -> int:
    """Score every probability source against resolved outcomes (Brier vs the
    market mid's own Brier on the same rows) and write calibration_status.json.
    The scan reads that status: forecast probabilities can't price allocatable
    opportunities until this report proves they beat the market."""
    from .calibration import CalibrationLog

    config = load_discovery_config(config_path)
    report = CalibrationLog(config.data_dir).report(min_resolved=config.opportunity.min_resolved_for_calibration)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def fleet_status_command(config_path: Path) -> int:
    """One consolidated view of everything an operator needs at 3am: global
    mode, positions and their heartbeats, ledger utilization and drawdown
    headroom, pending corroborations, calibration status, and the last scan's
    executable edges. Reads state files only -- no network, no side effects
    beyond the standard config load."""
    from .calibration import CalibrationLog
    from .fleet import (
        FleetManager,
        _forward_books_disabled_status,
        fleet_operator_dir,
    )

    config, store, allocator = _load(config_path)
    manager = FleetManager(config, store, live=False, per_order_usd=allocator.config.per_order_usd, ledger_path=str(allocator.state_path))

    snapshot = allocator.snapshot()
    realized = float(snapshot.get("realized_net", 0.0))
    drawdown_limit = allocator.config.max_drawdown_usd
    ledger = {
        "realized_net": realized,
        "max_drawdown_usd": drawdown_limit,
        "drawdown_headroom": round(drawdown_limit + realized, 2) if drawdown_limit > 0 else None,
        "open_positions": snapshot.get("open_positions", []),
        "total_spent": snapshot.get("total", 0.0),
        "total_cap": allocator.config.total_usd,
        "per_region": snapshot.get("per_region", {}),
    }

    global_mode_path = fleet_operator_dir() / "global_mode.json"
    global_mode: dict[str, Any] = {"mode": "unset"}
    if global_mode_path.exists():
        try:
            raw = json.loads(global_mode_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                global_mode = raw
        except (OSError, json.JSONDecodeError):
            global_mode = {"mode": "unreadable"}

    contexts = store.all_contexts()
    markets: list[dict[str, Any]] = []
    for context in contexts:
        holding = manager.is_holding(context.market_id)
        if context.state not in TRADEABLE_STATES and not holding:
            continue
        heartbeat_age = manager._heartbeat_age_seconds(context.market_id)
        markets.append(
            {
                "market_id": context.market_id,
                "state": context.state,
                "question": context.question[:100],
                "deadline": context.deadline_iso,
                "holding": holding,
                "heartbeat_age_seconds": round(heartbeat_age, 1) if heartbeat_age is not None else None,
            }
        )

    fleet_state: dict[str, Any] = {}
    fleet_state_path = config.data_dir / "fleet_state.json"
    if fleet_state_path.exists():
        try:
            raw = json.loads(fleet_state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                fleet_state = raw
        except (OSError, json.JSONDecodeError):
            fleet_state = {"error": "unreadable"}

    scan = _last_scan(config)
    executable = [o for o in scan if not o.get("blockers")]
    calibration = CalibrationLog(config.data_dir)
    central_feed_status: dict[str, Any] = fleet_state.get(
        "central_feed",
        {
            "enabled": config.central_feed.enabled,
            "path": str(central_feed_db_path(config)),
            "heartbeat": None,
        },
    )
    central_path = central_feed_db_path(config)
    if config.central_feed.enabled and central_path.exists():
        try:
            from polybot.core.central_feed import CentralFeedStore

            central_feed_status = {
                "enabled": True,
                **CentralFeedStore(central_path).status(),
            }
            heartbeat_raw = central_feed_status.get("heartbeat")
            if isinstance(heartbeat_raw, str):
                heartbeat = datetime.fromisoformat(heartbeat_raw.replace("Z", "+00:00"))
                if heartbeat.tzinfo is None:
                    heartbeat = heartbeat.replace(tzinfo=timezone.utc)
                age = max(0.0, (datetime.now(timezone.utc) - heartbeat).total_seconds())
                central_feed_status["heartbeat_age_seconds"] = round(age, 1)
                central_feed_status["healthy"] = age <= config.central_feed.stale_after_seconds
        except Exception as exc:
            central_feed_status = {
                "enabled": True,
                "path": str(central_path),
                "healthy": False,
                "error": str(exc),
            }
    classifier_budget_status = _classifier_budget_status(config)
    rule_engine_status = _rule_engine_status(config, store, contexts)
    from .coverage import build_semantic_coverage

    semantic_coverage = build_semantic_coverage(config)
    coverage_by_market = {
        row["market_id"]: row for row in semantic_coverage["markets"]
    }
    for market in markets:
        readiness = coverage_by_market.get(market["market_id"], {})
        market.update(
            {
                key: readiness.get(key)
                for key in (
                    "book_capture_ready",
                    "rule_ready",
                    "source_plan_ready",
                    "evidence_ready",
                    "paper_execution_ready",
                    "rule_family",
                    "blockers",
                )
            }
        )
    live_forward_state = _read_json_dict(
        config.data_dir / "forward_books_status.json"
    )
    live_forward_fresh = False
    if (
        config.forward_recorder.enabled
        and config.forward_recorder.shared_book_service
        and live_forward_state.get("published_at")
    ):
        try:
            published = datetime.fromisoformat(
                str(live_forward_state["published_at"]).replace(
                    "Z",
                    "+00:00",
                )
            )
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            live_forward_fresh = (
                datetime.now(timezone.utc) - published
            ).total_seconds() <= max(
                30.0,
                config.forward_recorder.heartbeat_seconds * 3.0,
            )
        except ValueError:
            live_forward_fresh = False
    if (
        config.forward_recorder.enabled
        and config.forward_recorder.shared_book_service
    ):
        raw_forward_status = (
            live_forward_state if live_forward_fresh else None
        )
    else:
        raw_forward_status = fleet_state.get("forward_books")
    forward_books_status = (
        dict(raw_forward_status)
        if isinstance(raw_forward_status, dict)
        else _forward_books_disabled_status(config)
    )
    if (
        config.forward_recorder.enabled
        and config.forward_recorder.shared_book_service
        and not isinstance(raw_forward_status, dict)
    ):
        forward_books_status = _forward_books_disabled_status(
            config,
            reason="status_unavailable",
        )
    forward_path = forward_recorder_db_path(config)
    if forward_path.exists():
        try:
            from polybot.rules.forward import ForwardRecorderStore

            forward_books_status.update(
                ForwardRecorderStore(forward_path).capture_status()
            )
        except Exception as exc:
            forward_books_status["storage_error"] = str(exc)

    status = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global_mode": global_mode,
        "ledger": ledger,
        "fleet": {
            "running": fleet_state.get("running", []),
            "desired": fleet_state.get("desired", []),
            "last_sync": fleet_state.get("updated_at"),
            "live": fleet_state.get("live"),
        },
        "central_feed": central_feed_status,
        "forward_books": forward_books_status,
        "classifier_budget": classifier_budget_status,
        "rule_engine": rule_engine_status,
        "semantic_coverage": semantic_coverage["summary"],
        "markets": sorted(markets, key=lambda m: (not m["holding"], m["market_id"])),
        "holding_count": sum(1 for m in markets if m["holding"]),
        "scan": {
            "scanned_outcomes": len(scan),
            "executable": [
                {"market_id": o.get("market_id"), "outcome": o.get("outcome"), "side": o.get("side"), "edge": o.get("tradable_edge")}
                for o in executable[:10]
            ],
            "group_arbitrage": _last_scan_arbitrage(config),
        },
        "model_pricing": {
            **opportunity_reachability(config.opportunity),
            "forecast_calibrated": calibration.forecast_calibrated(),
        },
        "calibration": {"forecast_calibrated": calibration.forecast_calibrated()},
    }
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def semantic_coverage_command(
    config_path: Path,
    *,
    all_contexts: bool = False,
) -> int:
    from .coverage import build_semantic_coverage

    config = load_discovery_config(config_path)
    report = build_semantic_coverage(config, persist=True)
    if not all_contexts:
        report = {
            **report,
            "markets": [
                row for row in report["markets"] if row["recorded"]
            ],
        }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _classifier_budget_status(config: DiscoveryConfig) -> dict[str, Any]:
    path = classifier_budget_db_path(config)
    if not config.classifier_budget.enabled:
        return {"enabled": False, "path": str(path)}
    if not path.exists():
        return {
            "enabled": True,
            "path": str(path),
            "initialized": False,
            "attempts_this_hour": 0,
            "attempts_today": 0,
            "errors_this_hour": 0,
        }
    from polybot.core.budget import ClassifierBudgetStore

    limits = replace(
        config.classifier,
        budget_db_path=str(path),
        max_escalations_per_hour=config.classifier_budget.max_escalations_per_hour,
        max_escalations_per_day=config.classifier_budget.max_escalations_per_day,
        max_classifier_errors_per_hour=config.classifier_budget.max_classifier_errors_per_hour,
    )
    return {
        "enabled": True,
        "initialized": True,
        **ClassifierBudgetStore(
            config.data_dir,
            path,
            priority_quotas=True,
            exploitation_fraction=(
                config.classifier_budget.exploitation_fraction
            ),
            exploration_fraction=(
                config.classifier_budget.exploration_fraction
            ),
            system_fraction=config.classifier_budget.system_fraction,
        ).status(limits),
    }


def _rule_engine_status(
    config: DiscoveryConfig,
    store: DiscoveryStore,
    contexts: list[MarketContext],
) -> dict[str, Any]:
    path = rule_store_db_path(config)
    if not config.rule_compiler.enabled:
        return {"enabled": False, "path": str(path)}
    try:
        from polybot.rules.store import RuleStore

        rule_store = RuleStore(path)
        current_specs = []
        stale_plans = 0
        missing_required = 0
        current_plans = 0
        legacy_plans = 0
        for context in contexts:
            plan = store.load_source_plan(context.market_id)
            if plan is not None:
                if plan.semantic_status == SOURCE_PLAN_LEGACY:
                    legacy_plans += 1
                elif plan.semantic_status == SOURCE_PLAN_CURRENT:
                    current_plans += 1
            spec = rule_store.load_spec(
                context.market_id,
                context.rule_text_sha256,
            )
            if spec is None:
                continue
            current_specs.append(spec)
            if (
                plan is None
                or plan.semantic_status != SOURCE_PLAN_CURRENT
                or plan.rule_text_sha256 != context.rule_text_sha256
                or plan.rule_spec_sha256 != spec.spec_sha256
            ):
                stale_plans += 1
            elif plan.missing_required_source_refs:
                missing_required += 1
        families = Counter(
            spec.semantics.rule_family for spec in current_specs
        )
        return {
            "enabled": True,
            **rule_store.status(),
            "current_specs": len(current_specs),
            "current_families": dict(families),
            "current_source_plans": current_plans,
            "legacy_source_plans": legacy_plans,
            "stale_or_missing_source_plans": stale_plans,
            "plans_missing_required_sources": missing_required,
            "paper_families": config.rule_compiler.paper_families,
            "live_confirmation_families": (
                config.rule_compiler.live_confirmation_families
            ),
            "generic_paper_runner": {
                "enabled": config.rule_runner.enabled,
                "paper_only": True,
                "execution_families": (
                    config.rule_runner.paper_execution_families
                ),
            },
            "forward_recorder": {
                "enabled": config.forward_recorder.enabled,
                "paper_only": True,
                "path": str(forward_recorder_db_path(config)),
                "quote_survival_horizons_ms": (
                    config.forward_recorder.quote_survival_horizons_ms
                ),
            },
        }
    except Exception as exc:
        return {
            "enabled": True,
            "path": str(path),
            "healthy": False,
            "error": str(exc),
        }


def funnel_report_command(config_path: Path) -> int:
    """Measure the whole opportunity funnel instead of one hand-picked market:
    all -> understandable -> observable -> eligible -> mispriced ->
    executable, plus current portfolio exposure."""
    config, store, allocator = _load(config_path)
    contexts = store.all_contexts()
    scoring = config.scoring
    understandable = [
        c for c in contexts if c.rule_analysis is not None and c.rule_analysis.rule_clarity >= scoring.min_clarity_paper
    ]
    observable = [
        c for c in understandable if c.rule_analysis is not None and c.rule_analysis.evidence_observability >= scoring.min_observability_paper
    ]
    opportunities_raw: list[dict[str, Any]] = []
    opportunities_path = config.data_dir / "opportunities.json"
    if opportunities_path.exists():
        raw = json.loads(opportunities_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("opportunities"), list):
            opportunities_raw = [item for item in raw["opportunities"] if isinstance(item, dict)]
    mispriced = [o for o in opportunities_raw if o.get("tradable_edge") is not None and not any(str(b).startswith("edge_below_minimum") for b in o.get("blockers", [])) and o.get("tradable_edge", 0) >= config.opportunity.min_edge]
    executable = [o for o in opportunities_raw if not o.get("blockers")]
    rule_engine = _rule_engine_status(config, store, contexts)
    report = {
        "funnel": {
            "all_markets": len(contexts),
            "understandable_markets": len(understandable),
            "observable_markets": len(observable),
            "paper_eligible": sum(1 for c in contexts if c.state == "PAPER_ELIGIBLE"),
            "live_confirmation_eligible": sum(1 for c in contexts if c.state == "LIVE_CONFIRMATION_ELIGIBLE"),
            "mispriced_outcomes": len(mispriced),
            "executable_opportunities": len(executable),
            "current_rule_specs": rule_engine.get("current_specs", 0),
            "stale_or_missing_source_plans": rule_engine.get(
                "stale_or_missing_source_plans",
                0,
            ),
        },
        "states": dict(Counter(c.state for c in contexts)),
        "rule_families": rule_engine.get("current_families", {}),
        "rule_engine": rule_engine,
        "top_blockers": dict(Counter(str(b).split(":")[0] for o in opportunities_raw for b in o.get("blockers", []))),
        "model_pricing": opportunity_reachability(config.opportunity),
        "portfolio": allocator.snapshot(),
    }
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


def run_discovery_command(
    config_path: Path,
    *,
    once: bool = False,
    events_fetch: Callable[[str, dict[str, Any]], list[dict[str, Any]]] | None = None,
    quotes: QuoteProviderProtocol | None = None,
    analyzer=None,
    notifier=None,
    markets_fetch=None,
) -> int:
    """Scheduled pipeline loop: discover -> grade -> plan-sources -> scan on
    an interval, alerting (Telegram) on newly LIVE_CONFIRMATION_ELIGIBLE
    markets and newly executable opportunities. Each stage is fault-isolated:
    one bad cycle logs and waits for the next instead of killing the loop."""
    import time

    from polybot.core.notifier import TelegramNotifier

    config, _, _ = _load(config_path)
    notifier = notifier or TelegramNotifier()
    while True:
        try:
            _run_discovery_cycle(config_path, config, events_fetch=events_fetch, quotes=quotes, analyzer=analyzer, notifier=notifier, markets_fetch=markets_fetch)
        except Exception as exc:
            log_event("discovery_cycle_error", error=str(exc))
            try:
                notifier.notify("Discovery pipeline cycle failed; continuing", error=str(exc))
            except Exception as notify_exc:
                log_event("discovery_notify_failed", error=str(notify_exc))
        if once:
            return 0
        time.sleep(max(60.0, config.schedule.interval_minutes * 60.0))


def _run_discovery_cycle(
    config_path: Path,
    config: DiscoveryConfig,
    *,
    events_fetch,
    quotes,
    analyzer,
    notifier,
    markets_fetch=None,
) -> None:
    store = DiscoveryStore(config.data_dir)
    previous = _pipeline_state(config)
    discover_markets_command(config_path, events_fetch=events_fetch)
    # Migrate the already-monitored semantic universe in bounded batches.
    # Existing plans keep a deferred market in this queue even after the
    # strict pass marks its missing RuleSpec as review-required.
    semantic_candidate_ids: set[str] | None = None
    if config.rule_compiler.enabled:
        semantic_candidate_ids = {
            context.market_id
            for context in store.all_contexts()
            if (
                context.state in TRADEABLE_STATES
                or (
                    store.load_source_plan(context.market_id) is not None
                    and not context.closed
                    and context.state not in {"CLOSED", "REJECTED"}
                )
            )
        }
    # The descriptive pre-pass supplies RuleAnalysis and prioritization, but
    # cannot authorize execution. Only the strict post-plan pass does that.
    grade_markets_command(
        config_path,
        analyzer=analyzer,
        require_semantic_assets=False,
        market_ids=semantic_candidate_ids,
    )
    if config.rule_compiler.enabled:
        compile_rules_command(
            config_path,
            market_ids=semantic_candidate_ids or set(),
        )
        plan_sources_command(config_path)
        # Eligibility is computed only after both semantic assets exist.
        grade_markets_command(
            config_path,
            analyzer=analyzer,
            require_semantic_assets=True,
            market_ids=semantic_candidate_ids or set(),
        )
    else:
        plan_sources_command(config_path)

    if config.profit_priority.enabled:
        from .profit_priority import build_priority_snapshot

        priority = build_priority_snapshot(config, store)
        log_event(
            "profit_priority_refreshed",
            markets=len(priority.get("records", [])),
            snapshot_sha256=priority.get("snapshot_sha256"),
        )

    # Autonomous estimation: the system's own P(YES) per tradeable binary
    # market, persisted where forecast_probability_lookup reads it. The
    # scan below prices these against the book; capture_resolutions scores
    # them; require_calibrated_forecast gates them from live sizing until
    # the calibration report proves they beat the market.
    from .estimator import refresh_estimates

    estimates = refresh_estimates(
        store.all_contexts(),
        forecast_data_root=config.opportunity.forecast_data_root,
        config=config.estimator,
    )
    if estimates.get("estimated") or estimates.get("errors"):
        log_event(
            "discovery_estimates_refreshed",
            estimated=len(estimates.get("estimated", [])),
            errors=len(estimates.get("errors", [])),
            skipped_fresh=estimates.get("skipped_fresh", 0),
        )

    scan_opportunities_command(config_path, quotes=quotes)

    # Resolved markets feed the calibration loop automatically -- every
    # resolution makes the probability sources measurably scoreable.
    from .calibration import CalibrationLog, capture_resolutions

    resolutions = capture_resolutions(store, CalibrationLog(config.data_dir), markets_fetch=markets_fetch)
    if config.forward_recorder.enabled and resolutions["recorded"]:
        from polybot.rules.forward import record_forward_resolutions

        record_forward_resolutions(config, resolutions["recorded"])
    for item in resolutions["recorded"]:
        notifier.notify("Discovery: outcome resolved; calibration log updated", **item)

    contexts = store.all_contexts()
    live_now = sorted(c.market_id for c in contexts if c.state == "LIVE_CONFIRMATION_ELIGIBLE")
    executable_now = sorted(
        f"{o.get('market_id')}:{o.get('outcome')}:{o.get('side', 'YES')}"
        for o in _last_scan(config)
        if not o.get("blockers")
    )
    arbs_now = sorted(f"{a.get('market_id')}:{a.get('type')}" for a in _last_scan_arbitrage(config))
    new_live = [m for m in live_now if m not in set(previous.get("live_eligible", []))]
    new_executable = [o for o in executable_now if o not in set(previous.get("executable", []))]
    new_arbs = [a for a in arbs_now if a not in set(previous.get("group_arbitrage", []))]
    for market_id in new_live:
        context = store.load_context(market_id)
        notifier.notify(
            "Discovery: market newly LIVE_CONFIRMATION_ELIGIBLE",
            market_id=market_id,
            question=(context.question if context else ""),
            deadline=(context.deadline_iso if context else ""),
            next_step=f"emit-bot-config --market {market_id}",
        )
    for key in new_executable:
        notifier.notify("Discovery: newly executable opportunity", opportunity=key)
    for key in new_arbs:
        notifier.notify("Discovery: group-consistency arbitrage detected", arbitrage=key)
    _atomic_json_write(
        config.data_dir / "pipeline_state.json",
        {"live_eligible": live_now, "executable": executable_now, "group_arbitrage": arbs_now},
    )
    log_event(
        "discovery_cycle_complete",
        live_eligible=len(live_now),
        executable=len(executable_now),
        new_live=len(new_live),
        new_executable=len(new_executable),
        group_arbitrage=len(arbs_now),
    )


def _pipeline_state(config: DiscoveryConfig) -> dict[str, Any]:
    path = config.data_dir / "pipeline_state.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _last_scan(config: DiscoveryConfig) -> list[dict[str, Any]]:
    path = config.data_dir / "opportunities.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items = raw.get("opportunities") if isinstance(raw, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _last_scan_arbitrage(config: DiscoveryConfig) -> list[dict[str, Any]]:
    path = config.data_dir / "opportunities.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    items = raw.get("group_arbitrage") if isinstance(raw, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _live_quotes(contexts: list[MarketContext]) -> QuoteProviderProtocol:
    # The location quote adapter is market-agnostic (token ids in, best bid/ask
    # out over public CLOB books) and already enforces freshness; reused here
    # rather than duplicated.
    from polybot.location.quotes import PublicClobQuoteAdapter

    token_ids = [
        outcome.yes_token_id
        for context in contexts
        if context.state in TRADEABLE_STATES
        for outcome in context.outcomes
        if outcome.yes_token_id
    ]
    return PublicClobQuoteAdapter(token_ids)


def _safe(market_id: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_-]", "-", market_id)[:120]
