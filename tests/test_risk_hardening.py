from __future__ import annotations

import json
import multiprocessing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_binary_bot import _bot as _binary_bot, _config as _binary_config, _executor as _binary_executor, _signal as _binary_signal, _verification, article
from test_location_entry import _executor as _location_executor, _flat_config, _signal as _location_signal

from polybot.core.confirmations import CorroborationTracker, SecondSourceGate
from polybot.core.config import SourcesConfig
from polybot.core.execution import DryRunTradingAdapter, LivePosition
from polybot.core.portfolio import AllocationRequest, AllocatorConfig, PortfolioAllocator
from polybot.core.runtime import ReconciliationError
from polybot.binary.config import EntryConfig as BinaryEntryConfig, ExecutionConfig, FlipBuyConfig, MarketConfig, SellConfig, TimeDecayConfig
from polybot.binary.decision import BinaryDecision
from polybot.location.config import EntryConfig as LocationEntryConfig
from polybot.location.decision import LocationDecision


def _concurrent_allocator_commit(ledger: str, market_id: str, barrier, results) -> None:
    allocator = PortfolioAllocator.from_ledger(Path(ledger))
    barrier.wait()
    request = AllocationRequest(
        market_id=market_id,
        event_slug=market_id,
        correlation_group=market_id,
        deadline_iso="2026-09-30T00:00:00Z",
        usd=1.0,
        region=market_id,
    )
    try:
        allocator.commit(request)
        results.put(True)
    except ValueError:
        results.put(False)


# ---- second-source entry gate (P2.14) ----


def test_binary_large_entry_waits_for_second_source(tmp_path) -> None:
    config = _binary_config(
        entry=BinaryEntryConfig(enabled=True, side="YES", usd_budget=100.0, max_entries=2, second_source_above_usd=50.0)
    )
    executor = _binary_executor(tmp_path, config, DryRunTradingAdapter(yes_ask=0.40))
    decision = BinaryDecision("ENTER_YES", "4B", "qualifying_event_confirmed:scheduled", _binary_signal())

    first = executor.execute(decision, article("The round will be held next week.", domain="reuters.com"))
    assert first == "ENTRY_AWAITING_SECOND_SOURCE"
    assert executor.holdings.held_location() is None

    # The same outlet repeating itself is NOT independent confirmation.
    repeat = executor.execute(decision, article("Reuters again: round next week.", domain="reuters.com"))
    assert repeat == "ENTRY_AWAITING_SECOND_SOURCE"
    assert executor.holdings.held_location() is None

    second = executor.execute(decision, article("AP confirms the round next week.", domain="apnews.com"))
    assert second == "ENTERED"
    assert executor.holdings.held_location() == "yes"


def test_binary_small_entry_skips_second_source_gate(tmp_path) -> None:
    config = _binary_config(
        entry=BinaryEntryConfig(enabled=True, side="YES", usd_budget=20.0, second_source_above_usd=50.0)
    )
    executor = _binary_executor(tmp_path, config, DryRunTradingAdapter(yes_ask=0.40))
    decision = BinaryDecision("ENTER_YES", "4B", "qualifying_event_confirmed:scheduled", _binary_signal())
    assert executor.execute(decision, article("The round will be held next week.")) == "ENTERED"


def test_location_large_entry_waits_for_second_source(tmp_path) -> None:
    config = _flat_config(
        entry=LocationEntryConfig(enabled=True, targets=["qatar"], usd_budget=100.0, max_entries=2, second_source_above_usd=10.0)
    )
    executor = _location_executor(tmp_path, config, DryRunTradingAdapter(yes_ask=0.40))
    decision = LocationDecision("ENTER_YES", "4B", "confirmed_location:qatar", target_outcome="qatar", factors=_location_signal())

    first = executor.execute(decision, article("Officials confirm the round will be held in Qatar.", domain="reuters.com"))
    assert first == "ENTRY_AWAITING_SECOND_SOURCE"
    assert executor.holdings.held_location() is None

    second = executor.execute(decision, article("AP: round to be held in Qatar.", domain="apnews.com"))
    assert second == "ENTERED"
    assert executor.holdings.held_location() == "qatar"


def test_second_source_gate_window_expires(tmp_path) -> None:
    gate = SecondSourceGate(tmp_path, window_minutes=60.0)
    assert gate.confirm("yes", "reuters.com") is False
    # Age the recorded confirmation past the freshness window.
    records = json.loads((tmp_path / "entry_confirmations.json").read_text(encoding="utf-8"))
    records["yes"]["at"] = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
    (tmp_path / "entry_confirmations.json").write_text(json.dumps(records), encoding="utf-8")
    # A stale first source cannot be confirmed; the new trigger becomes the
    # pending first confirmation again.
    assert gate.confirm("yes", "apnews.com") is False
    assert gate.confirm("yes", "reuters.com") is True


def test_second_source_gate_rejects_reuters_syndication_mirror(tmp_path) -> None:
    gate = SecondSourceGate(tmp_path, window_minutes=60.0)
    assert gate.confirm(
        "yes",
        "reuters.com",
        origin_organization="Reuters",
    ) is False
    assert gate.confirm(
        "yes",
        "finance.yahoo.com",
        origin_organization="Reuters",
    ) is False
    assert gate.confirm("yes", "apnews.com") is True


# ---- post-entry corroboration (P2.11) ----


def _corroboration_bot(tmp_path, *, action: str = "alert", adapter: DryRunTradingAdapter | None = None):
    config = _binary_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(max_trade_article_age_hours=0.0),
        entry=BinaryEntryConfig(
            enabled=True, side="YES", usd_budget=100.0, max_entries=1,
            corroboration_minutes=30.0, corroboration_action=action,
        ),
    )
    bot = _binary_bot(tmp_path, config, adapter or DryRunTradingAdapter(yes_shares=250.0, yes_ask=0.40))
    notified: list[tuple[str, dict]] = []

    class _Notifier:
        def notify(self, message, **fields):
            notified.append((message, fields))

    bot.notifier = _Notifier()
    bot.executor.notifier = bot.notifier
    return bot, notified


def test_corroboration_armed_on_entry_and_satisfied_by_second_domain(tmp_path) -> None:
    bot, _notified = _corroboration_bot(tmp_path)
    entered = bot.process_article(article("US and Iran senior talks scheduled: the round will be held in Doha next week."))
    assert entered.action == "ENTER_YES"
    tracker = CorroborationTracker(bot.store.data_dir)
    assert tracker.pending() is not None

    # Same-domain reinforcement does NOT satisfy corroboration.
    bot.process_article(article("Reuters follow-up: the round is still scheduled for Doha next week.", title="followup"))
    assert tracker.pending() is not None

    # Different-domain reinforcement satisfies it.
    reinforced = bot.process_article(article("AP: the senior round is scheduled in Doha next week.", domain="apnews.com"))
    assert reinforced.reason in {"held_yes_thesis_reinforced", "held_no_thesis_reinforced"}
    assert tracker.pending() is None
    raw = json.loads((bot.store.data_dir / "corroboration.json").read_text(encoding="utf-8"))
    assert raw["satisfied"] is True and raw["satisfied_by"] == "apnews.com"


def test_corroboration_rejects_same_origin_across_domains(tmp_path) -> None:
    tracker = CorroborationTracker(tmp_path)
    tracker.start(
        entry_domain="reuters.com",
        minutes=30.0,
        action="alert",
        entry_origin_organization="Reuters",
    )
    assert tracker.satisfy(
        "aol.com",
        byline="Reporting by Reuters",
    ) is False
    assert tracker.satisfy("apnews.com") is True


def test_corroboration_overdue_alerts_once(tmp_path) -> None:
    bot, notified = _corroboration_bot(tmp_path)
    entered = bot.process_article(article("US and Iran senior talks scheduled: the round will be held in Doha next week."))
    assert entered.action == "ENTER_YES"
    tracker = CorroborationTracker(bot.store.data_dir)
    record = json.loads(tracker.path.read_text(encoding="utf-8"))
    record["deadline"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    tracker.path.write_text(json.dumps(record), encoding="utf-8")

    bot._check_corroboration_deadline()
    alerts = [m for m, _f in notified if "NOT corroborated" in m]
    assert len(alerts) == 1
    assert bot.holdings.held_location() == "yes"  # alert action never trades

    # Escalation is once: a second cycle stays silent.
    bot._check_corroboration_deadline()
    assert len([m for m, _f in notified if "NOT corroborated" in m]) == 1


def test_corroboration_overdue_trim_action_trims_position(tmp_path) -> None:
    bot, notified = _corroboration_bot(tmp_path, action="trim")
    entered = bot.process_article(article("US and Iran senior talks scheduled: the round will be held in Doha next week."))
    assert entered.action == "ENTER_YES"
    tracker = CorroborationTracker(bot.store.data_dir)
    record = json.loads(tracker.path.read_text(encoding="utf-8"))
    record["deadline"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    tracker.path.write_text(json.dumps(record), encoding="utf-8")

    bot._check_corroboration_deadline()
    current = bot.store.current()
    assert current is not None and current.state == "TRIMMED"
    assert bot.holdings.held_location() == "yes"  # trim keeps the position
    assert [m for m, _f in notified if "NOT corroborated" in m]
    assert tracker.pending() is None  # escalated; no repeat next cycle


# ---- staged exits (P2.12) ----


class _CountingAdapter(DryRunTradingAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sell_calls: list[float] = []

    def sell_yes_fak(self, yes_token_id: str, shares: float, min_price: float):
        self.sell_calls.append(shares)
        return super().sell_yes_fak(yes_token_id, shares, min_price)

    def sell_no_fak(self, no_token_id: str, shares: float, min_price: float):
        self.sell_calls.append(shares)
        return super().sell_no_fak(no_token_id, shares, min_price)


def test_binary_staged_exit_splits_into_two_orders(tmp_path) -> None:
    config = _binary_config(
        market=MarketConfig(slug="test-slug", deadline_date="2026-09-30", held_side="YES", resolution_rules="test rules"),
        execution=ExecutionConfig(
            dry_run=True,
            sell=SellConfig(max_fraction_per_order=0.5, retry_delay_seconds=0.0),
            flip_buy=FlipBuyConfig(),
        ),
    )
    adapter = _CountingAdapter(yes_shares=400.0)
    executor = _binary_executor(tmp_path, config, adapter)
    decision = BinaryDecision("EXIT_HELD", "4B", "yes_foreclosure_confirmed", _binary_signal(resolves_no=True))
    result = executor.execute(decision, article("Talks cancelled, will not happen."))
    assert result == "EXITED"
    assert executor.holdings.held_location() is None
    # Two staged orders: half first, remainder after the requote delay.
    assert adapter.sell_calls == pytest.approx([200.0, 200.0])


def test_binary_full_fraction_exit_is_single_order(tmp_path) -> None:
    config = _binary_config(
        market=MarketConfig(slug="test-slug", deadline_date="2026-09-30", held_side="YES", resolution_rules="test rules"),
    )
    adapter = _CountingAdapter(yes_shares=400.0)
    executor = _binary_executor(tmp_path, config, adapter)
    decision = BinaryDecision("EXIT_HELD", "4B", "yes_foreclosure_confirmed", _binary_signal(resolves_no=True))
    assert executor.execute(decision, article("Talks cancelled, will not happen.")) == "EXITED"
    assert adapter.sell_calls == pytest.approx([400.0])


# ---- region correlation caps (P2.13) ----


def test_allocator_region_cap_blocks_same_theater_pileup(tmp_path) -> None:
    allocator = PortfolioAllocator(
        tmp_path / "ledger.json",
        AllocatorConfig(per_order_usd=100.0, per_market_usd=100.0, per_event_usd=1000.0, per_group_usd=1000.0, per_region_usd=150.0),
    )

    def request(market: str, group: str, region: str) -> AllocationRequest:
        return AllocationRequest(
            market_id=market, event_slug=market, correlation_group=group,
            deadline_iso="2026-09-30T00:00:00Z", usd=100.0, region=region,
        )

    allocator.commit(request("m1", "g1", "middle_east"))
    granted, blockers = allocator.preview(request("m2", "g2", "middle_east"))
    # Different market, event, AND correlation group -- only the shared
    # region budget binds.
    assert granted == pytest.approx(50.0)
    allocator.commit(AllocationRequest(market_id="m2", event_slug="m2", correlation_group="g2", deadline_iso="2026-09-30T00:00:00Z", usd=50.0, region="middle_east"))
    _granted, blockers = allocator.preview(request("m3", "g3", "middle_east"))
    assert "region_limit" in blockers
    # A different theater is unaffected.
    granted, blockers = allocator.preview(request("m4", "g4", "east_asia"))
    assert granted == pytest.approx(100.0) and not blockers


def test_allocator_concurrent_commits_cannot_overspend_or_lose_updates(tmp_path) -> None:
    ledger = tmp_path / "ledger.json"
    allocator = PortfolioAllocator(
        ledger,
        AllocatorConfig(
            per_order_usd=1.0,
            per_market_usd=1.0,
            per_event_usd=1.0,
            per_group_usd=100.0,
            per_region_usd=100.0,
            daily_usd=100.0,
            total_usd=10.0,
            max_open_positions=100,
            max_per_deadline_week_usd=100.0,
        ),
    )
    allocator.write_caps()
    context = multiprocessing.get_context("fork")
    worker_count = 20
    barrier = context.Barrier(worker_count)
    results = context.Queue()
    workers = [
        context.Process(
            target=_concurrent_allocator_commit,
            args=(str(ledger), f"market-{index}", barrier, results),
        )
        for index in range(worker_count)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0

    successes = sum(bool(results.get(timeout=1)) for _ in workers)
    snapshot = allocator.snapshot()
    assert successes == 10
    assert snapshot["total"] == pytest.approx(10.0)
    assert len(snapshot["open_positions"]) == 10


def test_allocator_partial_fill_corrects_basis_but_retains_attempt_caps(tmp_path) -> None:
    allocator = PortfolioAllocator(
        tmp_path / "ledger.json",
        AllocatorConfig(per_order_usd=100.0, per_market_usd=100.0),
    )
    request = AllocationRequest(
        market_id="thin-book",
        event_slug="event",
        correlation_group="group",
        deadline_iso="",
        usd=100.0,
    )
    allocator.commit(request)

    allocator.reconcile_entry_basis("thin-book", reserved_usd=100.0, actual_cost_usd=13.0)
    snapshot = allocator.snapshot()

    assert snapshot["cost_basis"]["thin-book"] == pytest.approx(13.0)
    assert snapshot["per_market"]["thin-book"] == pytest.approx(100.0)
    assert snapshot["total"] == pytest.approx(100.0)
    assert allocator.settle("thin-book", 15.0) == pytest.approx(2.0)


def test_region_of_weights_global_actors_last() -> None:
    from polybot.discovery.registry import region_of

    assert region_of(["united_states", "iran"]) == "middle_east"
    assert region_of(["russia", "ukraine", "united_states"]) == "eastern_europe"
    assert region_of(["united_states"]) == "north_america"
    assert region_of([]) == "global"


# ---- ledger reconcile + drawdown (P0/P1) ----


def test_allocator_reconcile_prunes_closed_positions(tmp_path) -> None:
    allocator = PortfolioAllocator(tmp_path / "ledger.json", AllocatorConfig(per_order_usd=100.0, per_market_usd=100.0))
    allocator.commit(AllocationRequest(market_id="open-m", event_slug="e1", correlation_group="g", deadline_iso="", usd=50.0))
    allocator.commit(AllocationRequest(market_id="closed-m", event_slug="e2", correlation_group="g", deadline_iso="", usd=50.0))

    allocator.reconcile(is_open=lambda market_id: market_id == "open-m")
    state = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    # The closed market's position slot and cost basis are freed; the
    # per-market spend bucket is cumulative by design and stays.
    assert state["open_positions"] == ["open-m"]
    assert "closed-m" not in state.get("cost_basis", {})
    assert "open-m" in state.get("cost_basis", {})


def test_settle_tracks_realized_losses_for_drawdown(tmp_path) -> None:
    allocator = PortfolioAllocator(tmp_path / "ledger.json", AllocatorConfig(per_order_usd=100.0, per_market_usd=100.0, max_drawdown_usd=60.0))
    allocator.commit(AllocationRequest(market_id="m1", event_slug="e1", correlation_group="g", deadline_iso="", usd=80.0))
    realized = allocator.settle("m1", 10.0)  # bought 80, sold for 10
    assert realized == pytest.approx(-70.0)
    assert allocator.realized_net() == pytest.approx(-70.0)


def test_fleet_drawdown_halt_flips_global_mode(tmp_path, monkeypatch) -> None:
    from polybot.discovery.config import DiscoveryConfig, FleetConfig
    from polybot.discovery.fleet import FleetManager
    from polybot.discovery.store import DiscoveryStore

    geo = tmp_path / "geo"
    monkeypatch.setattr("polybot.discovery.emit.GEO_DATA_ROOT", str(geo))
    monkeypatch.setattr("polybot.discovery.fleet.GEO_DATA_ROOT", str(geo))

    ledger = tmp_path / "ledger.json"
    allocator = PortfolioAllocator(ledger, AllocatorConfig(per_order_usd=100.0, per_market_usd=100.0, max_drawdown_usd=60.0))
    allocator.write_caps()
    allocator.commit(AllocationRequest(market_id="m1", event_slug="e1", correlation_group="g", deadline_iso="", usd=80.0))
    allocator.settle("m1", 10.0)

    class _Notifier:
        def __init__(self):
            self.messages = []

        def notify(self, message, **fields):
            self.messages.append(message)

    config = DiscoveryConfig(fleet=FleetConfig(enabled=True), data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")
    notifier = _Notifier()
    manager = FleetManager(config, DiscoveryStore(config.data_dir), live=True, per_order_usd=50.0, ledger_path=str(ledger), notifier=notifier)
    manager._drawdown_check()

    mode = json.loads((geo / "operator" / "global_mode.json").read_text(encoding="utf-8"))
    assert mode["mode"] == "alert_only" and mode["reason"] == "max_drawdown"
    assert any("HALTED" in m for m in notifier.messages)


# ---- fleet hang detection + restart backoff (P1) ----


def test_fleet_terminates_hung_bot_on_stale_heartbeat(tmp_path, monkeypatch) -> None:
    from polybot.discovery.config import DiscoveryConfig, FleetConfig
    from polybot.discovery.fleet import FleetManager
    from polybot.discovery.store import DiscoveryStore
    from polybot.discovery.types import market_dir_slug

    geo = tmp_path / "geo"
    monkeypatch.setattr("polybot.discovery.fleet.GEO_DATA_ROOT", str(geo))
    config = DiscoveryConfig(fleet=FleetConfig(enabled=True, heartbeat_stale_seconds=300.0), data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")
    manager = FleetManager(config, DiscoveryStore(config.data_dir), live=False, per_order_usd=50.0, ledger_path=str(tmp_path / "ledger.json"))

    class _Process:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    hung, healthy = _Process(), _Process()
    manager.processes = {"hung-market": hung, "healthy-market": healthy}
    stale = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    for market_id, stamp in (("hung-market", stale), ("healthy-market", fresh)):
        path = geo / market_dir_slug(market_id) / "heartbeat.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": stamp}), encoding="utf-8")

    manager._terminate_hung_bots()
    assert hung.terminated is True
    assert healthy.terminated is False


def test_fleet_restart_backoff_limits_crash_loops(tmp_path) -> None:
    from polybot.discovery.config import DiscoveryConfig, FleetConfig
    from polybot.discovery.fleet import FleetManager
    from polybot.discovery.store import DiscoveryStore

    config = DiscoveryConfig(fleet=FleetConfig(enabled=True, max_restarts_per_hour=3), data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")
    manager = FleetManager(config, DiscoveryStore(config.data_dir), live=False, per_order_usd=50.0, ledger_path=str(tmp_path / "ledger.json"))
    for _ in range(3):
        assert manager._restart_allowed("m1") is True
        manager._record_restart("m1")
    assert manager._restart_allowed("m1") is False
    assert manager._restart_allowed("other-market") is True


# ---- domain backoff on throttling (P1) ----


def test_domain_backoff_blocks_after_429_and_grows(tmp_path) -> None:
    from polybot.core import source_fetcher as sf

    url = "https://throttled.example.com/feed.xml"
    try:
        sf._check_backoff(url)  # clean slate: no exception
        sf._register_throttle(url, 429)
        with pytest.raises(sf.DomainBackoff):
            sf._check_backoff(url)
        # Success clears the backoff.
        sf._clear_throttle(url)
        sf._check_backoff(url)
        # Non-throttle statuses never register.
        sf._register_throttle(url, 500)
        sf._check_backoff(url)
    finally:
        sf._clear_throttle(url)


# ---- binary wallet reconciliation (P0) ----


class _PositionAdapter(DryRunTradingAdapter):
    def __init__(self, *, yes=0.0, no=0.0, orders=None, **kwargs):
        super().__init__(**kwargs)
        self._yes = yes
        self._no = no
        self._orders = orders or []

    def query_live_position(self, yes_token_id: str, no_token_id: str) -> LivePosition:
        return LivePosition(yes_token_id=yes_token_id, no_token_id=no_token_id, yes_shares=self._yes, no_shares=self._no)

    def open_orders_for_market(self, condition_id: str):
        return self._orders


def test_binary_reconciliation_adopts_wallet_side(tmp_path) -> None:
    executor = _binary_executor(tmp_path, _binary_config(), _PositionAdapter(no=120.0))
    assert executor.holdings.held_location() is None
    result = executor.reconcile_live_holding()
    assert result["held_side"] == "no" and result["changed"] is True
    assert executor.holdings.held_location() == "no"


def test_binary_reconciliation_clears_phantom_local_holding(tmp_path) -> None:
    executor = _binary_executor(tmp_path, _binary_config(), _PositionAdapter())
    executor.holdings.set_held("yes", source="entry")
    result = executor.reconcile_live_holding()
    assert result["held_side"] is None and result["changed"] is True
    assert executor.holdings.held_location() is None


def test_binary_reconciliation_fails_closed_on_both_sides(tmp_path) -> None:
    executor = _binary_executor(tmp_path, _binary_config(), _PositionAdapter(yes=50.0, no=50.0))
    with pytest.raises(ReconciliationError):
        executor.reconcile_live_holding()


def test_binary_reconciliation_fails_closed_on_resting_orders(tmp_path) -> None:
    executor = _binary_executor(tmp_path, _binary_config(), _PositionAdapter(yes=50.0, orders=[{"id": "o1"}]))
    with pytest.raises(ReconciliationError):
        executor.reconcile_live_holding()


# ---- take-profit exits ----


def test_binary_take_profit_exits_at_target(tmp_path) -> None:
    from polybot.binary.config import PositionConfig as BinaryPositionConfig

    config = _binary_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        market=MarketConfig(slug="test-slug", deadline_date="2026-09-30", held_side="YES", resolution_rules="test rules"),
        position=BinaryPositionConfig(max_shares_to_sell=1000.0, max_flip_usd_to_buy=500.0, take_profit_price=0.90),
    )
    bot = _binary_bot(tmp_path, config, DryRunTradingAdapter(yes_shares=400.0, yes_bid=0.95))
    decision = bot._check_take_profit()
    assert decision is not None and decision.action == "EXIT_HELD"
    assert decision.reason.startswith("take_profit_target_reached")
    current = bot.store.current()
    assert current is not None and current.state == "EXITED"
    assert bot.holdings.held_location() is None
    # Terminal now; a second cycle stays quiet.
    assert bot._check_take_profit() is None


def test_binary_take_profit_holds_below_target(tmp_path) -> None:
    from polybot.binary.config import PositionConfig as BinaryPositionConfig

    config = _binary_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        market=MarketConfig(slug="test-slug", deadline_date="2026-09-30", held_side="YES", resolution_rules="test rules"),
        position=BinaryPositionConfig(take_profit_price=0.90),
    )
    bot = _binary_bot(tmp_path, config, DryRunTradingAdapter(yes_shares=400.0, yes_bid=0.85))
    assert bot._check_take_profit() is None
    assert bot.holdings.held_location() == "yes"


def test_binary_take_profit_on_held_no_uses_complement_bid(tmp_path) -> None:
    from polybot.binary.config import PositionConfig as BinaryPositionConfig

    config = _binary_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        market=MarketConfig(slug="test-slug", deadline_date="2026-09-30", held_side="NO", resolution_rules="test rules"),
        position=BinaryPositionConfig(max_shares_to_sell=1000.0, take_profit_price=0.90),
    )
    # NO bid = 1 - YES ask = 0.96 >= 0.90 target.
    bot = _binary_bot(tmp_path, config, DryRunTradingAdapter(no_shares=400.0, yes_ask=0.04))
    decision = bot._check_take_profit()
    assert decision is not None and decision.action == "EXIT_HELD"
    assert bot.holdings.held_location() is None


def test_location_take_profit_exits_at_target(tmp_path) -> None:
    from polybot.location.config import EventConfig, PositionConfig as LocationPositionConfig
    from polybot.location.runner import LocationProtectionBot

    config = _flat_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        event=EventConfig(
            slug="test-slug",
            question="Where will the next diplomatic US-Iran meeting be by September 30, 2026?",
            deadline_date="2026-09-30",
            held_location="qatar",
            resolution_rules="test rules",
        ),
        position=LocationPositionConfig(max_yes_shares_to_sell=1000.0, max_rotation_usd_to_buy=500.0, take_profit_price=0.90),
    )
    bot = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_shares=400.0, yes_bid=0.95))
    decision = bot._check_take_profit()
    assert decision is not None and decision.action == "EXIT_YES_ONLY"
    current = bot.store.current()
    assert current is not None and current.state == "EXITED"
    assert bot.holdings.held_location() is None


def test_location_take_profit_disabled_by_default(tmp_path) -> None:
    from polybot.location.config import EventConfig
    from polybot.location.runner import LocationProtectionBot

    config = _flat_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        event=EventConfig(
            slug="test-slug",
            question="Where will the next diplomatic US-Iran meeting be by September 30, 2026?",
            deadline_date="2026-09-30",
            held_location="qatar",
            resolution_rules="test rules",
        ),
    )
    bot = LocationProtectionBot(config=config, adapter=DryRunTradingAdapter(yes_shares=400.0, yes_bid=0.99))
    assert bot._check_take_profit() is None
    assert bot.holdings.held_location() == "qatar"


# ---- screen tier never gates defense (P0) ----


class _ExplodingScreen:
    def __init__(self):
        self.calls = 0

    def classify(self, article, context, held_side=""):
        self.calls += 1
        raise AssertionError("screen model must not run while holding")


def test_screen_tier_bypassed_while_holding(tmp_path) -> None:
    config = _binary_config(
        data_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        sources=SourcesConfig(max_trade_article_age_hours=0.0),
    )
    bot = _binary_bot(tmp_path, config, DryRunTradingAdapter(yes_shares=250.0, yes_ask=0.40))
    entered = bot.process_article(article("US and Iran senior talks scheduled: the round will be held in Doha next week."))
    assert entered.action == "ENTER_YES"

    screen = _ExplodingScreen()
    bot.screen_classifier = screen
    # A foreclosure while HOLDING must reach the strong model directly.
    exited = bot.process_article(article("Officials say the talks are cancelled and the round will not happen.", title="cancelled"))
    assert exited.action == "EXIT_HELD"
    assert screen.calls == 0
    assert bot.holdings.held_location() is None
