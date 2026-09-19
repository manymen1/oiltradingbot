from dataclasses import asdict, replace
from datetime import timedelta

import pytest

from oilbot.clock import instant
from oilbot.paper import PaperEngine, PaperLimits
from oilbot.schema import InstrumentDefinition, MarketEvent
from oilbot.store import Journal


BASE = "2026-09-18T12:00:00Z"


def at(seconds):
    return (instant(BASE) + timedelta(seconds=seconds)).isoformat()


def definition():
    return InstrumentDefinition("MCL-2026-10", "MCL", "2026-10", "NYMEX", "USD", "100", "0.01",
                                "2026-09-22T18:00:00Z",
                                [{"open": "2026-09-18T00:00:00Z", "close": "2026-09-18T21:00:00Z"}],
                                "2026-09-17T00:00:00Z", "fixture", "fixture")


@pytest.fixture
def engine(tmp_path):
    return PaperEngine(Journal(tmp_path / "paper.sqlite3"), definition(), PaperLimits())


def quote(engine, seconds, sequence, bid="72.00", ask="72.02", size=10, **kwargs):
    value = asdict(MarketEvent(definition().instrument_id, at(seconds), bid, ask, size, size,
                              at(seconds), at(seconds), sequence, "fixture", "synthetic"))
    value.update(kwargs)
    return engine.process("quote", value, at=at(seconds), event_id=f"q{sequence}")


def signal(engine, seconds=0, identity="s1", side=1, action="RESEARCH_CANDIDATE"):
    return engine.process("signal", {"direction": side, "action": action}, at=at(seconds), event_id=identity)


def opened(engine, side=1):
    quote(engine, 0, 1)
    signal(engine, side=side)
    return quote(engine, 1, 2)


@pytest.mark.parametrize("side,bid,ask,pnl", [(1, "72.50", "72.52", "44.00"), (-1, "71.48", "71.50", "46.00")])
def test_delayed_entry_and_take_profit_accounting(engine, side, bid, ask, pnl):
    quote(engine, 0, 1)
    state = signal(engine, side=side)
    assert state["position"] is None and state["pending"]
    state = quote(engine, 1, 2)
    assert state["position"]["quantity"] == 1
    assert state["cash_pnl"] == "-1"
    state = quote(engine, 2, 3, bid, ask)
    assert state["position"] is None
    assert state["cash_pnl"] == pnl
    assert engine.summary()["events"][-1]["reason"] == "TAKE_PROFIT"


def test_restart_and_duplicate_input_do_not_duplicate_order(engine):
    opened(engine)
    original = engine.summary()
    restarted = PaperEngine(engine.store, definition(), PaperLimits())
    signal(restarted)  # Duplicate old delivery is idempotent even after newer quotes.
    assert restarted.summary() == original
    with pytest.raises(ValueError, match="collision"):
        signal(restarted, side=-1)
    with pytest.raises(ValueError, match="chronological"):
        signal(restarted, identity="new-old-input")


def test_configuration_change_cannot_reuse_account(engine):
    opened(engine)
    changed = PaperEngine(engine.store, definition(), replace(PaperLimits(), contracts=2, max_contracts=2))
    with pytest.raises(ValueError, match="configuration changed"):
        changed.summary()


def test_daily_loss_latches_and_rejects_next_signal(engine):
    engine = PaperEngine(engine.store, definition(), replace(PaperLimits(), daily_loss_usd="10"))
    opened(engine)
    state = quote(engine, 2, 3, "71.80", "71.82")
    assert state["loss_halt"] and state["position"] is None
    assert state["cash_pnl"] == "-26.00"
    signal(engine, 2, "s2")
    assert engine.summary()["events"][-1]["reason"] == "ACCOUNT_HALTED"


def test_halt_cancels_pending_and_exits_on_next_quote(engine):
    opened(engine)
    state = engine.process("halt", {}, at=at(1), event_id="halt")
    assert state["position"] and state["manual_halt"]
    state = quote(engine, 2, 3)
    assert state["position"] is None
    assert engine.summary()["events"][-1]["reason"] == "MANUAL_HALT"


@pytest.mark.parametrize("changes,reason", [
    ({"bid_at": at(-10)}, "QUOTE_UNAVAILABLE_OR_STALE"),
    ({"data_mode": "delayed"}, "QUOTE_UNAVAILABLE_OR_STALE"),
    ({"ask": "72.20"}, "SPREAD_LIMIT"),
])
def test_market_quality_blocks_entries(engine, changes, reason):
    quote(engine, 0, 1, **changes)
    signal(engine)
    assert engine.summary()["events"][-1]["reason"] == reason


def test_missing_quote_keeps_open_exposure_visible(engine):
    opened(engine)
    state = quote(engine, 2, 3, bid=None, ask=None)
    assert state["position"]
    summary = engine.summary()
    assert summary["mark_available_at"] == at(1)
    assert summary["position"]["quantity"] == 1


def test_limit_cap_and_ttl_prevent_chasing_price(engine):
    quote(engine, 0, 1)
    signal(engine)
    assert quote(engine, 1, 2, "72.10", "72.12")["position"] is None
    state = quote(engine, 10, 3)
    assert state["position"] is None and state["pending"] is None
    assert engine.summary()["events"][-1]["reason"] == "TTL_EXPIRED"


def test_position_limit_partial_fill_and_partial_exit(engine):
    engine = PaperEngine(engine.store, definition(), replace(PaperLimits(), contracts=3, max_contracts=3))
    quote(engine, 0, 1)
    signal(engine)
    state = quote(engine, 1, 2, size=2)
    assert state["position"]["quantity"] == 2
    assert engine.summary()["events"][-1]["unfilled_cancelled"] == 1
    signal(engine, 1, "s2")
    assert engine.summary()["events"][-1]["reason"] == "POSITION_OR_ORDER_EXISTS"
    state = quote(engine, 2, 3, "72.50", "72.52", size=1)
    assert state["position"]["quantity"] == 1
    assert state["close_reason"] == "TAKE_PROFIT"
    assert quote(engine, 3, 4, "72.10", "72.12")["position"] is None


def test_quote_gap_halts_and_nonincreasing_sequence_rolls_back(engine):
    quote(engine, 0, 1)
    signal(engine)
    state = quote(engine, 1, 3)
    assert state["manual_halt"] and state["pending"] is None
    before = engine.summary()
    with pytest.raises(ValueError, match="sequence"):
        quote(engine, 2, 2)
    assert engine.summary() == before


def test_trade_limit_and_session_restriction(engine):
    engine = PaperEngine(engine.store, definition(), replace(PaperLimits(), max_trades_per_day=1))
    opened(engine)
    quote(engine, 2, 3, "72.50", "72.52")
    signal(engine, 2, "s2")
    assert engine.summary()["events"][-1]["reason"] == "DAILY_TRADE_LIMIT"
    other = PaperEngine(Journal(engine.store.path.parent / "other.sqlite3"), definition(), PaperLimits())
    seconds = 9 * 3600 - 1
    quote(other, seconds, 1)
    signal(other, seconds)
    assert other.summary()["events"][-1]["reason"] == "SESSION_OR_EXPIRY_RESTRICTED"


@pytest.mark.parametrize("changes", [{"daily_loss_usd": "NaN"}, {"contracts": 2}, {"order_delay_seconds": 10}, {"max_spread_ticks": True}])
def test_invalid_limits_rejected(changes):
    with pytest.raises(ValueError):
        replace(PaperLimits(), **changes).validate()


@pytest.mark.parametrize("seconds,bid,ask,reason", [(2, "71.80", "71.82", "STOP_LOSS"), (1801, "72.00", "72.02", "MAX_HOLD")])
def test_stop_and_holding_period(engine, seconds, bid, ask, reason):
    opened(engine)
    assert quote(engine, seconds, 3, bid, ask)["position"] is None
    assert engine.summary()["events"][-1]["reason"] == reason


def test_failed_commit_does_not_consume_signal(engine, monkeypatch):
    quote(engine, 0, 1)
    append = engine.store.append
    def fail(kind, *args, **kwargs):
        if kind == "paper_event":
            raise OSError("simulated disk failure")
        return append(kind, *args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(engine.store, "append", fail)
        with pytest.raises(OSError, match="disk failure"):
            signal(engine)
    assert engine.summary()["pending_order"] is None
    assert engine.store.cursor("paper:event:s1") is None
    assert signal(engine)["pending"]


def test_paper_replay_cli_from_verified_snapshot(tmp_path, capsys):
    import json
    from pathlib import Path
    import yaml
    from oilbot.cli import main
    from oilbot.config import load_config
    from oilbot.market import FixtureAdapter, record_adapter
    from oilbot.replay import export_manifest

    raw = yaml.safe_load(Path("configs/oil/observe.yaml").read_text())
    raw["storage"]["root"] = str(tmp_path / "capture")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)
    Journal(config.db("analysis")).append("decision", {
        "input_revision_ids": [], "action": "RESEARCH_CANDIDATE", "direction": 1, "synthetic": True}, available_at=BASE)
    quotes = [asdict(MarketEvent(definition().instrument_id, at(n), bid, ask, 10, 10, at(n), at(n), n + 1, "fixture", "synthetic"))
              for n, bid, ask in [(0, "72.00", "72.02"), (1, "72.00", "72.02"), (2, "72.50", "72.52")]]
    fixture = tmp_path / "quotes.json"
    fixture.write_text(json.dumps({"definitions": [asdict(definition())], "events": quotes}))
    record_adapter(FixtureAdapter(fixture), config.root / "quotes")
    manifest = export_manifest(config, tmp_path / "snapshot")
    args = ["paper-replay", "--manifest", str(manifest), "--instrument", definition().instrument_id,
            "--limits", "configs/oil/paper-limits.json", "--out", str(tmp_path / "paper")]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["cash_pnl"] == "44.00" and not result["open_position"]
    report = json.loads(Path(result["report"]).read_text())
    assert report["dataset_role"] == "engineering_fixture"
    assert report["economic_evaluation"] == "unavailable"
    assert [event["event"] for event in report["events"]] == ["ORDER_SUBMITTED", "ENTRY_FILL", "EXIT_FILL"]
    assert main(args) == 2  # Frozen outputs cannot be silently overwritten.


def test_legacy_entrypoint_uses_maintained_package():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-m", "polybot.oil", "--help"], capture_output=True, text=True, check=True)
    assert "paper-replay" in result.stdout
