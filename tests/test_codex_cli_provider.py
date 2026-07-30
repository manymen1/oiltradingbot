from __future__ import annotations

import json
from dataclasses import replace

import pytest

from polybot.core.codex_cli import (
    extract_codex_cli_result,
    is_codex_cli_provider,
)
from test_binary_bot import _config as _binary_config, article


def _signal() -> dict[str, object]:
    return {
        "source_is_trusted": True,
        "source_tier": "wire",
        "qualifies_under_rules": True,
        "event_status": "scheduled",
        "evidence_strength": "confirmed_scheduled",
        "before_deadline": True,
        "resolves_no": False,
        "level": "4A",
        "quote_supporting_trigger": "The round will begin next week.",
        "final_decision_announced": True,
    }


def test_codex_result_extraction_and_provider_aliases() -> None:
    assert json.loads(extract_codex_cli_result(json.dumps({"ok": True}))) == {
        "ok": True
    }
    assert extract_codex_cli_result("plain output") == "plain output"
    with pytest.raises(RuntimeError, match="no result text"):
        extract_codex_cli_result(" ")
    assert is_codex_cli_provider("codex_cli")
    assert is_codex_cli_provider("Codex-CLI")
    assert is_codex_cli_provider("codex")
    assert not is_codex_cli_provider("anthropic")


def test_binary_classifier_codex_cli_provider() -> None:
    from polybot.binary.classifier import LLMBinaryClassifier

    config = _binary_config()
    classifier = LLMBinaryClassifier(
        replace(
            config.classifier,
            provider="codex_cli",
            model="gpt-5.5",
            cli_binary="codex",
        ),
        config,
        cli_runner=lambda prompt: json.dumps(_signal()),
    )

    signal = classifier.classify(
        article("The round will begin next week."),
        "test rules",
        held_side="",
    )

    assert signal.qualifies_under_rules is True
    assert signal.level == "4A"


def test_binary_classifier_codex_failure_without_api_key_fails_closed(
    monkeypatch,
) -> None:
    from polybot.binary.classifier import LLMBinaryClassifier

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    config = _binary_config()
    classifier = LLMBinaryClassifier(
        replace(config.classifier, provider="codex_cli"),
        config,
        cli_runner=lambda _prompt: (_ for _ in ()).throw(
            RuntimeError("codex not logged in")
        ),
    )

    with pytest.raises(RuntimeError, match="not logged in"):
        classifier.classify(article("news"), "test rules", held_side="")


def test_operator_gate_checks_codex_binary(monkeypatch, tmp_path) -> None:
    from polybot.core.operator import OperatorGate

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    config = _binary_config(data_dir=tmp_path / "data")
    config = replace(
        config,
        classifier=replace(
            config.classifier,
            provider="codex_cli",
            cli_binary="codex",
        ),
    )
    config_path = tmp_path / "bot.yaml"
    config_path.write_text("binary-config\n", encoding="utf-8")
    gate = OperatorGate(config_path, config)

    monkeypatch.setattr(
        "polybot.core.codex_cli.codex_cli_available",
        lambda binary="codex": False,
    )
    assert "codex_cli_not_installed" in gate.status(
        live_requested=True
    ).blockers

    monkeypatch.setattr(
        "polybot.core.codex_cli.codex_cli_available",
        lambda binary="codex": True,
    )
    assert "codex_cli_not_installed" not in gate.status(
        live_requested=True
    ).blockers


def test_emitted_config_inherits_full_codex_transport(tmp_path) -> None:
    from polybot.binary.config import load_binary_config
    from polybot.discovery.emit import emit_bot_config
    from polybot.discovery.sources import build_source_plan
    from test_discovery import _binary_event, _graded

    context = _graded(_binary_event())
    plan = build_source_plan(context)
    out = tmp_path / "bot.yaml"
    emit_bot_config(
        context,
        plan,
        entry_usd=25.0,
        out_path=out,
        classifier_provider="codex_cli",
        classifier_model="gpt-5.5",
        classifier_cli_binary="codex",
        classifier_cli_timeout_seconds=90,
    )

    loaded = load_binary_config(out)
    assert loaded.classifier.provider == "codex_cli"
    assert loaded.classifier.model == "gpt-5.5"
    assert loaded.classifier.cli_binary == "codex"
    assert loaded.classifier.cli_timeout_seconds == 90
    assert loaded.classifier.screen_model == ""
