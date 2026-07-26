from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.config import SETTINGS
from polybot.core.budget import ClassifierBudgetExceeded, ClassifierBudgetStore
from polybot.core.execution import live_adapter_from_env
from polybot.gamma import MarketMeta
from polybot.log import log_event

from .classifier import build_classifier, run_classifier_passes
from .config import ClassifierConfig, IranBotConfig, load_iran_config
from .decision import Decision, classify_agreement, final_decision, time_decay_decision, verify_quote_or_alert
from .executor import DryRunTradingAdapter, FlipExecutor, LiveClobTradingAdapter, TradingAdapter
from .keyword_gate import keyword_gate
from .market_verifier import load_and_verify_market
from .notifier import TelegramNotifier
from .operator import OperatorGate, build_preflight
from .source_fetcher import ArticleStore, fetch_article, fetch_feed_articles, promote_feed_article
from .storage import StateStore, append_jsonl
from .types import Article


TRADE_ACTIONS = {"SELL_NO_CONDITIONAL_BUY_YES", "SELL_NO_BUY_YES", "TRIM_YES", "EXIT_YES_ONLY", "EXIT_YES_OPTIONAL_BUY_NO"}


def domain_allowed(domain: str, allowed: list[str]) -> bool:
    normalized = domain.lower().removeprefix("www.")
    return any(normalized == item or normalized.endswith("." + item) for item in allowed)


def inspect_iran_command(config_path: Path) -> int:
    config = load_iran_config(config_path)
    _market, verification = load_and_verify_market(config)
    print(json.dumps(verification.as_dict(), indent=2, sort_keys=True))
    print("\nManual confirmation required before live mode. Pin rule_text_sha256 in config after review.")
    return 0


def inspect_iran_position_command(config_path: Path) -> int:
    config = load_iran_config(config_path)
    market, verification = load_and_verify_market(config)
    result = {
        "config": str(config_path),
        "market": {
            "slug": config.market.slug,
            "target_leg": config.market.target_leg,
            "held_side": config.market.held_side,
            "condition_id": market.condition_id,
            "question": market.question,
            "tradeable": market.tradeable(),
            "active": market.active,
            "closed": market.closed,
            "accepting_orders": market.accepting_orders,
        },
        "rule_text_sha256": verification.rule_text_sha256,
        "tokens": {
            "yes_token_id": market.yes_token_id,
            "no_token_id": market.no_token_id,
            "expected_yes_token_id": config.position.expected_yes_token_id,
            "expected_no_token_id": config.position.expected_no_token_id,
            "token_mapping_matches_config": (
                (not config.position.expected_yes_token_id or config.position.expected_yes_token_id == market.yes_token_id)
                and (not config.position.expected_no_token_id or config.position.expected_no_token_id == market.no_token_id)
            ),
        },
        "clob_settings": {
            "host": SETTINGS.clob_host,
            "chain_id": SETTINGS.chain_id,
            "signature_type": SETTINGS.signature_type,
            "funder_address": SETTINGS.funder_address,
            "has_private_key": bool(SETTINGS.private_key),
            "has_api_creds": bool(SETTINGS.clob_api_key and SETTINGS.clob_secret and SETTINGS.clob_passphrase),
        },
    }
    try:
        adapter = LiveClobTradingAdapter()
        position = adapter.query_live_position(market.yes_token_id, market.no_token_id)
    except Exception as exc:
        result["live_position_error"] = str(exc)
        result["diagnosis"] = "could not query live CLOB balances; check API credentials and environment"
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1
    result["live_position"] = {
        "yes_shares": position.yes_shares,
        "no_shares": position.no_shares,
    }
    result["diagnosis"] = _position_diagnosis(config.market.held_side, position.yes_shares, position.no_shares)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def preflight_iran_command(config_path: Path, live_flag: bool = False) -> int:
    config = load_iran_config(config_path)
    market, _verification = load_and_verify_market(config)
    adapter = _live_adapter_for_market(market)
    gate = OperatorGate(config_path, config)
    result = build_preflight(config_path=config_path, config=config, market=market, adapter=adapter, live_requested=live_flag, gate=gate)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["status"] == "blocked" else 0


def ack_iran_live_command(config_path: Path, note: str = "") -> int:
    config = load_iran_config(config_path)
    gate = OperatorGate(config_path, config)
    print(json.dumps(gate.write_ack(note=note), indent=2, sort_keys=True))
    return 0


def set_iran_mode_command(config_path: Path, mode: str) -> int:
    config = load_iran_config(config_path)
    gate = OperatorGate(config_path, config)
    print(json.dumps(gate.set_position_mode(mode), indent=2, sort_keys=True))
    return 0


def probe_iran_clob_v2_command(config_path: Path, amount: float = 5.0, post: bool = False, price: float | None = None) -> int:
    config = load_iran_config(config_path)
    market, _verification = load_and_verify_market(config)
    if config.market.held_side.upper() == "YES":
        token_id = market.yes_token_id
        side = "SELL"
        config_floor = config.execution.sell_yes.min_price
    else:
        token_id = market.no_token_id
        side = "SELL"
        config_floor = config.execution.sell_no.min_price
    probe_price = 0.99 if post else (price if price is not None else config_floor)
    env = dict(os.environ)
    env.setdefault("TMPDIR", "/tmp")
    command = [
        "./node_modules/.bin/tsx",
        "tools/polymarket-ts/clobV2ExecutionProbe.ts",
        "--token-id",
        token_id,
        "--condition-id",
        market.condition_id,
        "--side",
        side,
        "--amount",
        str(amount),
        "--price",
        str(probe_price),
        "--tick-size",
        market.tick_size,
        "--neg-risk",
        "true" if market.neg_risk else "false",
        "--post",
        "true" if post else "false",
    ]
    completed = subprocess.run(command, env=env, check=False)
    return int(completed.returncode)


def smoke_iran_classifier_command(
    config_path: Path,
    *,
    url: str | None = None,
    text: str | None = None,
    title: str = "classifier smoke",
    domain: str = "reuters.com",
) -> int:
    if not url and not text:
        raise SystemExit("smoke-iran-classifier requires --url or --text")
    config = load_iran_config(config_path)
    _market, verification = load_and_verify_market(config)
    article = fetch_article(url, SETTINGS.user_agent) if url else Article(
        url="smoke://local",
        domain=domain,
        title=title,
        published_at=None,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        raw_text=text or "",
        hash=f"smoke:{hash(text or '')}",
    )
    classifier = build_classifier(config.classifier, config.sources)
    budget_status = ClassifierBudgetStore(config.data_dir).status(config.classifier)
    try:
        passes = run_classifier_passes(
            classifier,
            article,
            _classifier_context_for(config, verification.rule_text),
            config.classifier.passes,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "article": article.__dict__,
                    "classifier": {
                        "provider": config.classifier.provider,
                        "model": config.classifier.model,
                        "passes": config.classifier.passes,
                        "budget": budget_status,
                    },
                    "ok": False,
                    "error": str(exc),
                    "executed": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    if config.classifier.require_pass_agreement:
        decision = classify_agreement(passes, held_side=config.market.held_side)
    else:
        decision = final_decision(passes[0], held_side=config.market.held_side)
    if config.classifier.require_verbatim_quote and config.safety.quote_must_match_article_text:
        decision = verify_quote_or_alert(decision, article.raw_text)
    print(
        json.dumps(
            {
                "article": article.__dict__,
                "passes": [asdict(item) for item in passes],
                "decision": {
                    "action": decision.action,
                    "level": decision.level,
                    "reason": decision.reason,
                    "factors": asdict(decision.factors) if decision.factors else None,
                },
                "pass_agreement": len(passes) <= 1 or all(passes[0] == item for item in passes[1:]),
                "classifier": {
                    "provider": config.classifier.provider,
                    "model": config.classifier.model,
                    "passes": config.classifier.passes,
                    "budget": budget_status,
                },
                "executed": False,
                "ok": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def run_iran_command(config_path: Path, live_flag: bool) -> int:
    config = load_iran_config(config_path)
    if not config.sources.poll_urls and not config.sources.feed_urls and not config.time_decay.enabled:
        raise SystemExit("no sources configured and time_decay is disabled; configure sources.poll_urls, sources.feed_urls, or enable time decay")
    if not config.execution.dry_run and not live_flag:
        raise SystemExit("execution.dry_run=false requires --live")
    if live_flag and config.execution.dry_run:
        raise SystemExit("--live requires execution.dry_run=false")
    market, verification = load_and_verify_market(config)
    if live_flag and not market.tradeable():
        raise SystemExit("market is not active/open/accepting orders; refusing live execution")
    adapter = _live_adapter_for_market(market) if live_flag else DryRunTradingAdapter()
    gate = OperatorGate(config_path, config)
    if live_flag:
        preflight = build_preflight(config_path=config_path, config=config, market=market, adapter=adapter, live_requested=True, gate=gate)
        log_event("iran_live_preflight", **preflight)
        print(json.dumps(preflight, indent=2, sort_keys=True))
        if preflight["status"] == "blocked":
            raise SystemExit("live preflight blocked execution; fix blockers or use ack/set-mode commands")
    bot = IranProtectionBot(
        config=config,
        market=market,
        market_rule_text=verification.rule_text,
        adapter=adapter,
        operator_gate=gate,
        live_requested=live_flag,
    )
    while True:
        try:
            bot.run_once()
        except Exception as exc:
            log_event("iran_run_once_error", error=str(exc))
            try:
                bot.notifier.notify("Iran protection polling cycle failed; continuing", error=str(exc))
            except Exception as notify_exc:  # never let alerting kill the loop
                log_event("iran_notify_failed", error=str(notify_exc))
        time.sleep(config.safety.poll_seconds)


def _live_adapter_for_market(market: MarketMeta) -> TradingAdapter:
    return live_adapter_from_env(tick_size=market.tick_size, neg_risk=market.neg_risk)


class IranProtectionBot:
    def __init__(
        self,
        *,
        config: IranBotConfig,
        market: MarketMeta,
        market_rule_text: str,
        adapter: TradingAdapter,
        operator_gate: OperatorGate | None = None,
        live_requested: bool = False,
    ) -> None:
        self.config = config
        self.market = market
        self.market_rule_text = market_rule_text
        self.store = StateStore(config.data_dir / "dry_run" if config.execution.dry_run else config.data_dir)
        self.article_store = ArticleStore(config.logs_dir / "articles.jsonl")
        self.notifier = TelegramNotifier()
        self.classifier = build_classifier(config.classifier, config.sources)
        self.classifier_budget = ClassifierBudgetStore(
            self.store.data_dir,
            Path(config.classifier.budget_db_path) if config.classifier.budget_db_path else None,
        )
        self.executor = FlipExecutor(config, self.store, self.notifier, adapter)
        self.operator_gate = operator_gate
        self.live_requested = live_requested

    def run_once(self) -> list[Decision]:
        if self.operator_gate is not None and self.operator_gate.current_mode() == "off":
            log_event("iran_operator_off_cycle_skip")
            return []
        decisions: list[Decision] = []
        decay = self._process_time_decay()
        if decay.action != "NO_ACTION":
            decisions.append(decay)
        for url in self.config.sources.poll_urls:
            try:
                article = fetch_article(url, SETTINGS.user_agent)
            except Exception as exc:
                log_event("iran_source_fetch_error", url=url, error=str(exc))
                continue
            if not self.article_store.store(article):
                continue
            # poll_urls is the curated, always-relevant Iran-blog tracker (not
            # the broad general feed), so every distinct update from it is
            # pushed to Telegram unconditionally -- no keyword filtering.
            decisions.append(self.process_article(article, always_notify=True))
        for feed_url in self.config.sources.feed_urls:
            try:
                articles = fetch_feed_articles(
                    feed_url,
                    SETTINGS.user_agent,
                    include_terms=self.config.sources.feed_include_terms,
                    exclude_terms=self.config.sources.feed_exclude_terms,
                    limit=self.config.sources.max_feed_entries_per_cycle,
                )
            except Exception as exc:
                log_event("iran_feed_fetch_error", url=feed_url, error=str(exc))
                continue
            for article in articles:
                if not self.article_store.store(article):
                    continue
                decisions.append(self.process_article(article))
                promoted = self._promote_feed_article(article)
                if promoted is None or not self.article_store.store(promoted):
                    continue
                decisions.append(self.process_article(promoted))
        return decisions

    def process_article(self, article: Article, *, always_notify: bool = False) -> Decision:
        if always_notify:
            # This article came straight from the curated Iran-blog tracker
            # (sources.poll_urls), not the broad general feed, so every
            # distinct update is pushed to Telegram unconditionally -- no
            # keyword filtering. This is a live-news ping, not a trade
            # signal; the ALERT_ONLY/trade notifications below still fire
            # separately once a verdict is reached.
            # Telegram only receives the `message` string (fields below are
            # for structured logging only), so the full article text is
            # embedded directly in the message, chunked to stay under
            # Telegram's per-message character limit.
            for chunk in _chunk_telegram_message(_format_live_update_message(article)):
                self.notifier.notify(
                    chunk,
                    title=article.title,
                    url=article.url,
                    domain=article.domain,
                    published_at=article.published_at,
                )
        gate = keyword_gate(f"{article.title}\n{article.raw_text}")
        if not gate.escalate:
            decision = Decision("NO_ACTION", "0", "keyword_gate_no_escalation")
            self._log_decision(article, decision, gate=gate.__dict__)
            return decision
        if not always_notify:
            # For non-poll_urls sources (the broad general feed), still only
            # ping on genuinely Iran/talks-relevant escalations so World Cup
            # scores and unrelated regional news don't flood the channel.
            self.notifier.notify(
                "Iran protection: new live update detected",
                title=article.title,
                url=article.url,
                domain=article.domain,
                published_at=article.published_at,
            )
        age_hours = _article_age_hours(article)
        max_age = self.config.sources.max_trade_article_age_hours
        if max_age > 0 and age_hours is not None and age_hours > max_age:
            # Stale items can neither trade nor suspend time decay; skip the
            # classifier entirely instead of paying two LLM passes for a
            # decision the freshness gate would discard.
            decision = Decision("ALERT_ONLY", "3", f"article_stale_skipped_classification:{age_hours:.0f}h")
            self._log_decision(article, decision, gate=gate.__dict__)
            return decision
        if _is_feed_summary(article) and not self.config.classifier.classify_feed_summaries:
            decision = Decision("ALERT_ONLY", "3", "feed_summary_classification_disabled")
            self._log_decision(article, decision, gate=gate.__dict__)
            self.notifier.notify("Iran protection feed summary skipped classifier", reason=decision.reason, url=article.url)
            return decision
        block_reason = self.classifier_budget.block_reason(self.config.classifier)
        if block_reason is not None:
            decision = Decision("ALERT_ONLY", "3", block_reason)
            self._log_decision(article, decision, gate=gate.__dict__)
            self._notify_classifier_budget_block_once(block_reason)
            return decision
        try:
            count = max(1, self.config.classifier.passes)
            block_reason = self.classifier_budget.reserve_attempts(self.config.classifier, count)
            if block_reason is not None:
                raise ClassifierBudgetExceeded(block_reason)
            passes = [self._classify_with_budget(article, index) for index in range(count)]
            if self.config.classifier.require_pass_agreement:
                decision = classify_agreement(passes, held_side=self.config.market.held_side)
            else:
                decision = final_decision(passes[0], held_side=self.config.market.held_side)
        except ClassifierBudgetExceeded as exc:
            decision = Decision("ALERT_ONLY", "3", exc.reason)
            self._notify_classifier_budget_block_once(exc.reason)
            self._log_decision(article, decision, gate=gate.__dict__)
            return decision
        except Exception as exc:
            self.classifier_budget.record_error()
            decision = Decision("ALERT_ONLY", "3", f"classifier_error:{exc}")
            self.notifier.notify("Classifier unavailable or failed; no trade", error=str(exc))
            self._log_decision(article, decision, gate=gate.__dict__)
            return decision
        if self.config.classifier.require_verbatim_quote and self.config.safety.quote_must_match_article_text:
            decision = verify_quote_or_alert(decision, article.raw_text)
        if _is_yes_scheduled_hold(self.config, decision) and domain_allowed(article.domain, self.config.sources.auto_trade_domains):
            self._record_scheduled_hold(article, decision)
        decision = self._enforce_source_policy(article, decision)
        decision = self._enforce_execution_policy(decision)
        self._log_decision(article, decision, gate=gate.__dict__)
        if decision.action == "ALERT_ONLY":
            self.notifier.notify("Iran protection alert only; no trade", level=decision.level, reason=decision.reason, url=article.url)
        if decision.action in TRADE_ACTIONS:
            decision = self._execute_if_allowed(decision, article)
        return decision

    def _process_time_decay(self) -> Decision:
        decision = time_decay_decision(self.config, today=datetime.now(timezone.utc).date())
        if decision.action == "NO_ACTION":
            return decision
        hold = _active_scheduled_hold(self.config, self.store.current())
        if hold is None:
            # A hold signal overwritten in state.json by a later transient state
            # must still suspend time decay while its window is active.
            hold = _active_scheduled_hold(self.config, self.store.marker("YES_SCHEDULED_HOLD_SIGNAL"))
        if hold is not None:
            skipped = Decision("ALERT_ONLY", "3", "time_decay_suspended_by_scheduled_round_signal", decision.factors)
            article = Article(
                url="time-decay://local",
                domain="time-decay.local",
                title=skipped.reason,
                published_at=None,
                fetched_at=datetime.now(timezone.utc).isoformat(),
                raw_text=skipped.reason,
                hash=f"time-decay:{skipped.reason}:{decision.action}",
            )
            self.store.write(
                "TIME_DECAY_SUSPENDED",
                reason=skipped.reason,
                suspended_action=decision.action,
                hold_updated_at=hold.updated_at,
                hold_payload=hold.payload,
            )
            self._log_decision(article, skipped, gate={"time_decay": True, "suspended": True})
            self.notifier.notify("Time decay skipped because a scheduled-round hold signal is active", action=decision.action)
            return skipped
        article = Article(
            url="time-decay://local",
            domain="time-decay.local",
            title=decision.reason,
            published_at=None,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            raw_text=decision.reason,
            hash=f"time-decay:{decision.reason}:{decision.level}",
        )
        self._log_decision(article, decision, gate={"time_decay": True})
        return self._execute_if_allowed(decision, article)

    def _execute_if_allowed(self, decision: Decision, article: Article) -> Decision:
        if self.operator_gate is not None:
            gate_result = self.operator_gate.check(decision, live_requested=self.live_requested)
            if not gate_result.allowed:
                if self.operator_gate.log_block_once(gate_result, decision):
                    self.notifier.notify(
                        "Iran protection execution blocked by operator gate",
                        action=decision.action,
                        mode=gate_result.mode,
                        reason=gate_result.reason,
                        url=article.url,
                    )
                blocked = Decision("ALERT_ONLY", "3", f"operator_block:{gate_result.reason}", decision.factors)
                self._log_decision(article, blocked, gate={"operator_gate": True, "mode": gate_result.mode})
                return blocked
        self.executor.execute(decision, article, self.market)
        return decision

    def _enforce_source_policy(self, article: Article, decision: Decision) -> Decision:
        if article.source_kind == "feed" and not self.config.sources.allow_feed_auto_trade and decision.action in TRADE_ACTIONS:
            return Decision("ALERT_ONLY", "3", "feed_item_auto_trade_disabled", decision.factors)
        if article.source_kind == "promoted_feed_summary" and decision.action in TRADE_ACTIONS:
            return Decision("ALERT_ONLY", "3", "promoted_feed_summary_auto_trade_disabled", decision.factors)
        if decision.action in TRADE_ACTIONS:
            # Feeds resurface old items (and Google News re-emits them under new
            # URLs, defeating hash dedup); never auto-trade on stale news the
            # market has already priced.
            age_hours = _article_age_hours(article)
            max_age = self.config.sources.max_trade_article_age_hours
            if max_age > 0 and age_hours is not None and age_hours > max_age:
                return Decision("ALERT_ONLY", "3", f"article_stale_for_auto_trade:{age_hours:.0f}h", decision.factors)
        if domain_allowed(article.domain, self.config.sources.alert_only_domains):
            return Decision("ALERT_ONLY", "3", "source_domain_alert_only", decision.factors)
        if not domain_allowed(article.domain, self.config.sources.auto_trade_domains):
            return Decision("ALERT_ONLY", "3", "source_domain_not_auto_trade", decision.factors)
        return decision

    def _record_scheduled_hold(self, article: Article, decision: Decision) -> None:
        if self.store.terminal_state() is not None:
            log_event("iran_scheduled_hold_skip", reason="terminal_state_exists", url=article.url)
            return
        current = self.store.current()
        if current is not None and current.state == "YES_SCHEDULED_HOLD_SIGNAL":
            previous_url = current.payload.get("article", {}).get("url")
            if previous_url == article.url:
                return
        self.store.write(
            "YES_SCHEDULED_HOLD_SIGNAL",
            reason=decision.reason,
            decision={
                "action": decision.action,
                "level": decision.level,
                "reason": decision.reason,
                "factors": decision.factors.__dict__ if decision.factors else None,
            },
            article=article.__dict__,
        )
        self.notifier.notify("YES time decay suspended by scheduled-round signal", reason=decision.reason, url=article.url)

    def _promote_feed_article(self, article: Article) -> Article | None:
        if article.source_kind != "feed" or not self.config.sources.promote_feed_to_article:
            return None
        if not domain_allowed(article.domain, self.config.sources.auto_trade_domains):
            return None
        promoted = promote_feed_article(article, SETTINGS.user_agent)
        if promoted is None:
            log_event("iran_feed_promotion_failed", url=article.url, domain=article.domain)
            return None
        if not domain_allowed(promoted.domain, self.config.sources.auto_trade_domains):
            log_event(
                "iran_feed_promotion_rejected",
                reason="promoted_domain_not_auto_trade",
                feed_domain=article.domain,
                promoted_domain=promoted.domain,
                url=promoted.url,
            )
            return None
        return promoted

    def _enforce_execution_policy(self, decision: Decision) -> Decision:
        if decision.action not in TRADE_ACTIONS:
            return decision
        if self.config.trigger.require_two_sources:
            return Decision("ALERT_ONLY", "3", "two_source_confirmation_not_implemented", decision.factors)
        if not self.config.trigger.trusted_single_source_execution:
            return Decision("ALERT_ONLY", "3", "single_source_execution_disabled", decision.factors)
        if not _level_meets_threshold(decision.level, self.config.trigger.auto_execute_level):
            return Decision("ALERT_ONLY", "3", "below_auto_execute_level", decision.factors)
        if self.config.safety.max_executions <= 0:
            return Decision("ALERT_ONLY", "3", "max_executions_zero", decision.factors)
        return decision

    def _log_decision(self, article: Article, decision: Decision, **fields: Any) -> None:
        append_jsonl(
            self.config.logs_dir / "decisions.jsonl",
            {
                "article": article.__dict__,
                "decision": {
                    "action": decision.action,
                    "level": decision.level,
                    "reason": decision.reason,
                    "factors": decision.factors.__dict__ if decision.factors else None,
                },
                **fields,
            },
        )

    def _classify_with_budget(self, article: Article, pass_index: int):
        context = self._classifier_context()
        input_chars = len(article.title) + len(article.raw_text) + len(context)
        telemetry = {
            "provider": self.config.classifier.provider,
            "model": self.config.classifier.model,
            "pass_index": pass_index,
            "article_hash": article.hash,
            "source_kind": article.source_kind,
            "domain": article.domain,
            "input_char_count": input_chars,
            "estimated_input_tokens": max(1, input_chars // 4),
        }
        log_event("iran_classifier_attempt", **telemetry)
        if hasattr(self.classifier, "last_usage"):
            setattr(self.classifier, "last_usage", None)
        factors = self.classifier.classify(article, context)
        usage = getattr(self.classifier, "last_usage", None)
        if isinstance(usage, dict):
            telemetry["usage"] = usage
        log_event("iran_classifier_result", **telemetry)
        return factors

    def _notify_classifier_budget_block_once(self, reason: str) -> None:
        window = "hour" if reason in {"classifier_budget_exhausted_hourly", "classifier_error_cap_exceeded"} else "day"
        if self.classifier_budget.mark_notified_once(reason, window):
            self.notifier.notify("Iran protection classifier budget blocked classification", reason=reason)

    def _classifier_context(self) -> str:
        return _classifier_context_for(self.config, self.market_rule_text)


def _classifier_context_for(config: IranBotConfig, market_rule_text: str) -> str:
    if not config.classifier.include_market_rule_text:
        return f"Target leg: {config.market.target_leg}"
    return f"{market_rule_text}\nTarget leg: {config.market.target_leg}"


_TELEGRAM_CHUNK_CHARS = 3500  # headroom under Telegram's 4096-char message limit


def _format_live_update_message(article: Article) -> str:
    parts = [article.title.strip(), "", article.raw_text.strip(), "", f"Source: {article.url}"]
    return "\n".join(part for part in parts if part)


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


def _is_feed_summary(article: Article) -> bool:
    return article.source_kind in {"feed", "feed_item", "promoted_feed_summary"}


def _article_age_hours(article: Article) -> float | None:
    if not article.published_at:
        return None
    try:
        from email.utils import parsedate_to_datetime

        published = parsedate_to_datetime(article.published_at)
    except (TypeError, ValueError):
        try:
            published = datetime.fromisoformat(article.published_at)
        except ValueError:
            return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - published).total_seconds() / 3600.0


def _level_meets_threshold(level: str, threshold: int) -> bool:
    if level == "TIME":
        return True
    digits = "".join(ch for ch in level if ch.isdigit())
    if not digits:
        return False
    return int(digits) >= threshold


def _position_diagnosis(held_side: str, yes_shares: float, no_shares: float) -> str:
    if held_side.upper() == "YES":
        if yes_shares > 0:
            return "configured account has YES shares on this market"
        return "configured account has no YES shares on this market; check funder/signature/account/token IDs"
    if held_side.upper() == "NO":
        if no_shares > 0:
            return "configured account has NO shares on this market"
        return "configured account has no NO shares on this market; check funder/signature/account/token IDs"
    return "unknown held_side"


def _is_yes_scheduled_hold(config: IranBotConfig, decision: Decision) -> bool:
    if config.market.held_side.upper() != "YES":
        return False
    factors = decision.factors
    return (
        decision.action == "NO_ACTION"
        and decision.reason == "senior_round_scheduled_hold_not_resolved"
        and factors is not None
        and factors.event_type == "round_scheduled"
        and factors.seniority == "senior"
        and factors.timing_relative_to_deadline == "before"
    )


def _active_scheduled_hold(config: IranBotConfig, record: Any) -> Any | None:
    if config.market.held_side.upper() != "YES":
        return None
    if not config.time_decay.suspend_exit_on_scheduled_signal:
        return None
    if record is None or record.state not in {"YES_SCHEDULED_HOLD_SIGNAL", "TIME_DECAY_SUSPENDED"}:
        return None
    hold_updated_at = record.payload.get("hold_updated_at") if record.state == "TIME_DECAY_SUSPENDED" else record.updated_at
    try:
        updated = datetime.fromisoformat(str(hold_updated_at))
    except ValueError:
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    max_age_seconds = max(0, config.time_decay.scheduled_signal_suspension_days) * 24 * 60 * 60
    if (datetime.now(timezone.utc) - updated).total_seconds() <= max_age_seconds:
        return record
    return None
