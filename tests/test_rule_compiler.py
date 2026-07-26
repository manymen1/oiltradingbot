from __future__ import annotations

import json
from pathlib import Path

from polybot.core.budget import ClassifierBudgetStore
from polybot.core.config import ClassifierConfig
from polybot.rules.compiler import RuleCompiler, fixture_semantics
from polybot.rules.store import RuleStore
from test_rule_contracts import _golden_rules, context_for_case


def _envelope(payload: dict) -> str:
    return json.dumps(
        {
            "type": "result",
            "is_error": False,
            "structured_output": payload,
        }
    )


def test_two_pass_compiler_binds_instrument_and_caches(tmp_path: Path) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    semantic = fixture_semantics(context).as_dict()
    calls: list[str] = []

    def runner(prompt: str) -> str:
        calls.append(prompt)
        return _envelope(semantic)

    store = RuleStore(tmp_path / "rules.sqlite3")
    budget_path = tmp_path / "budget.sqlite3"
    budget = ClassifierBudgetStore(tmp_path, budget_path)
    limits = ClassifierConfig(
        provider="claude_cli",
        max_escalations_per_hour=2,
        max_escalations_per_day=2,
    )
    compiler = RuleCompiler(
        limits,
        store,
        budget_store=budget,
        budget_limits=limits,
        cli_runner=runner,
    )
    result = compiler.compile(context)
    assert result.status == "COMPILED"
    assert result.calls_reserved == 2
    assert result.spec is not None
    assert result.spec.outcomes[0].yes_token_id == context.outcomes[0].yes_token_id
    assert len(calls) == 2

    cached = compiler.compile(context)
    assert cached.status == "CACHED"
    assert cached.cached is True
    assert len(calls) == 2
    assert budget.status(limits)["attempts_this_hour"] == 2


def test_two_pass_disagreement_fails_closed_and_retains_diagnostics(
    tmp_path: Path,
) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    base = fixture_semantics(context).as_dict()

    def runner(prompt: str) -> str:
        payload = json.loads(json.dumps(base))
        if "pass: 2 of 2" in prompt:
            payload["qualifying_conditions"] = ["different interpretation"]
        return _envelope(payload)

    store = RuleStore(tmp_path / "rules.sqlite3")
    compiler = RuleCompiler(
        ClassifierConfig(provider="claude_cli"),
        store,
        cli_runner=runner,
    )
    result = compiler.compile(context)
    assert result.status == "DISAGREEMENT"
    assert result.spec is None
    assert store.load_spec(context.market_id, context.rule_text_sha256) is None
    passes = store.compilation_passes(
        context.market_id,
        context.rule_text_sha256,
    )
    assert len(passes) == 2
    assert passes[0]["output_sha256"] != passes[1]["output_sha256"]


def test_compiler_reserves_both_passes_atomically(tmp_path: Path) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    calls = {"count": 0}

    def runner(prompt: str) -> str:
        calls["count"] += 1
        return _envelope(fixture_semantics(context).as_dict())

    limits = ClassifierConfig(
        provider="claude_cli",
        max_escalations_per_hour=1,
        max_escalations_per_day=1,
    )
    budget = ClassifierBudgetStore(
        tmp_path,
        tmp_path / "budget.sqlite3",
    )
    compiler = RuleCompiler(
        limits,
        RuleStore(tmp_path / "rules.sqlite3"),
        budget_store=budget,
        budget_limits=limits,
        cli_runner=runner,
    )
    result = compiler.compile(context)
    assert result.status == "BUDGET_BLOCKED"
    assert calls["count"] == 0
    assert budget.status(limits)["attempts_this_hour"] == 0


def test_model_cannot_inject_market_or_trade_fields(tmp_path: Path) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    payload = fixture_semantics(context).as_dict()
    payload["market_id"] = "attacker-market"
    payload["trade_action"] = "BUY_YES"
    compiler = RuleCompiler(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "rules.sqlite3"),
        cli_runner=lambda prompt: _envelope(payload),
    )
    result = compiler.compile(context)
    assert result.status == "INVALID"
    assert "unknown keys" in result.reason


def test_fixture_compiler_is_two_pass_but_cost_free(tmp_path: Path) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    store = RuleStore(tmp_path / "rules.sqlite3")
    result = RuleCompiler(
        ClassifierConfig(provider="rule_based"),
        store,
    ).compile(context)
    assert result.status == "COMPILED"
    assert result.calls_reserved == 0
    assert len(
        store.compilation_passes(
            context.market_id,
            context.rule_text_sha256,
        )
    ) == 2
