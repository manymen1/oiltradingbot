from __future__ import annotations

import json
import types
from datetime import date, datetime, timedelta, timezone

import pytest

from polybot.iran.executor import DryRunTradingAdapter
from polybot.iran.storage import StateStore
from polybot.iran.types import Article
from polybot.location.classifier import LLMLocationClassifier, RuleBasedFixtureLocationClassifier
from polybot.location.config import (
    ClassifierConfig,
    EventConfig,
    ExecutionConfig,
    LocationBotConfig,
    OutcomeMarket,
    PositionConfig,
    MonitoringConfig,
    PriceAlertConfig,
    HeartbeatConfig,
    MarketVerificationMonitorConfig,
    SafetyConfig,
    SellConfig,
    SourcesConfig,
    BuyRotationConfig,
    TimeDecayConfig,
    TriggerConfig,
)
from polybot.location.decision import LocationDecision, classify_agreement, final_decision, time_decay_decision
from polybot.location.executor import LocationExecutor
from polybot.location.runner import LocationProtectionBot
from polybot.location.types import LocationSignal
from polybot.location import market_verifier as market_verifier_mod
from polybot.location import runner as runner_mod
from polybot.location.market_verifier import verify_all_outcomes, verify_critical_outcomes, verify_location_event
from polybot.iran.source_fetcher import ArticleStore, extract_listing_article_urls, _extract_published_at


def article(text: str, domain: str = "reuters.com", title: str | None = None) -> Article:
    return Article(
        url=f"https://{domain}/story",
        domain=domain,
        title=title or text,
        published_at=None,
        fetched_at="2026-07-06T00:00:00Z",
        raw_text=text,
        hash=str(abs(hash((domain, text)))),
    )


def _outcomes() -> list[OutcomeMarket]:
    return [
        OutcomeMarket(name="qatar", label="Qatar", condition_id="0xqatar", yes_token_id="qatar-yes", no_token_id="qatar-no", rotation_target=True),
        OutcomeMarket(name="pakistan", label="Pakistan", condition_id="0xpk", yes_token_id="pk-yes", no_token_id="pk-no", rotation_target=True),
        OutcomeMarket(name="switzerland", label="Switzerland", condition_id="0xch", yes_token_id="ch-yes", no_token_id="ch-no", rotation_target=True),
        OutcomeMarket(name="oman", label="Oman", condition_id="0xom", yes_token_id="om-yes", no_token_id="om-no", rotation_target=True),
        OutcomeMarket(name="no_meeting", label="No Meeting by September 30", condition_id="0xnm", yes_token_id="nm-yes", no_token_id="nm-no", rotation_target=False),
        OutcomeMarket(name="russia", label="Russia", condition_id="0xru", yes_token_id="ru-yes", no_token_id="ru-no", rotation_target=False),
    ]


def _config(**overrides) -> LocationBotConfig:
    defaults = dict(
        event=EventConfig(
            slug="test-slug",
            question="Will the next diplomatic US-Iran meeting be in Qatar by September 30, 2026?",
            deadline_date="2026-09-30",
            held_location="qatar",
            resolution_rules="test rules",
        ),
        outcomes=_outcomes(),
        position=PositionConfig(held_yes_shares=1000.0, max_yes_shares_to_sell=1000.0, max_rotation_usd_to_buy=500.0),
        trigger=TriggerConfig(auto_execute_level=4, trusted_single_source_execution=True),
        classifier=ClassifierConfig(provider="rule_based"),
        execution=ExecutionConfig(dry_run=True, sell=SellConfig(), buy_rotation=BuyRotationConfig()),
        time_decay=TimeDecayConfig(),
    )
    defaults.update(overrides)
    return LocationBotConfig(**defaults)


def _signal(**overrides) -> LocationSignal:
    defaults = dict(
        source_is_trusted=True,
        qualifies_as_senior_round=True,
        round_status="scheduled",
        location_country_name="Qatar",
        confirmed_location="qatar",
        evidence_strength="confirmed_scheduled",
        would_resolve_held_location_yes=True,
        would_resolve_held_location_no=False,
        level="4A",
        quote_supporting_trigger="A new round will begin in Doha.",
        source_tier="wire",
    )
    defaults.update(overrides)
    return LocationSignal(**defaults)


# ---- decision engine ----


def test_held_location_confirmed_is_no_action() -> None:
    config = _config()
    decision = final_decision(config, _signal(confirmed_location="qatar"))
    assert decision.action == "NO_ACTION"
    assert decision.reason == "held_location_reinforced"


def test_rotation_target_confirmed_strong_triggers_rotate() -> None:
    config = _config()
    decision = final_decision(config, _signal(confirmed_location="pakistan", evidence_strength="confirmed_started", source_tier="wire"))
    assert decision.action == "ROTATE_YES"
    assert decision.target_outcome == "pakistan"


def test_rotation_target_confirmed_weak_evidence_is_alert_only() -> None:
    config = _config()
    decision = final_decision(config, _signal(confirmed_location="pakistan", evidence_strength="speculative"))
    assert decision.action == "ALERT_ONLY"
    assert "not_yet_confirmed" in decision.reason


def test_rotation_target_confirmed_non_tier_one_source_is_alert_only() -> None:
    config = _config()
    decision = final_decision(config, _signal(confirmed_location="pakistan", evidence_strength="confirmed_scheduled", source_tier="other"))
    assert decision.action == "ALERT_ONLY"


def test_untracked_location_confirmed_is_sell_only() -> None:
    config = _config()
    decision = final_decision(config, _signal(confirmed_location="russia", evidence_strength="confirmed_started", source_tier="wire"))
    assert decision.action == "EXIT_YES_ONLY"
    assert decision.target_outcome is None


def test_no_meeting_confirmed_is_sell_only() -> None:
    config = _config()
    decision = final_decision(config, _signal(confirmed_location="no_meeting", evidence_strength="confirmed_started", source_tier="official_government"))
    assert decision.action == "EXIT_YES_ONLY"
    assert decision.reason == "no_meeting_confirmed"


def test_technical_only_is_no_action() -> None:
    config = _config()
    decision = final_decision(config, _signal(round_status="technical_only", qualifies_as_senior_round=False, confirmed_location="qatar"))
    assert decision.action == "NO_ACTION"
    assert decision.reason == "technical_or_non_qualifying"


def test_untrusted_source_is_alert_only() -> None:
    config = _config()
    decision = final_decision(config, _signal(source_is_trusted=False))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "source_not_trusted"


def test_no_location_signal_is_no_action() -> None:
    config = _config()
    decision = final_decision(config, _signal(confirmed_location="none"))
    assert decision.action == "NO_ACTION"
    assert decision.reason == "no_location_signal"


def test_time_decay_disabled_by_default() -> None:
    config = _config()
    assert time_decay_decision(config).action == "NO_ACTION"


def test_time_decay_trim_and_exit() -> None:
    config = _config(time_decay=TimeDecayConfig(enabled=True, trim_after_date="2026-09-16", exit_after_date="2026-09-23"))
    assert time_decay_decision(config, today=date(2026, 9, 1)).action == "NO_ACTION"
    assert time_decay_decision(config, today=date(2026, 9, 17)).action == "TRIM_YES"
    assert time_decay_decision(config, today=date(2026, 9, 24)).action == "EXIT_YES_ONLY"


# ---- rule-based classifier ----


def test_rule_based_classifier_detects_held_location() -> None:
    config = _config()
    classifier = RuleBasedFixtureLocationClassifier(config)
    signal = classifier.classify(article("The next round of talks will begin in Qatar next week."), "rules")
    assert signal.confirmed_location == "qatar"
    assert signal.would_resolve_held_location_yes is True


def test_rule_based_classifier_detects_rotation_target() -> None:
    config = _config()
    classifier = RuleBasedFixtureLocationClassifier(config)
    signal = classifier.classify(article("Officials confirm the next round will begin in Pakistan."), "rules")
    assert signal.confirmed_location == "pakistan"
    assert signal.would_resolve_held_location_no is True


# ---- executor ----


def _executor(tmp_path, config: LocationBotConfig, adapter: DryRunTradingAdapter) -> LocationExecutor:
    store = StateStore(tmp_path / "state")
    notified: list[tuple[str, dict]] = []

    class _Notifier:
        def notify(self, message, **fields):
            notified.append((message, fields))

    executor = LocationExecutor(config, store, _Notifier(), adapter)
    executor._notified = notified  # type: ignore[attr-defined]
    return executor


def test_executor_rotate_sells_held_and_buys_target(tmp_path) -> None:
    config = _config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ROTATE_YES", "4B", "confirmed_location:pakistan", target_outcome="pakistan", factors=_signal(confirmed_location="pakistan"))
    result = executor.execute(decision, article("Officials confirm the round begins in Pakistan."))
    assert result == "ROTATED"
    current = executor.store.current()
    assert current is not None
    assert current.state == "ROTATED"
    assert current.payload["from_outcome"] == "qatar"
    assert current.payload["to_outcome"] == "pakistan"


def test_executor_rotate_skips_buy_when_target_price_above_cap(tmp_path) -> None:
    config = _config(execution=ExecutionConfig(dry_run=True, sell=SellConfig(), buy_rotation=BuyRotationConfig(max_price=0.10)))
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_ask=0.90)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ROTATE_YES", "4B", "confirmed_location:pakistan", target_outcome="pakistan", factors=_signal(confirmed_location="pakistan"))
    result = executor.execute(decision, article("Officials confirm the round begins in Pakistan."))
    assert result == "EXITED"


def test_executor_exit_only_sells_without_buying(tmp_path) -> None:
    config = _config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("EXIT_YES_ONLY", "4B", "no_meeting_confirmed", factors=_signal(confirmed_location="no_meeting"))
    result = executor.execute(decision, article("No qualifying round will occur before the deadline."))
    assert result == "EXITED"


def test_executor_one_shot_blocks_second_trade(tmp_path) -> None:
    config = _config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("EXIT_YES_ONLY", "4B", "no_meeting_confirmed", factors=_signal(confirmed_location="no_meeting"))
    first = executor.execute(decision, article("No qualifying round will occur before the deadline."))
    second = executor.execute(decision, article("No qualifying round will occur before the deadline, again."))
    assert first == "EXITED"
    assert second == "EXITED"  # terminal state returned, no duplicate sell
    assert adapter.yes_shares == 1000.0  # DryRunTradingAdapter doesn't mutate balances; sanity that no crash occurred on 2nd call


def test_executor_no_position_skips_trade(tmp_path) -> None:
    config = _config()
    adapter = DryRunTradingAdapter(yes_shares=0.0)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("EXIT_YES_ONLY", "4B", "no_meeting_confirmed", factors=_signal(confirmed_location="no_meeting"))
    result = executor.execute(decision, article("No qualifying round will occur before the deadline."))
    assert result == "YES_POSITION_UNCONFIRMED"


# ---- claude_cli provider (mirrors iran classifier's coverage) ----


def _claude_cli_classifier(cli_runner) -> LLMLocationClassifier:
    config = _config(classifier=ClassifierConfig(provider="claude_cli", model="claude-sonnet-4-6"))
    return LLMLocationClassifier(config.classifier, config, cli_runner=cli_runner)


def test_claude_cli_location_classifier_parses_structured_output() -> None:
    payload = {
        "source_is_trusted": True,
        "qualifies_as_senior_round": True,
        "round_status": "scheduled",
        "location_country_name": "Pakistan",
        "confirmed_location": "pakistan",
        "evidence_strength": "confirmed_scheduled",
        "would_resolve_held_location_yes": False,
        "would_resolve_held_location_no": True,
        "level": "4A",
        "quote_supporting_trigger": "The next round will begin in Islamabad.",
        "source_tier": "wire",
    }
    envelope = json.dumps({"type": "result", "is_error": False, "result": "see structured_output", "structured_output": payload})
    classifier = _claude_cli_classifier(lambda prompt: envelope)
    signal = classifier.classify(article("The next round will begin in Islamabad."), "rules")
    assert signal.confirmed_location == "pakistan"
    assert signal.level == "4A"


def test_claude_cli_location_classifier_error_envelope_raises() -> None:
    classifier = _claude_cli_classifier(lambda prompt: json.dumps({"type": "result", "is_error": True, "result": "boom"}))
    with pytest.raises(RuntimeError, match="claude CLI reported an error"):
        classifier.classify(article("test"), "rules")


def _codex_cli_classifier(cli_runner) -> LLMLocationClassifier:
    config = _config(classifier=ClassifierConfig(provider="codex_cli", model="gpt-5"))
    return LLMLocationClassifier(config.classifier, config, cli_runner=cli_runner)


def _location_payload(**overrides) -> dict:
    payload = {
        "source_is_trusted": True,
        "qualifies_as_senior_round": True,
        "round_status": "scheduled",
        "location_country_name": "Pakistan",
        "confirmed_location": "pakistan",
        "evidence_strength": "confirmed_scheduled",
        "would_resolve_held_location_yes": False,
        "would_resolve_held_location_no": True,
        "level": "4A",
        "quote_supporting_trigger": "The next round will begin in Islamabad.",
        "source_tier": "wire",
    }
    payload.update(overrides)
    return payload


def test_codex_cli_location_classifier_parses_json_result() -> None:
    classifier = _codex_cli_classifier(lambda prompt: json.dumps(_location_payload()))
    signal = classifier.classify(article("The next round will begin in Islamabad."), "rules")
    assert signal.confirmed_location == "pakistan"
    assert signal.level == "4A"


def test_codex_cli_location_classifier_falls_back_to_anthropic(monkeypatch) -> None:
    classifier = _codex_cli_classifier(lambda prompt: (_ for _ in ()).throw(RuntimeError("cli down")))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def fake_anthropic(prompt: str, *, model: str | None = None) -> str:
        classifier.last_usage = {"input_tokens": 10}
        return json.dumps(_location_payload(confirmed_location="oman", location_country_name="Oman"))

    monkeypatch.setattr(classifier, "_anthropic", fake_anthropic)
    signal = classifier.classify(article("The next round will begin in Oman."), "rules")
    assert signal.confirmed_location == "oman"
    assert classifier.last_usage is not None
    assert classifier.last_usage["fallback_from"] == "codex CLI"


def test_prompt_includes_analyst_context_when_set() -> None:
    from polybot.location.classifier import _prompt

    config = _config(
        event=EventConfig(
            slug="test-slug",
            question="Will the next diplomatic US-Iran meeting be in Qatar by September 30, 2026?",
            deadline_date="2026-09-30",
            held_location="qatar",
            resolution_rules="test rules",
            analyst_context="Pakistan meeting is technical/non-resolving; watch for the delayed Doha round instead.",
        )
    )
    prompt = _prompt(article("test"), "rules", config)
    assert "Analyst context" in prompt
    assert "delayed Doha round" in prompt


def test_prompt_omits_analyst_context_section_when_blank() -> None:
    from polybot.location.classifier import _prompt

    config = _config()  # default EventConfig has analyst_context=""
    prompt = _prompt(article("test"), "rules", config)
    assert "Analyst context" not in prompt


def test_prompt_lists_held_location_even_when_not_rotation_target() -> None:
    from dataclasses import replace
    from polybot.location.classifier import _prompt

    outcomes = [replace(outcome, rotation_target=False) if outcome.name == "qatar" else outcome for outcome in _outcomes()]
    config = _config(outcomes=outcomes)
    prompt = _prompt(article("test"), "rules", config)
    assert '"qatar" (Qatar)' in prompt
    assert 'If the article confirms the held venue Qatar, set confirmed_location to "qatar"' in prompt
    assert 'Automatic rotation buy targets only:' in prompt


# ---- keyword gate ----


def test_location_keyword_gate_escalates_meeting_with_tracked_location() -> None:
    from polybot.location.keyword_gate import should_escalate_location_article

    assert should_escalate_location_article(article("The next round of talks will begin in Qatar next week."), _config()) is True


def test_location_keyword_gate_escalates_no_meeting_language_without_location() -> None:
    from polybot.location.keyword_gate import should_escalate_location_article

    assert should_escalate_location_article(article("Officials said the negotiations were called off."), _config()) is True


def test_location_keyword_gate_skips_unrelated_article() -> None:
    from polybot.location.keyword_gate import should_escalate_location_article

    assert should_escalate_location_article(article("Oil exports rose after a regional statement."), _config()) is False


def test_location_keyword_gate_escalates_senior_official_without_named_venue() -> None:
    from polybot.location.keyword_gate import should_escalate_location_article

    # No country/city is named at all -- named senior-principal attendance is
    # the signal per the position's own thesis, so this must still escalate.
    assert should_escalate_location_article(article("Witkoff met with Araqchi today to discuss the roadmap."), _config()) is True


# ---- keyword gate runs before Telegram notify and before codex classification ----


def _spy_bot(tmp_path):
    from polybot.location.runner import LocationProtectionBot

    config = _config(data_dir=tmp_path / "state", logs_dir=tmp_path / "logs")
    bot = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_shares=1000.0))
    notified: list[str] = []
    bot.notifier = types.SimpleNamespace(notify=lambda message, **fields: notified.append(message))
    return bot, notified


def test_irrelevant_poll_article_is_not_notified_or_classified(tmp_path) -> None:
    # Reproduces the World Cup/FIFA noise seen on the AJ tag-page listing:
    # an unrelated article must not reach Telegram or spend classifier budget.
    bot, notified = _spy_bot(tmp_path)
    decision = bot.process_article(article("Ronaldo scores as Portugal beats Uzbekistan at the World Cup."))
    assert decision.reason == "keyword_gate_no_location_trigger"
    assert notified == []


def test_relevant_poll_article_is_notified_and_classified(tmp_path) -> None:
    # A genuinely relevant update still gets pushed to Telegram and goes on
    # to the real classifier ("low classifier" keyword filter, then codex).
    bot, notified = _spy_bot(tmp_path)
    bot.process_article(article("The next round of talks will begin in Qatar next week."))
    assert notified  # the live-update chunk(s) were pushed


def test_relevant_feed_article_is_also_notified_with_full_text(tmp_path) -> None:
    # Previously only poll_urls (the AJ tag page) got the full-text push;
    # RSS-sourced articles (Dawn, AJ all.xml, Reuters via Google News) only
    # got the terse "alert only" summary. Any source should get the full
    # text once it clears the keyword gate.
    bot, notified = _spy_bot(tmp_path)
    feed_article = article("The next round of talks will begin in Qatar next week.", domain="dawn.com")
    bot.process_article(feed_article)
    assert any(feed_article.raw_text in message for message in notified)


def _listing_article(text: str, url: str = "https://www.aljazeera.com/liveblog/2026/7/7/live-updates") -> Article:
    return Article(
        url=url,
        domain="aljazeera.com",
        title="Live updates",
        published_at=None,
        fetched_at="2026-07-07T00:00:00Z",
        raw_text=text,
        hash=str(abs(hash((url, text)))),
        source_kind="listing_article",
    )


def test_listing_article_first_sighting_gets_full_text(tmp_path) -> None:
    # First time a liveblog URL is seen, there's nothing to diff against, so
    # the full text should go to Telegram.
    bot, notified = _spy_bot(tmp_path)
    first = _listing_article("Update 1: talks will resume in Qatar next week.")
    bot.process_article(first)
    assert any("Update 1: talks will resume in Qatar next week." in message for message in notified)


def test_listing_article_resend_only_notifies_new_lines(tmp_path) -> None:
    # A liveblog refetch that grew (new entry prepended, old ones still
    # present) must only push the new line(s) to Telegram, not the whole
    # accumulated page again.
    bot, notified = _spy_bot(tmp_path)
    first = _listing_article("Update 1: talks will resume in Qatar next week.")
    bot.process_article(first)
    notified.clear()
    second = _listing_article("Update 2: delegation lands in Doha today.\nUpdate 1: talks will resume in Qatar next week.")
    bot.process_article(second)
    assert any("Update 2: delegation lands in Doha today." in message for message in notified)
    assert not any("Update 1: talks will resume in Qatar next week." in message for message in notified)


def test_listing_article_classifier_still_sees_full_accumulated_text(tmp_path) -> None:
    # The Telegram diff must never leak into what the classifier/quote
    # verification see -- those must keep operating on the full raw_text.
    bot, notified = _spy_bot(tmp_path)
    bot.classifier = _CountingLocationClassifier()
    first = _listing_article("Update 1: talks will resume in Qatar next week.")
    bot.process_article(first)
    second_text = "Update 2: delegation lands in Doha today.\nUpdate 1: talks will resume in Qatar next week."
    second = _listing_article(second_text)
    decision = bot.process_article(second)
    assert decision.factors is not None
    assert bot.classifier.calls == 2


def test_extract_published_at_prefers_most_recent_of_published_and_modified() -> None:
    # A liveblog's article:published_time is pinned to when the page first
    # went live; article:modified_time keeps advancing as new entries land.
    # Freshness gating must reflect the most recent, not the first match.
    # Timestamps are computed relative to "now" (not hardcoded) so this test
    # doesn't become a time bomb the day after it's written.
    published = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat().replace("+00:00", "Z")
    modified = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    markup = (
        "<html><head>"
        f'<meta property="article:published_time" content="{published}" />'
        f'<meta property="article:modified_time" content="{modified}" />'
        "</head><body>Live updates.</body></html>"
    )
    result = _extract_published_at(markup)
    assert result is not None
    parsed = datetime.fromisoformat(result)
    expected = datetime.fromisoformat(modified.replace("Z", "+00:00"))
    assert abs((parsed - expected).total_seconds()) < 2


def test_extract_published_at_discards_bogus_future_timestamp() -> None:
    markup = (
        "<html><head>"
        '<meta property="article:published_time" content="2026-07-01T00:00:00Z" />'
        '<meta property="article:modified_time" content="2099-01-01T00:00:00Z" />'
        "</head><body>Live updates.</body></html>"
    )
    result = _extract_published_at(markup)
    assert result is not None
    assert result.startswith("2026-07-01T00:00:00")


# ---- rotation buy capped by confirmed sale proceeds (2026-07-06 hardening) ----


def test_rotation_buy_capped_by_global_order_limit_and_confirmed_proceeds(tmp_path) -> None:
    # Configured budget/cap allow up to $500, but the held outcome only has a
    # $0.10 best bid -- confirmed proceeds (1000 * 0.10 = $100) must cap the
    # rotation buy, not the full $500 configured budget.
    config = _config(position=PositionConfig(held_yes_shares=1000.0, max_yes_shares_to_sell=1000.0, max_rotation_usd_to_buy=500.0))
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_ask=0.40, yes_bid=0.10)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ROTATE_YES", "4B", "confirmed_location:pakistan", target_outcome="pakistan", factors=_signal(confirmed_location="pakistan"))
    result = executor.execute(decision, article("Officials confirm the round begins in Pakistan."))
    assert result == "ROTATED"
    current = executor.store.current()
    assert current is not None
    # The $100 proceeds cap is further reduced by the global $25 per-order
    # guardrail; rotations cannot bypass the same limit as flat entries.
    assert current.payload["rotation_filled_shares"] == pytest.approx(25.0 / 0.95)


def test_rotation_buy_skipped_when_proceeds_too_small(tmp_path) -> None:
    # Best bid near zero -> confirmed proceeds below the minimum viable order
    # size -> sell-only, no rotation buy attempted at all.
    config = _config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_ask=0.40, yes_bid=0.0005)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ROTATE_YES", "4B", "confirmed_location:pakistan", target_outcome="pakistan", factors=_signal(confirmed_location="pakistan"))
    result = executor.execute(decision, article("Officials confirm the round begins in Pakistan."))
    assert result == "EXITED"
    current = executor.store.current()
    assert current.payload["reason"] == "insufficient_sale_proceeds_for_rotation_buy"


# ---- 2-pass classifier agreement (2026-07-06 hardening) ----


def test_classify_agreement_fires_when_passes_agree() -> None:
    config = _config()
    signal = _signal(confirmed_location="pakistan", round_status="scheduled")
    decision = classify_agreement(config, [signal, signal])
    assert decision.action == "ROTATE_YES"


def test_classify_agreement_alerts_on_disagreement() -> None:
    config = _config()
    first = _signal(confirmed_location="pakistan", round_status="scheduled")
    second = _signal(confirmed_location="qatar", round_status="scheduled")
    decision = classify_agreement(config, [first, second])
    assert decision.action == "ALERT_ONLY"
    assert "classifier_pass_disagreement" in decision.reason
    assert "confirmed_location" in decision.reason


def test_classify_agreement_no_meeting_fast_paths_on_first_pass() -> None:
    config = _config()
    collapse = _signal(
        confirmed_location="no_meeting",
        round_status="none",
        qualifies_as_senior_round=False,
        evidence_strength="denied",
        source_tier="wire",
        quote_supporting_trigger="Iran officially terminated the negotiation process.",
    )
    other = _signal(confirmed_location="pakistan")  # would disagree if agreement were required
    decision = classify_agreement(config, [collapse, other])
    assert decision.action == "EXIT_YES_ONLY"
    assert decision.reason == "no_meeting_confirmed"


def test_classify_agreement_no_meeting_with_delay_language_not_fast_pathed() -> None:
    config = _config()
    ambiguous = _signal(
        confirmed_location="no_meeting",
        round_status="none",
        qualifies_as_senior_round=False,
        evidence_strength="denied",
        source_tier="wire",
        quote_supporting_trigger="The talks have been postponed indefinitely.",
    )
    other = _signal(confirmed_location="pakistan")
    decision = classify_agreement(config, [ambiguous, other])
    assert decision.action == "ALERT_ONLY"
    assert "classifier_pass_disagreement" in decision.reason


def test_classify_agreement_empty_passes_is_alert_only() -> None:
    config = _config()
    decision = classify_agreement(config, [])
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "classifier_unavailable"


# ---- location market verifier ----


def _fake_market(
    *,
    label: str,
    question: str,
    slug: str,
    condition_id: str,
    yes_token: str,
    no_token: str,
    active: bool = True,
    closed: bool = False,
    accepting_orders: bool = True,
) -> dict:
    return {
        "groupItemTitle": label,
        "question": question,
        "slug": slug,
        "conditionId": condition_id,
        "clobTokenIds": json.dumps([yes_token, no_token]),
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.5", "0.5"]),
        "active": active,
        "closed": closed,
        "acceptingOrders": accepting_orders,
        "negRisk": True,
    }


def _fake_event(markets: list[dict], description: str = "rules text") -> dict:
    return {
        "slug": "test-event-slug",
        "title": "Test event",
        "description": description,
        "markets": markets,
    }


def test_verify_location_event_matches_by_group_item_title(monkeypatch) -> None:
    config = _config()
    event = _fake_event(
        [
            _fake_market(label="Qatar", question="Q?", slug="qatar-slug", condition_id="0xqatar", yes_token="qatar-yes", no_token="qatar-no"),
            _fake_market(label="Pakistan", question="P?", slug="pk-slug", condition_id="0xpk", yes_token="pk-yes", no_token="pk-no"),
        ]
    )
    monkeypatch.setattr(market_verifier_mod, "fetch_event_by_slug", lambda slug, **kw: event)
    verification = verify_location_event(config)
    assert verification.outcomes["qatar"].found
    assert verification.outcomes["qatar"].condition_id_matches
    assert verification.outcomes["qatar"].yes_token_matches
    assert verification.outcomes["qatar"].no_token_matches
    assert verification.outcomes["qatar"].tradeable


def test_verify_location_event_flags_token_mismatch(monkeypatch) -> None:
    config = _config()
    event = _fake_event(
        [_fake_market(label="Qatar", question="Q?", slug="qatar-slug", condition_id="0xqatar", yes_token="WRONG-TOKEN", no_token="qatar-no")]
    )
    monkeypatch.setattr(market_verifier_mod, "fetch_event_by_slug", lambda slug, **kw: event)
    verification = verify_location_event(config)
    assert verification.outcomes["qatar"].found
    assert not verification.outcomes["qatar"].yes_token_matches
    assert verification.outcomes["qatar"].mismatch_reason == "token_id_mismatch"


def test_verify_critical_outcomes_raises_on_missing_rotation_target(monkeypatch) -> None:
    config = _config()
    # Qatar (held) present and correct, but Pakistan (a rotation target) is
    # entirely missing from the live event -- must fail closed.
    event = _fake_event(
        [_fake_market(label="Qatar", question="Q?", slug="qatar-slug", condition_id="0xqatar", yes_token="qatar-yes", no_token="qatar-no")]
    )
    monkeypatch.setattr(market_verifier_mod, "fetch_event_by_slug", lambda slug, **kw: event)
    verification = verify_location_event(config)
    with pytest.raises(ValueError, match="location market verification failed"):
        verify_critical_outcomes(config, verification)


def _event_for_config(config: LocationBotConfig, *, inactive: str | None = None, wrong_token: str | None = None) -> dict:
    markets = []
    for outcome in config.outcomes:
        markets.append(
            _fake_market(
                label=outcome.label,
                question=f"{outcome.label}?",
                slug=f"{outcome.name}-slug",
                condition_id=outcome.condition_id,
                yes_token=("WRONG-TOKEN" if outcome.name == wrong_token else outcome.yes_token_id),
                no_token=outcome.no_token_id,
                active=outcome.name != inactive,
                accepting_orders=outcome.name != inactive,
            )
        )
    return _fake_event(markets)


def test_verify_all_outcomes_checks_every_configured_leg(monkeypatch) -> None:
    config = _config()
    event = _event_for_config(config, wrong_token="russia")
    monkeypatch.setattr(market_verifier_mod, "fetch_event_by_slug", lambda slug, **kw: event)
    verification = verify_location_event(config)
    with pytest.raises(ValueError, match="russia: token_id_mismatch"):
        verify_all_outcomes(config, verification)


def test_verify_all_outcomes_live_requires_tradeable_markets(monkeypatch) -> None:
    config = _config()
    event = _event_for_config(config, inactive="oman")
    monkeypatch.setattr(market_verifier_mod, "fetch_event_by_slug", lambda slug, **kw: event)
    verification = verify_location_event(config)
    with pytest.raises(ValueError, match="oman: market_not_tradeable"):
        verify_all_outcomes(config, verification, require_tradeable=True)


def test_verify_location_event_raises_on_pinned_rule_hash_mismatch(monkeypatch) -> None:
    config = _config(
        event=EventConfig(
            slug="test-slug",
            question="Will the next diplomatic US-Iran meeting be in Qatar by September 30, 2026?",
            deadline_date="2026-09-30",
            held_location="qatar",
            resolution_rules="test rules",
            expected_rule_text_sha256="deadbeef" * 8,
        )
    )
    event = _fake_event([_fake_market(label="Qatar", question="Q?", slug="qatar-slug", condition_id="0xqatar", yes_token="qatar-yes", no_token="qatar-no")])
    monkeypatch.setattr(market_verifier_mod, "fetch_event_by_slug", lambda slug, **kw: event)
    with pytest.raises(ValueError, match="rule text changed"):
        verify_location_event(config)


# ---- no_meeting ordering regression (found via smoke-location-classifier) ----


def test_no_meeting_realistic_llm_signal_still_exits() -> None:
    # A collapse report as the LLM actually emits it: there is no senior round,
    # so qualifies_as_senior_round is False and strength is "denied". Before the
    # ordering fix this fell into the senior-round gate and returned NO_ACTION.
    config = _config()
    decision = final_decision(
        config,
        _signal(
            confirmed_location="no_meeting",
            qualifies_as_senior_round=False,
            round_status="none",
            evidence_strength="denied",
            source_tier="wire",
            level="0",
        ),
    )
    assert decision.action == "EXIT_YES_ONLY"
    assert decision.reason == "no_meeting_confirmed"


def test_no_meeting_speculative_is_alert_only() -> None:
    config = _config()
    decision = final_decision(
        config,
        _signal(
            confirmed_location="no_meeting",
            qualifies_as_senior_round=False,
            round_status="rumor",
            evidence_strength="speculative",
            source_tier="other",
        ),
    )
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "no_meeting_reported_unconfirmed"


# ---- time-decay price floors ----


def _decay_config(**overrides) -> LocationBotConfig:
    return _config(
        time_decay=TimeDecayConfig(
            enabled=True,
            trim_after_date="2026-09-16",
            exit_after_date="2026-09-23",
            trim_fraction=0.25,
            min_trim_price=0.05,
            min_exit_price=0.10,
        ),
        **overrides,
    )


def test_time_decay_exit_blocked_below_price_floor(tmp_path) -> None:
    config = _decay_config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_bid=0.04)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("EXIT_YES_ONLY", "TIME", "time_decay_exit")
    assert executor.execute(decision, article("time decay")) == "TIME_DECAY_PRICE_FLOOR"
    current = executor.store.current()
    assert current is not None and current.state == "TIME_DECAY_PRICE_FLOOR"
    # Second blocked cycle keeps the state but must not re-notify.
    assert executor.execute(decision, article("time decay again")) == "TIME_DECAY_PRICE_FLOOR"
    floor_notes = [n for n in executor._notified if "floor" in n[0]]  # type: ignore[attr-defined]
    assert len(floor_notes) == 1


def test_time_decay_trim_blocked_below_trim_floor(tmp_path) -> None:
    config = _decay_config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_bid=0.04)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("TRIM_YES", "TIME", "time_decay_trim")
    assert executor.execute(decision, article("time decay")) == "TIME_DECAY_PRICE_FLOOR"


def test_time_decay_trim_proceeds_above_floor(tmp_path) -> None:
    config = _decay_config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_bid=0.20)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("TRIM_YES", "TIME", "time_decay_trim")
    assert executor.execute(decision, article("time decay")) == "TRIMMED"


def test_news_triggered_exit_is_not_floored(tmp_path) -> None:
    # Floors only apply to calendar-decay sales; a confirmed adverse news
    # trigger must still exit even below the decay floor.
    config = _decay_config()
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_bid=0.01)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("EXIT_YES_ONLY", "4B", "no_meeting_confirmed", factors=_signal(confirmed_location="no_meeting"))
    assert executor.execute(decision, article("No qualifying round will occur.")) == "EXITED"


# ---- runner: time-decay decisions must not re-execute/notify every cycle ----


def test_run_once_skips_decay_after_trim_and_exit(tmp_path) -> None:
    from polybot.location.runner import LocationProtectionBot

    config = _config(
        time_decay=TimeDecayConfig(enabled=True, trim_after_date="2020-01-01", exit_after_date="2099-01-01", trim_fraction=0.25),
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
    )
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_bid=0.50)
    bot = LocationProtectionBot(config=config, adapter=adapter)
    first = bot.run_once()
    assert [d.action for d in first] == ["TRIM_YES"]
    current = bot.store.current()
    assert current is not None and current.state == "TRIMMED"
    # Subsequent cycles: trim already recorded, decay decision suppressed.
    assert bot.run_once() == []
    assert bot.run_once() == []


# ---- source policy: freshness gate + promoted_feed_summary gating ----


def _policy_bot(tmp_path, sources):
    from polybot.location.runner import LocationProtectionBot

    config = _config(data_dir=tmp_path / "state", logs_dir=tmp_path / "logs")
    object.__setattr__(config, "sources", sources)
    return LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_shares=1000.0))


def _sources(**overrides):
    import dataclasses

    from polybot.location.config import SourcesConfig

    base = SourcesConfig(auto_trade_domains=["reuters.com"], max_trade_article_age_hours=24)
    return dataclasses.replace(base, **overrides)


def _trade_decision() -> LocationDecision:
    return LocationDecision(
        "ROTATE_YES", "4B", "confirmed_location:pakistan", target_outcome="pakistan", factors=_signal(confirmed_location="pakistan")
    )


def _aged_article(published_at: str | None, source_kind: str = "article", domain: str = "reuters.com") -> Article:
    return Article(
        url=f"https://{domain}/story",
        domain=domain,
        title="story",
        published_at=published_at,
        fetched_at="2026-07-06T00:00:00Z",
        raw_text="Officials confirm the round begins in Pakistan.",
        hash="h1",
        source_kind=source_kind,
    )


def test_stale_article_cannot_auto_trade(tmp_path) -> None:
    bot = _policy_bot(tmp_path, _sources())
    out = bot._enforce_source_policy(_aged_article("Mon, 01 Jun 2026 00:00:00 GMT"), _trade_decision())
    assert out.action == "ALERT_ONLY"
    assert out.reason.startswith("article_stale_for_auto_trade")


def test_undated_article_cannot_auto_trade_by_default(tmp_path) -> None:
    bot = _policy_bot(tmp_path, _sources())
    out = bot._enforce_source_policy(_aged_article(None), _trade_decision())
    assert out.action == "ALERT_ONLY"
    assert out.reason == "article_age_unknown_for_auto_trade"


def test_undated_article_can_be_allowed_by_explicit_source_flag(tmp_path) -> None:
    bot = _policy_bot(tmp_path, _sources(allow_unknown_age_poll_auto_trade=True))
    out = bot._enforce_source_policy(_aged_article(None), _trade_decision())
    assert out.action == "ROTATE_YES"


def test_promoted_feed_summary_blocked_when_feed_auto_trade_disabled(tmp_path) -> None:
    bot = _policy_bot(tmp_path, _sources(allow_feed_auto_trade=False))
    out = bot._enforce_source_policy(_aged_article(None, source_kind="promoted_feed_summary"), _trade_decision())
    assert out.action == "ALERT_ONLY"
    assert out.reason == "feed_item_auto_trade_disabled"


def test_promoted_feed_summary_allowed_when_feed_auto_trade_enabled_and_timestamped(tmp_path) -> None:
    bot = _policy_bot(tmp_path, _sources(allow_feed_auto_trade=True))
    # Must stay well within max_trade_article_age_hours (24h) regardless of
    # what day the suite runs on -- a hardcoded absolute timestamp here was a
    # time bomb that started failing the day after it was written.
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    out = bot._enforce_source_policy(_aged_article(fresh, source_kind="promoted_feed_summary"), _trade_decision())
    assert out.action == "ROTATE_YES"


@pytest.mark.parametrize("domain", ["irna.ir", "t.me", "x.com", "news.google.com"])
def test_promoted_feed_summary_non_wire_domains_stay_alert_only(tmp_path, domain: str) -> None:
    bot = _policy_bot(
        tmp_path,
        _sources(
            allow_feed_auto_trade=True,
            auto_trade_domains=["reuters.com", "apnews.com", "afp.com", "irna.ir", "t.me", "x.com", "news.google.com"],
        ),
    )
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    out = bot._enforce_source_policy(_aged_article(fresh, source_kind="promoted_feed_summary", domain=domain), _trade_decision())
    assert out.action == "ALERT_ONLY"
    assert out.reason == "promoted_feed_summary_domain_not_auto_trade"


def test_promoted_feed_summary_ap_wire_can_auto_trade_when_configured(tmp_path) -> None:
    bot = _policy_bot(
        tmp_path,
        _sources(allow_feed_auto_trade=True, auto_trade_domains=["reuters.com", "apnews.com", "afp.com"]),
    )
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    out = bot._enforce_source_policy(_aged_article(fresh, source_kind="promoted_feed_summary", domain="apnews.com"), _trade_decision())
    assert out.action == "ROTATE_YES"


def test_max_executions_counts_prior_trim_marker(tmp_path) -> None:
    config = _config(data_dir=tmp_path / "state", logs_dir=tmp_path / "logs", safety=SafetyConfig(max_executions=1))
    bot = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_shares=1000.0))
    bot.store.write("TRIMMED", reason="prior_trim")

    out = bot._enforce_execution_policy(_trade_decision())

    assert out.action == "ALERT_ONLY"
    assert out.reason == "max_executions_reached:1"


def test_article_store_dedupes_same_extracted_text_with_different_hash(tmp_path) -> None:
    store = ArticleStore(tmp_path / "articles.jsonl")
    first = Article(
        url="https://www.aljazeera.com/tag/israel-iran-conflict/",
        domain="aljazeera.com",
        title="Tag page",
        published_at=None,
        fetched_at="2026-07-06T00:00:00Z",
        raw_text="Same extracted listing text",
        hash="url-hash-1",
        source_kind="article",
    )
    second = Article(
        url="https://www.aljazeera.com/tag/israel-iran-conflict/?page=2",
        domain="aljazeera.com",
        title="Tag page",
        published_at=None,
        fetched_at="2026-07-06T00:01:00Z",
        raw_text="Same   extracted\nlisting text",
        hash="url-hash-2",
        source_kind="article",
    )
    assert store.store(first) is True
    assert store.store(second) is False


def test_extract_listing_article_urls_keeps_same_site_articles_only() -> None:
    markup = """
    <main>
      <a href="/news/2026/7/6/iran-talks">Iran talks</a>
      <a href="/news/2026/7/6/iran-talks#updates">duplicate</a>
      <a href="/tag/israel-iran-conflict/">tag self</a>
      <a href="https://example.com/news/other">external</a>
    </main>
    """
    urls = extract_listing_article_urls("https://www.aljazeera.com/tag/israel-iran-conflict/", markup)
    assert urls == ["https://www.aljazeera.com/news/2026/7/6/iran-talks"]


def test_location_feed_summary_skip_does_not_increment_classifier_budget(tmp_path) -> None:
    bot = _policy_bot(tmp_path, _sources(allow_feed_auto_trade=True))
    bot.classifier = object()  # would crash if the classifier were reached
    decision = bot.process_article(_aged_article(None, source_kind="promoted_feed_summary"))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "feed_summary_classification_disabled"
    assert not (bot.store.data_dir / "classifier_budget.json").exists()


class _CountingLocationClassifier:
    def __init__(self) -> None:
        self.calls = 0

    def classify(self, article: Article, market_rule_text: str, held_location: str | None = None) -> LocationSignal:
        self.calls += 1
        return _signal(confirmed_location="qatar", quote_supporting_trigger="The next round will begin in Qatar.")


def test_location_classifier_budget_persists_across_bot_instances(tmp_path) -> None:
    classifier_config = ClassifierConfig(max_escalations_per_hour=1, max_escalations_per_day=10, max_classifier_errors_per_hour=10)
    config = _config(classifier=classifier_config, data_dir=tmp_path / "state", logs_dir=tmp_path / "logs")
    first = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_shares=1000.0))
    first_classifier = _CountingLocationClassifier()
    first.classifier = first_classifier
    first_decision = first.process_article(article("The next round will begin in Qatar."))
    assert first_decision.reason == "held_location_reinforced"
    assert first_classifier.calls == 1

    second = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_shares=1000.0))
    second_classifier = _CountingLocationClassifier()
    second.classifier = second_classifier
    second_decision = second.process_article(article("The next round will begin in Qatar. New item."))
    assert second_decision.action == "ALERT_ONLY"
    assert second_decision.reason == "classifier_budget_exhausted_hourly"
    assert second_classifier.calls == 0


def _monitoring_bot(tmp_path, adapter: DryRunTradingAdapter, monitoring: MonitoringConfig):
    config = _config(
        monitoring=monitoring,
        sources=_sources(),
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
    )
    bot = LocationProtectionBot(config=config, adapter=adapter)
    notified: list[tuple[str, dict]] = []

    class _Notifier:
        def notify(self, message, **fields):
            notified.append((message, fields))

    bot.notifier = _Notifier()  # type: ignore[assignment]
    return bot, notified


def test_price_band_alert_fires_only_on_crossing(tmp_path) -> None:
    adapter = DryRunTradingAdapter(yes_bid=0.25, yes_ask=0.25)
    bot, notified = _monitoring_bot(
        tmp_path,
        adapter,
        MonitoringConfig(price_alerts=PriceAlertConfig(enabled=True, outcome="qatar", thresholds=[0.28, 0.40])),
    )
    bot.run_once()
    assert notified == []
    adapter.yes_bid_value = 0.29
    adapter.yes_ask_value = 0.29
    bot.run_once()
    assert notified[-1][0] == "Location price band crossed"
    assert notified[-1][1]["threshold"] == 0.28
    bot.run_once()
    assert len(notified) == 1


def test_price_band_alert_can_use_live_clob_quote_in_dry_run(tmp_path, monkeypatch) -> None:
    class _Book:
        snapshots = [
            {"best_bid": 0.25, "best_ask": 0.25},
            {"best_bid": 0.14, "best_ask": 0.14},
        ]

        def __init__(self, token_ids):
            self.token_ids = token_ids
            self.calls = 0

        def rest_snapshot(self, token_id):
            self.calls += 1

        def snapshot_state(self, token_id):
            index = min(self.calls - 1, len(self.snapshots) - 1)
            return self.snapshots[index]

    monkeypatch.setattr(runner_mod, "BookCache", _Book)
    adapter = DryRunTradingAdapter(yes_bid=0.50, yes_ask=0.50)
    bot, notified = _monitoring_bot(
        tmp_path,
        adapter,
        MonitoringConfig(
            price_alerts=PriceAlertConfig(
                enabled=True,
                outcome="qatar",
                thresholds=[0.15],
                live_quotes_in_dry_run=True,
            )
        ),
    )

    bot.run_once()
    bot.run_once()

    assert notified[-1][0] == "Location price band crossed"
    assert notified[-1][1]["direction"] == "down"
    assert notified[-1][1]["price"] == 0.14
    assert notified[-1][1]["quote_source"] == "live_clob_book"


def test_daily_heartbeat_persists_last_sent_time(tmp_path) -> None:
    adapter = DryRunTradingAdapter(yes_bid=0.25, yes_ask=0.27)
    bot, notified = _monitoring_bot(
        tmp_path,
        adapter,
        MonitoringConfig(heartbeat=HeartbeatConfig(enabled=True, interval_hours=24)),
    )
    bot.run_once()
    bot.run_once()
    heartbeats = [item for item in notified if item[0] == "Location protection heartbeat"]
    assert len(heartbeats) == 1
    assert heartbeats[0][1]["held_outcome"] == "Qatar"


# ---- runner policy/logging hardening ----


def test_operator_blocked_location_trade_is_logged_as_blocked(tmp_path) -> None:
    config = _config(data_dir=tmp_path / "state", logs_dir=tmp_path / "logs")
    bot = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_shares=1000.0))

    class _Gate:
        def check(self, decision, *, live_requested: bool):
            from polybot.iran.operator import GateResult
            return GateResult(False, "alert_only", "operator_mode_alert_only")
        def log_block_once(self, result, decision):
            return False

    bot.operator_gate = _Gate()  # type: ignore[assignment]
    decision = LocationDecision("ROTATE_YES", "4B", "confirmed_location:pakistan", target_outcome="pakistan", factors=_signal(confirmed_location="pakistan"))
    out = bot._execute_if_allowed(decision, article("The next round will begin in Pakistan."))
    assert out.action == "ALERT_ONLY"
    assert out.reason == "operator_block:operator_mode_alert_only"
    log_path = tmp_path / "logs" / "location_decisions.jsonl"
    assert "operator_block:operator_mode_alert_only" in log_path.read_text(encoding="utf-8")


def test_location_live_backend_selector_supports_ts_clob(monkeypatch) -> None:
    from polybot.core.execution import TsClobV2TradingAdapter

    monkeypatch.setenv("POLYBOT_EXECUTION_BACKEND", "clob_v2")

    adapter = runner_mod._live_adapter(tick_size="0.001", neg_risk=True)

    assert isinstance(adapter, TsClobV2TradingAdapter)
    assert adapter.tick_size == "0.001"
    assert adapter.neg_risk is True


def test_run_location_live_preflight_blocks_before_poll_loop(tmp_path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "qatar.yaml"
    config_path.write_text("event:\n  slug: test-slug\nexecution:\n  dry_run: false\n", encoding="utf-8")
    config = _config(
        classifier=ClassifierConfig(passes=2, require_pass_agreement=True),
        execution=ExecutionConfig(dry_run=False, sell=SellConfig(), buy_rotation=BuyRotationConfig()),
        sources=SourcesConfig(poll_urls=["https://example.com/story"]),
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
    )
    monkeypatch.setattr(runner_mod, "load_location_config", lambda path: config)
    monkeypatch.setattr(runner_mod, "_live_adapter", lambda **kwargs: DryRunTradingAdapter(yes_shares=1000.0))
    monkeypatch.setattr(market_verifier_mod, "fetch_event_by_slug", lambda slug, **kw: _event_for_config(config))

    with pytest.raises(SystemExit, match="live preflight blocked execution"):
        runner_mod.run_location_command(config_path, live_flag=True)

    output = capsys.readouterr().out
    assert "operator_mode_alert_only" in output
    assert "live_config_hash_not_acknowledged" in output


def test_run_location_live_preflight_allows_acknowledged_alert_only_monitoring(tmp_path, monkeypatch, capsys) -> None:
    from polybot.core.operator import OperatorGate
    from polybot.core.execution import LivePosition

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    config_path = tmp_path / "qatar.yaml"
    config_path.write_text("event:\n  slug: test-slug\nexecution:\n  dry_run: false\n", encoding="utf-8")
    config = _config(
        classifier=ClassifierConfig(passes=2, require_pass_agreement=True),
        execution=ExecutionConfig(dry_run=False, sell=SellConfig(), buy_rotation=BuyRotationConfig()),
        sources=SourcesConfig(poll_urls=["https://example.com/story"]),
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
    )
    OperatorGate(config_path, config).write_ack(note="test")
    monkeypatch.setattr(runner_mod, "load_location_config", lambda path: config)
    class _TokenAwareAdapter(DryRunTradingAdapter):
        def query_live_position(self, yes_token_id, no_token_id):
            shares = 1000.0 if yes_token_id == config.held_outcome().yes_token_id else 0.0
            return LivePosition(yes_token_id=yes_token_id, no_token_id=no_token_id, no_shares=0.0, yes_shares=shares)

    monkeypatch.setattr(runner_mod, "_live_adapter", lambda **kwargs: _TokenAwareAdapter())
    monkeypatch.setattr(market_verifier_mod, "fetch_event_by_slug", lambda slug, **kw: _event_for_config(config))
    started = {}

    class _Bot:
        def __init__(self, *, config, adapter, operator_gate, live_requested):
            started["live_requested"] = live_requested
            started["operator_mode"] = operator_gate.current_mode()

        def run_once(self):
            raise SystemExit(0)

    monkeypatch.setattr(runner_mod, "LocationProtectionBot", _Bot)

    with pytest.raises(SystemExit) as excinfo:
        runner_mod.run_location_command(config_path, live_flag=True)

    assert excinfo.value.code == 0
    assert started == {"live_requested": True, "operator_mode": "alert_only"}
    output = capsys.readouterr().out
    assert '"config_acknowledged": true' in output
    assert "operator_mode_alert_only" in output


def test_quote_verification_applies_to_trade_actions_below_level_4(tmp_path) -> None:
    bot = _policy_bot(tmp_path, _sources(allow_unknown_age_poll_auto_trade=True))
    decision = LocationDecision(
        "ROTATE_YES",
        "3",
        "confirmed_location:pakistan",
        target_outcome="pakistan",
        factors=_signal(confirmed_location="pakistan", level="3", quote_supporting_trigger="missing quote"),
    )
    out = bot._verify_quote_or_alert(decision, article("The next round will begin in Pakistan."))
    assert out.action == "ALERT_ONLY"
    assert out.reason == "quote_verification_failed"


# ---- source timestamp extraction ----


def test_extract_published_at_from_meta_tag() -> None:
    from polybot.iran.source_fetcher import _extract_published_at

    markup = '<html><head><meta property="article:published_time" content="2026-07-06T12:34:56Z"></head><body>x</body></html>'
    assert _extract_published_at(markup) == "2026-07-06T12:34:56+00:00"


def test_extract_published_at_from_json_ld() -> None:
    from polybot.iran.source_fetcher import _extract_published_at

    markup = '<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"Mon, 06 Jul 2026 12:34:56 GMT"}</script>'
    assert _extract_published_at(markup) == "2026-07-06T12:34:56+00:00"


# ---- continued hardening: execution audit + market-verification monitor ----


def test_rotated_state_records_execution_audit_fields(tmp_path) -> None:
    config = _config(position=PositionConfig(held_yes_shares=1000.0, max_yes_shares_to_sell=1000.0, max_rotation_usd_to_buy=500.0))
    adapter = DryRunTradingAdapter(yes_shares=1000.0, yes_bid=0.10, yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ROTATE_YES", "4B", "confirmed_location:pakistan", target_outcome="pakistan", factors=_signal(confirmed_location="pakistan"))

    assert executor.execute(decision, article("Officials confirm the round begins in Pakistan.")) == "ROTATED"

    current = executor.store.current()
    assert current is not None
    payload = current.payload
    assert payload["pre_trade_yes_best_bid"] == 0.10
    assert payload["pre_trade_yes_best_ask"] == 0.40
    assert payload["sell_min_price"] == config.execution.sell.min_price
    assert payload["sale_price_used"] == 0.10
    assert payload["confirmed_proceeds"] == pytest.approx(100.0)
    assert payload["target_best_ask"] == 0.40
    assert payload["target_max_price"] == config.execution.buy_rotation.max_price
    assert payload["rotation_usd_budget"] == pytest.approx(25.0)
    assert payload["configured_rotation_usd_budget"] == config.execution.buy_rotation.usd_budget
    assert payload["max_rotation_usd_to_buy"] == config.position.max_rotation_usd_to_buy


def test_heartbeat_reports_position_state_and_market_verification(tmp_path) -> None:
    adapter = DryRunTradingAdapter(yes_shares=321.0, no_shares=2.0, yes_bid=0.25, yes_ask=0.27)
    bot, notified = _monitoring_bot(
        tmp_path,
        adapter,
        MonitoringConfig(heartbeat=HeartbeatConfig(enabled=True, interval_hours=24)),
    )
    bot.store.write("EXITED", reason="test_exit")
    state = bot._monitoring_state()
    state["market_verification"] = {"status": "ok"}
    bot._write_monitoring_state(state)

    bot.run_once()

    heartbeat = [item for item in notified if item[0] == "Location protection heartbeat"][-1][1]
    assert heartbeat["held_yes_shares"] == 321.0
    assert heartbeat["held_no_shares"] == 2.0
    assert heartbeat["current_state"] == "EXITED"
    assert heartbeat["current_reason"] == "test_exit"
    assert heartbeat["market_verification_status"] == "ok"


def test_live_heartbeat_reports_selected_execution_backend(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("POLYBOT_EXECUTION_BACKEND", "clob_v2")
    adapter = DryRunTradingAdapter(yes_shares=321.0, no_shares=2.0, yes_bid=0.25, yes_ask=0.27)
    bot, notified = _monitoring_bot(
        tmp_path,
        adapter,
        MonitoringConfig(heartbeat=HeartbeatConfig(enabled=True, interval_hours=24)),
    )
    bot.live_requested = True

    bot.run_once()

    heartbeat = [item for item in notified if item[0] == "Location protection heartbeat"][-1][1]
    assert heartbeat["execution_backend"] == "clob_v2"


def test_market_verification_monitor_records_failure_and_blocks_trade_policy(tmp_path, monkeypatch) -> None:
    config = _config(
        monitoring=MonitoringConfig(market_verification=MarketVerificationMonitorConfig(enabled=True, interval_minutes=30)),
        sources=_sources(),
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
    )
    bot = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_shares=1000.0))
    notified: list[tuple[str, dict]] = []

    class _Notifier:
        def notify(self, message, **fields):
            notified.append((message, fields))

    bot.notifier = _Notifier()  # type: ignore[assignment]
    monkeypatch.setattr(runner_mod, "verify_location_event", lambda config: (_ for _ in ()).throw(ValueError("rule hash drift")))

    bot.run_once()

    monitor_state = bot._monitoring_state()["market_verification"]
    assert monitor_state["status"] == "blocked"
    assert "rule hash drift" in monitor_state["error"]
    assert notified[-1][0] == "Location market verification failed"
    decision = bot._enforce_execution_policy(_trade_decision())
    assert decision.action == "ALERT_ONLY"
    assert decision.reason.startswith("market_verification_blocked:rule hash drift")


def test_market_verification_monitor_records_recovery(tmp_path, monkeypatch) -> None:
    config = _config(
        monitoring=MonitoringConfig(market_verification=MarketVerificationMonitorConfig(enabled=True, interval_minutes=30)),
        sources=_sources(),
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
    )
    bot = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_shares=1000.0))
    notified: list[tuple[str, dict]] = []

    class _Notifier:
        def notify(self, message, **fields):
            notified.append((message, fields))

    bot.notifier = _Notifier()  # type: ignore[assignment]
    state = bot._monitoring_state()
    state["market_verification"] = {"status": "blocked", "error": "old error", "last_checked_at": "2020-01-01T00:00:00+00:00"}
    bot._write_monitoring_state(state)
    event = _event_for_config(config)
    monkeypatch.setattr(runner_mod, "verify_location_event", lambda config: market_verifier_mod.LocationMarketVerification(
        event_slug="test-slug",
        event_title="Test event",
        rule_text="rules text",
        rule_text_sha256="abc",
        outcomes=verify_location_event(config).outcomes,
    ))
    monkeypatch.setattr(runner_mod, "verify_all_outcomes", lambda config, verification, *, require_tradeable=False: None)
    monkeypatch.setattr(market_verifier_mod, "fetch_event_by_slug", lambda slug, **kw: event)

    bot.run_once()

    monitor_state = bot._monitoring_state()["market_verification"]
    assert monitor_state["status"] == "ok"
    assert monitor_state["rule_text_sha256"] == "abc"
    assert notified[-1][0] == "Location market verification recovered"


def test_dawn_headline_islamabad_but_body_technical_and_doha_high_level_does_not_rotate() -> None:
    config = _config()
    classifier = RuleBasedFixtureLocationClassifier(config)
    text = (
        "Islamabad has emerged as a frontrunner to host the next round of technical negotiations. "
        "Officials said the July 11 discussions would be technical talks, and the final decision has not been announced. "
        "High-level direct talks are expected to take place in Doha during the third week of July after technical teams finish details."
    )
    signal = classifier.classify(article(text, domain="dawn.com", title="Islamabad frontrunner to host US-Iran talks"), "rules")
    assert signal.headline_location == "pakistan"
    assert signal.technical_location == "pakistan"
    assert signal.future_expected_formal_location == "qatar"
    assert signal.final_decision_announced is False
    assert signal.qualifies_as_senior_round is False
    assert signal.confirmed_location == "none"

    decision = final_decision(config, signal)
    assert decision.action == "NO_ACTION"
    assert decision.reason == "technical_location_not_qualifying_held_future_expected"
    assert decision.target_outcome is None


def test_headline_says_doha_but_body_says_final_venue_unannounced_does_not_confirm_held() -> None:
    config = _config()
    classifier = RuleBasedFixtureLocationClassifier(config)
    text = (
        "Diplomats said Qatar remains under discussion as a possible host. "
        "The final venue has not been announced, and officials said several mediator capitals remain under review."
    )

    signal = classifier.classify(article(text, domain="reuters.com", title="Doha eyed for next US-Iran talks"), "rules")

    assert signal.headline_location == "qatar"
    assert signal.final_decision_announced is False
    assert signal.confirmed_location == "none"
    assert signal.qualifies_as_senior_round is False

    decision = final_decision(config, signal)
    assert decision.action == "NO_ACTION"
    assert decision.reason == "technical_or_non_qualifying"


def test_delegation_lands_in_doha_without_meeting_wording_does_not_confirm_held() -> None:
    config = _config()
    classifier = RuleBasedFixtureLocationClassifier(config)
    text = (
        "An Iranian delegation landed in Doha on Wednesday. "
        "Officials did not announce any formal senior-level US-Iran round, venue, or schedule."
    )

    signal = classifier.classify(article(text, domain="aljazeera.com", title="Iranian delegation lands in Doha"), "rules")

    assert signal.headline_location == "qatar"
    assert signal.confirmed_location == "none"
    assert signal.qualifies_as_senior_round is False

    decision = final_decision(config, signal)
    assert decision.action == "NO_ACTION"
    assert decision.reason == "technical_or_non_qualifying"


def test_wire_confirmed_non_qatar_rotation_target_rotates() -> None:
    config = _config()
    classifier = RuleBasedFixtureLocationClassifier(config)
    text = "Officials confirm the next formal senior-level round will begin in Pakistan on July 21."

    signal = classifier.classify(article(text, domain="apnews.com", title="US-Iran talks to resume in Islamabad"), "rules")

    assert signal.source_tier == "wire"
    assert signal.confirmed_location == "pakistan"
    assert signal.qualifies_as_senior_round is True

    decision = final_decision(config, signal)
    assert decision.action == "ROTATE_YES"
    assert decision.target_outcome == "pakistan"


def test_wire_confirmed_non_rotation_location_exits_without_buying_target() -> None:
    config = _config()
    signal = _signal(
        source_is_trusted=True,
        qualifies_as_senior_round=True,
        round_status="scheduled",
        location_country_name="Russia",
        confirmed_location="russia",
        evidence_strength="confirmed_scheduled",
        would_resolve_held_location_yes=False,
        would_resolve_held_location_no=True,
        source_tier="wire",
        quote_supporting_trigger="The next formal round will be held in Russia.",
    )

    decision = final_decision(config, signal)

    assert decision.action == "EXIT_YES_ONLY"
    assert decision.reason == "confirmed_non_held_location_not_rotated:russia"
    assert decision.target_outcome is None


def test_irna_official_denial_stays_alert_only_not_auto_exit() -> None:
    config = _config()
    signal = _signal(
        source_is_trusted=True,
        qualifies_as_senior_round=False,
        round_status="none",
        confirmed_location="no_meeting",
        evidence_strength="denied",
        would_resolve_held_location_yes=False,
        would_resolve_held_location_no=True,
        source_tier="state_media",
        quote_supporting_trigger="Iranian officials denied that a new round had been scheduled.",
    )

    decision = final_decision(config, signal)

    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "no_meeting_reported_unconfirmed"


def test_social_rumor_non_qatar_location_stays_alert_only() -> None:
    config = _config()
    signal = _signal(
        source_is_trusted=False,
        qualifies_as_senior_round=True,
        round_status="rumor",
        location_country_name="Pakistan",
        confirmed_location="pakistan",
        evidence_strength="speculative",
        would_resolve_held_location_yes=False,
        would_resolve_held_location_no=True,
        source_tier="other",
        quote_supporting_trigger="A Telegram channel claimed the next round would be in Islamabad.",
    )

    decision = final_decision(config, signal)

    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "source_not_trusted"


def test_prompt_instructs_body_over_headline_and_technical_location_extraction() -> None:
    from polybot.location.classifier import _prompt

    prompt = _prompt(article("test"), "rules", _config())
    assert "Never classify from the headline alone" in prompt
    assert "technical_location" in prompt
    assert "future_expected_formal_location" in prompt
    assert "final_decision_announced=false" in prompt


def test_classifier_pass_agreement_includes_body_aware_location_fields() -> None:
    config = _config(classifier=ClassifierConfig(provider="rule_based", require_pass_agreement=True, passes=2))
    first = _signal(
        qualifies_as_senior_round=False,
        round_status="technical_only",
        confirmed_location="none",
        technical_location="pakistan",
        future_expected_formal_location="qatar",
        final_decision_announced=False,
    )
    second = _signal(
        qualifies_as_senior_round=False,
        round_status="technical_only",
        confirmed_location="none",
        technical_location="pakistan",
        future_expected_formal_location="none",
        final_decision_announced=False,
    )
    decision = classify_agreement(config, [first, second])
    assert decision.action == "ALERT_ONLY"
    assert "future_expected_formal_location" in decision.reason
