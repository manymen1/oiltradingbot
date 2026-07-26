from __future__ import annotations

import difflib
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.book import BookCache
from polybot.config import SETTINGS
from polybot.log import log_event

from polybot.core.article import article_age_hours as _article_age_hours, is_feed_summary as _is_feed_summary
from polybot.core.budget import ClassifierBudgetExceeded, ClassifierBudgetStore
from polybot.core.execution import DryRunTradingAdapter, PaperTradingAdapter, TradingAdapter, live_adapter_from_env, live_backend_name
from polybot.core.notifier import TelegramNotifier
from polybot.core.operator import OperatorGate
from polybot.core.source_fetcher import ArticleStore, fetch_article, fetch_feed_articles, fetch_listing_article_urls, promote_feed_article
from polybot.core.storage import StateStore, append_jsonl
from polybot.core.types import Article
from polybot.core.verifier import quote_in_article

from .classifier import build_location_classifier
from .config import LocationBotConfig, load_location_config
from .decision import LocationDecision, classify_agreement, entry_decision, final_decision, time_decay_decision
from .executor import TERMINAL_STATES, LocationExecutor
from .forecast import ForecastPaperEngine
from .holdings import HoldingsStore
from .market_verifier import verify_all_outcomes, verify_location_event
from .keyword_gate import should_escalate_location_article
from .runtime import ProcessLock
from .quotes import PublicClobQuoteAdapter, QuoteAdapter, QuoteOnlyFacade


PROMOTED_FEED_SUMMARY_AUTO_TRADE_DOMAINS = {"reuters.com", "apnews.com", "afp.com"}
LOCATION_EXECUTION_STATES = {"TRIMMED", "EXITED", "ROTATED", "FLIP_INCOMPLETE"}
# ENTER_YES deliberately excluded from LOCATION_EXECUTION_STATES/max_executions:
# entries are capped separately by entry.max_entries, so an entry never
# consumes the execution budget reserved for defending the resulting position.
TRADE_ACTIONS = {"TRIM_YES", "EXIT_YES_ONLY", "ROTATE_YES", "ENTER_YES"}
_ALERT_ONLY_STARTUP_BLOCKERS = {"operator_mode_alert_only"}


def exact_domain(domain: str) -> str:
    return domain.lower().removeprefix("www.")


def domain_allowed(domain: str, allowed: list[str]) -> bool:
    normalized = exact_domain(domain)
    return any(normalized == item or normalized.endswith("." + item) for item in allowed)


def promoted_feed_summary_auto_trade_allowed(domain: str) -> bool:
    return exact_domain(domain) in PROMOTED_FEED_SUMMARY_AUTO_TRADE_DOMAINS


def location_execution_count(store: StateStore) -> int:
    current = store.current()
    current_state = current.state if current is not None else None
    count = 0
    for state in LOCATION_EXECUTION_STATES:
        if store.marker(state) is not None:
            count += 1
        elif current_state == state:
            count += 1
    return count


def _config_holdings(config: LocationBotConfig) -> HoldingsStore:
    # Mirror of the executor's _effective_store dry-run isolation, for command
    # paths that need the live holding before a bot/executor exists.
    data_dir = config.data_dir / "dry_run" if config.execution.dry_run else config.data_dir
    return HoldingsStore(data_dir, default_held=config.event.held_location or None)


def inspect_location_command(config_path: Path) -> int:
    config = load_location_config(config_path)
    holdings = _config_holdings(config)
    held = config.outcome(holdings.held_location() or "")
    result: dict[str, Any] = {
        "config": str(config_path),
        "event": {
            "slug": config.event.slug,
            "question": config.event.question,
            "deadline_date": config.event.deadline_date,
            "held_location": config.event.held_location,
        },
        "holdings": holdings.record().as_dict(),
        "held_outcome": (
            {
                "name": held.name,
                "label": held.label,
                "condition_id": held.condition_id,
                "yes_token_id": held.yes_token_id,
                "no_token_id": held.no_token_id,
            }
            if held is not None
            else None
        ),
        "entry": {
            "enabled": config.entry.enabled,
            "targets": sorted(config.entry_target_names()),
            "usd_budget": config.entry.usd_budget,
            "max_price": config.entry.max_price,
            "max_entries": config.entry.max_entries,
        },
        "forecast": {
            "enabled": config.forecast.enabled,
            "paper_only": config.forecast.paper_only,
            "prior_probabilities": config.forecast.prior_probabilities,
            "min_paper_edge": config.forecast.min_paper_edge,
        },
        "rotation_targets": [o.name for o in config.rotation_targets()],
        "all_outcomes": [o.name for o in config.outcomes],
        "classifier": {"provider": config.classifier.provider, "model": config.classifier.model},
    }
    try:
        verification = verify_location_event(config)
        result["market_verification"] = verification.as_dict()
    except Exception as exc:
        result["market_verification_error"] = str(exc)
    print(json.dumps(result, indent=2, sort_keys=True))
    print(
        "\nManual confirmation required before live mode. Review market_verification above and "
        "pin event.expected_rule_text_sha256 in config after review."
    )
    return 0


def preflight_location_command(config_path: Path, live_flag: bool = False) -> int:
    config = load_location_config(config_path)
    adapter = _live_adapter() if live_flag else DryRunTradingAdapter()
    result = _location_preflight_result(config_path, config, live_flag=live_flag, adapter=adapter)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["status"] == "blocked" else 0


def _location_preflight_result(
    config_path: Path,
    config: LocationBotConfig,
    *,
    live_flag: bool,
    adapter: TradingAdapter,
    verification: Any | None = None,
) -> dict[str, Any]:
    holdings = _config_holdings(config)
    gate = OperatorGate(config_path, config)
    status = gate.status(live_requested=live_flag)
    reconciliation = None
    if live_flag:
        try:
            preflight_executor = LocationExecutor(config, StateStore(holdings.data_dir), TelegramNotifier(), adapter)
            reconciliation = preflight_executor.reconcile_live_holding()
            holdings = preflight_executor.holdings
        except Exception as exc:
            status.blockers.append("wallet_reconciliation_failed")
            reconciliation = {"error": str(exc)}
    held = config.outcome(holdings.held_location() or "")
    result: dict[str, Any] = {
        "status": "blocked" if status.blockers else "ok",
        "operator": status.as_dict(),
        "config": str(config_path),
        "held_outcome": held.name if held is not None else None,
        "holdings": holdings.record().as_dict(),
        "wallet_reconciliation": reconciliation,
        "entry": {
            "enabled": config.entry.enabled,
            "targets": sorted(config.entry_target_names()),
            "usd_budget": config.entry.usd_budget,
            "max_price": config.entry.max_price,
            "max_entries": config.entry.max_entries,
        },
        "forecast": {
            "enabled": config.forecast.enabled,
            "paper_only": config.forecast.paper_only,
            "min_paper_edge": config.forecast.min_paper_edge,
            "paper_order_usd": config.forecast.paper_order_usd,
        },
        "execution_backend": live_backend_name() if live_flag else "dry_run",
    }
    if held is not None:
        try:
            position = adapter.query_live_position(held.yes_token_id, held.no_token_id)
            result["live_position"] = {"yes_shares": position.yes_shares, "no_shares": position.no_shares}
        except Exception as exc:
            result["live_position_error"] = str(exc)
            status.blockers.append("live_position_query_failed")
    elif not config.entry.enabled:
        status.blockers.append("flat_without_entry_enabled")
    try:
        verification = verification or verify_location_event(config)
        verify_all_outcomes(config, verification, require_tradeable=live_flag)
        result["market_verification"] = verification.as_dict()
    except Exception as exc:
        result["market_verification_error"] = str(exc)
        status.blockers.append("market_verification_failed")
    result["operator"] = status.as_dict()
    result["status"] = "blocked" if status.blockers else "ok"
    return result


def ack_location_live_command(config_path: Path, note: str = "") -> int:
    config = load_location_config(config_path)
    gate = OperatorGate(config_path, config)
    print(json.dumps(gate.write_ack(note=note), indent=2, sort_keys=True))
    return 0


def set_location_mode_command(config_path: Path, mode: str) -> int:
    config = load_location_config(config_path)
    gate = OperatorGate(config_path, config)
    print(json.dumps(gate.set_position_mode(mode), indent=2, sort_keys=True))
    return 0


def _hard_live_startup_blockers(preflight: dict[str, Any]) -> list[str]:
    operator = preflight.get("operator")
    blockers = operator.get("blockers") if isinstance(operator, dict) else None
    if not isinstance(blockers, list):
        return []
    return [str(blocker) for blocker in blockers if str(blocker) not in _ALERT_ONLY_STARTUP_BLOCKERS]


def smoke_location_classifier_command(
    config_path: Path,
    *,
    url: str | None = None,
    text: str | None = None,
    title: str = "classifier smoke",
    domain: str = "reuters.com",
) -> int:
    if not url and not text:
        raise SystemExit("smoke-location-classifier requires --url or --text")
    config = load_location_config(config_path)
    article = fetch_article(url, SETTINGS.user_agent) if url else Article(
        url="smoke://local",
        domain=domain,
        title=title,
        published_at=None,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        raw_text=text or "",
        hash=f"smoke:{hash(text or '')}",
    )
    holdings = _config_holdings(config)
    held = holdings.held_location()
    classifier = build_location_classifier(config)
    try:
        factors = classifier.classify(article, config.event.resolution_rules, held_location=held or "")
    except Exception as exc:
        print(json.dumps({"article": article.__dict__, "ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 1
    decision = entry_decision(config, factors) if held is None else final_decision(config, factors, held=held)
    print(
        json.dumps(
            {
                "article": article.__dict__,
                "factors": asdict(factors),
                "decision": {
                    "action": decision.action,
                    "level": decision.level,
                    "reason": decision.reason,
                    "target_outcome": decision.target_outcome,
                },
                "ok": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def run_location_command(config_path: Path, live_flag: bool) -> int:
    config = load_location_config(config_path)
    if not config.sources.poll_urls and not config.sources.feed_urls and not config.time_decay.enabled:
        raise SystemExit("no sources configured and time_decay is disabled")
    if not config.execution.dry_run and not live_flag:
        raise SystemExit("execution.dry_run=false requires --live")
    if live_flag and config.execution.dry_run:
        raise SystemExit("--live requires execution.dry_run=false")
    holdings = _config_holdings(config)
    held_name = holdings.held_location()
    if held_name is None and not config.entry.enabled:
        raise SystemExit("holdings are flat and entry is disabled; nothing to protect or enter")
    if held_name is not None and config.outcome(held_name) is None:
        raise SystemExit(f"live holding {held_name!r} is not in configured outcomes; refusing to run")
    verification = verify_location_event(config)
    verify_all_outcomes(config, verification, require_tradeable=live_flag)
    adapter: TradingAdapter
    if live_flag:
        # The markets the bot may actually trade first: the held leg when
        # holding, otherwise every configured entry target.
        critical = [config.outcome(held_name)] if held_name else config.entry_targets()
        critical_verified = []
        for outcome in critical:
            if outcome is None:
                continue
            outcome_verification = verification.outcomes.get(outcome.name)
            if outcome_verification is None or not outcome_verification.tradeable:
                raise SystemExit(f"{outcome.name} market is not active/open/accepting orders; refusing live execution")
            critical_verified.append(outcome_verification)
        if not critical_verified:
            raise SystemExit("no tradeable held or entry-target market; refusing live execution")
        adapter = _live_adapter(tick_size=critical_verified[0].tick_size, neg_risk=critical_verified[0].neg_risk)
    else:
        adapter = PaperTradingAdapter(
            state_path=config.data_dir / "dry_run" / "paper_broker.json",
            quote_provider=PublicClobQuoteAdapter(
                [
                    token_id
                    for outcome in config.outcomes
                    for token_id in (outcome.yes_token_id, outcome.no_token_id)
                ],
                refresh_seconds=2.0,
            ),
            token_pairs=[(outcome.yes_token_id, outcome.no_token_id) for outcome in config.outcomes],
            fee_bps=config.execution.paper_fee_bps,
            slippage_bps=config.execution.paper_slippage_bps,
            max_book_age_seconds=config.execution.paper_max_book_age_seconds,
        )
    gate = OperatorGate(config_path, config)
    if live_flag:
        preflight = _location_preflight_result(
            config_path,
            config,
            live_flag=True,
            adapter=adapter,
            verification=verification,
        )
        log_event("location_live_preflight", **preflight)
        print(json.dumps(preflight, indent=2, sort_keys=True))
        if _hard_live_startup_blockers(preflight):
            raise SystemExit("live preflight blocked execution; fix blockers or use ack/set-mode commands")
    lock_dir = config.data_dir / "dry_run" if config.execution.dry_run else config.data_dir
    process_lock = ProcessLock(lock_dir / "location_bot.lock")
    with process_lock:
        forecast_adapter: QuoteAdapter = QuoteOnlyFacade(adapter)
        if config.forecast.enabled and config.execution.dry_run and config.forecast.live_quotes_in_dry_run:
            forecast_adapter = PublicClobQuoteAdapter(
                [outcome.yes_token_id for outcome in config.outcomes],
                refresh_seconds=config.forecast.quote_refresh_seconds,
            )
        bot_kwargs: dict[str, Any] = {
            "config": config,
            "adapter": adapter,
            "operator_gate": gate,
            "live_requested": live_flag,
        }
        if config.forecast.enabled:
            bot_kwargs["forecast_adapter"] = forecast_adapter
        bot = LocationProtectionBot(**bot_kwargs)
        while True:
            try:
                bot.run_once()
            except Exception as exc:
                log_event("location_run_once_error", error=str(exc))
                try:
                    bot.notifier.notify("Location protection polling cycle failed; continuing", error=str(exc))
                except Exception as notify_exc:
                    log_event("location_notify_failed", error=str(notify_exc))
            time.sleep(effective_poll_seconds(config.safety, live_flag))


def _live_adapter(*, tick_size: str = "0.01", neg_risk: bool = False) -> TradingAdapter:
    return live_adapter_from_env(tick_size=tick_size, neg_risk=neg_risk)


def effective_poll_seconds(safety: Any, live_flag: bool) -> float:
    """Live-armed bots poll at armed_poll_seconds when configured: the news
    race is lost in the gap between publication and the next cycle."""
    armed = getattr(safety, "armed_poll_seconds", 0.0) or 0.0
    if live_flag and armed > 0:
        return max(1.0, armed)
    return safety.poll_seconds


class LocationProtectionBot:
    def __init__(
        self,
        *,
        config: LocationBotConfig,
        adapter: TradingAdapter,
        forecast_adapter: QuoteAdapter | None = None,
        operator_gate: OperatorGate | None = None,
        live_requested: bool = False,
    ):
        self.config = config
        self.store = StateStore(config.data_dir / "dry_run" if config.execution.dry_run else config.data_dir)
        article_store_path = (
            config.data_dir / "central_feed_articles.jsonl"
            if config.sources.central_feed_db
            else config.logs_dir / "location_articles.jsonl"
        )
        self.article_store = ArticleStore(article_store_path)
        self.central_feed_reader = None
        self.promotion_cache = None
        if config.sources.central_feed_db:
            from polybot.core.central_feed import CentralFeedPromotionCache, CentralFeedReader

            self.central_feed_reader = CentralFeedReader(
                Path(config.sources.central_feed_db),
                config.data_dir / "central_feed_cursor.json",
                stale_after_seconds=config.sources.central_feed_stale_after_seconds,
            )
            self.promotion_cache = CentralFeedPromotionCache(Path(config.sources.central_feed_db))
        self.notifier = TelegramNotifier()
        self.classifier = build_location_classifier(config)
        # Cheap/fast screen tier: one pass on a smaller model decides whether
        # the expensive trade-grade passes run at all. Most escalated articles
        # are noise (does-not-break-thesis / technical / no signal); paying
        # the strong model for them is the dominant classifier cost.
        self.screen_classifier = None
        if config.classifier.screen_model and config.classifier.provider != "rule_based":
            from dataclasses import replace as _replace

            from .classifier import LLMLocationClassifier

            self.screen_classifier = LLMLocationClassifier(
                _replace(config.classifier, model=config.classifier.screen_model), config
            )
        self.classifier_budget = ClassifierBudgetStore(
            self.store.data_dir,
            Path(config.classifier.budget_db_path) if config.classifier.budget_db_path else None,
        )
        self.executor = LocationExecutor(config, self.store, self.notifier, adapter)
        self.holdings = self.executor.holdings
        # Event-anchored book capture shares the executor's logger (same
        # data_dir, same enable flag) so gate/order/fill snapshots interleave
        # in one book_snapshots.jsonl per market.
        self.book_snapshots = self.executor.book_snapshots
        self.forecast = ForecastPaperEngine(
            config,
            forecast_adapter or QuoteOnlyFacade(adapter),
            self.store.data_dir,
            config.logs_dir,
        )
        self.operator_gate = operator_gate
        self.live_requested = live_requested
        self._monitoring_books: dict[str, BookCache] = {}
        self._listing_text_cache_path = self.store.data_dir / "listing_article_text_cache.json"

    def run_once(self) -> list[LocationDecision]:
        self._write_heartbeat()
        if self.operator_gate is not None and self.operator_gate.current_mode() == "off":
            log_event("location_operator_off_cycle_skip")
            return []
        if self.live_requested and not self.config.execution.dry_run:
            reconciliation = self.executor.reconcile_live_holding()
            if reconciliation.get("changed"):
                log_event("location_wallet_reconciled", **reconciliation)
        self._check_corroboration_deadline()
        mark_report = self.forecast.mark_cycle()
        self._notify_forecast_exits(mark_report.get("exits", []))
        self._process_monitoring()
        decisions: list[LocationDecision] = []
        take_profit = self._check_take_profit()
        if take_profit is not None:
            decisions.append(take_profit)
        if self.holdings.held_location() is not None:
            # Time decay only applies to a held position; a flat bot has
            # nothing to trim or exit on the calendar.
            decay = time_decay_decision(self.config)
            if decay.action != "NO_ACTION" and self._decay_still_actionable(decay):
                decisions.append(self._execute_if_allowed(decay, _synthetic_article(decay.reason)))
        # Sources are fetched CONCURRENTLY: sequential fetching made cycle
        # wall-time the SUM of feed round-trips, dwarfing the poll interval.
        # Classification/decisions stay serial and in stable order.
        for kind, batch in self._fetch_all_sources():
            for article in batch:
                if not self.article_store.store(article):
                    continue
                decisions.append(self.process_article(article))
                if kind not in {"feed", "central_feed"}:
                    continue
                promoted = self._promote_feed_article(article)
                if promoted is None or not self.article_store.store(promoted):
                    continue
                decisions.append(self.process_article(promoted))
            if kind == "central_feed" and self.central_feed_reader is not None:
                self.central_feed_reader.ack_pending()
        return decisions

    def _fetch_all_sources(self) -> list[tuple[str, list[Article]]]:
        jobs: list[tuple[str, str]] = [("poll", url) for url in self.config.sources.poll_urls]
        if self.central_feed_reader is None:
            jobs += [("feed", url) for url in self.config.sources.feed_urls]

        def fetch(job: tuple[str, str]) -> tuple[str, list[Article]]:
            kind, url = job
            if kind == "poll":
                return kind, self._fetch_poll_articles(url)
            try:
                return kind, fetch_feed_articles(
                    url,
                    SETTINGS.user_agent,
                    include_terms=self.config.sources.feed_include_terms,
                    exclude_terms=self.config.sources.feed_exclude_terms,
                    limit=self.config.sources.max_feed_entries_per_cycle,
                )
            except Exception as exc:
                log_event("location_feed_fetch_error", url=url, error=str(exc))
                return kind, []

        results: list[tuple[str, list[Article]]] = []
        if len(jobs) == 1:
            results = [fetch(jobs[0])]
        elif jobs:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
                results = list(pool.map(fetch, jobs))

        if self.central_feed_reader is not None:
            try:
                batch = self.central_feed_reader.read(
                    self.config.sources.feed_urls,
                    include_terms=self.config.sources.feed_include_terms,
                    exclude_terms=self.config.sources.feed_exclude_terms,
                    limit_per_feed=self.config.sources.max_feed_entries_per_cycle,
                )
                results.insert(0, ("central_feed", batch.articles))
            except Exception as exc:
                log_event("location_central_feed_unavailable", error=str(exc))
        return results

    def _decay_still_actionable(self, decay: LocationDecision) -> bool:
        """Suppress re-executing a time-decay decision every polling cycle.

        The decay decision recurs on every run_once after its date passes; the
        executor's dedupe branches would otherwise notify (Telegram) each cycle
        (every safety.poll_seconds) once the trim/exit has already happened.
        """
        current = self.store.current()
        if current is not None and current.state in TERMINAL_STATES:
            log_event("location_time_decay_skip_terminal", state=current.state, action=decay.action)
            return False
        if decay.action == "TRIM_YES" and (
            (current is not None and current.state == "TRIMMED") or self.store.marker("TRIMMED") is not None
        ):
            log_event("location_time_decay_skip_already_trimmed", action=decay.action)
            return False
        return True

    def process_article(self, article: Article) -> LocationDecision:
        # Cheap deterministic keyword pre-filter runs FIRST, before spending a
        # Telegram push or any classifier budget: only articles that mention a
        # tracked location/meeting term (or a collapse term) go any further.
        # This is the "low classifier" stage -- it selects relevant articles
        # and updates; codex only ever sees what passes it.
        if not should_escalate_location_article(article, self.config):
            decision = LocationDecision("ALERT_ONLY", "1", "keyword_gate_no_location_trigger")
            self._log_decision(article, decision)
            return decision
        # Every article/update that clears the gate gets its full text pushed
        # to Telegram, regardless of source (poll_urls tag page, RSS feeds,
        # or promoted feed summaries) -- previously only poll_urls articles
        # got this, so relevant Dawn/AJ-RSS/Reuters items were silently only
        # summarized via the terse "alert only" message, never their actual
        # text.
        #
        # listing_article sources (the AJ tag page, including liveblogs) are
        # refetched in full every cycle -- the publisher re-renders the whole
        # page, not just the new entry -- so without diffing, every poll where
        # the page grows would resend the same old paragraphs alongside the
        # new ones. _listing_update_notify_text sends only the new/changed
        # lines on repeat sightings of the same URL; classification below
        # still runs on the untouched, full article.raw_text.
        notify_text, is_incremental = self._listing_update_notify_text(article)
        message = _format_live_update_message(article, body_text=notify_text, incremental=is_incremental)
        for chunk in _chunk_telegram_message(message):
            self.notifier.notify(chunk, title=article.title, url=article.url, domain=article.domain, published_at=article.published_at)
        # The repricing window starts NOW: capture every leg's book while
        # prices are (possibly) still stale, before classification spends
        # seconds. The whole group is one snapshot set -- also feeds the
        # group-sum consistency analysis.
        self.book_snapshots.snapshot(
            [o.yes_token_id for o in self.config.outcomes],
            moment="gate_escalation",
            article_hash=article.hash,
            domain=article.domain,
        )
        age_hours = _article_age_hours(article)
        max_age = self.config.sources.max_trade_article_age_hours
        if max_age > 0 and age_hours is not None and age_hours > max_age:
            decision = LocationDecision("ALERT_ONLY", "3", f"article_stale_skipped_classification:{age_hours:.0f}h")
            self._log_decision(article, decision)
            return decision
        if _is_feed_summary(article) and not self.config.classifier.classify_feed_summaries:
            decision = LocationDecision("ALERT_ONLY", "3", "feed_summary_classification_disabled")
            self._log_decision(article, decision)
            self.notifier.notify("Location protection feed summary skipped classifier", reason=decision.reason, url=article.url)
            return decision
        block_reason = self.classifier_budget.block_reason(self.config.classifier)
        if block_reason is not None:
            decision = LocationDecision("ALERT_ONLY", "3", block_reason)
            self._log_decision(article, decision)
            self._notify_classifier_budget_block_once(block_reason)
            return decision
        # The cheap screen tier gates the strong model ONLY WHILE FLAT: missing
        # an entry costs an opportunity, but a screen false-negative on a
        # foreclosure while HOLDING would suppress the exit that protects the
        # stake. Defense never depends on the weakest model.
        if self.screen_classifier is not None and self.holdings.held_location() is None:
            screen_decision = self._screen_stage(article)
            if screen_decision is not None:
                return screen_decision
        try:
            passes = self._run_confirm_passes(article)
        except ClassifierBudgetExceeded as exc:
            decision = LocationDecision("ALERT_ONLY", "3", exc.reason)
            self._log_decision(article, decision)
            self._notify_classifier_budget_block_once(exc.reason)
            return decision
        except Exception as exc:
            self.classifier_budget.record_error()
            decision = LocationDecision("ALERT_ONLY", "3", f"classifier_error:{exc}")
            self._log_decision(article, decision)
            self.notifier.notify("Location classifier unavailable or failed; no trade", error=str(exc))
            return decision
        forecast_report = self.forecast.process(article, passes)
        if forecast_report.get("opened"):
            opened = forecast_report["opened"]
            self.notifier.notify(
                "Anticipatory forecast paper entry",
                outcome=opened.get("outcome"),
                price=opened.get("price"),
                fair_probability=opened.get("fair_probability"),
                edge=opened.get("edge_after_buffers"),
            )
        self._notify_forecast_exits(forecast_report.get("exits", []))
        # Route on the LIVE holding: a flat bot can only produce an entry, a
        # holding bot only protection actions on what it actually holds.
        held = self.holdings.held_location()
        if self.config.classifier.require_pass_agreement:
            decision = classify_agreement(self.config, passes, held=held, flat=held is None)
        elif held is None:
            decision = entry_decision(self.config, passes[0])
        else:
            decision = final_decision(self.config, passes[0], held=held)
        decision = self._verify_quote_or_alert(decision, article)
        decision = self._enforce_source_policy(article, decision)
        decision = self._enforce_execution_policy(decision)
        self._log_decision(article, decision)
        self._corroboration_on_decision(decision, article)
        if decision.action == "ALERT_ONLY":
            self.notifier.notify("Location protection alert only; no trade", level=decision.level, reason=decision.reason, url=article.url)
        return self._execute_if_allowed(decision, article)

    def _notify_forecast_exits(self, exits: list[dict[str, Any]]) -> None:
        for closed in exits:
            self.notifier.notify(
                "Anticipatory forecast paper exit",
                outcome=closed.get("outcome"),
                price=closed.get("price"),
                pnl=closed.get("pnl"),
                trigger=closed.get("trigger_kind"),
            )

    def _verify_quote_or_alert(self, decision: LocationDecision, article: Article) -> LocationDecision:
        if decision.factors is None:
            return decision
        is_trade_action = decision.action in TRADE_ACTIONS
        requires_quote = self.config.safety.quote_must_match_article_text and is_trade_action
        if not requires_quote and decision.level not in {"4A", "4B"}:
            return decision
        if quote_in_article(decision.factors.quote_supporting_trigger, article.raw_text):
            return decision
        return LocationDecision("ALERT_ONLY", "3", "quote_verification_failed", decision.target_outcome, decision.factors)

    def _promote_feed_article(self, article: Article) -> Article | None:
        if article.source_kind != "feed" or not self.config.sources.promote_feed_to_article:
            return None
        promoted = (
            self.promotion_cache.promote(
                article,
                SETTINGS.user_agent,
                promoter=promote_feed_article,
            )
            if self.promotion_cache is not None
            else promote_feed_article(article, SETTINGS.user_agent)
        )
        if promoted is None:
            log_event("location_feed_promotion_failed", url=article.url, domain=article.domain)
            return None
        return promoted

    def _enforce_source_policy(self, article: Article, decision: LocationDecision) -> LocationDecision:
        if decision.action not in TRADE_ACTIONS:
            return decision
        # Opening risk from a truncated discovery snippet is materially
        # different from reducing an existing position. Full publisher text or
        # a first-party full-text feed is required for autonomous entry.
        if decision.action == "ENTER_YES" and article.source_kind in {"feed", "promoted_feed_summary"}:
            return LocationDecision("ALERT_ONLY", "3", "entry_requires_full_source_text", decision.target_outcome, decision.factors)
        if article.source_kind in {"feed", "promoted_feed_summary"} and not self.config.sources.allow_feed_auto_trade:
            return LocationDecision("ALERT_ONLY", "3", "feed_item_auto_trade_disabled", decision.target_outcome, decision.factors)
        # A promoted_feed_summary is feed-derived text used when the publisher
        # page could not be fetched. Keep live execution to exact wire domains;
        # republishers, social posts, and official-but-partial feeds alert only.
        if article.source_kind == "promoted_feed_summary" and not promoted_feed_summary_auto_trade_allowed(article.domain):
            return LocationDecision(
                "ALERT_ONLY", "3", "promoted_feed_summary_domain_not_auto_trade", decision.target_outcome, decision.factors
            )
        # Freshness gate (ported from the iran runner): stale items can alert
        # but not trade -- old news is already priced in, and with feed
        # auto-trade enabled a stale feed item must never fire a sale.
        age_hours = _article_age_hours(article)
        max_age = self.config.sources.max_trade_article_age_hours
        if max_age > 0 and article.published_at is None and not self.config.sources.allow_unknown_age_poll_auto_trade:
            return LocationDecision("ALERT_ONLY", "3", "article_age_unknown_for_auto_trade", decision.target_outcome, decision.factors)
        if max_age > 0 and age_hours is not None and age_hours > max_age:
            return LocationDecision("ALERT_ONLY", "3", f"article_stale_for_auto_trade:{age_hours:.0f}h", decision.target_outcome, decision.factors)
        if domain_allowed(article.domain, self.config.sources.alert_only_domains):
            return LocationDecision("ALERT_ONLY", "3", "source_domain_alert_only", decision.target_outcome, decision.factors)
        if not domain_allowed(article.domain, self.config.sources.auto_trade_domains):
            return LocationDecision("ALERT_ONLY", "3", "source_domain_not_auto_trade", decision.target_outcome, decision.factors)
        return decision

    def _enforce_execution_policy(self, decision: LocationDecision) -> LocationDecision:
        if decision.action not in TRADE_ACTIONS:
            return decision
        if not self.config.trigger.trusted_single_source_execution:
            return LocationDecision("ALERT_ONLY", "3", "single_source_execution_disabled", decision.target_outcome, decision.factors)
        if not _level_meets_threshold(decision.level, self.config.trigger.auto_execute_level):
            return LocationDecision("ALERT_ONLY", "3", "below_auto_execute_level", decision.target_outcome, decision.factors)
        if decision.action == "ENTER_YES":
            # Entries are budgeted separately from protection executions so an
            # entry can never consume the execution allowance needed to later
            # defend the position it just opened.
            if not self.config.entry.enabled:
                return LocationDecision("ALERT_ONLY", "3", "entry_disabled", decision.target_outcome, decision.factors)
            entry_count = self.executor.entry_count()
            if entry_count >= self.config.entry.max_entries:
                return LocationDecision(
                    "ALERT_ONLY", "3", f"max_entries_reached:{entry_count}", decision.target_outcome, decision.factors
                )
        else:
            if self.config.safety.max_executions <= 0:
                return LocationDecision("ALERT_ONLY", "3", "max_executions_zero", decision.target_outcome, decision.factors)
            execution_count = self.executor.protection_execution_count()
            if execution_count >= self.config.safety.max_executions:
                return LocationDecision(
                    "ALERT_ONLY", "3", f"max_executions_reached:{execution_count}", decision.target_outcome, decision.factors
                )
        market_block = self._market_verification_block_reason()
        if market_block is not None:
            return LocationDecision("ALERT_ONLY", "3", market_block, decision.target_outcome, decision.factors)
        return decision

    def _market_verification_block_reason(self) -> str | None:
        if not self.config.monitoring.market_verification.enabled:
            return None
        raw = self._monitoring_state().get("market_verification")
        if not isinstance(raw, dict):
            return "market_verification_not_run"
        if raw.get("status") != "blocked":
            return None
        error = str(raw.get("error") or "unknown")[:160]
        return f"market_verification_blocked:{error}"

    def _execute_if_allowed(self, decision: LocationDecision, article: Article) -> LocationDecision:
        if self.operator_gate is not None:
            # OperatorGate.check/log_block_once only read `.action` off the
            # object passed in, so LocationDecision satisfies it structurally
            # without needing the iran-specific Decision dataclass.
            gate_result = self.operator_gate.check(decision, live_requested=self.live_requested)
            if not gate_result.allowed:
                if self.operator_gate.log_block_once(gate_result, decision):
                    self.notifier.notify(
                        "Location protection execution blocked by operator gate",
                        action=decision.action,
                        mode=gate_result.mode,
                        reason=gate_result.reason,
                        url=article.url,
                    )
                blocked = LocationDecision("ALERT_ONLY", "3", f"operator_block:{gate_result.reason}", decision.target_outcome, decision.factors)
                self._log_decision(article, blocked)
                return blocked
        result = self.executor.execute(decision, article)
        self._maybe_start_corroboration(result, article)
        return decision

    def _check_take_profit(self) -> LocationDecision | None:
        """Sell into strength: once the held outcome's YES bid reaches the
        target, the remaining upside is small against full resolution/UMA
        risk. Exits through the normal gated path (staged sell, ledger settle)."""
        target = self.config.position.take_profit_price
        if target <= 0:
            return None
        held = self.holdings.held_location()
        if held is None:
            return None
        current = self.store.current()
        if current is not None and current.state in TERMINAL_STATES:
            return None
        outcome = self.config.outcome(held)
        if outcome is None:
            return None
        bid = self.executor.adapter.yes_best_bid(outcome.yes_token_id)
        if bid is None or bid < target:
            return None
        decision = LocationDecision("EXIT_YES_ONLY", "TIME", f"take_profit_target_reached:{bid}")
        log_event("location_take_profit_triggered", held=held, bid=bid, target=target)
        return self._execute_if_allowed(decision, _synthetic_article(decision.reason))

    def _corroboration_tracker(self) -> Any:
        from polybot.core.confirmations import CorroborationTracker

        return CorroborationTracker(self.store.data_dir)

    def _maybe_start_corroboration(self, result: Any, article: Article) -> None:
        """Arm the corroboration clock the moment an autonomous entry fills."""
        if self.config.entry.corroboration_minutes <= 0:
            return
        if result not in {"ENTERED", "PARTIALLY_ENTERED"}:
            return
        self._corroboration_tracker().start(
            entry_domain=article.domain,
            minutes=self.config.entry.corroboration_minutes,
            action=self.config.entry.corroboration_action,
            entry_origin_organization=article.origin_organization,
            entry_byline=article.byline,
        )
        log_event("location_corroboration_started", entry_domain=article.domain, minutes=self.config.entry.corroboration_minutes)

    def _corroboration_on_decision(self, decision: LocationDecision, article: Article) -> None:
        """A held-location reinforcement from a DIFFERENT domain satisfies the
        post-entry corroboration requirement."""
        if self.config.entry.corroboration_minutes <= 0:
            return
        if decision.reason != "held_location_reinforced":
            return
        if self._corroboration_tracker().satisfy(
            article.domain,
            origin_organization=article.origin_organization,
            byline=article.byline,
        ):
            log_event("location_corroboration_satisfied", domain=article.domain)

    def _check_corroboration_deadline(self) -> None:
        if self.config.entry.corroboration_minutes <= 0:
            return
        tracker = self._corroboration_tracker()
        record = tracker.overdue()
        if record is None:
            return
        if self.holdings.held_location() is None:
            # Position already gone (exit or reconciliation); nothing to defend.
            tracker.mark_escalated()
            return
        action = str(record.get("action") or "alert")
        self.notifier.notify(
            "Entry NOT corroborated by a second source within the window",
            entry_domain=record.get("entry_domain"),
            deadline=record.get("deadline"),
            configured_action=action,
        )
        log_event("location_corroboration_overdue", entry_domain=record.get("entry_domain"), action=action)
        if action == "trim":
            decision = LocationDecision("TRIM_YES", "TIME", "corroboration_window_expired_trim")
            self._execute_if_allowed(decision, _synthetic_article(decision.reason))
        tracker.mark_escalated()

    def _fetch_poll_articles(self, url: str) -> list[Article]:
        if _is_listing_url(url):
            try:
                article_urls = fetch_listing_article_urls(url, SETTINGS.user_agent, limit=self.config.sources.max_feed_entries_per_cycle)
            except Exception as exc:
                log_event("location_listing_fetch_error", url=url, error=str(exc))
                article_urls = []
            if article_urls:
                log_event("location_listing_articles_discovered", url=url, count=len(article_urls))
                articles: list[Article] = []
                for article_url in article_urls:
                    try:
                        fetched = fetch_article(article_url, SETTINGS.user_agent)
                        articles.append(Article(**{**fetched.__dict__, "source_kind": "listing_article"}))
                    except Exception as exc:
                        log_event("location_listing_article_fetch_error", listing_url=url, url=article_url, error=str(exc))
                return articles
        try:
            return [fetch_article(url, SETTINGS.user_agent)]
        except Exception as exc:
            log_event("location_source_fetch_error", url=url, error=str(exc))
            return []

    def _listing_update_notify_text(self, article: Article) -> tuple[str, bool]:
        """Returns (text_to_notify, is_incremental) for the Telegram push.

        Only listing_article sources (currently the AJ tag page, which is
        where liveblogs live) get diffed: those URLs are refetched in full on
        every poll cycle, so the first sighting of a URL still gets the full
        text (for context) but every later sighting of the *same* URL only
        surfaces the new/changed lines since the last time we notified about
        it. Non-listing sources (RSS feed items, promoted articles) are
        one-shot per URL already via ArticleStore's content-hash dedupe, so
        they always get the full text.
        """
        if article.source_kind != "listing_article":
            return article.raw_text, False
        cache = self._load_listing_text_cache()
        previous = cache.get(article.url)
        cache[article.url] = {"text": article.raw_text, "updated_at": datetime.now(timezone.utc).isoformat()}
        self._save_listing_text_cache(cache)
        if not isinstance(previous, dict):
            return article.raw_text, False
        delta = _new_lines_since(str(previous.get("text") or ""), article.raw_text)
        if not delta:
            # No new lines detected (e.g. formatting-only change) -- fall
            # back to the full text rather than sending an empty message.
            return article.raw_text, False
        return delta, True

    def _load_listing_text_cache(self) -> dict[str, Any]:
        path = self._listing_text_cache_path
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_listing_text_cache(self, cache: dict[str, Any]) -> None:
        path = self._listing_text_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Bound growth over a long-running process: keep only the most
        # recently updated URLs.
        if len(cache) > 500:
            ordered = sorted(cache.items(), key=lambda kv: str(kv[1].get("updated_at", "")) if isinstance(kv[1], dict) else "", reverse=True)
            cache = dict(ordered[:500])
        path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _process_monitoring(self) -> None:
        self._process_price_alerts()
        self._process_market_verification()
        self._process_heartbeat()

    def _process_price_alerts(self) -> None:
        price_config = self.config.monitoring.price_alerts
        if not price_config.enabled or not price_config.thresholds:
            return
        outcome_name = price_config.outcome or (self.holdings.held_location() or "")
        if not outcome_name:
            log_event("location_price_alert_skip", reason="flat_and_no_outcome_configured")
            return
        outcome = self.config.outcome(outcome_name)
        if outcome is None:
            log_event("location_price_alert_skip", reason="outcome_not_found", outcome=outcome_name)
            return
        bid, ask, quote_source = self._price_alert_quote(outcome.yes_token_id)
        price = _monitor_price(bid, ask)
        if price is None:
            log_event("location_price_alert_skip", reason="price_unavailable", outcome=outcome.name)
            return
        state = self._monitoring_state()
        price_state = state.setdefault("price_alerts", {})
        key = outcome.name
        previous_raw = price_state.get(key, {}).get("last_price") if isinstance(price_state.get(key), dict) else None
        previous = _as_float(previous_raw)
        crossed = [
            threshold
            for threshold in sorted(float(item) for item in price_config.thresholds)
            if previous is not None and _crossed(previous, price, threshold)
        ]
        price_state[key] = {
            "last_price": price,
            "yes_best_bid": bid,
            "yes_best_ask": ask,
            "quote_source": quote_source,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_monitoring_state(state)
        for threshold in crossed:
            direction = "up" if price > previous else "down"
            self.notifier.notify(
                "Location price band crossed",
                outcome=outcome.label,
                threshold=threshold,
                direction=direction,
                price=price,
                yes_best_bid=bid,
                yes_best_ask=ask,
                quote_source=quote_source,
            )
            log_event(
                "location_price_band_crossed",
                outcome=outcome.name,
                threshold=threshold,
                direction=direction,
                price=price,
                quote_source=quote_source,
            )

    def _price_alert_quote(self, token_id: str) -> tuple[float | None, float | None, str]:
        price_config = self.config.monitoring.price_alerts
        if self.config.execution.dry_run and price_config.live_quotes_in_dry_run:
            try:
                book = self._monitoring_books.get(token_id)
                if book is None:
                    book = BookCache([token_id])
                    self._monitoring_books[token_id] = book
                book.rest_snapshot(token_id)
                snapshot = book.snapshot_state(token_id)
                return _as_float(snapshot.get("best_bid")), _as_float(snapshot.get("best_ask")), "live_clob_book"
            except Exception as exc:
                log_event("location_price_alert_live_quote_error", token_id=token_id, error=str(exc))
                return None, None, "live_clob_book_error"
        return (
            self.executor.adapter.yes_best_bid(token_id),
            self.executor.adapter.yes_best_ask(token_id),
            "execution_adapter",
        )

    def _process_market_verification(self) -> None:
        verification_config = self.config.monitoring.market_verification
        if not verification_config.enabled:
            return
        state = self._monitoring_state()
        raw = state.get("market_verification")
        previous = raw if isinstance(raw, dict) else {}
        last_checked = _parse_iso(previous.get("last_checked_at"))
        now = datetime.now(timezone.utc)
        interval_seconds = max(60.0, verification_config.interval_minutes * 60.0)
        if last_checked is not None and (now - last_checked).total_seconds() < interval_seconds:
            return
        payload: dict[str, Any] = {"last_checked_at": now.isoformat()}
        try:
            verification = verify_location_event(self.config)
            verify_all_outcomes(self.config, verification, require_tradeable=self.live_requested)
            payload.update(
                {
                    "status": "ok",
                    "event_slug": verification.event_slug,
                    "event_title": verification.event_title,
                    "rule_text_sha256": verification.rule_text_sha256,
                }
            )
            if previous.get("status") == "blocked":
                self.notifier.notify(
                    "Location market verification recovered",
                    event_slug=verification.event_slug,
                    rule_text_sha256=verification.rule_text_sha256,
                )
            log_event("location_market_verification_ok", event_slug=verification.event_slug, rule_text_sha256=verification.rule_text_sha256)
        except Exception as exc:
            error = str(exc)
            payload.update({"status": "blocked", "error": error})
            if previous.get("status") != "blocked" or previous.get("error") != error:
                self.notifier.notify("Location market verification failed", error=error)
            log_event("location_market_verification_failed", error=error)
        state["market_verification"] = payload
        self._write_monitoring_state(state)

    def _process_heartbeat(self) -> None:
        heartbeat = self.config.monitoring.heartbeat
        if not heartbeat.enabled:
            return
        state = self._monitoring_state()
        raw = state.get("heartbeat")
        last_sent = _parse_iso(raw.get("last_sent_at")) if isinstance(raw, dict) else None
        now = datetime.now(timezone.utc)
        interval_seconds = max(1.0, heartbeat.interval_hours * 3600.0)
        if last_sent is not None and (now - last_sent).total_seconds() < interval_seconds:
            return
        held = self.executor.held_outcome()
        bid = self.executor.adapter.yes_best_bid(held.yes_token_id) if held is not None else None
        ask = self.executor.adapter.yes_best_ask(held.yes_token_id) if held is not None else None
        budget = self.classifier_budget.status(self.config.classifier)
        current_state = self.store.current()
        operator_status = self.operator_gate.status(live_requested=self.live_requested).as_dict() if self.operator_gate else None
        position_payload: dict[str, Any] = {}
        if held is not None:
            try:
                position = self.executor.adapter.query_live_position(held.yes_token_id, held.no_token_id)
                position_payload = {"held_yes_shares": position.yes_shares, "held_no_shares": position.no_shares}
            except Exception as exc:
                position_payload = {"position_query_error": str(exc)}
        else:
            position_payload = {
                "entry_enabled": self.config.entry.enabled,
                "entry_targets": ",".join(sorted(self.config.entry_target_names())),
                "entry_count": self.executor.entry_count(),
            }
        market_verification = self._monitoring_state().get("market_verification")
        forecast_snapshot = self.forecast.snapshot() if self.config.forecast.enabled else None
        self.notifier.notify(
            "Location protection heartbeat",
            held_outcome=held.label if held is not None else "flat",
            dry_run=self.config.execution.dry_run,
            execution_backend=live_backend_name() if self.live_requested else "dry_run",
            current_state=current_state.state if current_state else None,
            current_reason=current_state.payload.get("reason") if current_state else None,
            operator_mode=operator_status.get("effective_mode") if isinstance(operator_status, dict) else None,
            config_acknowledged=operator_status.get("config_acknowledged") if isinstance(operator_status, dict) else None,
            market_verification_status=(market_verification.get("status") if isinstance(market_verification, dict) else None),
            yes_best_bid=bid,
            yes_best_ask=ask,
            classifier_budget=budget,
            forecast_paper=forecast_snapshot,
            **position_payload,
        )
        state["heartbeat"] = {"last_sent_at": now.isoformat()}
        self._write_monitoring_state(state)
        log_event("location_heartbeat_sent", held_outcome=held.name if held is not None else None, yes_best_bid=bid, yes_best_ask=ask)

    def _monitoring_state_path(self) -> Path:
        return self.store.data_dir / "monitoring.json"

    def _monitoring_state(self) -> dict[str, Any]:
        path = self._monitoring_state_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_monitoring_state(self, state: dict[str, Any]) -> None:
        path = self._monitoring_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    def _write_heartbeat(self) -> None:
        from polybot.core.holdings import _atomic_json_write

        _atomic_json_write(self.store.data_dir / "heartbeat.json", {"at": datetime.now(timezone.utc).isoformat()})

    def _log_decision(self, article: Article, decision: LocationDecision) -> None:
        append_jsonl(
            self.config.logs_dir / "location_decisions.jsonl",
            {
                "article": article.__dict__,
                "decision": {
                    "action": decision.action,
                    "level": decision.level,
                    "reason": decision.reason,
                    "target_outcome": decision.target_outcome,
                    "factors": decision.factors.__dict__ if decision.factors else None,
                },
            },
        )

    def _screen_stage(self, article: Article) -> LocationDecision | None:
        """One cheap-model pass. Returns the final decision when the screen
        says NO_ACTION (the strong model never runs), or None to escalate to
        the trade-grade passes. A screen failure escalates rather than blocks:
        the screen tier may only save money, never miss a trade."""
        try:
            screen = self._classify_with_budget(article, 0, classifier=self.screen_classifier, stage="screen")
        except ClassifierBudgetExceeded as exc:
            decision = LocationDecision("ALERT_ONLY", "3", exc.reason)
            self._log_decision(article, decision)
            self._notify_classifier_budget_block_once(exc.reason)
            return decision
        except Exception as exc:
            log_event("location_screen_classifier_error", error=str(exc))
            if not self.live_requested:
                # Paper posture fails CLOSED: a broken screen path must not
                # convert every noise article into full-price confirm passes.
                # Live keeps the fail-open contract: the screen tier may only
                # save money, never miss a trade defending a real position.
                decision = LocationDecision("NO_ACTION", "1", "screen_error_paper_fail_closed")
                self._log_decision(article, decision)
                return decision
            return None
        held = self.holdings.held_location()
        provisional = entry_decision(self.config, screen) if held is None else final_decision(self.config, screen, held=held)
        if provisional.action != "NO_ACTION":
            return None
        # The forecast research layer still consumes the screen signal:
        # trade-irrelevant evidence (speculative reports, denials) is exactly
        # what moves anticipatory priors.
        try:
            self.forecast.process(article, [screen])
        except Exception as exc:
            log_event("location_forecast_screen_error", error=str(exc))
        decision = LocationDecision("NO_ACTION", provisional.level, f"screen:{provisional.reason}", factors=screen)
        self._log_decision(article, decision)
        return decision

    def _run_confirm_passes(self, article: Article) -> list[Any]:
        """Trade-grade passes run CONCURRENTLY: on a confirmation race, N
        sequential strong-model calls would multiply reaction time by N."""
        count = max(1, self.config.classifier.passes)
        reason = self.classifier_budget.reserve_attempts(self.config.classifier, count)
        if reason is not None:
            raise ClassifierBudgetExceeded(reason)
        if count == 1:
            return [self._classify_with_budget(article, 0, budget_reserved=True)]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [
                pool.submit(self._classify_with_budget, article, index, budget_reserved=True)
                for index in range(count)
            ]
            return [future.result() for future in futures]

    def _classify_with_budget(
        self,
        article: Article,
        pass_index: int,
        *,
        classifier: Any = None,
        stage: str = "confirm",
        budget_reserved: bool = False,
    ) -> Any:
        active = classifier if classifier is not None else self.classifier
        context = self.config.event.resolution_rules
        input_chars = len(article.title) + len(article.raw_text) + len(context)
        telemetry = {
            "provider": self.config.classifier.provider,
            "model": getattr(getattr(active, "config", None), "model", self.config.classifier.model),
            "stage": stage,
            "pass_index": pass_index,
            "article_hash": article.hash,
            "source_kind": article.source_kind,
            "domain": article.domain,
            "input_char_count": input_chars,
            "estimated_input_tokens": max(1, input_chars // 4),
        }
        if not budget_reserved:
            reason = self.classifier_budget.reserve_attempts(self.config.classifier, 1)
            if reason is not None:
                raise ClassifierBudgetExceeded(reason)
        log_event("location_classifier_attempt", **telemetry)
        if hasattr(active, "last_usage"):
            setattr(active, "last_usage", None)
        factors = active.classify(article, context, held_location=self.holdings.held_location() or "")
        usage = getattr(active, "last_usage", None)
        if isinstance(usage, dict):
            telemetry["usage"] = usage
        log_event("location_classifier_result", **telemetry)
        return factors

    def _notify_classifier_budget_block_once(self, reason: str) -> None:
        window = "hour" if reason in {"classifier_budget_exhausted_hourly", "classifier_error_cap_exceeded"} else "day"
        if self.classifier_budget.mark_notified_once(reason, window):
            self.notifier.notify("Location protection classifier budget blocked classification", reason=reason)


def _synthetic_article(reason: str) -> Article:
    return Article(
        url="time-decay://local",
        domain="time-decay.local",
        title=reason,
        published_at=None,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        raw_text=reason,
        hash=f"time-decay:{reason}",
    )


def _level_meets_threshold(level: str, threshold: int) -> bool:
    if level == "TIME":
        return True
    digits = "".join(ch for ch in level if ch.isdigit())
    if not digits:
        return False
    return int(digits) >= threshold


def _is_listing_url(url: str) -> bool:
    return "/tag/" in url or "/topics/" in url


def _monitor_price(bid: float | None, ask: float | None) -> float | None:
    if bid is not None and ask is not None:
        return round((bid + ask) / 2.0, 4)
    return bid if bid is not None else ask


def _crossed(previous: float, current: float, threshold: float) -> bool:
    return (previous < threshold <= current) or (previous > threshold >= current)


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


_TELEGRAM_CHUNK_CHARS = 3500


def _format_live_update_message(article: Article, *, body_text: str | None = None, incremental: bool = False) -> str:
    body = (body_text if body_text is not None else article.raw_text).strip()
    label = "Live update (new since last check):" if incremental else None
    parts = [article.title.strip(), "", label, body, "", f"Source: {article.url}"]
    return "\n".join(part for part in parts if part)


def _new_lines_since(old_text: str, new_text: str) -> str:
    """Returns only the lines in new_text that weren't in old_text, in order.

    Liveblog pages commonly prepend new entries above older ones (reverse
    chronological), so a naive suffix/prefix diff would miss updates; a
    line-level SequenceMatcher handles insertion at either end (or in the
    middle) generically.
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    added: list[str] = []
    for tag, _, _, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(new_lines[j1:j2])
    return "\n".join(added).strip()


def _chunk_telegram_message(message: str, limit: int = _TELEGRAM_CHUNK_CHARS) -> list[str]:
    if len(message) <= limit:
        return [message]
    chunks: list[str] = []
    remaining = message
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    total = len(chunks)
    return [f"[{i + 1}/{total}]\n{chunk}" for i, chunk in enumerate(chunks)]
