from __future__ import annotations

from pathlib import Path

from polybot.gamma import MarketMeta
from polybot.iran.config import ClassifierConfig, ExecutionConfig, IranBotConfig, MarketConfig, SafetyConfig
from polybot.iran.decision import Decision
from polybot.iran.executor import DryRunTradingAdapter
from polybot.iran.operator import OperatorGate, build_preflight


def test_live_preflight_defaults_to_blocked_until_mode_and_ack(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    config_path = tmp_path / "iran-july17-yes-protection.yaml"
    config_path.write_text("market:\n  slug: iran-event\nexecution:\n  dry_run: false\n", encoding="utf-8")
    cfg = IranBotConfig(
        market=MarketConfig(slug="iran-event", held_side="YES"),
        classifier=ClassifierConfig(passes=2, require_pass_agreement=True),
        execution=ExecutionConfig(dry_run=False),
        data_dir=tmp_path / "data" / "iran-protection-bot",
        logs_dir=tmp_path / "logs",
    )
    gate = OperatorGate(config_path, cfg)

    blocked = build_preflight(
        config_path=config_path,
        config=cfg,
        market=_market(),
        adapter=DryRunTradingAdapter(),
        live_requested=True,
        gate=gate,
    )

    assert blocked["status"] == "blocked"
    assert "operator_mode_alert_only" in blocked["operator"]["blockers"]
    assert "live_config_hash_not_acknowledged" in blocked["operator"]["blockers"]

    gate.set_position_mode("live")
    gate.write_ack(note="test")
    allowed = build_preflight(
        config_path=config_path,
        config=cfg,
        market=_market(),
        adapter=DryRunTradingAdapter(),
        live_requested=True,
        gate=gate,
    )

    assert allowed["status"] == "ok"
    assert allowed["operator"]["effective_mode"] == "live"
    assert allowed["operator"]["config_acknowledged"] is True


def test_live_preflight_blocks_missing_required_integrations(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    config_path = tmp_path / "iran.yaml"
    config_path.write_text("market:\n  slug: iran-event\n", encoding="utf-8")
    cfg = IranBotConfig(
        market=MarketConfig(slug="iran-event"),
        classifier=ClassifierConfig(provider="anthropic", passes=2, require_pass_agreement=True),
        execution=ExecutionConfig(dry_run=False),
        safety=SafetyConfig(degraded_mode_alert=True),
        data_dir=tmp_path / "data" / "iran-protection-bot",
    )
    gate = OperatorGate(config_path, cfg)
    gate.set_position_mode("live")
    gate.write_ack(note="test")

    status = gate.status(live_requested=True)

    assert "telegram_not_configured" in status.blockers
    assert "anthropic_not_configured" in status.blockers
    assert "telegram_not_configured" not in status.warnings
    assert "anthropic_not_configured" not in status.warnings


def test_live_preflight_blocks_single_classifier_pass(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    config_path = tmp_path / "iran.yaml"
    config_path.write_text("execution:\n  dry_run: false\n", encoding="utf-8")
    cfg = IranBotConfig(
        market=MarketConfig(slug="iran-event"),
        classifier=ClassifierConfig(passes=1, require_pass_agreement=False),
        execution=ExecutionConfig(dry_run=False),
        data_dir=tmp_path / "data" / "iran-protection-bot",
    )
    gate = OperatorGate(config_path, cfg)
    gate.set_position_mode("live")
    gate.write_ack(note="test")

    status = gate.status(live_requested=True)

    assert "live_classifier_requires_two_passes" in status.blockers
    assert "live_classifier_requires_pass_agreement" in status.blockers


def test_telegram_id_alias_counts_as_configured(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_ID", "chat")

    from polybot.iran.operator import _telegram_configured

    assert _telegram_configured() is True


def test_read_only_status_keeps_missing_integrations_as_warnings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    config_path = tmp_path / "iran.yaml"
    config_path.write_text("market:\n  slug: iran-event\n", encoding="utf-8")
    cfg = IranBotConfig(
        market=MarketConfig(slug="iran-event"),
        classifier=ClassifierConfig(provider="anthropic"),
        data_dir=tmp_path / "data" / "iran-protection-bot",
    )

    status = OperatorGate(config_path, cfg).status(live_requested=False)

    assert status.blockers == []
    assert "telegram_not_configured" in status.warnings
    assert "anthropic_not_configured" in status.warnings


def test_operator_gate_blocks_trade_decision_in_alert_only_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "iran.yaml"
    config_path.write_text("market:\n  slug: iran-event\n", encoding="utf-8")
    cfg = IranBotConfig(market=MarketConfig(slug="iran-event"), data_dir=tmp_path / "data" / "iran-protection-bot")
    gate = OperatorGate(config_path, cfg)

    result = gate.check(Decision("EXIT_YES_ONLY", "4B", "test"), live_requested=True)

    assert result.allowed is False
    assert result.mode == "alert_only"
    assert result.reason == "operator_mode_alert_only"


def test_operator_dry_run_mode_allows_non_live_execution_only(tmp_path: Path) -> None:
    config_path = tmp_path / "iran.yaml"
    config_path.write_text("market:\n  slug: iran-event\n", encoding="utf-8")
    cfg = IranBotConfig(market=MarketConfig(slug="iran-event"), data_dir=tmp_path / "data" / "iran-protection-bot")
    gate = OperatorGate(config_path, cfg)
    gate.set_position_mode("dry_run")

    dry_result = gate.check(Decision("EXIT_YES_ONLY", "4B", "test"), live_requested=False)
    live_result = gate.check(Decision("EXIT_YES_ONLY", "4B", "test"), live_requested=True)

    assert dry_result.allowed is True
    assert dry_result.reason == "operator_allows_dry_run_execution"
    assert live_result.allowed is False
    assert live_result.reason == "operator_mode_dry_run"


def test_operator_gate_combines_global_off_with_position_live(tmp_path: Path) -> None:
    config_path = tmp_path / "iran.yaml"
    config_path.write_text("market:\n  slug: iran-event\n", encoding="utf-8")
    cfg = IranBotConfig(market=MarketConfig(slug="iran-event"), data_dir=tmp_path / "data" / "iran-protection-bot")
    gate = OperatorGate(config_path, cfg)
    gate.set_position_mode("live")
    global_mode = tmp_path / "data" / "operator" / "global_mode.json"
    global_mode.parent.mkdir(parents=True, exist_ok=True)
    global_mode.write_text('{"mode":"off"}\n', encoding="utf-8")

    status = gate.status(live_requested=True)

    assert status.global_mode == "off"
    assert status.position_mode == "live"
    assert status.effective_mode == "off"
    assert "operator_mode_off" in status.blockers


def _market() -> MarketMeta:
    return MarketMeta(
        event_slug="iran-event",
        market_slug="july-17",
        condition_id="cond",
        question="Will the next round of US-Iran peace talks happen by July 17?",
        description="rules",
        resolution_source="source",
        outcomes=["Yes", "No"],
        outcome_prices=[0.5, 0.5],
        yes_token_id="yes",
        no_token_id="no",
        tick_size="0.01",
        neg_risk=False,
        active=True,
        closed=False,
        accepting_orders=True,
        volume=1,
        liquidity=1,
    )
