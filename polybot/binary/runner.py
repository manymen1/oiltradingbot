from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.config import SETTINGS
from polybot.log import log_event

from polybot.core.article import article_age_hours as _article_age_hours, is_feed_summary as _is_feed_summary
from polybot.core.budget import ClassifierBudgetExceeded, ClassifierBudgetStore
from polybot.core.execution import DryRunTradingAdapter, PaperTradingAdapter, TradingAdapter, live_adapter_from_env, live_backend_name
from polybot.core.holdings import HoldingsStore
from polybot.core.notifier import TelegramNotifier
from polybot.core.operator import OperatorGate
from polybot.core.source_fetcher import ArticleStore, fetch_article, fetch_feed_articles, promote_feed_article
from polybot.core.storage import StateStore, append_jsonl
from polybot.core.types import Article
from polybot.core.verifier import quote_in_article

from .classifier import build_binary_classifier
from .config import BinaryBotConfig, load_binary_config
from .decision import BinaryDecision, classify_agreement, entry_decision, held_decision, time_decay_decision
from .executor import TERMINAL_STATES, BinaryExecutor
from .keyword_gate import should_escalate_binary_article
from .market_verifier import BinaryMarketVerification, load_and_verify_market
from polybot.location.quotes import PublicClobQuoteAdapter


PROMOTED_FEED_SUMMARY_AUTO_TRADE_DOMAINS = {"reuters.com", "apnews.com", "afp.com"}
# ENTER_* deliberately excluded (same policy as the location runner): entries
# are capped by entry.max_entries, never by safety.max_executions.
EXECUTION_STATES = {"TRIMMED", "EXITED", "FLIPPED", "FLIP_INCOMPLETE", "EXIT_INCOMPLETE"}
TRADE_ACTIONS = {"TRIM_HELD", "EXIT_HELD", "ENTER_YES", "ENTER_NO"}
_ALERT_ONLY_STARTUP_BLOCKERS = {"operator_mode_alert_only"}


def exact_domain(domain: str) -> str:
    return domain.lower().removeprefix("www.")


def domain_allowed(domain: str, allowed: list[str]) -> bool:
    normalized = exact_domain(domain)
    return any(normalized == item or normalized.endswith("." + item) for item in allowed)


def promoted_feed_summary_auto_trade_allowed(domain: str) -> bool:
    return exact_domain(domain) in PROMOTED_FEED_SUMMARY_AUTO_TRADE_DOMAINS


def execution_count(store: StateStore) -> int:
    current = store.current()
    current_state = current.state if current is not None else None
    count = 0
    for state in EXECUTION_STATES:
        if store.marker(state) is not None:
            count += 1
        elif current_state == state:
            count += 1
    return count


def _config_holdings(config: BinaryBotConfig) -> HoldingsStore:
    data_dir = config.data_dir / "dry_run" if config.execution.dry_run else config.data_dir
    return HoldingsStore(data_dir, default_held=config.market.held_side.lower() or None)


def _entry_summary(config: BinaryBotConfig) -> dict[str, Any]:
    return {
        "enabled": config.entry.enabled,
        "side": config.entry.side,
        "usd_budget": config.entry.usd_budget,
        "max_price": config.entry.max_price,
        "max_entries": config.entry.max_entries,
    }


def inspect_binary_command(config_path: Path) -> int:
    config = load_binary_config(config_path)
    holdings = _config_holdings(config)
    result: dict[str, Any] = {
        "config": str(config_path),
        "market": {
            "slug": config.market.slug,
            "question": config.market.question,
            "deadline_date": config.market.deadline_date,
            "held_side": config.market.held_side,
        },
        "holdings": holdings.record().as_dict(),
        "entry": _entry_summary(config),
        "classifier": {"provider": config.classifier.provider, "model": config.classifier.model},
    }
    try:
        _, verification = load_and_verify_market(config)
        result["market_verification"] = verification.as_dict()
    except Exception as exc:
        result["market_verification_error"] = str(exc)
    print(json.dumps(result, indent=2, sort_keys=True))
    print(
        "\nManual confirmation required before live mode. Review market_verification above and "
        "pin market.expected_rule_text_sha256 in config after review."
    )
    return 0


def preflight_binary_command(config_path: Path, live_flag: bool = False) -> int:
    config = load_binary_config(config_path)
    adapter = _live_adapter() if live_flag else DryRunTradingAdapter()
    result = _binary_preflight_result(config_path, config, live_flag=live_flag, adapter=adapter)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["status"] == "blocked" else 0


def _binary_preflight_result(
    config_path: Path,
    config: BinaryBotConfig,
    *,
    live_flag: bool,
    adapter: TradingAdapter,
    verification: BinaryMarketVerification | None = None,
) -> dict[str, Any]:
    holdings = _config_holdings(config)
    held = holdings.held_location()
    gate = OperatorGate(config_path, config)
    status = gate.status(live_requested=live_flag)
    result: dict[str, Any] = {
        "status": "blocked" if status.blockers else "ok",
        "operator": status.as_dict(),
        "config": str(config_path),
        "held_side": held,
        "holdings": holdings.record().as_dict(),
        "entry": _entry_summary(config),
        "execution_backend": live_backend_name() if live_flag else "dry_run",
    }
    try:
        verification = verification or load_and_verify_market(config)[1]
        result["market_verification"] = verification.as_dict()
        if live_flag and not verification.tradeable:
            status.blockers.append("market_not_tradeable")
    except Exception as exc:
        result["market_verification_error"] = str(exc)
        status.blockers.append("market_verification_failed")
        verification = None
    if held is not None and verification is not None:
        try:
            position = adapter.query_live_position(verification.yes_token_id, verification.no_token_id)
            result["live_position"] = {"yes_shares": position.yes_shares, "no_shares": position.no_shares}
        except Exception as exc:
            result["live_position_error"] = str(exc)
            status.blockers.append("live_position_query_failed")
    elif held is None and not config.entry.enabled:
        status.blockers.append("flat_without_entry_enabled")
    result["operator"] = status.as_dict()
    result["status"] = "blocked" if status.blockers else "ok"
    return result


def ack_binary_live_command(config_path: Path, note: str = "") -> int:
    config = load_binary_config(config_path)
    gate = OperatorGate(config_path, config)
    print(json.dumps(gate.write_ack(note=note), indent=2, sort_keys=True))
    return 0


def set_binary_mode_command(config_path: Path, mode: str) -> int:
    config = load_binary_config(config_path)
    gate = OperatorGate(config_path, config)
    print(json.dumps(gate.set_position_mode(mode), indent=2, sort_keys=True))
    return 0


def smoke_binary_classifier_command(
    config_path: Path,
    *,
    url: str | None = None,
    text: str | None = None,
    title: str = "classifier smoke",
    domain: str = "reuters.com",
) -> int:
    if not url and not text:
        raise SystemExit("smoke-binary-classifier requires --url or --text")
    config = load_binary_config(config_path)
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
    classifier = build_binary_classifier(config)
    try:
        factors = classifier.classify(article, config.market.resolution_rules, held_side=held or "")
    except Exception as exc:
        print(json.dumps({"article": article.__dict__, "ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 1
    decision = entry_decision(config, factors) if held is None else held_decision(config, factors, held)
    print(
        json.dumps(
            {
                "article": article.__dict__,
                "factors": asdict(factors),
                "decision": {"action": decision.action, "level": decision.level, "reason": decision.reason},
                "ok": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _hard_live_startup_blockers(preflight: dict[str, Any]) -> list[str]:
    operator = preflight.get("operator")
    blockers = operator.get("blockers") if isinstance(operator, dict) else None
    if not isinstance(blockers, list):
        return []
    return [str(blocker) for blocker in blockers if str(blocker) not in _ALERT_ONLY_STARTUP_BLOCKERS]


def run_binary_command(config_path: Path, live_flag: bool) -> int:
    config = load_binary_config(config_path)
    if not config.sources.poll_urls and not config.sources.feed_urls and not config.time_decay.enabled:
        raise SystemExit("no sources configured and time_decay is disabled")
    if not config.execution.dry_run and not live_flag:
        raise SystemExit("execution.dry_run=false requires --live")
    if live_flag and config.execution.dry_run:
        raise SystemExit("--live requires execution.dry_run=false")
    holdings = _config_holdings(config)
    if holdings.held_location() is None and not config.entry.enabled:
        raise SystemExit("holdings are flat and entry is disabled; nothing to protect or enter")
    _, verification = load_and_verify_market(config)
    adapter: TradingAdapter
    if live_flag:
        if not verification.tradeable:
            raise SystemExit("market is not active/open/accepting orders; refusing live execution")
        adapter = _live_adapter(tick_size=verification.tick_size, neg_risk=verification.neg_risk)
    else:
        adapter = PaperTradingAdapter(
            state_path=config.data_dir / "dry_run" / "paper_broker.json",
            quote_provider=PublicClobQuoteAdapter(
                [verification.yes_token_id, verification.no_token_id],
                refresh_seconds=2.0,
            ),
            token_pairs=[(verification.yes_token_id, verification.no_token_id)],
            fee_bps=config.execution.paper_fee_bps,
            slippage_bps=config.execution.paper_slippage_bps,
            max_book_age_seconds=config.execution.paper_max_book_age_seconds,
        )
    gate = OperatorGate(config_path, config)
    if live_flag:
        preflight = _binary_preflight_result(config_path, config, live_flag=True, adapter=adapter, verification=verification)
        log_event("binary_live_preflight", **preflight)
        print(json.dumps(preflight, indent=2, sort_keys=True))
        if _hard_live_startup_blockers(preflight):
            raise SystemExit("live preflight blocked execution; fix blockers or use ack/set-mode commands")
    bot = BinaryRuleBot(config=config, market=verification, adapter=adapter, operator_gate=gate, live_requested=live_flag)
    from polybot.core.runtime import ProcessLock

    with ProcessLock(bot.store.data_dir / "process.lock"):
        return _run_loop(bot, config, live_flag)


def _run_loop(bot: "BinaryRuleBot", config: BinaryBotConfig, live_flag: bool) -> int:
    while True:
        try:
            bot.run_once()
        except Exception as exc:
            log_event("binary_run_once_error", error=str(exc))
            try:
                bot.notifier.notify("Binary rule bot polling cycle failed; continuing", error=str(exc))
            except Exception as notify_exc:
                log_event("binary_notify_failed", error=str(notify_exc))
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


class BinaryRuleBot:
    def __init__(
        self,
        *,
        config: BinaryBotConfig,
        market: BinaryMarketVerification,
        adapter: TradingAdapter,
        operator_gate: OperatorGate | None = None,
        live_requested: bool = False,
    ):
        self.config = config
        self.market = market
        self.store = StateStore(config.data_dir / "dry_run" if config.execution.dry_run else config.data_dir)
        article_store_path = (
            config.data_dir / "central_feed_articles.jsonl"
            if config.sources.central_feed_db
            else config.logs_dir / "binary_articles.jsonl"
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
        self.classifier = build_binary_classifier(config)
        # Cheap/fast screen tier (see the location runner for rationale).
        self.screen_classifier = None
        if config.classifier.screen_model and config.classifier.provider != "rule_based":
            from dataclasses import replace as _replace

            from .classifier import LLMBinaryClassifier

            self.screen_classifier = LLMBinaryClassifier(
                _replace(config.classifier, model=config.classifier.screen_model), config
            )
        self.classifier_budget = ClassifierBudgetStore(
            self.store.data_dir,
            Path(config.classifier.budget_db_path) if config.classifier.budget_db_path else None,
        )
        self.executor = BinaryExecutor(config, market, self.store, self.notifier, adapter)
        self.holdings = self.executor.holdings
        # Event-anchored book capture shares the executor's logger (same
        # data_dir, same enable flag) so gate/order/fill snapshots interleave
        # in one book_snapshots.jsonl per market.
        self.book_snapshots = self.executor.book_snapshots
        self.operator_gate = operator_gate
        self.live_requested = live_requested

    def run_once(self) -> list[BinaryDecision]:
        self._write_heartbeat()
        if self.operator_gate is not None and self.operator_gate.current_mode() == "off":
            log_event("binary_operator_off_cycle_skip")
            return []
        if self.live_requested and not self.config.execution.dry_run:
            reconciliation = self.executor.reconcile_live_holding()
            if reconciliation.get("changed"):
                log_event("binary_wallet_reconciled", **reconciliation)
        self._check_corroboration_deadline()
        decisions: list[BinaryDecision] = []
        take_profit = self._check_take_profit()
        if take_profit is not None:
            decisions.append(take_profit)
        held = self.holdings.held_location()
        decay = time_decay_decision(self.config, held)
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
            try:
                if kind == "poll":
                    return kind, [fetch_article(url, SETTINGS.user_agent)]
                return kind, fetch_feed_articles(
                    url,
                    SETTINGS.user_agent,
                    include_terms=self.config.sources.feed_include_terms,
                    exclude_terms=self.config.sources.feed_exclude_terms,
                    limit=self.config.sources.max_feed_entries_per_cycle,
                )
            except Exception as exc:
                log_event("binary_source_fetch_error", url=url, error=str(exc))
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
                # Shared feed rows are local and should be classified before a
                # slow per-market poll URL can delay the breaking-news path.
                results.insert(0, ("central_feed", batch.articles))
            except Exception as exc:
                log_event("binary_central_feed_unavailable", error=str(exc))
        return results

    def _decay_still_actionable(self, decay: BinaryDecision) -> bool:
        current = self.store.current()
        if current is not None and current.state in TERMINAL_STATES:
            log_event("binary_time_decay_skip_terminal", state=current.state, action=decay.action)
            return False
        if decay.action == "TRIM_HELD" and (
            (current is not None and current.state == "TRIMMED") or self.store.marker("TRIMMED") is not None
        ):
            log_event("binary_time_decay_skip_already_trimmed", action=decay.action)
            return False
        return True

    def process_article(self, article: Article) -> BinaryDecision:
        if not should_escalate_binary_article(article, self.config):
            decision = BinaryDecision("ALERT_ONLY", "1", "keyword_gate_no_trigger")
            self._log_decision(article, decision)
            return decision
        self.notifier.notify(
            _bounded_notify_text(article),
            title=article.title,
            url=article.url,
            domain=article.domain,
            published_at=article.published_at,
        )
        # The repricing window starts NOW: capture the book while the price is
        # (possibly) still stale, before classification spends seconds.
        self.book_snapshots.snapshot(
            [self.market.yes_token_id], moment="gate_escalation", article_hash=article.hash, domain=article.domain
        )
        age_hours = _article_age_hours(article)
        max_age = self.config.sources.max_trade_article_age_hours
        if max_age > 0 and age_hours is not None and age_hours > max_age:
            decision = BinaryDecision("ALERT_ONLY", "3", f"article_stale_skipped_classification:{age_hours:.0f}h")
            self._log_decision(article, decision)
            return decision
        if _is_feed_summary(article) and not self.config.classifier.classify_feed_summaries:
            decision = BinaryDecision("ALERT_ONLY", "3", "feed_summary_classification_disabled")
            self._log_decision(article, decision)
            return decision
        block_reason = self.classifier_budget.block_reason(self.config.classifier)
        if block_reason is not None:
            decision = BinaryDecision("ALERT_ONLY", "3", block_reason)
            self._log_decision(article, decision)
            self._notify_classifier_budget_block_once(block_reason)
            return decision
        # Screen tier gates the strong model ONLY WHILE FLAT (see the location
        # runner): defense must never depend on the weakest model.
        if self.screen_classifier is not None and self.holdings.held_location() is None:
            screen_decision = self._screen_stage(article)
            if screen_decision is not None:
                return screen_decision
        try:
            passes = self._run_confirm_passes(article)
        except ClassifierBudgetExceeded as exc:
            decision = BinaryDecision("ALERT_ONLY", "3", exc.reason)
            self._log_decision(article, decision)
            self._notify_classifier_budget_block_once(exc.reason)
            return decision
        except Exception as exc:
            self.classifier_budget.record_error()
            decision = BinaryDecision("ALERT_ONLY", "3", f"classifier_error:{exc}")
            self._log_decision(article, decision)
            self.notifier.notify("Binary classifier unavailable or failed; no trade", error=str(exc))
            return decision
        held = self.holdings.held_location()
        if self.config.classifier.require_pass_agreement:
            decision = classify_agreement(self.config, passes, held_side=held)
        elif held is None:
            decision = entry_decision(self.config, passes[0])
        else:
            decision = held_decision(self.config, passes[0], held)
        decision = self._verify_quote_or_alert(decision, article)
        decision = self._enforce_source_policy(article, decision)
        decision = self._enforce_execution_policy(decision)
        self._log_decision(article, decision)
        self._corroboration_on_decision(decision, article)
        if decision.action == "ALERT_ONLY":
            self.notifier.notify("Binary rule bot alert only; no trade", level=decision.level, reason=decision.reason, url=article.url)
        return self._execute_if_allowed(decision, article)

    def _verify_quote_or_alert(self, decision: BinaryDecision, article: Article) -> BinaryDecision:
        if decision.factors is None:
            return decision
        is_trade_action = decision.action in TRADE_ACTIONS
        requires_quote = self.config.safety.quote_must_match_article_text and is_trade_action
        if not requires_quote and decision.level not in {"4A", "4B"}:
            return decision
        if quote_in_article(decision.factors.quote_supporting_trigger, article.raw_text):
            return decision
        return BinaryDecision("ALERT_ONLY", "3", "quote_verification_failed", decision.factors)

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
            log_event("binary_feed_promotion_failed", url=article.url, domain=article.domain)
            return None
        return promoted

    def _enforce_source_policy(self, article: Article, decision: BinaryDecision) -> BinaryDecision:
        if decision.action not in TRADE_ACTIONS:
            return decision
        if article.source_kind in {"feed", "promoted_feed_summary"} and not self.config.sources.allow_feed_auto_trade:
            return BinaryDecision("ALERT_ONLY", "3", "feed_item_auto_trade_disabled", decision.factors)
        if article.source_kind == "promoted_feed_summary" and not promoted_feed_summary_auto_trade_allowed(article.domain):
            return BinaryDecision("ALERT_ONLY", "3", "promoted_feed_summary_domain_not_auto_trade", decision.factors)
        age_hours = _article_age_hours(article)
        max_age = self.config.sources.max_trade_article_age_hours
        if max_age > 0 and article.published_at is None and not self.config.sources.allow_unknown_age_poll_auto_trade:
            return BinaryDecision("ALERT_ONLY", "3", "article_age_unknown_for_auto_trade", decision.factors)
        if max_age > 0 and age_hours is not None and age_hours > max_age:
            return BinaryDecision("ALERT_ONLY", "3", f"article_stale_for_auto_trade:{age_hours:.0f}h", decision.factors)
        if domain_allowed(article.domain, self.config.sources.alert_only_domains):
            return BinaryDecision("ALERT_ONLY", "3", "source_domain_alert_only", decision.factors)
        if not domain_allowed(article.domain, self.config.sources.auto_trade_domains):
            return BinaryDecision("ALERT_ONLY", "3", "source_domain_not_auto_trade", decision.factors)
        return decision

    def _enforce_execution_policy(self, decision: BinaryDecision) -> BinaryDecision:
        if decision.action not in TRADE_ACTIONS:
            return decision
        if not self.config.trigger.trusted_single_source_execution:
            return BinaryDecision("ALERT_ONLY", "3", "single_source_execution_disabled", decision.factors)
        if not _level_meets_threshold(decision.level, self.config.trigger.auto_execute_level):
            return BinaryDecision("ALERT_ONLY", "3", "below_auto_execute_level", decision.factors)
        if decision.action in {"ENTER_YES", "ENTER_NO"}:
            if not self.config.entry.enabled:
                return BinaryDecision("ALERT_ONLY", "3", "entry_disabled", decision.factors)
            entry_count = self.executor.entry_count()
            if entry_count >= self.config.entry.max_entries:
                return BinaryDecision("ALERT_ONLY", "3", f"max_entries_reached:{entry_count}", decision.factors)
        else:
            if self.config.safety.max_executions <= 0:
                return BinaryDecision("ALERT_ONLY", "3", "max_executions_zero", decision.factors)
            count = execution_count(self.store)
            if count >= self.config.safety.max_executions:
                return BinaryDecision("ALERT_ONLY", "3", f"max_executions_reached:{count}", decision.factors)
        return decision

    def _execute_if_allowed(self, decision: BinaryDecision, article: Article) -> BinaryDecision:
        if self.operator_gate is not None:
            gate_result = self.operator_gate.check(decision, live_requested=self.live_requested)
            if not gate_result.allowed:
                if self.operator_gate.log_block_once(gate_result, decision):
                    self.notifier.notify(
                        "Binary rule bot execution blocked by operator gate",
                        action=decision.action,
                        mode=gate_result.mode,
                        reason=gate_result.reason,
                        url=article.url,
                    )
                blocked = BinaryDecision("ALERT_ONLY", "3", f"operator_block:{gate_result.reason}", decision.factors)
                self._log_decision(article, blocked)
                return blocked
        result = self.executor.execute(decision, article)
        self._maybe_start_corroboration(result, article)
        return decision

    def _check_take_profit(self) -> BinaryDecision | None:
        """Sell into strength: once the held side's bid reaches the target,
        the remaining upside is small against full resolution/UMA risk. Exits
        through the normal gated path (staged sell, ledger settle)."""
        target = self.config.position.take_profit_price
        if target <= 0:
            return None
        held = self.holdings.held_location()
        if held is None:
            return None
        current = self.store.current()
        if current is not None and current.state in TERMINAL_STATES:
            return None
        if held == "yes":
            bid = self.executor.adapter.yes_best_bid(self.market.yes_token_id)
        else:
            # Mirrored book: the NO bid is the complement of the YES ask.
            yes_ask = self.executor.adapter.yes_best_ask(self.market.yes_token_id)
            bid = round(1.0 - yes_ask, 4) if yes_ask is not None else None
        if bid is None or bid < target:
            return None
        decision = BinaryDecision("EXIT_HELD", "TIME", f"take_profit_target_reached:{bid}")
        log_event("binary_take_profit_triggered", held=held, bid=bid, target=target)
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
        log_event("binary_corroboration_started", entry_domain=article.domain, minutes=self.config.entry.corroboration_minutes)

    def _corroboration_on_decision(self, decision: BinaryDecision, article: Article) -> None:
        """A held-thesis reinforcement from a DIFFERENT domain satisfies the
        post-entry corroboration requirement."""
        if self.config.entry.corroboration_minutes <= 0:
            return
        if decision.reason not in {"held_yes_thesis_reinforced", "held_no_thesis_reinforced"}:
            return
        if self._corroboration_tracker().satisfy(
            article.domain,
            origin_organization=article.origin_organization,
            byline=article.byline,
        ):
            log_event("binary_corroboration_satisfied", domain=article.domain)

    def _check_corroboration_deadline(self) -> None:
        if self.config.entry.corroboration_minutes <= 0:
            return
        tracker = self._corroboration_tracker()
        record = tracker.overdue()
        if record is None:
            return
        held = self.holdings.held_location()
        if held is None:
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
        log_event("binary_corroboration_overdue", entry_domain=record.get("entry_domain"), action=action)
        if action == "trim":
            decision = BinaryDecision("TRIM_HELD", "TIME", "corroboration_window_expired_trim")
            self._execute_if_allowed(decision, _synthetic_article(decision.reason))
        tracker.mark_escalated()

    def _write_heartbeat(self) -> None:
        from polybot.core.holdings import _atomic_json_write

        _atomic_json_write(self.store.data_dir / "heartbeat.json", {"at": datetime.now(timezone.utc).isoformat()})

    def _log_decision(self, article: Article, decision: BinaryDecision) -> None:
        append_jsonl(
            self.config.logs_dir / "binary_decisions.jsonl",
            {
                "article": article.__dict__,
                "decision": {
                    "action": decision.action,
                    "level": decision.level,
                    "reason": decision.reason,
                    "factors": decision.factors.__dict__ if decision.factors else None,
                },
            },
        )

    def _screen_stage(self, article: Article) -> BinaryDecision | None:
        """One cheap-model pass. Returns the final decision when the screen
        says NO_ACTION (the strong model never runs), or None to escalate. A
        screen failure escalates rather than blocks: the screen tier may only
        save money, never miss a trade."""
        try:
            screen = self._classify_with_budget(article, 0, classifier=self.screen_classifier, stage="screen")
        except ClassifierBudgetExceeded as exc:
            decision = BinaryDecision("ALERT_ONLY", "3", exc.reason)
            self._log_decision(article, decision)
            self._notify_classifier_budget_block_once(exc.reason)
            return decision
        except Exception as exc:
            log_event("binary_screen_classifier_error", error=str(exc))
            if not self.live_requested:
                # Paper posture fails CLOSED: a broken screen path must not
                # convert every noise article into full-price confirm passes.
                # Live keeps the fail-open contract: the screen tier may only
                # save money, never miss a trade defending a real position.
                decision = BinaryDecision("NO_ACTION", "1", "screen_error_paper_fail_closed")
                self._log_decision(article, decision)
                return decision
            return None
        held = self.holdings.held_location()
        provisional = entry_decision(self.config, screen) if held is None else held_decision(self.config, screen, held)
        if provisional.action != "NO_ACTION":
            return None
        decision = BinaryDecision("NO_ACTION", provisional.level, f"screen:{provisional.reason}", screen)
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
        context = self.config.market.resolution_rules
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
        log_event("binary_classifier_attempt", **telemetry)
        if hasattr(active, "last_usage"):
            setattr(active, "last_usage", None)
        factors = active.classify(article, context, held_side=self.holdings.held_location() or "")
        usage = getattr(active, "last_usage", None)
        if isinstance(usage, dict):
            telemetry["usage"] = usage
        log_event("binary_classifier_result", **telemetry)
        return factors

    def _notify_classifier_budget_block_once(self, reason: str) -> None:
        window = "hour" if reason in {"classifier_budget_exhausted_hourly", "classifier_error_cap_exceeded"} else "day"
        if self.classifier_budget.mark_notified_once(reason, window):
            self.notifier.notify("Binary rule bot classifier budget blocked classification", reason=reason)


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


_NOTIFY_TEXT_LIMIT = 3500


def _bounded_notify_text(article: Article) -> str:
    body = article.raw_text.strip()
    if len(body) > _NOTIFY_TEXT_LIMIT:
        body = body[:_NOTIFY_TEXT_LIMIT] + "\n[truncated]"
    parts = [article.title.strip(), "", body, "", f"Source: {article.url}"]
    return "\n".join(part for part in parts if part)
