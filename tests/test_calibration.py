from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_discovery import _FakeQuotes, _allocator, _binary_event, _graded, _write_forecast_state

from polybot.discovery.calibration import CalibrationLog
from polybot.discovery.config import OpportunityConfig
from polybot.discovery.opportunity import config_probability_lookup, scan_opportunities
from polybot.discovery.types import Opportunity


# ---- market-anchored blending + disagreement-scaled uncertainty ----


def test_scan_blends_probability_toward_market_mid(tmp_path) -> None:
    context = _graded(_binary_event())
    config = OpportunityConfig(probability_estimates={context.market_id: {"yes": 0.60}})
    results = scan_opportunities(
        [context],
        config,
        _FakeQuotes(ask=0.40, bid=0.38),
        _allocator(tmp_path),
        probability_lookup=config_probability_lookup(config),
    )
    opp = results[0]
    # mid = 0.39; blend = 0.35*0.60 + 0.65*0.39 = 0.4635
    assert opp.detail["market_mid"] == pytest.approx(0.39)
    assert opp.detail["blended_probability"] == pytest.approx(0.4635)
    # disagreement |0.60-0.39| = 0.21 -> penalty 0.21*0.25 = 0.0525
    assert opp.detail["disagreement_penalty"] == pytest.approx(0.0525)
    # The raw model estimate is still reported (that is what gets calibrated).
    assert opp.estimated_probability == pytest.approx(0.60)
    # Anchored edge collapses below min_edge: a standing 21-point disagreement
    # with the market does not trade on defaults.
    assert any(b.startswith("edge_below_minimum") for b in opp.blockers)


def test_disagreement_widens_the_edge_bar(tmp_path) -> None:
    context = _graded(_binary_event())
    # Full model weight isolates the disagreement penalty.
    config = OpportunityConfig(
        probability_estimates={context.market_id: {"yes": 0.60}},
        model_weight=1.0,
        disagreement_buffer_scale=0.5,
    )
    results = scan_opportunities(
        [context],
        config,
        _FakeQuotes(ask=0.40, bid=0.38),
        _allocator(tmp_path),
        probability_lookup=config_probability_lookup(config),
    )
    risk_extra = round(context.rule_analysis.resolution_risk * 0.05, 4)
    # base edge 0.14 minus per-market risk minus |0.60-0.39|*0.5 = 0.105
    expected = round(0.14 - risk_extra - 0.105, 4)
    assert results[0].tradable_edge == pytest.approx(expected)


# ---- deadline decay for flagged config estimates ----


def test_config_estimate_deadline_decay(tmp_path) -> None:
    context = _graded(_binary_event())  # deadline 2026-09-30T23:59:00Z
    now = datetime.now(timezone.utc)
    deadline = datetime.fromisoformat(context.deadline_iso.replace("Z", "+00:00"))
    # as_of chosen so exactly half the window has elapsed.
    as_of = now - (deadline - now)
    config = OpportunityConfig(
        probability_estimates={
            context.market_id: {"yes": 0.60, "_decay": True, "_as_of": as_of.isoformat()}
        },
        model_weight=1.0,
        disagreement_buffer_scale=0.0,
    )
    results = scan_opportunities(
        [context],
        config,
        _FakeQuotes(),
        _allocator(tmp_path),
        probability_lookup=config_probability_lookup(config),
    )
    opp = next(r for r in results if r.side == "YES")
    assert opp.probability_source == "config_estimate_decayed"
    assert opp.estimated_probability == pytest.approx(0.30, rel=1e-2)
    # The NO side decays consistently (complement of the decayed estimate).
    no_row = next(r for r in results if r.side == "NO")
    assert no_row.estimated_probability == pytest.approx(0.70, rel=1e-2)


def test_config_estimate_without_decay_flag_is_untouched(tmp_path) -> None:
    context = _graded(_binary_event())
    config = OpportunityConfig(
        probability_estimates={context.market_id: {"yes": 0.60}},
        model_weight=1.0,
        disagreement_buffer_scale=0.0,
    )
    results = scan_opportunities(
        [context],
        config,
        _FakeQuotes(),
        _allocator(tmp_path),
        probability_lookup=config_probability_lookup(config),
    )
    assert results[0].probability_source == "config_estimate"
    assert results[0].estimated_probability == pytest.approx(0.60)


def test_meta_keys_are_not_outcomes() -> None:
    config = OpportunityConfig(probability_estimates={"m": {"yes": 0.6, "_decay": True, "_as_of": "2026-01-01"}})
    lookup = config_probability_lookup(config)
    assert lookup("m", "yes") == (0.6, "config_estimate")
    assert lookup("m", "_decay") is None
    assert lookup("m", "_as_of") is None


# ---- forecast probabilities gated on proven calibration ----


def test_forecast_probability_blocked_until_calibrated(tmp_path) -> None:
    context = _graded(_binary_event())
    root = tmp_path / "geo"
    _write_forecast_state(root, context.market_id, {"yes": 0.70}, datetime.now(timezone.utc).isoformat())
    config = OpportunityConfig(
        model_pricing_mode="allocatable",
        forecast_data_root=str(root),
        model_weight=1.0,
        disagreement_buffer_scale=0.0,
    )

    blocked = scan_opportunities([context], config, _FakeQuotes(), _allocator(tmp_path / "a"))
    assert blocked[0].probability_source == "forecast_state"
    assert "forecast_probability_uncalibrated" in blocked[0].blockers

    proven = scan_opportunities([context], config, _FakeQuotes(), _allocator(tmp_path / "b"), forecast_calibrated=True)
    assert "forecast_probability_uncalibrated" not in proven[0].blockers
    assert not proven[0].blockers


def test_forecast_gate_can_be_disabled_explicitly(tmp_path) -> None:
    context = _graded(_binary_event())
    root = tmp_path / "geo"
    _write_forecast_state(root, context.market_id, {"yes": 0.70}, datetime.now(timezone.utc).isoformat())
    config = OpportunityConfig(
        forecast_data_root=str(root), model_weight=1.0, disagreement_buffer_scale=0.0, require_calibrated_forecast=False
    )
    results = scan_opportunities([context], config, _FakeQuotes(), _allocator(tmp_path / "a"))
    assert "forecast_probability_uncalibrated" not in results[0].blockers


# ---- the calibration ledger and report ----


def _opportunity(market_id: str, outcome: str, probability: float, source: str, mid: float | None) -> Opportunity:
    return Opportunity(
        market_id=market_id,
        outcome=outcome,
        side="YES",
        estimated_probability=probability,
        probability_source=source,
        executable_price=0.40,
        spread=0.02,
        tradable_edge=0.0,
        detail={"market_mid": mid},
    )


def test_calibration_report_scores_sources_against_market(tmp_path) -> None:
    log = CalibrationLog(tmp_path)
    log.record_estimates(
        [
            _opportunity("m1", "yes", 0.80, "forecast_state", 0.60),
            _opportunity("m2", "yes", 0.30, "forecast_state", 0.50),
            _opportunity("m1", "yes", 0.55, "config_estimate", 0.60),
        ]
    )
    log.record_resolution("m1", "yes", True)
    log.record_resolution("m2", "yes", False)

    report = log.report(min_resolved=2)
    forecast = report["sources"]["forecast_state"]
    # Brier = ((0.8-1)^2 + (0.3-0)^2) / 2 = 0.065
    assert forecast["brier"] == pytest.approx(0.065)
    # Market mid Brier on the same rows = ((0.6-1)^2 + (0.5-0)^2) / 2 = 0.205
    assert forecast["market_brier"] == pytest.approx(0.205)
    assert forecast["beats_market"] is True
    assert report["forecast_calibrated"] is True
    # Status file drives the scan gate.
    assert log.forecast_calibrated() is True

    # The bar is evidence VOLUME too: same scores, higher n requirement.
    report = log.report(min_resolved=20)
    assert report["forecast_calibrated"] is False
    assert log.forecast_calibrated() is False


def test_calibration_uses_latest_estimate_per_outcome(tmp_path) -> None:
    log = CalibrationLog(tmp_path)
    log.record_estimates([_opportunity("m1", "yes", 0.20, "forecast_state", 0.50)])
    log.record_estimates([_opportunity("m1", "yes", 0.90, "forecast_state", 0.50)])  # final opinion
    log.record_resolution("m1", "yes", True)
    report = log.report(min_resolved=1)
    assert report["sources"]["forecast_state"]["brier"] == pytest.approx(round((0.90 - 1.0) ** 2, 4))


def test_calibration_buckets_show_realized_frequency(tmp_path) -> None:
    log = CalibrationLog(tmp_path)
    log.record_estimates(
        [
            _opportunity("m1", "yes", 0.65, "config_estimate", None),
            _opportunity("m2", "yes", 0.68, "config_estimate", None),
        ]
    )
    log.record_resolution("m1", "yes", True)
    log.record_resolution("m2", "yes", False)
    report = log.report(min_resolved=1)
    bucket = report["buckets"]["0.6-0.7"]
    assert bucket["n"] == 2
    assert bucket["mean_estimate"] == pytest.approx(0.665)
    assert bucket["realized_frequency"] == pytest.approx(0.5)


def test_forecast_calibrated_fails_closed_without_report(tmp_path) -> None:
    assert CalibrationLog(tmp_path).forecast_calibrated() is False


# ---- automatic resolution capture ----


def _closed_gamma_market(yes_price: float) -> dict:
    return {"closed": True, "outcomes": '["Yes", "No"]', "outcomePrices": f'["{yes_price}", "{1 - yes_price}"]'}


def test_capture_resolutions_records_and_closes_past_deadline_markets(tmp_path) -> None:
    from dataclasses import replace

    from polybot.discovery.calibration import capture_resolutions
    from polybot.discovery.store import DiscoveryStore

    store = DiscoveryStore(tmp_path / "data")
    context = _graded(_binary_event())
    past = replace(context, deadline_iso="2020-01-01T00:00:00Z")
    future = replace(_graded(_binary_event(slug="future")), market_id="0xfuture-m")
    store.save_context(past)
    store.save_context(future)

    calls: list[str] = []

    def markets_fetch(condition_id: str) -> dict:
        calls.append(condition_id)
        return _closed_gamma_market(1.0)

    log = CalibrationLog(tmp_path / "data")
    result = capture_resolutions(store, log, markets_fetch=markets_fetch)
    # Only the past-deadline market is queried; the future one is untouched.
    assert result["recorded"] == [{"market_id": past.market_id, "outcome": "yes", "resolved_yes": True}]
    assert result["closed_markets"] == [past.market_id]
    assert len(calls) == 1
    assert store.load_context(past.market_id).state == "CLOSED"
    assert store.load_context(future.market_id).state != "CLOSED"

    # Second pass is a no-op: resolution already recorded, context closed.
    result = capture_resolutions(store, log, markets_fetch=markets_fetch)
    assert result["recorded"] == [] and len(calls) == 1


def test_capture_resolutions_waits_for_gamma_finalization(tmp_path) -> None:
    from dataclasses import replace

    from polybot.discovery.calibration import capture_resolutions
    from polybot.discovery.store import DiscoveryStore

    store = DiscoveryStore(tmp_path / "data")
    past = replace(_graded(_binary_event()), deadline_iso="2020-01-01T00:00:00Z")
    store.save_context(past)
    log = CalibrationLog(tmp_path / "data")

    # Deadline passed but Gamma has not finalized: nothing recorded, context
    # stays open for the next cycle.
    result = capture_resolutions(store, log, markets_fetch=lambda _cid: {"closed": False})
    assert result["recorded"] == [] and result["closed_markets"] == []
    assert store.load_context(past.market_id).state != "CLOSED"

    result = capture_resolutions(store, log, markets_fetch=lambda _cid: _closed_gamma_market(0.0))
    assert result["recorded"] == [{"market_id": past.market_id, "outcome": "yes", "resolved_yes": False}]
    assert store.load_context(past.market_id).state == "CLOSED"


def test_resolved_yes_parses_gamma_price_formats() -> None:
    from polybot.discovery.calibration import _resolved_yes

    assert _resolved_yes(_closed_gamma_market(1.0)) is True
    assert _resolved_yes(_closed_gamma_market(0.0)) is False
    assert _resolved_yes({"closed": True, "outcomes": ["No", "Yes"], "outcomePrices": ["0", "1"]}) is True
    assert _resolved_yes({"closed": False, "outcomePrices": '["1", "0"]'}) is None
    assert _resolved_yes(None) is None
    assert _resolved_yes({"closed": True, "outcomePrices": "not-json"}) is None


# ---- CLI commands ----


def test_calibration_commands_roundtrip(tmp_path, capsys) -> None:
    from polybot.discovery.runner import calibration_report_command, record_resolution_command

    config_path = tmp_path / "discovery.yaml"
    config_path.write_text(
        f"""
classifier:
  provider: rule_based
opportunity:
  min_resolved_for_calibration: 1
data_dir: {tmp_path / 'data'}
logs_dir: {tmp_path / 'logs'}
""",
        encoding="utf-8",
    )
    log = CalibrationLog(tmp_path / "data")
    log.record_estimates([_opportunity("m1", "yes", 0.80, "forecast_state", 0.60)])

    assert record_resolution_command(config_path, "m1", "yes", "yes") == 0
    capsys.readouterr()
    assert calibration_report_command(config_path) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["forecast_calibrated"] is True
    assert (tmp_path / "data" / "calibration_status.json").exists()


def test_scan_command_records_estimates_for_calibration(tmp_path, capsys) -> None:
    from polybot.discovery.runner import discover_markets_command, grade_markets_command, scan_opportunities_command

    config_path = tmp_path / "discovery.yaml"
    binary_id = "0xiran-ceasefire-m"
    config_path.write_text(
        f"""
classifier:
  provider: rule_based
scoring:
  allow_fixture_analysis_live: true
opportunity:
  forecast_data_root: {tmp_path / 'forecast'}
  probability_estimates:
    "{binary_id}":
      "yes": 0.60
data_dir: {tmp_path / 'data'}
logs_dir: {tmp_path / 'logs'}
""",
        encoding="utf-8",
    )
    events = [_binary_event()]

    def fetch(url: str, params: dict) -> list[dict]:
        return events if params.get("offset", 0) == 0 else []

    assert discover_markets_command(config_path, events_fetch=fetch) == 0
    assert grade_markets_command(config_path) == 0
    assert scan_opportunities_command(config_path, quotes=_FakeQuotes()) == 0
    capsys.readouterr()
    lines = (tmp_path / "data" / "calibration_estimates.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["market_id"] == binary_id
    assert record["probability"] == pytest.approx(0.60)
    assert record["market_mid"] == pytest.approx(0.39)
