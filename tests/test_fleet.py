from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from test_discovery import _FakeQuotes, _analyzed_context, _binary_event, _grouped_event, _sports_event

from polybot.discovery.config import (
    CentralFeedConfig,
    ClassifierBudgetConfig,
    DiscoveryConfig,
    FleetConfig,
    ForwardRecorderConfig,
)
from polybot.discovery.fleet import (
    FleetManager,
    _forward_books_disabled_status,
    run_fleet_command,
    set_fleet_mode_command,
)
from polybot.discovery.scorer import grade_market
from polybot.discovery.config import ScoringConfig
from polybot.discovery.sources import build_source_plan
from polybot.discovery.store import DiscoveryStore
from polybot.discovery.profit_priority import (
    PriorityEvidence,
    _snapshot_hash_payload,
    score_profit_priority,
)
from polybot.rules.contracts import sha256_json


class _FakeProcess:
    def __init__(self):
        self.terminated = False
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = -15


class _Spawner:
    def __init__(self):
        self.spawned: list[tuple[list[str], Path]] = []
        self.processes: list[_FakeProcess] = []

    def __call__(self, command: list[str], log_path: Path) -> _FakeProcess:
        self.spawned.append((command, log_path))
        process = _FakeProcess()
        self.processes.append(process)
        return process


class _Notifier:
    def __init__(self):
        self.messages: list[tuple[str, dict]] = []

    def notify(self, message, **fields):
        self.messages.append((message, fields))


def _patch_roots(monkeypatch, tmp_path: Path) -> Path:
    geo = tmp_path / "geo"
    monkeypatch.setattr("polybot.discovery.emit.GEO_DATA_ROOT", str(geo))
    monkeypatch.setattr("polybot.discovery.fleet.GEO_DATA_ROOT", str(geo))
    return geo


def _fleet_yaml(tmp_path: Path, *, position_mode: str = "alert_only", auto_ack: bool = False) -> Path:
    path = tmp_path / "discovery.yaml"
    path.write_text(
        f"""
classifier:
  provider: rule_based
scoring:
  allow_fixture_analysis_live: true
fleet:
  enabled: true
  max_bots: 5
  position_mode: "{position_mode}"
  auto_ack: {str(auto_ack).lower()}
  generated_dir: {tmp_path / 'generated'}
data_dir: {tmp_path / 'data'}
logs_dir: {tmp_path / 'logs'}
""",
        encoding="utf-8",
    )
    return path


def _events_fetch(events):
    def fetch(url: str, params: dict) -> list[dict]:
        return events if params.get("offset", 0) == 0 else []

    return fetch


def test_forward_books_disabled_status_names_shared_service_reason() -> None:
    config = DiscoveryConfig(
        forward_recorder=ForwardRecorderConfig(
            enabled=True,
            shared_book_service=False,
        )
    )

    assert _forward_books_disabled_status(config)["reason"] == (
        "forward_recorder.shared_book_service=false"
    )


def _write_priority_snapshot(config, contexts, *, cold_ids=()):
    records = []
    policy = replace(
        config.profit_priority,
        min_terminal_observations=1,
        min_human_labels=1,
        min_quote_samples=1,
        min_stressed_fill_samples=1,
        min_resolved_trades=1,
    )
    for index, context in enumerate(contexts):
        cold = context.market_id in set(cold_ids)
        evidence = (
            PriorityEvidence()
            if cold
            else PriorityEvidence(
                monitored_market_hours=10,
                terminal_opportunities=10 + index,
                human_labels=10,
                quote_trials=10,
                quote_survivals=10,
                stressed_fill_trials=10,
                stressed_fills=10,
                fillable_notionals_usd=(20.0,) * 10,
                one_cent_shocked_edges=(0.1,) * 10,
                resolved_trade_pnls_usd=(1.0,),
                p95_submission_latency_ms=1000,
                data_cutoff_at="2026-07-25T00:00:00+00:00",
            )
        )
        records.append(
            score_profit_priority(
                context=context,
                rule_family="SOURCE_LOCKED_ANNOUNCEMENT",
                source_adapter_id="official",
                policy_partition_sha256=f"partition-{index}",
                evidence=evidence,
                policy=policy,
                computed_at="2026-07-25T01:00:00+00:00",
                execution_supported=True,
            )
        )
    stable = {
        "schema_version": 1,
        "kind": "rules_first_profit_priority",
        "priority_policy_version": policy.policy_version,
        "priority_policy_sha256": sha256_json(
            {
                key: getattr(policy, key)
                for key in policy.__dataclass_fields__
            }
        ),
        "records": [record.as_dict() for record in records],
    }
    payload = {
        **stable,
        "computed_at": "2026-07-25T01:00:00+00:00",
        "data_cutoff_at": "2026-07-25T00:00:00+00:00",
    }
    payload["snapshot_sha256"] = sha256_json(
        _snapshot_hash_payload(payload)
    )
    config.data_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / "profit_priority.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_fleet_once_spawns_a_bot_per_eligible_market(tmp_path, monkeypatch) -> None:
    geo = _patch_roots(monkeypatch, tmp_path)
    config_path = _fleet_yaml(tmp_path)
    spawner = _Spawner()
    notifier = _Notifier()
    fetch = _events_fetch([_grouped_event(), _binary_event(), _sports_event()])

    assert run_fleet_command(config_path, once=True, events_fetch=fetch, quotes=_FakeQuotes(), notifier=notifier, spawner=spawner) == 0

    runners = sorted(cmd[3] for cmd, _log in spawner.spawned)
    assert runners == ["run-binary", "run-location-protection"]
    assert all("--live" not in cmd for cmd, _log in spawner.spawned)
    # Configs generated and operator gates armed with the fleet position mode.
    generated = sorted(p.name for p in (tmp_path / "generated").glob("*.yaml"))
    assert len(generated) == 2
    mode_files = list((geo / "operator" / "positions").glob("*.mode"))
    assert len(mode_files) == 2
    assert all("alert_only" in p.read_text(encoding="utf-8") for p in mode_files)
    # No auto-ack in alert_only mode.
    assert not (geo / "operator" / "live_ack").exists()
    # fleet_state.json records the cycle; once-mode shuts children down.
    state = json.loads((tmp_path / "data" / "fleet_state.json").read_text(encoding="utf-8"))
    assert len(state["desired"]) == 2
    assert state["forward_books"] == {
        "enabled": False,
        "paper_only": True,
        "reason": "forward_recorder.enabled=false",
        "shared": True,
    }
    assert state["discovery_cycle"]["state"] == "COMPLETE"
    assert state["discovery_cycle"]["last_completed_at"]
    assert all(process.terminated for process in spawner.processes)
    assert [m for m, _f in notifier.messages if "bot started" in m]


def test_fleet_bootstraps_central_feed_before_discovery_and_refreshes_after(
    tmp_path,
    monkeypatch,
) -> None:
    _patch_roots(monkeypatch, tmp_path)
    config_path = _fleet_yaml(tmp_path)
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"""
central_feed:
  enabled: true
  db_path: {tmp_path / 'central.sqlite3'}
  impact_feed_urls:
    - https://publisher.example/rss
"""
        )
    calls: list[str] = []

    def fake_poll_once(service, *, force=False):
        calls.append("feed")
        service.store.set_active_feeds(
            ["https://publisher.example/rss"],
            impact_feed_urls=["https://publisher.example/rss"],
            semantic_feed_urls=[],
        )
        service.store.touch_heartbeat()
        assert force is True
        return {"feeds": 0, "polled": 0, "inserted": 0, "errors": 0, "pruned": 0}

    monkeypatch.setattr(
        "polybot.core.central_feed.CentralFeedService.poll_once",
        fake_poll_once,
    )
    spawner = _Spawner()
    base_fetch = _events_fetch([_binary_event()])

    def ordered_fetch(url, params):
        if params.get("offset", 0) == 0:
            calls.append("discovery")
        return base_fetch(url, params)

    assert run_fleet_command(
        config_path,
        once=True,
        events_fetch=ordered_fetch,
        quotes=_FakeQuotes(),
        notifier=_Notifier(),
        spawner=spawner,
    ) == 0
    assert calls[0] == "feed"
    assert "discovery" in calls[1:-1]
    assert calls[-1] == "feed"
    from polybot.binary.config import load_binary_config

    generated = load_binary_config(next((tmp_path / "generated").glob("*.yaml")))
    assert generated.sources.central_feed_db == str(tmp_path / "central.sqlite3")
    state = json.loads(
        (tmp_path / "data" / "fleet_state.json").read_text(encoding="utf-8")
    )
    assert state["central_feed"]["enabled"] is True
    assert state["central_feed"]["impact_feeds"] == 1


def test_fleet_live_auto_ack_arms_and_passes_live_flag(tmp_path, monkeypatch) -> None:
    # The pre-spawn gate check requires the live env (telegram + anthropic)
    # to be configured, same as the bot's own startup preflight.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    geo = _patch_roots(monkeypatch, tmp_path)
    config_path = _fleet_yaml(tmp_path, position_mode="live", auto_ack=True)
    spawner = _Spawner()
    fetch = _events_fetch([_binary_event()])

    assert run_fleet_command(config_path, live=True, once=True, events_fetch=fetch, quotes=_FakeQuotes(), notifier=_Notifier(), spawner=spawner) == 0

    (command, _log), = spawner.spawned
    assert command[-1] == "--live"
    generated = next((tmp_path / "generated").glob("*.yaml"))
    generated_text = generated.read_text(encoding="utf-8")
    assert "dry_run: false" in generated_text
    assert "passes: 2" in generated_text
    assert "require_pass_agreement: true" in generated_text
    acks = list((geo / "operator" / "live_ack").rglob("*.json"))
    assert len(acks) == 1
    mode_files = list((geo / "operator" / "positions").glob("*.mode"))
    assert all("live" in p.read_text(encoding="utf-8") for p in mode_files)


def test_fleet_keeps_defender_for_held_market_and_stops_flat_demoted(tmp_path, monkeypatch) -> None:
    geo = _patch_roots(monkeypatch, tmp_path)
    config = DiscoveryConfig(
        fleet=FleetConfig(enabled=True, max_bots=5, generated_dir=str(tmp_path / "generated")),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    store = DiscoveryStore(config.data_dir)
    scoring = ScoringConfig(allow_fixture_analysis_live=True)
    held_market = grade_market(_analyzed_context(_binary_event(slug="held")), scoring)
    flat_market = grade_market(_analyzed_context(_binary_event(slug="flat")), scoring)
    for context in (held_market, flat_market):
        store.save_context(context)
        store.save_source_plan(build_source_plan(context))
    spawner = _Spawner()
    manager = FleetManager(config, store, live=False, per_order_usd=50.0, ledger_path=str(config.data_dir / "allocations.json"), spawner=spawner)

    summary = manager.sync([held_market, flat_market])
    assert sorted(summary["started"]) == sorted([held_market.market_id, flat_market.market_id])

    # The held market records a live holding; both markets then get demoted.
    from polybot.discovery.types import market_dir_slug

    holdings = Path(str(geo)) / market_dir_slug(held_market.market_id) / "dry_run" / "holdings.json"
    holdings.parent.mkdir(parents=True, exist_ok=True)
    holdings.write_text(json.dumps({"held_location": "yes", "source": "entry"}), encoding="utf-8")
    demoted_held = grade_market(held_market, ScoringConfig(allow_fixture_analysis_live=True, min_liquidity_live=10**9, small_live_enabled=False))
    demoted_flat = grade_market(flat_market, ScoringConfig(allow_fixture_analysis_live=True, min_liquidity_live=10**9, small_live_enabled=False))
    assert demoted_held.state == "PAPER_ELIGIBLE" and demoted_flat.state == "PAPER_ELIGIBLE"

    summary = manager.sync([demoted_held, demoted_flat])
    # Paper mode watches every paper-eligible market, whether it already has a
    # simulated holding or is still flat.
    assert summary["stopped"] == []
    assert sorted(summary["running"]) == sorted([held_market.market_id, flat_market.market_id])


def test_fleet_live_ignores_paper_markets_and_dry_run_holdings(tmp_path, monkeypatch) -> None:
    geo = _patch_roots(monkeypatch, tmp_path)
    config = DiscoveryConfig(
        fleet=FleetConfig(enabled=True, max_bots=5, generated_dir=str(tmp_path / "generated")),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    store = DiscoveryStore(config.data_dir)
    market = grade_market(
        _analyzed_context(_binary_event(slug="paper-only")),
        ScoringConfig(
            allow_fixture_analysis_live=True,
            min_liquidity_live=10**9,
            small_live_enabled=False,
        ),
    )
    assert market.state == "PAPER_ELIGIBLE"
    from polybot.discovery.types import market_dir_slug

    dry_holding = geo / market_dir_slug(market.market_id) / "dry_run" / "holdings.json"
    dry_holding.parent.mkdir(parents=True, exist_ok=True)
    dry_holding.write_text(json.dumps({"held_location": "yes"}), encoding="utf-8")

    live_manager = FleetManager(
        config,
        store,
        live=True,
        per_order_usd=50.0,
        ledger_path=str(config.data_dir / "allocations.json"),
    )
    paper_manager = FleetManager(
        config,
        store,
        live=False,
        per_order_usd=50.0,
        ledger_path=str(config.data_dir / "allocations.json"),
    )
    assert live_manager.is_holding(market.market_id) is False
    assert live_manager.desired_markets([market]) == []
    assert paper_manager.is_holding(market.market_id) is True
    assert [item.market_id for item in paper_manager.desired_markets([market])] == [market.market_id]


def test_fleet_reemits_stale_single_pass_live_config(tmp_path, monkeypatch) -> None:
    _patch_roots(monkeypatch, tmp_path)
    config = DiscoveryConfig(
        fleet=FleetConfig(enabled=True, generated_dir=str(tmp_path / "generated")),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    store = DiscoveryStore(config.data_dir)
    market = grade_market(
        _analyzed_context(_binary_event(slug="stale-live-config")),
        ScoringConfig(allow_fixture_analysis_live=True),
    )
    plan = build_source_plan(market)
    store.save_context(market)
    store.save_source_plan(plan)
    from polybot.discovery.emit import emit_bot_config
    from polybot.discovery.types import market_dir_slug

    generated = tmp_path / "generated" / f"{market_dir_slug(market.market_id)}.yaml"
    emit_bot_config(
        market,
        plan,
        entry_usd=50.0,
        out_path=generated,
        dry_run=True,
        classifier_provider="anthropic",
    )
    generated.write_text(
        generated.read_text(encoding="utf-8").replace("dry_run: true", "dry_run: false"),
        encoding="utf-8",
    )

    manager = FleetManager(
        config,
        store,
        live=True,
        per_order_usd=50.0,
        ledger_path=str(config.data_dir / "allocations.json"),
    )
    manager._ensure_config(market)
    refreshed = generated.read_text(encoding="utf-8")
    assert "passes: 2" in refreshed
    assert "require_pass_agreement: true" in refreshed


def test_fleet_routes_generated_bot_through_configured_central_feed(tmp_path, monkeypatch) -> None:
    _patch_roots(monkeypatch, tmp_path)
    central_db = tmp_path / "central.sqlite3"
    config = DiscoveryConfig(
        fleet=FleetConfig(enabled=True, generated_dir=str(tmp_path / "generated")),
        central_feed=CentralFeedConfig(
            enabled=True,
            db_path=str(central_db),
            stale_after_seconds=45.0,
        ),
        classifier_budget=ClassifierBudgetConfig(
            db_path=str(tmp_path / "classifier.sqlite3"),
            max_escalations_per_hour=12,
            max_escalations_per_day=80,
            max_classifier_errors_per_hour=5,
        ),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    store = DiscoveryStore(config.data_dir)
    market = grade_market(
        _analyzed_context(_binary_event(slug="central-feed")),
        ScoringConfig(allow_fixture_analysis_live=True),
    )
    store.save_source_plan(build_source_plan(market))
    manager = FleetManager(
        config,
        store,
        live=False,
        per_order_usd=50.0,
        ledger_path=str(config.data_dir / "allocations.json"),
    )

    from polybot.binary.config import load_binary_config

    generated = manager._ensure_config(market)
    loaded = load_binary_config(generated)
    assert loaded.sources.central_feed_db == str(central_db)
    assert loaded.sources.central_feed_stale_after_seconds == 45.0
    assert loaded.classifier.budget_db_path == str(tmp_path / "classifier.sqlite3")
    assert loaded.classifier.max_escalations_per_hour == 12
    assert loaded.classifier.max_escalations_per_day == 80
    assert loaded.classifier.max_classifier_errors_per_hour == 5


def test_fleet_reemits_when_source_plan_changes_without_rule_change(tmp_path, monkeypatch) -> None:
    _patch_roots(monkeypatch, tmp_path)
    config = DiscoveryConfig(
        fleet=FleetConfig(enabled=True, generated_dir=str(tmp_path / "generated")),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    store = DiscoveryStore(config.data_dir)
    market = grade_market(
        _analyzed_context(_binary_event(slug="source-plan-refresh")),
        ScoringConfig(allow_fixture_analysis_live=True),
    )
    original = build_source_plan(market)
    store.save_source_plan(original)
    manager = FleetManager(
        config,
        store,
        live=False,
        per_order_usd=50.0,
        ledger_path=str(config.data_dir / "allocations.json"),
    )
    generated = manager._ensure_config(market)

    changed = original.from_dict(
        {
            **original.as_dict(),
            "feed_urls": [*original.feed_urls, "https://new-source.example/feed"],
        }
    )
    store.save_source_plan(changed)
    assert manager._ensure_config(market) == generated

    from polybot.binary.config import load_binary_config
    from polybot.discovery.sources import source_plan_sha256

    loaded = load_binary_config(generated)
    assert loaded.sources.feed_urls == changed.feed_urls
    assert loaded.sources.source_plan_sha256 == source_plan_sha256(changed)


def test_set_fleet_mode_writes_master_switch(tmp_path, monkeypatch, capsys) -> None:
    geo = _patch_roots(monkeypatch, tmp_path)
    assert set_fleet_mode_command("off") == 0
    raw = json.loads((geo / "operator" / "global_mode.json").read_text(encoding="utf-8"))
    assert raw["mode"] == "off"


def test_fleet_ranks_by_profit_priority_without_liquidity_bias(
    tmp_path,
    monkeypatch,
) -> None:
    _patch_roots(monkeypatch, tmp_path)
    config = DiscoveryConfig(
        fleet=FleetConfig(enabled=True, max_bots=1, generated_dir=str(tmp_path / "generated")),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    store = DiscoveryStore(config.data_dir)
    scoring = ScoringConfig(allow_fixture_analysis_live=True)
    deep = grade_market(_analyzed_context(_binary_event(slug="deep", liquidity=50000.0)), scoring)
    thin = grade_market(_analyzed_context(_binary_event(slug="thin", liquidity=300.0)), scoring)
    manager = FleetManager(config, store, live=False, per_order_usd=50.0, ledger_path=str(config.data_dir / "allocations.json"))

    # No scan data: NO liquidity bias in either direction -- selection is
    # deterministic by market_id, not by book depth.
    desired = manager.desired_markets([deep, thin])
    assert [c.market_id for c in desired] == [min(deep.market_id, thin.market_id)]

    # Compatible point-in-time profit evidence, not liquidity or a forecast
    # edge, controls the scarce monitoring slot.
    _write_priority_snapshot(config, [deep, thin])
    desired = manager.desired_markets([deep, thin])
    assert [c.market_id for c in desired] == [thin.market_id]


def test_fleet_excludes_out_of_scope_tradeable_context(
    tmp_path,
    monkeypatch,
) -> None:
    _patch_roots(monkeypatch, tmp_path)
    config = DiscoveryConfig(
        fleet=FleetConfig(enabled=True),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    store = DiscoveryStore(config.data_dir)
    geopolitical = grade_market(
        _analyzed_context(_binary_event()),
        ScoringConfig(allow_fixture_analysis_live=True),
    )
    sports = replace(
        geopolitical,
        market_id="world-cup",
        event_title="World Cup winner",
        question="Which team will win the World Cup?",
        tags=["sports"],
    )
    manager = FleetManager(
        config,
        store,
        live=False,
        per_order_usd=50.0,
        ledger_path=str(config.data_dir / "allocations.json"),
    )

    desired = manager.desired_markets([geopolitical, sports])

    assert [context.market_id for context in desired] == [
        geopolitical.market_id
    ]


def test_fleet_status_reports_positions_ledger_and_scan(tmp_path, monkeypatch, capsys) -> None:
    from datetime import datetime, timezone

    from polybot.core.holdings import _atomic_json_write
    from polybot.discovery.runner import fleet_status_command
    from polybot.discovery.types import market_dir_slug

    geo = _patch_roots(monkeypatch, tmp_path)
    config_path = _fleet_yaml(tmp_path)
    store = DiscoveryStore(tmp_path / "data")
    market = grade_market(_analyzed_context(_binary_event()), ScoringConfig(allow_fixture_analysis_live=True))
    store.save_context(market)

    slug = market_dir_slug(market.market_id)
    _atomic_json_write(geo / slug / "dry_run" / "holdings.json", {"held_location": "yes", "source": "entry"})
    _atomic_json_write(geo / slug / "heartbeat.json", {"at": datetime.now(timezone.utc).isoformat()})
    _atomic_json_write(geo / "operator" / "global_mode.json", {"mode": "alert_only"})
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "opportunities.json").write_text(
        json.dumps(
            {
                "opportunities": [
                    {"market_id": market.market_id, "outcome": "yes", "side": "YES", "tradable_edge": 0.12, "blockers": []}
                ],
                "group_arbitrage": [],
            }
        ),
        encoding="utf-8",
    )

    assert fleet_status_command(config_path) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["global_mode"]["mode"] == "alert_only"
    assert status["holding_count"] == 1
    row = status["markets"][0]
    assert row["market_id"] == market.market_id and row["holding"] is True
    assert row["heartbeat_age_seconds"] is not None and row["heartbeat_age_seconds"] < 60
    assert status["scan"]["executable"][0]["edge"] == 0.12
    assert status["forward_books"]["enabled"] is False
    assert (
        status["forward_books"]["reason"]
        == "forward_recorder.enabled=false"
    )
    assert "semantic_coverage" in status
    assert status["semantic_coverage"]["contexts"] == 1
    assert status["fleet"]["status_stale"] is True
    assert status["fleet"]["snapshot_warning"] == (
        "running_and_desired_are_stale_snapshots"
    )
    assert status["discovery_cycle"]["state"] == "UNKNOWN"
    assert status["rule_engine"]["deadline_authority"] == {
        "policy": "STRICT_GAMMA_MATCH_V1",
        "market_ids": [],
        "paper_only": True,
    }
    assert row["rule_ready"] is False
    assert "current_rule_spec_missing" in row["blockers"]
    # Fresh ledger: full drawdown headroom available.
    assert status["ledger"]["realized_net"] == 0.0
    assert status["ledger"]["drawdown_headroom"] == status["ledger"]["max_drawdown_usd"]


def test_forward_books_bootstrap_precedes_discovery_cycle(
    tmp_path,
    monkeypatch,
) -> None:
    _patch_roots(monkeypatch, tmp_path)
    config_path = _fleet_yaml(tmp_path)
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write(
            """
rule_compiler:
  enabled: true
rule_runner:
  enabled: true
forward_recorder:
  enabled: true
  rest_seed: false
"""
        )
    calls: list[str] = []

    class FakeForwardBooks:
        def __init__(self, _config, **_kwargs):
            self._status = {
                "enabled": True,
                "paper_only": True,
                "shared": True,
                "reason": "no_recordable_contexts",
                "selected_contexts": 0,
                "bindings": 0,
                "tokens": 0,
                "connections": 0,
                "connected": 0,
                "reconnects": 0,
                "streaming": False,
                "last_sync_at": "startup",
            }

        def sync(self, _contexts, *, start_websocket=True):
            calls.append("startup_sync")
            return self._status

        def poll_once(self, _contexts):
            calls.append("post_discovery_sync")
            return self._status

        def status(self):
            return self._status

        def stop(self):
            calls.append("stop")

    monkeypatch.setattr(
        "polybot.rules.forward.ForwardBookService",
        FakeForwardBooks,
    )

    def fake_cycle(*_args, **_kwargs):
        state = json.loads(
            (tmp_path / "data" / "fleet_state.json").read_text(
                encoding="utf-8"
            )
        )
        assert state["discovery_cycle"]["state"] == "RUNNING"
        calls.append("discovery")

    monkeypatch.setattr(
        "polybot.discovery.runner._run_discovery_cycle",
        fake_cycle,
    )

    assert run_fleet_command(
        config_path,
        once=True,
        events_fetch=_events_fetch([]),
        quotes=_FakeQuotes(),
        notifier=_Notifier(),
        spawner=_Spawner(),
    ) == 0
    assert calls[:3] == [
        "startup_sync",
        "discovery",
        "post_discovery_sync",
    ]
    state = json.loads(
        (tmp_path / "data" / "fleet_state.json").read_text(encoding="utf-8")
    )
    assert state["forward_books"]["enabled"] is True
    assert state["discovery_cycle"]["state"] == "COMPLETE"
    assert state["discovery_cycle"]["last_completed_at"]


def test_fleet_emits_no_side_config_when_no_edge_is_best(tmp_path, monkeypatch) -> None:
    _patch_roots(monkeypatch, tmp_path)
    config = DiscoveryConfig(
        fleet=FleetConfig(enabled=True, max_bots=5, generated_dir=str(tmp_path / "generated")),
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
    )
    store = DiscoveryStore(config.data_dir)
    market = grade_market(_analyzed_context(_binary_event()), ScoringConfig(allow_fixture_analysis_live=True))
    store.save_context(market)
    store.save_source_plan(build_source_plan(market))
    config.data_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / "opportunities.json").write_text(
        json.dumps(
            {
                "opportunities": [
                    {"market_id": market.market_id, "outcome": "yes", "side": "NO", "tradable_edge": 0.15, "blockers": []},
                    {"market_id": market.market_id, "outcome": "yes", "side": "YES", "tradable_edge": 0.03, "blockers": []},
                ]
            }
        ),
        encoding="utf-8",
    )
    manager = FleetManager(config, store, live=False, per_order_usd=50.0, ledger_path=str(config.data_dir / "allocations.json"), spawner=_Spawner())
    assert manager.best_entry_side(market.market_id) == "NO"

    summary = manager.sync([market])
    assert summary["started"] == [market.market_id]
    from polybot.binary.config import load_binary_config

    generated = next((tmp_path / "generated").glob("*.yaml"))
    assert load_binary_config(generated).entry.side == "NO"
