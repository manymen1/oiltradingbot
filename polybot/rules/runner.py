from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from polybot.core.article import article_age_hours
from polybot.core.budget import ClassifierBudgetStore
from polybot.core.central_feed import (
    CentralFeedPromotionCache,
    CentralFeedReader,
)
from polybot.core.execution import PaperTradingAdapter
from polybot.core.fees import fee_schedule_map
from polybot.core.holdings import _atomic_json_write
from polybot.core.portfolio import (
    PortfolioConfig,
    PortfolioLink,
)
from polybot.core.source_fetcher import promote_feed_article
from polybot.core.storage import append_jsonl
from polybot.core.types import Article
from polybot.discovery.config import (
    DiscoveryConfig,
    central_feed_db_path,
    classifier_budget_db_path,
    load_discovery_config,
    rule_store_db_path,
)
from polybot.discovery.registry import region_of
from polybot.discovery.sources import validate_source_plan_freshness
from polybot.discovery.store import DiscoveryStore
from polybot.discovery.types import (
    MarketContext,
    SourcePlan,
    TRADEABLE_STATES,
    market_dir_slug,
)
from polybot.location.quotes import PublicClobQuoteAdapter
from polybot.log import log_event

from .contracts import DecisionProof, EvidenceClaim, RuleEvaluation, RuleSpec
from .decision import (
    ConfirmationDecisionEngine,
    ConfirmationPolicy,
)
from .evaluators import SUPPORTED_EVALUATOR_FAMILIES, evaluate_rule
from .evidence import EvidenceExtractor
from .portwatch import is_portwatch_spec, sync_portwatch_claims
from .store import RuleStore


class GenericRuleMarketRunner:
    """One rules-first market worker with no live execution surface."""

    def __init__(
        self,
        *,
        config: DiscoveryConfig,
        context: MarketContext,
        spec: RuleSpec,
        source_plan: SourcePlan,
        rule_store: RuleStore,
        extractor: EvidenceExtractor,
        feed_reader: CentralFeedReader | None,
        promotion_cache: CentralFeedPromotionCache | None,
        adapter: PaperTradingAdapter,
        portfolio: PortfolioLink,
        data_dir: Path,
        forward_recorder: Any | None = None,
    ) -> None:
        spec.validate_context_binding(context)
        validate_source_plan_freshness(context, source_plan, spec)
        family = spec.semantics.rule_family
        enabled_families = {
            item.strip().upper()
            for item in config.rule_runner.paper_execution_families
        }
        if family not in SUPPORTED_EVALUATOR_FAMILIES:
            raise ValueError(f"no deterministic evaluator for {family}")
        self.config = config
        self.context = context
        self.spec = spec
        self.source_plan = source_plan
        self.rule_store = rule_store
        self.extractor = extractor
        self.feed_reader = feed_reader
        self.promotion_cache = promotion_cache
        self.adapter = adapter
        self.portfolio = portfolio
        self.forward_recorder = forward_recorder
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.engine = ConfirmationDecisionEngine(
            context=context,
            spec=spec,
            adapter=adapter,
            portfolio=portfolio,
            policy=ConfirmationPolicy(
                min_edge=config.opportunity.min_edge,
                max_entry_price=config.opportunity.max_entry_price,
                slippage_buffer=config.opportunity.slippage_buffer,
                resolution_risk_buffer=(
                    config.opportunity.resolution_risk_buffer
                ),
                resolution_risk_scale=(
                    config.opportunity.resolution_risk_scale
                ),
                uncertainty_buffer=(
                    config.rule_runner.confirmation_uncertainty_buffer
                ),
                requested_usd=config.allocator.per_order_usd,
                exit_price_buffer=config.rule_runner.exit_price_buffer,
                max_fee_schedule_age_hours=(
                    config.rule_runner.max_fee_schedule_age_hours
                ),
            ),
            execution_enabled=family in enabled_families,
        )

    def run_once(
        self,
        *,
        articles: Iterable[Article] | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        replay_now = _aware_utc(as_of or datetime.now(timezone.utc))
        replay_at = replay_now.isoformat()
        cycle_id = hashlib.sha256(
            f"{self.context.market_id}:{replay_at}".encode("utf-8")
        ).hexdigest()
        self._heartbeat(replay_at)
        using_central_feed = articles is None
        batch_articles: list[Article]
        if articles is not None:
            batch_articles = list(articles)
        else:
            if self.feed_reader is None:
                raise ValueError("generic runner has no central-feed reader")
            batch = self.feed_reader.read(
                [
                    *self.source_plan.feed_urls,
                    *self.source_plan.poll_urls,
                ],
                include_terms=self.source_plan.escalate_terms or None,
                exclude_terms=None,
                limit_per_feed=(
                    self.config.rule_runner.max_articles_per_feed
                ),
            )
            batch_articles = batch.articles

        extraction_results: list[dict[str, Any]] = []
        budget_blocked = False
        for raw_article in batch_articles:
            article = self._promote(raw_article)
            if article is None:
                extraction_results.append(
                    {
                        "article_id": raw_article.hash,
                        "status": "PROMOTION_FAILED",
                    }
                )
                continue
            if self.forward_recorder is not None:
                article_observed_at = (
                    replay_at
                    if as_of is not None
                    else datetime.now(timezone.utc).isoformat()
                )
                self.forward_recorder.record_article(
                    article,
                    observed_at=article_observed_at,
                    cycle_id=cycle_id,
                )
            age = article_age_hours(article, as_of=replay_now)
            max_age = self.config.rule_runner.max_trade_article_age_hours
            if age is not None and max_age > 0 and age > max_age:
                stale_result = {
                    "article_id": article.hash,
                    "status": "STALE",
                    "age_hours": round(age, 3),
                }
                extraction_results.append(stale_result)
                if self.forward_recorder is not None:
                    self.forward_recorder.record_extraction(
                        article.hash,
                        started_at=article_observed_at,
                        completed_at=article_observed_at,
                        duration_ms=0.0,
                        result=stale_result,
                    )
                continue
            extraction_started_at = datetime.now(timezone.utc).isoformat()
            extraction_started_mono = time.monotonic()
            result = self.extractor.extract(
                context=self.context,
                spec=self.spec,
                source_plan=self.source_plan,
                article=article,
                extracted_at=replay_at,
            )
            extraction_completed_at = datetime.now(timezone.utc).isoformat()
            extraction_results.append(result.as_dict())
            if self.forward_recorder is not None:
                self.forward_recorder.record_extraction(
                    article.hash,
                    started_at=extraction_started_at,
                    completed_at=extraction_completed_at,
                    duration_ms=(
                        time.monotonic() - extraction_started_mono
                    )
                    * 1000.0,
                    result=result.as_dict(),
                )
            budget_blocked = budget_blocked or result.status == "BUDGET_BLOCKED"
            if result.status in {"INVALID", "DISAGREEMENT"}:
                log_event(
                    "rule_evidence_extraction_failed_closed",
                    market_id=self.context.market_id,
                    article_id=article.hash,
                    status=result.status,
                    reason=result.reason,
                )

        if using_central_feed and self.feed_reader is not None and not budget_blocked:
            self.feed_reader.ack_pending()

        oracle_claims = 0
        oracle_error = ""
        if is_portwatch_spec(self.spec):
            try:
                oracle_claims = len(
                    sync_portwatch_claims(
                        spec=self.spec,
                        rule_store=self.rule_store,
                        data_dir=self.data_dir,
                        as_of=replay_now,
                    )
                )
            except Exception as exc:  # fail closed on oracle/network failure
                oracle_error = f"{type(exc).__name__}: {exc}"
                log_event(
                    "portwatch_evidence_sync_failed",
                    market_id=self.context.market_id,
                    error=oracle_error,
                )

        claims = self.rule_store.claims_for_spec(self.spec.spec_sha256)
        semantic_key = self._semantic_key(claims, as_of=replay_now)
        if (
            not budget_blocked
            and semantic_key == self._last_semantic_key()
        ):
            summary = {
                "market_id": self.context.market_id,
                "status": "NO_CHANGE",
                "articles": len(batch_articles),
                "extraction_results": extraction_results,
                "claims": len(claims),
                "oracle_claims": oracle_claims,
                "oracle_error": oracle_error,
                "evaluations": 0,
                "proofs": 0,
                "executed": 0,
            }
            self._write_cycle(summary, replay_at)
            return summary

        evaluations = evaluate_rule(self.spec, claims, as_of=replay_now)
        saved_evaluations: list[RuleEvaluation] = []
        proofs: list[DecisionProof] = []
        for evaluation in evaluations:
            saved = self.rule_store.save_evaluation(evaluation)
            saved_evaluations.append(saved)
            decision_started_at = (
                replay_at
                if as_of is not None
                else datetime.now(timezone.utc).isoformat()
            )
            draft = self.engine.decide(
                saved,
                created_at=decision_started_at,
            )
            decision_completed_at = (
                replay_at
                if as_of is not None
                else datetime.now(timezone.utc).isoformat()
            )
            submission_started_at = (
                replay_at
                if as_of is not None
                else datetime.now(timezone.utc).isoformat()
            )
            execution_result = self.engine.execute(draft)
            submission_completed_at = (
                replay_at
                if as_of is not None
                else datetime.now(timezone.utc).isoformat()
            )
            execution_result = {
                **dict(execution_result),
                "_timing": {
                    "decision_started_at": decision_started_at,
                    "decision_completed_at": decision_completed_at,
                    "submission_started_at": submission_started_at,
                    "submission_completed_at": submission_completed_at,
                    "paper_only": True,
                },
            }
            proof = self.engine.proof(
                source_plan=self.source_plan,
                evaluation=saved,
                draft=draft,
                claims=claims,
                execution_result=execution_result,
                created_at=submission_completed_at,
            )
            proof = self.rule_store.save_proof(proof)
            proofs.append(proof)
            if self.forward_recorder is not None:
                self.forward_recorder.record_decision(
                    saved,
                    proof,
                    observed_at=submission_started_at,
                )
            append_jsonl(
                self.data_dir / "decision_proofs.jsonl",
                {
                    "proof_sha256": proof.proof_sha256,
                    **proof.as_dict(),
                },
            )
            log_event(
                "generic_rule_decision",
                market_id=self.context.market_id,
                rule_family=self.spec.semantics.rule_family,
                outcome=proof.outcome_name,
                evidence_state=saved.evidence_state,
                action=proof.action,
                executed=proof.executed,
                blockers=proof.blockers,
            )

        if not budget_blocked:
            self._write_semantic_key(semantic_key, replay_at)
        summary = {
            "market_id": self.context.market_id,
            "status": "BUDGET_BLOCKED" if budget_blocked else "EVALUATED",
            "articles": len(batch_articles),
            "extraction_results": extraction_results,
            "claims": len(claims),
            "oracle_claims": oracle_claims,
            "oracle_error": oracle_error,
            "evaluations": len(saved_evaluations),
            "proofs": len(proofs),
            "executed": sum(1 for proof in proofs if proof.executed),
            "states": [
                {
                    "outcome": item.outcome_name,
                    "evaluation_sha256": item.evaluation_sha256,
                    "evidence_state": item.evidence_state,
                    "terminal": item.terminal,
                    "blockers": item.blockers,
                }
                for item in saved_evaluations
            ],
            "actions": [
                {
                    "outcome": proof.outcome_name,
                    "proof_sha256": proof.proof_sha256,
                    "action": proof.action,
                    "side": proof.side,
                    "token_id": proof.token_id,
                    "executed": proof.executed,
                    "blockers": proof.blockers,
                    "executable_bid": proof.executable_bid,
                    "executable_ask": proof.executable_ask,
                    "minimum_edge": proof.minimum_edge,
                    "net_edge": proof.net_edge,
                    "execution_result": proof.execution_result,
                }
                for proof in proofs
            ],
        }
        self._write_cycle(summary, replay_at)
        return summary

    def start(self) -> None:
        if self.forward_recorder is not None:
            self.forward_recorder.start()

    def stop(self, *, reason: str = "normal_stop") -> None:
        if self.forward_recorder is not None:
            self.forward_recorder.stop(reason=reason)

    def _promote(self, article: Article) -> Article | None:
        if article.source_kind not in {
            "feed",
            "feed_item",
            "direct_listing",
            "direct_sitemap",
            "direct_json",
        }:
            return article
        try:
            return (
                self.promotion_cache.promote(
                    article,
                    "polybot/0.1",
                    promoter=promote_feed_article,
                )
                if self.promotion_cache is not None
                else promote_feed_article(article, "polybot/0.1")
            )
        except Exception as exc:
            log_event(
                "generic_rule_promotion_failed",
                market_id=self.context.market_id,
                article_id=article.hash,
                error=str(exc),
            )
            return None

    def _semantic_key(
        self,
        claims: list[EvidenceClaim],
        *,
        as_of: datetime,
    ) -> str:
        deadline = _deadline_reached(self.spec, as_of=as_of)
        balances = self.adapter.snapshot().get("balances", {})
        payload = {
            "spec_sha256": self.spec.spec_sha256,
            "claim_sha256s": sorted(claim.claim_sha256 for claim in claims),
            "deadline_reached": deadline,
            "balances": balances,
            # Once evidence exists, repricing or fresh liquidity must trigger a
            # new decision even if no later article changes the claim set.
            "quotes": (
                {
                    item.name: {
                        "yes_bid": self.adapter.yes_best_bid(item.yes_token_id),
                        "yes_ask": self.adapter.yes_best_ask(item.yes_token_id),
                        "no_bid": self.adapter.no_best_bid(item.no_token_id),
                        "no_ask": self.adapter.no_best_ask(item.no_token_id),
                    }
                    for item in self.spec.outcomes
                }
                if claims
                else {}
            ),
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _last_semantic_key(self) -> str:
        path = self.data_dir / "semantic_state.json"
        if not path.exists():
            return ""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        return str(raw.get("key") or "") if isinstance(raw, dict) else ""

    def _write_semantic_key(self, key: str, at: str) -> None:
        _atomic_json_write(
            self.data_dir / "semantic_state.json",
            {
                "key": key,
                "updated_at": at,
            },
        )

    def _heartbeat(self, at: str) -> None:
        _atomic_json_write(
            self.data_dir / "heartbeat.json",
            {
                "at": at,
                "market_id": self.context.market_id,
                "runner": "generic_rules_paper",
                "paper_only": True,
            },
        )

    def _write_cycle(self, summary: dict[str, Any], at: str) -> None:
        _atomic_json_write(
            self.data_dir / "last_cycle.json",
            {
                **summary,
                "updated_at": at,
            },
        )
        if self.forward_recorder is not None:
            self.forward_recorder.record_cycle(summary, observed_at=at)


def build_generic_rule_runner(
    config_path: Path,
    market_id: str,
    *,
    feed_reader: CentralFeedReader | None = None,
    promotion_cache: CentralFeedPromotionCache | None = None,
    adapter: PaperTradingAdapter | None = None,
    extractor: EvidenceExtractor | None = None,
) -> GenericRuleMarketRunner:
    config = load_discovery_config(config_path)
    if not config.rule_runner.enabled:
        raise SystemExit(
            "rule_runner.enabled is false; generic execution is not armed"
        )
    if config.fleet.position_mode == "live":
        raise SystemExit(
            "generic rule runner is paper-only and refuses fleet.position_mode=live"
        )
    discovery_store = DiscoveryStore(config.data_dir)
    context = discovery_store.load_context(market_id)
    if context is None:
        raise SystemExit(f"unknown market_id {market_id!r}")
    if context.state not in TRADEABLE_STATES:
        raise SystemExit(
            f"market {market_id} is {context.state}; generic runner requires a paper/live-eligible context"
        )
    source_plan = discovery_store.load_source_plan(market_id)
    if source_plan is None:
        raise SystemExit(
            f"market {market_id} has no source plan; run plan-sources"
        )
    rule_store = RuleStore(rule_store_db_path(config))
    spec = rule_store.load_spec(market_id, context.rule_text_sha256)
    if spec is None:
        raise SystemExit(
            f"market {market_id} has no current RuleSpec; run compile-rules"
        )
    spec.validate_context_binding(context)
    validate_source_plan_freshness(context, source_plan, spec)

    data_dir = (
        config.data_dir
        / "rule_runner"
        / market_dir_slug(context.market_id)
    )
    if feed_reader is None:
        if not config.central_feed.enabled:
            raise SystemExit(
                "generic rule runner requires central_feed.enabled"
            )
        feed_reader = CentralFeedReader(
            central_feed_db_path(config),
            data_dir / "central_feed_cursors.json",
            stale_after_seconds=config.central_feed.stale_after_seconds,
        )
    if promotion_cache is None and config.central_feed.enabled:
        promotion_cache = CentralFeedPromotionCache(
            central_feed_db_path(config)
        )

    token_ids = [
        token
        for outcome in spec.outcomes
        for token in (outcome.yes_token_id, outcome.no_token_id)
    ]
    forward_recorder = None
    if adapter is None:
        quote_provider: Any
        if config.forward_recorder.enabled:
            from .forward import RecordedClobQuoteAdapter

            forward_recorder = RecordedClobQuoteAdapter(
                config=config,
                context=context,
                spec=spec,
                source_plan=source_plan,
            )
            quote_provider = forward_recorder
        else:
            quote_provider = PublicClobQuoteAdapter(
                token_ids,
                refresh_seconds=config.rule_runner.poll_seconds,
            )
        adapter = PaperTradingAdapter(
            state_path=data_dir / "paper_broker.json",
            quote_provider=quote_provider,
            token_pairs=[
                (outcome.yes_token_id, outcome.no_token_id)
                for outcome in spec.outcomes
            ],
            fee_schedules=fee_schedule_map(context),
            slippage_bps=config.rule_runner.paper_slippage_bps,
            max_book_age_seconds=(
                config.rule_runner.paper_max_book_age_seconds
            ),
        )
    budget_store = None
    limits = config.classifier
    from polybot.discovery.profit_priority import load_priority_snapshot

    priority_record = load_priority_snapshot(config.data_dir).get(
        context.market_id
    )
    budget_purpose = (
        "exploration"
        if priority_record is None or priority_record.cold_start
        else "exploitation"
    )
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
    if extractor is None:
        extractor = EvidenceExtractor(
            config.classifier,
            rule_store,
            passes=config.rule_runner.extraction_passes,
            budget_store=budget_store,
            budget_limits=limits,
            budget_purpose=budget_purpose,
            priority_score_sha256=(
                priority_record.score_sha256
                if priority_record is not None
                else ""
            ),
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
        )

    from polybot.core.portfolio import PortfolioAllocator

    ledger_path = config.data_dir / "allocations.json"
    PortfolioAllocator(ledger_path, config.allocator).write_caps()
    parties = (
        context.rule_analysis.parties
        if context.rule_analysis is not None
        else []
    )
    portfolio = PortfolioLink(
        PortfolioConfig(
            ledger_path=str(ledger_path),
            market_id=context.market_id,
            event_slug=context.event_slug,
            correlation_group=(
                context.correlation_group or "uncategorized"
            ),
            region=region_of(parties),
            deadline_iso=context.deadline_iso,
        )
    )
    return GenericRuleMarketRunner(
        config=config,
        context=context,
        spec=spec,
        source_plan=source_plan,
        rule_store=rule_store,
        extractor=extractor,
        feed_reader=feed_reader,
        promotion_cache=promotion_cache,
        adapter=adapter,
        portfolio=portfolio,
        data_dir=data_dir,
        forward_recorder=forward_recorder,
    )


def run_generic_rule_market_command(
    config_path: Path,
    market_id: str,
    *,
    once: bool = False,
    live_flag: bool = False,
) -> int:
    if live_flag:
        raise SystemExit(
            "generic rules-first runner is paper-only; --live is forbidden"
        )
    runner = build_generic_rule_runner(config_path, market_id)
    from polybot.core.runtime import ProcessLock

    with ProcessLock(runner.data_dir / "process.lock"):
        runner.start()
        try:
            while True:
                try:
                    summary = runner.run_once()
                    print(json.dumps(summary, indent=2, sort_keys=True))
                except Exception as exc:
                    log_event(
                        "generic_rule_cycle_failed_closed",
                        market_id=market_id,
                        error=str(exc),
                    )
                    if once:
                        raise
                if once:
                    return 0
                time.sleep(runner.config.rule_runner.poll_seconds)
        finally:
            runner.stop(reason="command_exit")


def inspect_generic_rule_market_command(
    config_path: Path,
    market_id: str,
) -> int:
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    context = store.load_context(market_id)
    if context is None:
        raise SystemExit(f"unknown market_id {market_id!r}")
    rule_store = RuleStore(rule_store_db_path(config))
    spec = rule_store.load_spec(market_id, context.rule_text_sha256)
    plan = store.load_source_plan(market_id)
    data_dir = (
        config.data_dir / "rule_runner" / market_dir_slug(market_id)
    )
    from polybot.discovery.coverage import build_semantic_coverage

    semantic_readiness = next(
        (
            row
            for row in build_semantic_coverage(config)["markets"]
            if row["market_id"] == market_id
        ),
        {},
    )
    payload = {
        "market_id": market_id,
        "paper_only": True,
        "runner_enabled": config.rule_runner.enabled,
        "context_state": context.state,
        "rule_family": (
            spec.semantics.rule_family if spec is not None else None
        ),
        "rule_spec_sha256": spec.spec_sha256 if spec is not None else None,
        "source_plan_current": bool(
            spec is not None
            and plan is not None
            and plan.rule_spec_sha256 == spec.spec_sha256
        ),
        "claims": (
            len(rule_store.claims_for_spec(spec.spec_sha256))
            if spec is not None
            else 0
        ),
        "evaluations": (
            len(rule_store.evaluations_for_spec(spec.spec_sha256))
            if spec is not None
            else 0
        ),
        "decision_proofs": len(rule_store.proofs_for_market(market_id)),
        "last_cycle": _read_json(data_dir / "last_cycle.json"),
        "paper_broker": _read_json(data_dir / "paper_broker.json"),
        "heartbeat": _read_json(data_dir / "heartbeat.json"),
        "semantic_readiness": semantic_readiness,
    }
    if config.forward_recorder.enabled and spec is not None and plan is not None:
        from .forward import forward_completeness_report

        payload["forward_recorder"] = forward_completeness_report(
            config_path,
            market_id,
        )
    else:
        payload["forward_recorder"] = {
            "enabled": config.forward_recorder.enabled,
        }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _deadline_reached(
    spec: RuleSpec,
    *,
    as_of: datetime | None = None,
) -> bool:
    try:
        deadline = datetime.fromisoformat(
            spec.semantics.window.end_iso.replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return _aware_utc(as_of or datetime.now(timezone.utc)) >= deadline


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


__all__ = [
    "GenericRuleMarketRunner",
    "build_generic_rule_runner",
    "inspect_generic_rule_market_command",
    "run_generic_rule_market_command",
]
