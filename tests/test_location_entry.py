from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from polybot.core.config import SourcesConfig
from polybot.core.execution import DryRunTradingAdapter, Fill, LivePosition
from polybot.core.operator import OperatorGate
from polybot.core.storage import StateStore
from polybot.core.types import Article
from polybot.risk import RiskState, _today_key
from polybot.location.config import (
    BuyRotationConfig,
    ClassifierConfig,
    EntryConfig,
    EventConfig,
    ExecutionConfig,
    LocationBotConfig,
    OutcomeMarket,
    PositionConfig,
    SellConfig,
    TimeDecayConfig,
    TriggerConfig,
    load_location_config,
)
from polybot.location.decision import LocationDecision, classify_agreement, entry_decision, final_decision
from polybot.location.executor import LocationExecutor
from polybot.location.runner import LocationProtectionBot
from polybot.location.runtime import ProcessLock, ProcessLockError, ReconciliationError


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


def _outcomes() -> list[OutcomeMarket]:
    return [
        OutcomeMarket(name="qatar", label="Qatar", condition_id="0xqatar", yes_token_id="qatar-yes", no_token_id="qatar-no", rotation_target=True),
        OutcomeMarket(name="pakistan", label="Pakistan", condition_id="0xpk", yes_token_id="pk-yes", no_token_id="pk-no", rotation_target=True),
        OutcomeMarket(name="oman", label="Oman", condition_id="0xom", yes_token_id="om-yes", no_token_id="om-no", rotation_target=True),
        OutcomeMarket(name="no_meeting", label="No Meeting by September 30", condition_id="0xnm", yes_token_id="nm-yes", no_token_id="nm-no", rotation_target=False),
        OutcomeMarket(name="russia", label="Russia", condition_id="0xru", yes_token_id="ru-yes", no_token_id="ru-no", rotation_target=False),
    ]


def _flat_config(**overrides) -> LocationBotConfig:
    defaults = dict(
        event=EventConfig(
            slug="test-slug",
            question="Where will the next diplomatic US-Iran meeting be by September 30, 2026?",
            deadline_date="2026-09-30",
            held_location="",
            resolution_rules="test rules",
        ),
        outcomes=_outcomes(),
        position=PositionConfig(max_yes_shares_to_sell=1000.0, max_rotation_usd_to_buy=500.0),
        trigger=TriggerConfig(auto_execute_level=4, trusted_single_source_execution=True),
        classifier=ClassifierConfig(provider="rule_based"),
        entry=EntryConfig(enabled=True, targets=["qatar", "oman"], usd_budget=100.0, max_price=0.90, max_entries=1),
        execution=ExecutionConfig(dry_run=True, sell=SellConfig(), buy_rotation=BuyRotationConfig()),
        time_decay=TimeDecayConfig(),
    )
    defaults.update(overrides)
    return LocationBotConfig(**defaults)


def _signal(**overrides):
    from polybot.location.types import LocationSignal

    defaults = dict(
        source_is_trusted=True,
        qualifies_as_senior_round=True,
        round_status="scheduled",
        location_country_name="Qatar",
        confirmed_location="qatar",
        evidence_strength="confirmed_scheduled",
        would_resolve_held_location_yes=False,
        would_resolve_held_location_no=False,
        level="4A",
        quote_supporting_trigger="A new round will begin in Doha.",
        source_tier="wire",
    )
    defaults.update(overrides)
    return LocationSignal(**defaults)


def _executor(tmp_path, config: LocationBotConfig, adapter: DryRunTradingAdapter, risk: RiskState | None = None) -> LocationExecutor:
    store = StateStore(tmp_path / "state")
    notified: list[tuple[str, dict]] = []

    class _Notifier:
        def notify(self, message, **fields):
            notified.append((message, fields))

    executor = LocationExecutor(config, store, _Notifier(), adapter, risk=risk)
    executor._notified = notified  # type: ignore[attr-defined]
    return executor


# ---- entry decision table ----


def test_confirmed_entry_target_tier_one_strong_triggers_enter() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(confirmed_location="qatar", evidence_strength="confirmed_started", source_tier="wire"))
    assert decision.action == "ENTER_YES"
    assert decision.target_outcome == "qatar"
    assert decision.reason == "confirmed_location:qatar"


def test_weak_evidence_is_alert_only() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(confirmed_location="qatar", evidence_strength="speculative"))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "entry_signal_not_yet_confirmed:qatar"


def test_non_tier_one_source_is_alert_only() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(confirmed_location="qatar", evidence_strength="confirmed_scheduled", source_tier="other"))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "entry_signal_not_yet_confirmed:qatar"


def test_configured_non_entry_target_is_alert_only() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(confirmed_location="pakistan", evidence_strength="confirmed_started", source_tier="wire"))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "entry_target_not_allowed:pakistan"


def test_unconfigured_location_is_alert_only() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(confirmed_location="other_specific", evidence_strength="confirmed_started", source_tier="wire"))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "confirmed_location_not_configured:other_specific"


def test_entry_disabled_is_alert_only() -> None:
    config = _flat_config(entry=EntryConfig(enabled=False))
    decision = entry_decision(config, _signal(confirmed_location="qatar", evidence_strength="confirmed_started", source_tier="wire"))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "entry_disabled_confirmed_location:qatar"


def test_untrusted_source_is_alert_only() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(source_is_trusted=False))
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "source_not_trusted"


def test_technical_only_is_no_action() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(round_status="technical_only", qualifies_as_senior_round=False, confirmed_location="qatar"))
    assert decision.action == "NO_ACTION"


def test_no_location_signal_is_no_action() -> None:
    config = _flat_config()
    decision = entry_decision(config, _signal(confirmed_location="none"))
    assert decision.action == "NO_ACTION"
    assert decision.reason == "no_location_signal"


def test_no_meeting_not_an_entry_target_is_alert_only() -> None:
    config = _flat_config()
    decision = entry_decision(
        config,
        _signal(confirmed_location="no_meeting", qualifies_as_senior_round=False, evidence_strength="denied", source_tier="official_government"),
    )
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "no_meeting_reported_while_flat"


def test_no_meeting_as_entry_target_tier_one_triggers_enter() -> None:
    config = _flat_config(entry=EntryConfig(enabled=True, targets=["no_meeting"]))
    decision = entry_decision(
        config,
        _signal(confirmed_location="no_meeting", qualifies_as_senior_round=False, evidence_strength="denied", source_tier="official_government"),
    )
    assert decision.action == "ENTER_YES"
    assert decision.target_outcome == "no_meeting"


def test_venue_not_final_is_alert_only() -> None:
    config = _flat_config()
    decision = entry_decision(
        config,
        _signal(confirmed_location="qatar", evidence_strength="confirmed_scheduled", source_tier="wire", final_decision_announced=False),
    )
    assert decision.action == "ALERT_ONLY"
    assert decision.reason == "entry_venue_not_final:qatar"


# ---- classify_agreement while flat ----


def test_flat_agreement_pass_disagreement_is_alert_only() -> None:
    config = _flat_config()
    passes = [
        _signal(confirmed_location="qatar", evidence_strength="confirmed_started", source_tier="wire"),
        _signal(confirmed_location="oman", evidence_strength="confirmed_started", source_tier="wire"),
    ]
    decision = classify_agreement(config, passes, held=None, flat=True)
    assert decision.action == "ALERT_ONLY"
    assert decision.reason.startswith("classifier_pass_disagreement:")


def test_flat_agreement_matching_passes_enter() -> None:
    config = _flat_config()
    passes = [
        _signal(confirmed_location="qatar", evidence_strength="confirmed_started", source_tier="wire"),
        _signal(confirmed_location="qatar", evidence_strength="confirmed_started", source_tier="wire"),
    ]
    decision = classify_agreement(config, passes, held=None, flat=True)
    assert decision.action == "ENTER_YES"
    assert decision.target_outcome == "qatar"


def test_flat_collapse_fast_path_disabled_requires_agreement() -> None:
    # While holding, a tier-one collapse fires on the first pass alone; while
    # flat there is no loss to protect, so an entry into the no-meeting leg
    # must still meet the full agreement bar.
    config = _flat_config(entry=EntryConfig(enabled=True, targets=["no_meeting"]))
    collapse = _signal(
        confirmed_location="no_meeting",
        qualifies_as_senior_round=False,
        evidence_strength="denied",
        source_tier="official_government",
        quote_supporting_trigger="Talks are cancelled entirely.",
    )
    disagreeing = _signal(confirmed_location="none", evidence_strength="speculative", source_tier="official_government", qualifies_as_senior_round=False)
    decision = classify_agreement(config, [collapse, disagreeing], held=None, flat=True)
    assert decision.action == "ALERT_ONLY"
    assert decision.reason.startswith("classifier_pass_disagreement:")


def test_held_agreement_still_routes_to_protection() -> None:
    config = _flat_config()
    passes = [_signal(confirmed_location="pakistan", evidence_strength="confirmed_started", source_tier="wire")] * 2
    decision = classify_agreement(config, passes, held="qatar", flat=False)
    assert decision.action == "ROTATE_YES"
    assert decision.target_outcome == "pakistan"


# ---- executor entry path ----


def test_executor_enters_and_records_holding(tmp_path) -> None:
    config = _flat_config()
    adapter = DryRunTradingAdapter(yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    assert executor.holdings.held_location() is None
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    result = executor.execute(decision, article("Officials confirm the round will be held in Qatar."))
    assert result == "ENTERED"
    current = executor.store.current()
    assert current is not None and current.state == "ENTERED"
    assert current.payload["target_outcome"] == "qatar"
    assert executor.holdings.held_location() == "qatar"
    assert executor.holdings.record().source == "entry"
    assert executor.entry_count() == 1


def test_executor_requires_execution_adjusted_edge(tmp_path) -> None:
    config = _flat_config(entry=EntryConfig(enabled=True, targets=["qatar"], max_price=0.90, confirmed_probability=0.97, min_edge=0.05))
    adapter = DryRunTradingAdapter(yes_ask=0.90)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    assert executor.execute(decision, article("Officials confirm the round will be held in Qatar.")) == "ENTRY_NO_EDGE"


def test_entry_reserves_shared_risk_budget_before_order(tmp_path) -> None:
    config = _flat_config()
    risk = RiskState(path=tmp_path / "risk.json")
    executor = _executor(tmp_path, config, DryRunTradingAdapter(yes_ask=0.40, yes_bid=0.35), risk=risk)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    assert executor.execute(decision, article("A new round will begin in Doha.")) == "ENTERED"
    assert risk.per_market_spent["0xqatar"] == pytest.approx(risk.guardrails.per_order_notional)
    assert risk.per_day_spent[_today_key()] == pytest.approx(risk.guardrails.per_order_notional)


def test_entry_fails_closed_when_daily_risk_budget_is_exhausted(tmp_path) -> None:
    config = _flat_config()
    risk = RiskState(path=tmp_path / "risk.json")
    risk.per_day_spent[_today_key()] = risk.guardrails.per_day_notional
    executor = _executor(tmp_path, config, DryRunTradingAdapter(yes_ask=0.40, yes_bid=0.35), risk=risk)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    assert executor.execute(decision, article("A new round will begin in Doha.")) == "ENTRY_RISK_BLOCKED"
    assert executor.store.current().payload["reason"] == "risk_budget_exhausted"


def test_entry_fails_closed_when_shared_risk_state_is_halted(tmp_path) -> None:
    config = _flat_config()
    risk = RiskState(path=tmp_path / "risk.json", halted=True)
    executor = _executor(tmp_path, config, DryRunTradingAdapter(yes_ask=0.40, yes_bid=0.35), risk=risk)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    assert executor.execute(decision, article("A new round will begin in Doha.")) == "ENTRY_RISK_BLOCKED"
    assert executor.store.current().payload["reason"] == "risk_halted"
    assert executor.holdings.held_location() is None


def test_tiny_positive_fill_is_recorded_as_managed_partial_exposure(tmp_path) -> None:
    class _TinyFillAdapter(DryRunTradingAdapter):
        def verify_fill(self, result, token_id):
            return Fill(filled_shares=1.0, raw=result)

    config = _flat_config(entry=EntryConfig(enabled=True, targets=["qatar"], usd_budget=100.0, min_fill_usd=5.0, min_fill_fraction=0.25))
    executor = _executor(tmp_path, config, _TinyFillAdapter(yes_ask=0.40))
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    assert executor.execute(decision, article("Officials confirm the round will be held in Qatar.")) == "PARTIALLY_ENTERED"
    assert executor.holdings.held_location() == "qatar"
    assert executor.entry_count() == 1


def test_executor_entry_price_above_cap_stays_flat(tmp_path) -> None:
    config = _flat_config()
    adapter = DryRunTradingAdapter(yes_ask=0.95)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    result = executor.execute(decision, article("Officials confirm the round will be held in Qatar."))
    assert result == "ENTRY_PRICE_ABOVE_CAP"
    assert executor.holdings.held_location() is None
    assert executor.entry_count() == 0


def test_executor_global_guardrail_clamps_entry_cap(tmp_path) -> None:
    # SETTINGS.guardrails.max_entry_price defaults to 0.90 and can only be
    # lowered; a config cap of 0.99 must not buy through it.
    config = _flat_config(entry=EntryConfig(enabled=True, targets=["qatar"], max_price=0.99))
    adapter = DryRunTradingAdapter(yes_ask=0.95)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    result = executor.execute(decision, article("Officials confirm the round will be held in Qatar."))
    assert result == "ENTRY_PRICE_ABOVE_CAP"


def test_executor_entry_skipped_when_already_holding(tmp_path) -> None:
    config = _flat_config(
        event=EventConfig(
            slug="test-slug",
            question="q",
            deadline_date="2026-09-30",
            held_location="qatar",
            resolution_rules="test rules",
        ),
    )
    adapter = DryRunTradingAdapter(yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:oman", target_outcome="oman", factors=_signal(confirmed_location="oman"))
    result = executor.execute(decision, article("Officials confirm the round will be held in Oman."))
    assert result == "SKIPPED"
    assert executor.holdings.held_location() == "qatar"


def test_executor_entry_skipped_for_non_entry_target(tmp_path) -> None:
    config = _flat_config()
    adapter = DryRunTradingAdapter(yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:pakistan", target_outcome="pakistan", factors=_signal(confirmed_location="pakistan"))
    result = executor.execute(decision, article("Officials confirm the round will be held in Pakistan."))
    assert result == "SKIPPED"
    assert executor.holdings.held_location() is None


def test_executor_entry_respects_max_entries(tmp_path) -> None:
    config = _flat_config()
    adapter = DryRunTradingAdapter(yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    assert executor.execute(decision, article("Officials confirm the round will be held in Qatar.")) == "ENTERED"
    # Simulate a manual flat reset without resetting the entry budget.
    executor.holdings.clear_held(source="exit")
    result = executor.execute(decision, article("Officials re-confirm the round will be held in Qatar."))
    assert result == "SKIPPED"
    assert executor.holdings.held_location() is None


def test_entry_then_defend_rotates_the_entered_leg(tmp_path) -> None:
    config = _flat_config()
    adapter = DryRunTradingAdapter(yes_shares=250.0, yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    enter = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    assert executor.execute(enter, article("Officials confirm the round will be held in Qatar.")) == "ENTERED"

    # The venue shifts: with the live holding now qatar, the same protection
    # machinery must sell the ENTERED leg and rotate.
    signal = _signal(confirmed_location="pakistan", evidence_strength="confirmed_started", source_tier="wire")
    decision = final_decision(config, signal, held=executor.holdings.held_location())
    assert decision.action == "ROTATE_YES"
    result = executor.execute(decision, article("Officials confirm the round begins in Pakistan."))
    assert result == "ROTATED"
    current = executor.store.current()
    assert current is not None
    assert current.payload["from_outcome"] == "qatar"
    assert current.payload["to_outcome"] == "pakistan"
    assert executor.holdings.held_location() == "pakistan"
    assert executor.holdings.record().source == "rotation"


def test_rotated_position_remains_defendable_and_can_exit(tmp_path) -> None:
    config = _flat_config()
    adapter = DryRunTradingAdapter(yes_shares=250.0, yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    enter = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    assert executor.execute(enter, article("Officials confirm the round will be held in Qatar.")) == "ENTERED"
    rotate = LocationDecision("ROTATE_YES", "4B", "confirmed_location:pakistan", target_outcome="pakistan", factors=_signal(confirmed_location="pakistan"))
    assert executor.execute(rotate, article("The round moves to Pakistan.")) == "ROTATED"
    assert executor.holdings.held_location() == "pakistan"
    assert executor.protection_execution_count() == 1
    exit_decision = LocationDecision("EXIT_YES_ONLY", "4B", "confirmed_non_held_location_not_rotated:russia", factors=_signal(confirmed_location="russia"))
    assert executor.execute(exit_decision, article("The qualifying round begins in Russia.")) == "EXITED"
    assert executor.holdings.held_location() is None
    assert executor.protection_execution_count() == 2


def test_wallet_reconciliation_adopts_single_live_outcome(tmp_path) -> None:
    class _WalletAdapter(DryRunTradingAdapter):
        def query_live_position(self, yes_token_id, no_token_id):
            shares = 12.0 if yes_token_id == "pk-yes" else 0.0
            return LivePosition(yes_token_id=yes_token_id, no_token_id=no_token_id, no_shares=0.0, yes_shares=shares)

    config = _flat_config(execution=ExecutionConfig(dry_run=False, sell=SellConfig(), buy_rotation=BuyRotationConfig()))
    executor = _executor(tmp_path, config, _WalletAdapter())
    result = executor.reconcile_live_holding()
    assert result["held_location"] == "pakistan"
    assert result["changed"] is True
    assert executor.holdings.held_location() == "pakistan"


def test_wallet_reconciliation_fails_closed_on_multiple_outcomes(tmp_path) -> None:
    class _WalletAdapter(DryRunTradingAdapter):
        def query_live_position(self, yes_token_id, no_token_id):
            shares = 12.0 if yes_token_id in {"qatar-yes", "pk-yes"} else 0.0
            return LivePosition(yes_token_id=yes_token_id, no_token_id=no_token_id, no_shares=0.0, yes_shares=shares)

    config = _flat_config(execution=ExecutionConfig(dry_run=False, sell=SellConfig(), buy_rotation=BuyRotationConfig()))
    executor = _executor(tmp_path, config, _WalletAdapter())
    with pytest.raises(ReconciliationError, match="multiple live location YES balances"):
        executor.reconcile_live_holding()


def test_wallet_reconciliation_fails_closed_on_unexpected_no_exposure(tmp_path) -> None:
    class _WalletAdapter(DryRunTradingAdapter):
        def query_live_position(self, yes_token_id, no_token_id):
            no_shares = 3.0 if no_token_id == "qatar-no" else 0.0
            return LivePosition(yes_token_id=yes_token_id, no_token_id=no_token_id, no_shares=no_shares, yes_shares=0.0)

    config = _flat_config(execution=ExecutionConfig(dry_run=False, sell=SellConfig(), buy_rotation=BuyRotationConfig()))
    executor = _executor(tmp_path, config, _WalletAdapter())
    with pytest.raises(ReconciliationError, match="unexpected location NO balances"):
        executor.reconcile_live_holding()


def test_wallet_reconciliation_fails_closed_on_resting_orders(tmp_path) -> None:
    class _WalletAdapter(DryRunTradingAdapter):
        def query_live_position(self, yes_token_id, no_token_id):
            return LivePosition(yes_token_id=yes_token_id, no_token_id=no_token_id, no_shares=0.0, yes_shares=0.0)

        def open_orders_for_market(self, condition_id):
            return [{"id": "resting"}] if condition_id == "0xqatar" else []

    config = _flat_config(execution=ExecutionConfig(dry_run=False, sell=SellConfig(), buy_rotation=BuyRotationConfig()))
    executor = _executor(tmp_path, config, _WalletAdapter())
    with pytest.raises(ReconciliationError, match="unexpected resting orders"):
        executor.reconcile_live_holding()


def test_entry_budget_is_clamped_by_global_per_order_guardrail(tmp_path) -> None:
    config = _flat_config(entry=EntryConfig(enabled=True, targets=["qatar"], usd_budget=100.0))
    executor = _executor(tmp_path, config, DryRunTradingAdapter(yes_ask=0.40))
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    assert executor.execute(decision, article("Officials confirm the round will be held in Qatar.")) == "ENTERED"
    current = executor.store.current()
    assert current is not None
    assert current.payload["usd_budget"] == 25.0


def test_corrupt_entry_counter_fails_closed(tmp_path) -> None:
    executor = _executor(tmp_path, _flat_config(), DryRunTradingAdapter())
    path = executor._entry_count_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt entry counter"):
        executor.entry_count()


def test_process_lock_rejects_second_live_process(tmp_path) -> None:
    first = ProcessLock(tmp_path / "location.lock").acquire()
    try:
        with pytest.raises(ProcessLockError, match="already running"):
            ProcessLock(tmp_path / "location.lock").acquire()
    finally:
        first.release()


def test_promoted_feed_summary_cannot_open_position(tmp_path) -> None:
    config = _flat_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(
            allow_feed_auto_trade=True,
            auto_trade_domains=["reuters.com"],
            max_trade_article_age_hours=0.0,
        ),
    )
    bot = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter())
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    summary = Article(**{**article("A new round will begin in Doha.").__dict__, "source_kind": "promoted_feed_summary"})
    blocked = bot._enforce_source_policy(summary, decision)
    assert blocked.action == "ALERT_ONLY"
    assert blocked.reason == "entry_requires_full_source_text"


def test_exit_clears_holding_and_one_shot_blocks_reentry(tmp_path) -> None:
    config = _flat_config()
    adapter = DryRunTradingAdapter(yes_shares=250.0, yes_ask=0.40)
    executor = _executor(tmp_path, config, adapter)
    enter = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    assert executor.execute(enter, article("Officials confirm the round will be held in Qatar.")) == "ENTERED"

    exit_decision = LocationDecision(
        "EXIT_YES_ONLY", "4B", "confirmed_non_held_location_not_rotated:russia", factors=_signal(confirmed_location="russia")
    )
    assert executor.execute(exit_decision, article("Officials confirm the round begins in Russia.")) == "EXITED"
    assert executor.holdings.held_location() is None
    assert executor.holdings.record().source == "exit"

    # one_shot: the terminal EXITED state blocks a fresh entry.
    result = executor.execute(enter, article("Officials now confirm the round will be held in Qatar after all."))
    assert result == "EXITED"
    assert executor.holdings.held_location() is None


# ---- operator gate ----


def test_operator_gate_blocks_enter_yes_by_default(tmp_path) -> None:
    config = _flat_config(data_dir=tmp_path / "data")
    config_path = tmp_path / "entry.yaml"
    config_path.write_text("entry-config\n", encoding="utf-8")
    gate = OperatorGate(config_path, config)
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar")
    result = gate.check(decision, live_requested=True)
    assert not result.allowed


# ---- runner flow ----


def test_bot_enters_then_reinforces_held_location(tmp_path) -> None:
    config = _flat_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(max_trade_article_age_hours=0.0),
    )
    bot = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_ask=0.40))
    first = bot.process_article(article("US and Iran senior negotiators scheduled talks: the round will be held in Qatar."))
    assert first.action == "ENTER_YES"
    assert bot.holdings.held_location() == "qatar"

    # Same news again: now the bot is holding, so the identical confirmation
    # is routed through the protection table and reinforces the held leg.
    second = bot.process_article(article("Senior negotiators re-confirm the talks round will be held in Qatar.", title="second"))
    assert second.action == "NO_ACTION"
    assert second.reason == "held_location_reinforced"


def test_bot_max_entries_reached_is_alert_only(tmp_path) -> None:
    config = _flat_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(max_trade_article_age_hours=0.0),
    )
    bot = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_ask=0.40))
    first = bot.process_article(article("US and Iran senior negotiators scheduled talks: the round will be held in Qatar."))
    assert first.action == "ENTER_YES"
    # Force flat again without resetting the entry budget.
    bot.holdings.clear_held(source="exit")
    second = bot.process_article(article("Fresh reports say the talks round will be held in Oman.", title="oman"))
    assert second.action == "ALERT_ONLY"
    assert second.reason == "max_entries_reached:1"


# ---- config loading/validation ----


def _yaml_config(tmp_path: Path, *, held_location: str, entry_block: str) -> Path:
    path = tmp_path / "config.yaml"
    base = textwrap.dedent(
        f"""
        event:
          slug: "test-slug"
          question: "Where will the meeting be?"
          deadline_date: "2026-09-30"
          held_location: "{held_location}"
          resolution_rules: "Resolves to the venue of the qualifying meeting."
        outcomes:
          - name: qatar
            label: "Qatar"
            condition_id: "0xqatar"
            yes_token_id: "qatar-yes"
            no_token_id: "qatar-no"
            rotation_target: true
          - name: oman
            label: "Oman"
            condition_id: "0xom"
            yes_token_id: "om-yes"
            no_token_id: "om-no"
        """
    )
    path.write_text(base + entry_block, encoding="utf-8")
    return path


def test_load_config_parses_entry_section(tmp_path) -> None:
    path = _yaml_config(
        tmp_path,
        held_location="",
        entry_block=textwrap.dedent(
            """
            entry:
              enabled: true
              targets: ["Qatar", "oman"]
              usd_budget: 75.0
              max_price: 0.85
              max_entries: 2
            """
        ),
    )
    config = load_location_config(path)
    assert config.entry.enabled is True
    assert config.entry_target_names() == {"qatar", "oman"}
    assert config.entry.usd_budget == 75.0
    assert config.entry.max_price == 0.85
    assert config.entry.max_entries == 2


def test_load_config_rejects_unknown_entry_target(tmp_path) -> None:
    path = _yaml_config(
        tmp_path,
        held_location="",
        entry_block=textwrap.dedent(
            """
            entry:
              enabled: true
              targets: ["atlantis"]
            """
        ),
    )
    with pytest.raises(ValueError, match="entry.targets not found in outcomes"):
        load_location_config(path)


def test_load_config_rejects_flat_without_entry(tmp_path) -> None:
    path = _yaml_config(tmp_path, held_location="", entry_block="")
    with pytest.raises(ValueError, match="nothing to protect or enter"):
        load_location_config(path)


def test_load_config_rejects_entry_without_targets(tmp_path) -> None:
    path = _yaml_config(
        tmp_path,
        held_location="",
        entry_block=textwrap.dedent(
            """
            entry:
              enabled: true
            """
        ),
    )
    with pytest.raises(ValueError, match="at least one entry.targets"):
        load_location_config(path)


def test_load_config_rejects_held_location_in_entry_targets(tmp_path) -> None:
    path = _yaml_config(
        tmp_path,
        held_location="qatar",
        entry_block=textwrap.dedent(
            """
            entry:
              enabled: true
              targets: ["qatar"]
            """
        ),
    )
    with pytest.raises(ValueError, match="must not be listed in entry.targets"):
        load_location_config(path)


@pytest.mark.parametrize(
    ("entry_block", "message"),
    [
        ("entry:\n  enabled: true\n  targets: [qatar]\n  usd_budget: 0\n", "usd_budget must be positive"),
        ("entry:\n  enabled: true\n  targets: [qatar]\n  max_price: 1.2\n", "max_price must be in"),
        ("entry:\n  enabled: true\n  targets: [qatar]\n  min_fill_fraction: 1.2\n", "min_fill_fraction must be in"),
        ("entry:\n  enabled: true\n  targets: [qatar]\n  max_entries: 0\n", "max_entries must be at least 1"),
    ],
)
def test_load_config_rejects_unsafe_entry_numbers(tmp_path, entry_block, message) -> None:
    path = _yaml_config(tmp_path, held_location="", entry_block=entry_block)
    with pytest.raises(ValueError, match=message):
        load_location_config(path)


# ---- shared portfolio ledger ----


def test_entry_clamped_and_debited_by_portfolio_ledger(tmp_path) -> None:
    from polybot.core.portfolio import AllocatorConfig, PortfolioAllocator, PortfolioConfig

    ledger = tmp_path / "allocations.json"
    # Tighter than both entry.usd_budget (100) and the global per-order
    # guardrail (25), so the ledger is the binding constraint -- while staying
    # above the small-fill thresholds that would classify PARTIALLY_ENTERED.
    PortfolioAllocator(ledger, AllocatorConfig(per_order_usd=15.0)).write_caps()
    config = _flat_config(
        portfolio=PortfolioConfig(
            ledger_path=str(ledger),
            market_id="us-iran-talks-location",
            event_slug="us-iran-talks-location",
            correlation_group="iran|united_states",
            deadline_iso="2026-09-30T23:59:00Z",
        ),
    )
    executor = _executor(tmp_path, config, DryRunTradingAdapter(yes_ask=0.40, yes_bid=0.38))
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    result = executor.execute(decision, article("Officials confirm the round will be held in Qatar."))
    assert result == "ENTERED"
    snapshot = PortfolioAllocator.from_ledger(ledger).snapshot()
    assert snapshot["per_market"]["us-iran-talks-location"] == 15.0
    assert snapshot["open_positions"] == ["us-iran-talks-location"]

    exit_decision = LocationDecision("EXIT_YES_ONLY", "4B", "no_meeting_confirmed", factors=_signal(confirmed_location="no_meeting"))
    assert executor.execute(exit_decision, article("No qualifying round will occur before the deadline.")) == "EXITED"
    snapshot = PortfolioAllocator.from_ledger(ledger).snapshot()
    assert snapshot["open_positions"] == []


def test_entry_blocked_when_portfolio_exhausted(tmp_path) -> None:
    from polybot.core.portfolio import AllocationRequest, AllocatorConfig, PortfolioAllocator, PortfolioConfig

    ledger = tmp_path / "allocations.json"
    allocator = PortfolioAllocator(ledger, AllocatorConfig(total_usd=50.0))
    allocator.write_caps()
    allocator.commit(AllocationRequest(market_id="other", event_slug="other", correlation_group="g", deadline_iso="2026-09-30T23:59:00Z", usd=50.0))
    config = _flat_config(
        portfolio=PortfolioConfig(
            ledger_path=str(ledger),
            market_id="us-iran-talks-location",
            deadline_iso="2026-09-30T23:59:00Z",
        ),
    )
    executor = _executor(tmp_path, config, DryRunTradingAdapter(yes_ask=0.40, yes_bid=0.38))
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_signal())
    result = executor.execute(decision, article("Officials confirm the round will be held in Qatar."))
    assert result == "ENTRY_PORTFOLIO_BLOCKED"
    assert executor.holdings.held_location() is None


# ---- screen tier ----


def test_location_screen_no_action_skips_strong_model(tmp_path) -> None:
    config = _flat_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(max_trade_article_age_hours=0.0),
    )
    bot = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_ask=0.40))

    class _Counting:
        def __init__(self, signal):
            self.signal = signal
            self.calls = 0

        def classify(self, article_arg, market_rule_text, held_location=None):
            self.calls += 1
            return self.signal

    screen = _Counting(_signal(confirmed_location="none", qualifies_as_senior_round=False, round_status="unclear", evidence_strength="speculative"))
    strong = _Counting(_signal())
    bot.screen_classifier = screen
    bot.classifier = strong
    decision = bot.process_article(article("Background chatter about Qatar talks continues with no concrete developments."))
    assert decision.action == "NO_ACTION"
    assert decision.reason.startswith("screen:")
    assert screen.calls == 1
    assert strong.calls == 0


def test_location_screen_trade_signal_escalates(tmp_path) -> None:
    config = _flat_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(max_trade_article_age_hours=0.0),
    )
    bot = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_ask=0.40, yes_bid=0.38))

    class _Counting:
        def __init__(self, signal):
            self.signal = signal
            self.calls = 0

        def classify(self, article_arg, market_rule_text, held_location=None):
            self.calls += 1
            return self.signal

    quote = "A new round will begin in Doha."
    screen = _Counting(_signal(confirmed_location="qatar", quote_supporting_trigger=quote))
    strong = _Counting(_signal(confirmed_location="qatar", quote_supporting_trigger=quote))
    bot.screen_classifier = screen
    bot.classifier = strong
    decision = bot.process_article(article(f"{quote} Officials confirmed the talks round."))
    assert screen.calls == 1
    assert strong.calls >= 1
    assert decision.action == "ENTER_YES"
