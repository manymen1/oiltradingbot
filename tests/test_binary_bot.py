from __future__ import annotations

from pathlib import Path

import pytest

from polybot.core.config import SourcesConfig
from polybot.core.execution import DryRunTradingAdapter, Fill
from polybot.core.operator import OperatorGate
from polybot.core.storage import StateStore
from polybot.core.types import Article
from polybot.binary.config import (
    BinaryBotConfig,
    EntryConfig,
    ExecutionConfig,
    FlipBuyConfig,
    MarketConfig,
    PositionConfig,
    SellConfig,
    TimeDecayConfig,
    load_binary_config,
)
from polybot.binary.decision import (
    BinaryDecision,
    classify_agreement,
    entry_decision,
    held_decision,
    time_decay_decision,
)
from polybot.binary.executor import BinaryExecutor
from polybot.binary.market_verifier import BinaryMarketVerification
from polybot.binary.runner import BinaryRuleBot
from polybot.binary.types import RuleSignal


def article(text: str, domain: str = "reuters.com", title: str | None = None) -> Article:
    return Article(
        url=f"https://{domain}/story",
        domain=domain,
        title=title or text,
        published_at=None,
        fetched_at="2026-07-10T00:00:00Z",
        raw_text=text,
        hash=str(abs(hash((domain, text)))),
    )


def _verification() -> BinaryMarketVerification:
    return BinaryMarketVerification(
        event_slug="test-event",
        market_question="Will the qualifying event happen by September 30, 2026?",
        rule_text="test rules",
        rule_text_sha256="digest",
        condition_id="0xcond",
        yes_token_id="yes-token",
        no_token_id="no-token",
        tradeable=True,
        tick_size="0.01",
        neg_risk=False,
    )


def _config(**overrides) -> BinaryBotConfig:
    defaults = dict(
        market=MarketConfig(
            slug="test-slug",
            deadline_date="2026-09-30",
            question="Will the qualifying event happen by September 30, 2026?",
            held_side="",
            resolution_rules="test rules",
        ),
        position=PositionConfig(max_shares_to_sell=1000.0, max_flip_usd_to_buy=500.0),
        entry=EntryConfig(enabled=True, side="YES", usd_budget=100.0, max_price=0.90, max_entries=1),
        execution=ExecutionConfig(dry_run=True, sell=SellConfig(), flip_buy=FlipBuyConfig()),
        time_decay=TimeDecayConfig(),
    )
    defaults.update(overrides)
    return BinaryBotConfig(**defaults)


def _signal(**overrides) -> RuleSignal:
    defaults = dict(
        source_is_trusted=True,
        source_tier="wire",
        qualifies_under_rules=True,
        event_status="scheduled",
        evidence_strength="confirmed_scheduled",
        before_deadline=True,
        resolves_no=False,
        level="4A",
        quote_supporting_trigger="The round will begin next week.",
    )
    defaults.update(overrides)
    return RuleSignal(**defaults)


def _executor(tmp_path, config: BinaryBotConfig, adapter: DryRunTradingAdapter) -> BinaryExecutor:
    store = StateStore(tmp_path / "state")

    class _Notifier:
        def notify(self, message, **fields):
            pass

    return BinaryExecutor(config, _verification(), store, _Notifier(), adapter)


# ---- entry decision table ----


def test_qualifying_confirmed_triggers_enter_yes() -> None:
    decision = entry_decision(_config(), _signal(event_status="underway", evidence_strength="confirmed_started"))
    assert decision.action == "ENTER_YES"
    assert decision.reason == "qualifying_event_confirmed:underway"


def test_weak_evidence_is_alert_only() -> None:
    decision = entry_decision(_config(), _signal(evidence_strength="reported_indirect"))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "entry_signal_not_yet_confirmed:scheduled"


def test_non_tier_one_is_alert_only() -> None:
    decision = entry_decision(_config(), _signal(source_tier="other"))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "entry_signal_not_yet_confirmed:scheduled"


def test_after_deadline_is_alert_only() -> None:
    decision = entry_decision(_config(), _signal(before_deadline=False))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "qualifying_event_not_before_deadline"


def test_entry_disabled_is_alert_only() -> None:
    decision = entry_decision(_config(entry=EntryConfig(enabled=False)), _signal())
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "entry_disabled_qualifying_event_confirmed"


def test_not_final_is_alert_only() -> None:
    decision = entry_decision(_config(), _signal(final_decision_announced=False))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "entry_event_not_final"


def test_non_qualifying_is_no_action() -> None:
    decision = entry_decision(_config(), _signal(qualifies_under_rules=False, event_status="unclear"))
    assert decision.action == "NO_ACTION"
    assert decision.reason == "no_rule_qualifying_signal"


def test_untrusted_source_is_alert_only() -> None:
    decision = entry_decision(_config(), _signal(source_is_trusted=False))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "source_not_trusted"


def test_foreclosure_with_yes_entry_side_is_alert_only() -> None:
    decision = entry_decision(_config(), _signal(resolves_no=True, qualifies_under_rules=False, evidence_strength="denied", event_status="cancelled"))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "foreclosure_reported_while_flat"


def test_foreclosure_with_no_entry_side_triggers_enter_no() -> None:
    config = _config(entry=EntryConfig(enabled=True, side="NO"))
    decision = entry_decision(config, _signal(resolves_no=True, qualifies_under_rules=False, evidence_strength="denied", event_status="cancelled", source_tier="official_government"))
    assert decision.action == "ENTER_NO"
    assert decision.reason == "foreclosure_confirmed"


def test_qualifying_signal_with_no_entry_side_is_alert_only() -> None:
    config = _config(entry=EntryConfig(enabled=True, side="NO"))
    decision = entry_decision(config, _signal())
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "qualifying_signal_while_awaiting_no_entry"


# ---- held decision tables ----


def test_held_yes_foreclosure_confirmed_exits() -> None:
    decision = held_decision(_config(), _signal(resolves_no=True, qualifies_under_rules=False, evidence_strength="denied", event_status="cancelled"), "yes")
    assert decision.action == "EXIT_HELD"
    assert decision.reason == "yes_foreclosure_confirmed"


def test_held_yes_foreclosure_weak_is_alert_only() -> None:
    decision = held_decision(_config(), _signal(resolves_no=True, qualifies_under_rules=False, evidence_strength="speculative", source_tier="other"), "yes")
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "yes_foreclosure_reported_unconfirmed"


def test_held_yes_reinforced_is_no_action() -> None:
    decision = held_decision(_config(), _signal(event_status="underway", evidence_strength="confirmed_started"), "yes")
    assert decision.action == "NO_ACTION"
    assert decision.reason == "held_yes_thesis_reinforced"


def test_held_no_qualifying_begun_confirmed_exits() -> None:
    decision = held_decision(_config(), _signal(event_status="underway", evidence_strength="confirmed_started"), "no")
    assert decision.action == "EXIT_HELD"
    assert decision.level == "4B"
    assert decision.reason == "qualifying_event_begun_confirmed"


def test_held_no_qualifying_scheduled_confirmed_exits_4a() -> None:
    decision = held_decision(_config(), _signal(event_status="scheduled", evidence_strength="confirmed_scheduled"), "no")
    assert decision.action == "EXIT_HELD"
    assert decision.level == "4A"


def test_held_no_qualifying_after_deadline_is_no_action() -> None:
    decision = held_decision(_config(), _signal(before_deadline=False), "no")
    assert decision.action == "NO_ACTION"
    assert decision.reason == "qualifying_event_after_deadline"


def test_held_no_foreclosure_reinforces() -> None:
    decision = held_decision(_config(), _signal(resolves_no=True, qualifies_under_rules=False, evidence_strength="denied"), "no")
    assert decision.action == "NO_ACTION"
    assert decision.reason == "held_no_thesis_reinforced"


# ---- agreement ----


def test_flat_disagreement_is_alert_only() -> None:
    passes = [_signal(), _signal(event_status="underway", evidence_strength="confirmed_started")]
    decision = classify_agreement(_config(), passes, held_side=None)
    assert decision.action == "ALERT_ONLY"
    assert decision.reason.startswith("classifier_pass_disagreement:")


def test_flat_agreement_enters() -> None:
    passes = [_signal(), _signal()]
    decision = classify_agreement(_config(), passes, held_side=None)
    assert decision.action == "ENTER_YES"


def test_held_yes_foreclosure_fast_path_skips_agreement() -> None:
    foreclosure = _signal(resolves_no=True, qualifies_under_rules=False, evidence_strength="denied", event_status="cancelled", quote_supporting_trigger="Talks are cancelled entirely.")
    disagreeing = _signal()
    decision = classify_agreement(_config(), [foreclosure, disagreeing], held_side="yes")
    assert decision.action == "EXIT_HELD"


def test_flat_foreclosure_fast_path_disabled() -> None:
    config = _config(entry=EntryConfig(enabled=True, side="NO"))
    foreclosure = _signal(resolves_no=True, qualifies_under_rules=False, evidence_strength="denied", event_status="cancelled", source_tier="official_government", quote_supporting_trigger="Talks are cancelled entirely.")
    disagreeing = _signal(source_tier="official_government")
    decision = classify_agreement(config, [foreclosure, disagreeing], held_side=None)
    assert decision.action == "ALERT_ONLY"
    assert decision.reason.startswith("classifier_pass_disagreement:")


# ---- time decay ----


def test_time_decay_only_applies_to_held_yes() -> None:
    config = _config(time_decay=TimeDecayConfig(enabled=True, exit_after_date="2020-01-01"))
    assert time_decay_decision(config, None).action == "NO_ACTION"
    assert time_decay_decision(config, "no").action == "NO_ACTION"
    decayed = time_decay_decision(config, "yes")
    assert decayed.action == "EXIT_HELD"
    assert decayed.level == "TIME"


# ---- executor ----


def test_executor_enters_yes_and_records_holding(tmp_path) -> None:
    executor = _executor(tmp_path, _config(), DryRunTradingAdapter(yes_ask=0.40))
    decision = BinaryDecision("ENTER_YES", "4B", "qualifying_event_confirmed:scheduled", _signal())
    result = executor.execute(decision, article("The round will be held next week."))
    assert result == "ENTERED"
    assert executor.holdings.held_location() == "yes"
    assert executor.holdings.record().source == "entry"
    assert executor.entry_count() == 1
    current = executor.store.current()
    assert current is not None and current.state == "ENTERED"
    assert current.payload["side"] == "yes"


def test_executor_enters_no_side(tmp_path) -> None:
    config = _config(entry=EntryConfig(enabled=True, side="NO"))
    executor = _executor(tmp_path, config, DryRunTradingAdapter(no_ask=0.30))
    decision = BinaryDecision("ENTER_NO", "4B", "foreclosure_confirmed", _signal(resolves_no=True))
    result = executor.execute(decision, article("Talks cancelled, will not happen."))
    assert result == "ENTERED"
    assert executor.holdings.held_location() == "no"


def test_executor_records_tiny_positive_fill_as_partial_exposure(tmp_path) -> None:
    class _TinyFillAdapter(DryRunTradingAdapter):
        def verify_fill(self, result, token_id):
            return Fill(filled_shares=1.0, raw=result)

    config = _config(entry=EntryConfig(enabled=True, side="YES", usd_budget=100.0, min_fill_usd=5.0, min_fill_fraction=0.25))
    executor = _executor(tmp_path, config, _TinyFillAdapter(yes_ask=0.40))
    decision = BinaryDecision("ENTER_YES", "4B", "qualifying_event_confirmed:scheduled", _signal())

    result = executor.execute(decision, article("The round will be held next week."))

    assert result == "PARTIALLY_ENTERED"
    assert executor.holdings.held_location() == "yes"
    current = executor.store.current()
    assert current is not None and current.state == "PARTIALLY_ENTERED"
    assert current.payload["fill_fraction"] == pytest.approx(0.004)


def test_executor_entry_price_above_cap_stays_flat(tmp_path) -> None:
    executor = _executor(tmp_path, _config(), DryRunTradingAdapter(yes_ask=0.95))
    decision = BinaryDecision("ENTER_YES", "4B", "qualifying_event_confirmed:scheduled", _signal())
    result = executor.execute(decision, article("The round will be held next week."))
    assert result == "ENTRY_PRICE_ABOVE_CAP"
    assert executor.holdings.held_location() is None
    assert executor.entry_count() == 0


def test_executor_entry_side_mismatch_skips(tmp_path) -> None:
    executor = _executor(tmp_path, _config(), DryRunTradingAdapter(no_ask=0.30))
    decision = BinaryDecision("ENTER_NO", "4B", "foreclosure_confirmed", _signal(resolves_no=True))
    result = executor.execute(decision, article("Talks cancelled."))
    assert result == "SKIPPED"
    assert executor.holdings.held_location() is None


def test_enter_then_foreclosure_exit_and_one_shot(tmp_path) -> None:
    executor = _executor(tmp_path, _config(), DryRunTradingAdapter(yes_shares=250.0, yes_ask=0.40))
    enter = BinaryDecision("ENTER_YES", "4B", "qualifying_event_confirmed:scheduled", _signal())
    assert executor.execute(enter, article("The round will be held next week.")) == "ENTERED"

    exit_decision = BinaryDecision("EXIT_HELD", "4B", "yes_foreclosure_confirmed", _signal(resolves_no=True))
    assert executor.execute(exit_decision, article("Talks cancelled, will not happen.")) == "EXITED"
    assert executor.holdings.held_location() is None
    assert executor.holdings.record().source == "exit"

    result = executor.execute(enter, article("The round is back on and will be held after all."))
    assert result == "EXITED"  # terminal one_shot block, still flat
    assert executor.holdings.held_location() is None


def test_executor_flip_no_to_yes(tmp_path) -> None:
    config = _config(
        market=MarketConfig(slug="test-slug", deadline_date="2026-09-30", held_side="NO", resolution_rules="test rules"),
        execution=ExecutionConfig(dry_run=True, sell=SellConfig(), flip_buy=FlipBuyConfig(enabled=True, max_price=0.95, usd_budget=200.0)),
    )
    executor = _executor(tmp_path, config, DryRunTradingAdapter(no_shares=400.0, yes_ask=0.40))
    decision = BinaryDecision("EXIT_HELD", "4B", "qualifying_event_begun_confirmed", _signal(event_status="underway", evidence_strength="confirmed_started"))
    result = executor.execute(decision, article("The qualifying round has begun."))
    assert result == "FLIPPED"
    assert executor.holdings.held_location() == "yes"
    assert executor.holdings.record().source == "flip"
    current = executor.store.current()
    assert current is not None
    assert current.payload["from_side"] == "no"
    assert current.payload["to_side"] == "yes"


def test_executor_flip_above_cap_exits_flat(tmp_path) -> None:
    config = _config(
        market=MarketConfig(slug="test-slug", deadline_date="2026-09-30", held_side="NO", resolution_rules="test rules"),
        execution=ExecutionConfig(dry_run=True, sell=SellConfig(), flip_buy=FlipBuyConfig(enabled=True, max_price=0.10)),
    )
    executor = _executor(tmp_path, config, DryRunTradingAdapter(no_shares=400.0, yes_ask=0.90))
    decision = BinaryDecision("EXIT_HELD", "4B", "qualifying_event_begun_confirmed", _signal(event_status="underway", evidence_strength="confirmed_started"))
    result = executor.execute(decision, article("The qualifying round has begun."))
    assert result == "EXITED"
    assert executor.holdings.held_location() is None


def test_executor_trim_keeps_holding(tmp_path) -> None:
    config = _config(
        market=MarketConfig(slug="test-slug", deadline_date="2026-09-30", held_side="YES", resolution_rules="test rules"),
        time_decay=TimeDecayConfig(enabled=True, trim_after_date="2020-01-01", trim_fraction=0.25),
    )
    executor = _executor(tmp_path, config, DryRunTradingAdapter(yes_shares=400.0))
    decision = BinaryDecision("TRIM_HELD", "TIME", "time_decay_trim")
    result = executor.execute(decision, article("time decay"))
    assert result == "TRIMMED"
    assert executor.holdings.held_location() == "yes"


# ---- operator gate ----


def test_operator_gate_blocks_binary_entries_by_default(tmp_path) -> None:
    config = _config(data_dir=tmp_path / "data")
    config_path = tmp_path / "binary.yaml"
    config_path.write_text("binary-config\n", encoding="utf-8")
    gate = OperatorGate(config_path, config)
    for action in ("ENTER_YES", "ENTER_NO", "EXIT_HELD", "TRIM_HELD"):
        result = gate.check(BinaryDecision(action, "4B", "test"), live_requested=True)
        assert not result.allowed, action


# ---- bot flow ----


def _bot(tmp_path, config: BinaryBotConfig, adapter: DryRunTradingAdapter) -> BinaryRuleBot:
    return BinaryRuleBot(config=config, market=_verification(), adapter=adapter)


def test_bot_enters_then_defends(tmp_path) -> None:
    config = _config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(max_trade_article_age_hours=0.0),
    )
    bot = _bot(tmp_path, config, DryRunTradingAdapter(yes_shares=250.0, yes_ask=0.40))
    first = bot.process_article(article("US and Iran senior talks scheduled: the round will be held in Doha next week."))
    assert first.action == "ENTER_YES"
    assert bot.holdings.held_location() == "yes"

    second = bot.process_article(article("Officials say the talks are cancelled and the round will not happen.", title="cancelled"))
    assert second.action == "EXIT_HELD"
    assert bot.holdings.held_location() is None
    current = bot.store.current()
    assert current is not None and current.state == "EXITED"


def test_bot_max_entries_reached_is_alert_only(tmp_path) -> None:
    config = _config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(max_trade_article_age_hours=0.0),
    )
    bot = _bot(tmp_path, config, DryRunTradingAdapter(yes_ask=0.40))
    first = bot.process_article(article("The round will be held in Doha next week."))
    assert first.action == "ENTER_YES"
    bot.holdings.clear_held(source="exit")
    second = bot.process_article(article("New reports: the round will be held in Muscat instead.", title="second"))
    assert second.action == "ALERT_ONLY"
    assert second.reason == "max_entries_reached:1"


def test_bot_keyword_gate_blocks_unrelated(tmp_path) -> None:
    from polybot.binary.config import KeywordsConfig

    config = _config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        keywords=KeywordsConfig(escalate_terms=["talks", "negotiations"]),
    )
    bot = _bot(tmp_path, config, DryRunTradingAdapter())
    decision = bot.process_article(article("Unrelated sports story about the cup final."))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "keyword_gate_no_trigger"


# ---- config loading/validation ----


def _yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


_BASE_YAML = """
market:
  slug: "test-slug"
  question: "Will the qualifying event happen?"
  deadline_date: "2026-09-30"
  held_side: ""
  resolution_rules: |
    Resolves YES if the qualifying event happens by the deadline.
"""


def test_load_config_parses_entry_and_normalizes_sides(tmp_path) -> None:
    path = _yaml(
        tmp_path,
        _BASE_YAML
        + """
entry:
  enabled: true
  side: "yes"
  usd_budget: 55.0
  max_price: 0.80
  max_entries: 2
""",
    )
    config = load_binary_config(path)
    assert config.entry.enabled is True
    assert config.entry.side == "YES"
    assert config.entry.usd_budget == 55.0
    assert config.entry.max_entries == 2
    assert config.market.held_side == ""


def test_load_config_rejects_flat_without_entry(tmp_path) -> None:
    path = _yaml(tmp_path, _BASE_YAML)
    with pytest.raises(ValueError, match="nothing to protect or enter"):
        load_binary_config(path)


def test_load_config_rejects_bad_held_side(tmp_path) -> None:
    path = _yaml(tmp_path, _BASE_YAML.replace('held_side: ""', 'held_side: "MAYBE"') + "\nentry:\n  enabled: true\n")
    with pytest.raises(ValueError, match="held_side"):
        load_binary_config(path)


def test_load_config_rejects_bad_entry_side(tmp_path) -> None:
    path = _yaml(tmp_path, _BASE_YAML + "\nentry:\n  enabled: true\n  side: BOTH\n")
    with pytest.raises(ValueError, match="entry.side"):
        load_binary_config(path)


def test_load_config_requires_resolution_rules(tmp_path) -> None:
    body = """
market:
  slug: "test-slug"
  question: "q"
  deadline_date: "2026-09-30"
  held_side: "YES"
"""
    with pytest.raises(ValueError, match="resolution_rules"):
        load_binary_config(_yaml(tmp_path, body))


# ---- profit levers: screen tier + armed polling ----


class _CountingClassifier:
    def __init__(self, signal):
        self.signal = signal
        self.calls = 0

    def classify(self, article, market_rule_text, held_side=None):
        self.calls += 1
        return self.signal


def test_screen_no_action_skips_strong_model(tmp_path) -> None:
    config = _config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(max_trade_article_age_hours=0.0),
    )
    bot = _bot(tmp_path, config, DryRunTradingAdapter(yes_ask=0.40))
    noise = _signal(qualifies_under_rules=False, event_status="unclear", evidence_strength="speculative")
    screen = _CountingClassifier(noise)
    strong = _CountingClassifier(_signal())
    bot.screen_classifier = screen
    bot.classifier = strong
    decision = bot.process_article(article("Unrelated diplomatic chatter about the region."))
    assert decision.action == "NO_ACTION"
    assert decision.reason.startswith("screen:")
    assert screen.calls == 1
    assert strong.calls == 0  # the expensive model never ran


def test_screen_trade_signal_escalates_to_strong_model(tmp_path) -> None:
    config = _config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(max_trade_article_age_hours=0.0),
    )
    bot = _bot(tmp_path, config, DryRunTradingAdapter(yes_shares=250.0, yes_ask=0.40))
    screen = _CountingClassifier(_signal())  # qualifying scheduled -> escalate
    strong = _CountingClassifier(_signal(event_status="underway", evidence_strength="confirmed_started", quote_supporting_trigger="The round has begun."))
    bot.screen_classifier = screen
    bot.classifier = strong
    decision = bot.process_article(article("The round has begun. Officials confirmed the talks."))
    assert screen.calls == 1
    assert strong.calls == 1
    assert decision.action == "ENTER_YES"
    assert bot.holdings.held_location() == "yes"


def test_screen_failure_escalates_when_live_but_fails_closed_on_paper(tmp_path) -> None:
    class _Broken:
        def classify(self, article, market_rule_text, held_side=None):
            raise RuntimeError("screen down")

    # Live: fail-open — the screen tier may only save money, never miss a
    # trade defending a real position.
    config = _config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(max_trade_article_age_hours=0.0),
    )
    bot = _bot(tmp_path, config, DryRunTradingAdapter(yes_ask=0.40))
    strong = _CountingClassifier(_signal())
    bot.screen_classifier = _Broken()
    bot.classifier = strong
    bot.live_requested = True
    decision = bot.process_article(article("The round will begin next week. Officials confirmed the venue."))
    assert strong.calls == 1
    assert decision.action == "ENTER_YES"

    # Paper: fail-closed — a broken screen path must not convert every noise
    # article into full-price confirm passes.
    config2 = _config(
        data_dir=tmp_path / "state2",
        logs_dir=tmp_path / "logs2",
        sources=SourcesConfig(max_trade_article_age_hours=0.0),
    )
    bot2 = _bot(tmp_path, config2, DryRunTradingAdapter(yes_ask=0.40))
    strong2 = _CountingClassifier(_signal())
    bot2.screen_classifier = _Broken()
    bot2.classifier = strong2
    bot2.live_requested = False
    decision2 = bot2.process_article(article("The round will begin next week. Officials confirmed the venue."))
    assert strong2.calls == 0
    assert decision2.action == "NO_ACTION"
    assert decision2.reason == "screen_error_paper_fail_closed"


def test_effective_poll_seconds_armed() -> None:
    from polybot.binary.runner import effective_poll_seconds
    from polybot.core.config import SafetyConfig

    slow = SafetyConfig(poll_seconds=30.0, armed_poll_seconds=0.0)
    fast = SafetyConfig(poll_seconds=30.0, armed_poll_seconds=5.0)
    assert effective_poll_seconds(slow, live_flag=True) == 30.0
    assert effective_poll_seconds(fast, live_flag=False) == 30.0
    assert effective_poll_seconds(fast, live_flag=True) == 5.0


# ---- ingestion bottleneck fixes ----


def test_feed_conditional_get_uses_validators_and_304(monkeypatch) -> None:
    from polybot.core import source_fetcher

    calls: list[dict] = []

    class _FullResponse:
        status_code = 200
        headers = {"ETag": '"abc"', "Last-Modified": "Mon, 13 Jul 2026 00:00:00 GMT"}
        content = (
            b'<?xml version="1.0"?><rss><channel><item>'
            b"<title>Iran talks scheduled</title>"
            b"<link>https://reuters.com/x</link>"
            b"<description>Senior talks scheduled.</description>"
            b"</item></channel></rss>"
        )

        def raise_for_status(self):
            return None

    class _NotModified:
        status_code = 304
        headers = {}
        content = b""

        def raise_for_status(self):
            raise AssertionError("raise_for_status must not run on 304")

    responses = [_FullResponse(), _NotModified()]

    def fake_get(url, headers=None, timeout=None):
        calls.append(dict(headers or {}))
        return responses[len(calls) - 1]

    monkeypatch.setattr(source_fetcher.requests, "get", fake_get)
    monkeypatch.setattr(source_fetcher, "_FEED_CONDITIONAL", {})

    first = source_fetcher.fetch_feed_articles("https://example.com/feed", include_terms=["iran"])
    assert len(first) == 1
    second = source_fetcher.fetch_feed_articles("https://example.com/feed", include_terms=["iran"])
    assert second == []  # 304 -> nothing new, near-zero cost
    assert calls[1]["If-None-Match"] == '"abc"'
    assert calls[1]["If-Modified-Since"] == "Mon, 13 Jul 2026 00:00:00 GMT"


def test_bot_fetches_sources_concurrently(tmp_path, monkeypatch) -> None:
    import threading
    import time as _time

    config = _config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(
            max_trade_article_age_hours=0.0,
            feed_urls=["https://a.example/feed", "https://b.example/feed", "https://c.example/feed"],
            promote_feed_to_article=False,
        ),
    )
    bot = _bot(tmp_path, config, DryRunTradingAdapter())
    concurrent = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow_feed(url, user_agent, **kwargs):
        with lock:
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
        _time.sleep(0.05)
        with lock:
            concurrent["now"] -= 1
        return []

    monkeypatch.setattr("polybot.binary.runner.fetch_feed_articles", slow_feed)
    start = _time.monotonic()
    bot.run_once()
    elapsed = _time.monotonic() - start
    assert concurrent["max"] >= 2  # overlapping fetches, not sequential
    assert elapsed < 0.15  # ~one slow-feed latency, not three


def test_fleet_bot_uses_central_feed_without_direct_fallback(tmp_path, monkeypatch) -> None:
    from polybot.core.central_feed import CentralFeedStore

    db_path = tmp_path / "central.sqlite3"
    feed_url = "https://shared.example/feed"
    central = CentralFeedStore(db_path)
    central.record_success(
        feed_url,
        [
            Article(
                url="https://reuters.com/iran-talks",
                domain="reuters.com",
                title="Iran talks scheduled",
                published_at="2026-07-24T00:00:00+00:00",
                fetched_at="2026-07-24T00:00:01+00:00",
                raw_text="Iran and the United States scheduled talks.",
                hash="central-iran",
                source_kind="feed",
            )
        ],
    )
    config = _config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(
            max_trade_article_age_hours=0.0,
            feed_urls=[feed_url],
            feed_include_terms=["iran"],
            central_feed_db=str(db_path),
            central_feed_stale_after_seconds=60.0,
            promote_feed_to_article=False,
        ),
    )
    bot = _bot(tmp_path, config, DryRunTradingAdapter())

    def forbidden_direct_fetch(*args, **kwargs):
        raise AssertionError("fleet bot must not fetch RSS directly")

    monkeypatch.setattr("polybot.binary.runner.fetch_feed_articles", forbidden_direct_fetch)
    batches = bot._fetch_all_sources()
    assert len(batches) == 1
    assert batches[0][0] == "central_feed"
    assert [article.hash for article in batches[0][1]] == ["central-iran"]
