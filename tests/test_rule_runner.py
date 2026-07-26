from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from polybot.core.config import ClassifierConfig
from polybot.discovery.config import (
    DiscoveryConfig,
    RuleCompilerConfig,
    RuleRunnerConfig,
    load_discovery_config,
)
from polybot.discovery.fleet import FleetManager
from polybot.discovery.sources import build_source_plan
from polybot.discovery.store import DiscoveryStore
from polybot.rules.compiler import fixture_semantics
from polybot.rules.evidence import EvidenceExtractor
from polybot.rules.runner import (
    GenericRuleMarketRunner,
    run_generic_rule_market_command,
)
from polybot.rules.store import RuleStore
from test_rule_contracts import _golden_rules, context_for_case
from test_rule_evidence import (
    _article,
    _paper_adapter,
    _portfolio,
)


class _ForwardRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def start(self) -> None:
        self.calls.append(("start", None))

    def stop(self, *, reason: str) -> None:
        self.calls.append(("stop", reason))

    def record_article(self, article, *, observed_at: str, cycle_id: str) -> None:
        self.calls.append(("article", article.hash))

    def record_extraction(
        self,
        article_id: str,
        *,
        started_at: str,
        completed_at: str,
        duration_ms: float,
        result: dict,
    ) -> None:
        self.calls.append(("extraction", article_id))

    def record_decision(self, evaluation, proof, *, observed_at: str) -> None:
        self.calls.append(("decision", proof.proof_sha256))

    def record_cycle(self, summary: dict, *, observed_at: str) -> None:
        self.calls.append(("cycle", summary["status"]))


def _runner_config(tmp_path: Path) -> DiscoveryConfig:
    base = DiscoveryConfig()
    return replace(
        base,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        classifier=ClassifierConfig(provider="rule_based"),
        rule_compiler=RuleCompilerConfig(enabled=True),
        rule_runner=RuleRunnerConfig(
            enabled=True,
            paper_execution_families=[
                "OCCURRENCE_BEFORE_DEADLINE",
                "CATEGORICAL_EXCLUSIVE",
                "SOURCE_LOCKED_ANNOUNCEMENT",
            ],
        ),
    )


def test_generic_runner_extracts_evaluates_executes_and_dedupes(
    tmp_path: Path,
) -> None:
    case = next(
        item
        for item in _golden_rules()
        if item["expected_family"] == "OCCURRENCE_BEFORE_DEADLINE"
    )
    context = replace(
        context_for_case(case, strong_analysis=True),
        state="PAPER_ELIGIBLE",
    )
    from polybot.rules.contracts import RuleSpec

    spec = RuleSpec.from_context(
        context,
        fixture_semantics(context),
        compiler_model="fixture",
        compiled_at="2026-07-25T00:00:00+00:00",
    )
    plan = build_source_plan(context, spec)
    store = RuleStore(tmp_path / "rules.sqlite3")
    store.save_spec(spec)
    adapter = _paper_adapter(tmp_path, spec)
    forward = _ForwardRecorder()
    runner = GenericRuleMarketRunner(
        config=_runner_config(tmp_path),
        context=context,
        spec=spec,
        source_plan=plan,
        rule_store=store,
        extractor=EvidenceExtractor(
            ClassifierConfig(provider="rule_based"),
            store,
            passes=2,
        ),
        feed_reader=None,
        promotion_cache=None,
        adapter=adapter,
        portfolio=_portfolio(tmp_path, context),
        data_dir=tmp_path / "runner",
        forward_recorder=forward,
    )
    articles = [
        _article(
            "reuters-1",
            "Both senior delegations entered the room and talks began.",
            domain="reuters.com",
        ),
        _article(
            "ap-1",
            "Both senior delegations entered the room and talks began.",
            domain="apnews.com",
        ),
    ]
    runner.start()
    first = runner.run_once(
        articles=articles,
        as_of=datetime(2026, 7, 25, 0, 2, tzinfo=timezone.utc),
    )
    assert first["status"] == "EVALUATED"
    assert first["claims"] == 2
    assert first["states"][0]["evidence_state"] == "TERMINAL_YES"
    assert first["actions"][0]["action"] == "ENTER_YES"
    assert first["executed"] == 1
    assert len(store.proofs_for_market(context.market_id)) == 1
    timing = store.proofs_for_market(context.market_id)[0].execution_result[
        "_timing"
    ]
    assert set(timing) == {
        "decision_started_at",
        "decision_completed_at",
        "submission_started_at",
        "submission_completed_at",
        "paper_only",
    }
    assert timing["paper_only"] is True

    second = runner.run_once(
        articles=articles,
        as_of=datetime(2026, 7, 25, 0, 3, tzinfo=timezone.utc),
    )
    assert second["actions"][0]["action"] == "HOLD"
    assert second["executed"] == 0
    assert len(store.extraction_passes(spec.spec_sha256, "reuters-1")) == 2

    third = runner.run_once(
        articles=articles,
        as_of=datetime(2026, 7, 25, 0, 4, tzinfo=timezone.utc),
    )
    assert third["status"] == "NO_CHANGE"
    assert third["proofs"] == 0
    runner.stop(reason="test_complete")
    assert forward.calls[0] == ("start", None)
    assert forward.calls[-1] == ("stop", "test_complete")
    assert sum(kind == "decision" for kind, _value in forward.calls) == 2
    assert [value for kind, value in forward.calls if kind == "cycle"] == [
        "EVALUATED",
        "EVALUATED",
        "NO_CHANGE",
    ]


def test_generic_runner_live_flag_is_structurally_forbidden(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit, match="paper-only"):
        run_generic_rule_market_command(
            tmp_path / "missing.yaml",
            "market",
            live_flag=True,
        )


def test_paper_fleet_routes_to_generic_runner_but_live_stays_legacy(
    tmp_path: Path,
) -> None:
    config = _runner_config(tmp_path)
    store = DiscoveryStore(config.data_dir)
    case = next(
        item
        for item in _golden_rules()
        if item["expected_family"] == "OCCURRENCE_BEFORE_DEADLINE"
    )
    context = replace(
        context_for_case(case, strong_analysis=True),
        state="PAPER_ELIGIBLE",
    )
    config_path = tmp_path / "discovery.yaml"
    paper = FleetManager(
        config,
        store,
        live=False,
        per_order_usd=50.0,
        ledger_path=str(tmp_path / "ledger.json"),
        config_path=config_path,
    )
    command = paper._command(context, tmp_path / "generated.yaml")
    assert "run-rule-market" in command
    assert str(config_path) in command
    assert "--live" not in command

    live = FleetManager(
        config,
        store,
        live=True,
        per_order_usd=50.0,
        ledger_path=str(tmp_path / "ledger.json"),
        config_path=config_path,
    )
    command = live._command(context, tmp_path / "generated.yaml")
    assert "run-binary" in command
    assert "run-rule-market" not in command
    assert "--live" in command


def test_rule_runner_config_rejects_unsafe_family_and_dependency(
    tmp_path: Path,
) -> None:
    no_compiler = tmp_path / "no-compiler.yaml"
    no_compiler.write_text(
        """
rule_runner:
  enabled: true
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requires rule_compiler"):
        load_discovery_config(no_compiler)

    subjective = tmp_path / "subjective.yaml"
    subjective.write_text(
        """
rule_compiler:
  enabled: true
  paper_families:
    - SUBJECTIVE_DISCRETIONARY
rule_runner:
  enabled: true
  paper_execution_families:
    - SUBJECTIVE_DISCRETIONARY
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="SUBJECTIVE_DISCRETIONARY"):
        load_discovery_config(subjective)

    one_pass = tmp_path / "one-pass.yaml"
    one_pass.write_text(
        """
rule_compiler:
  enabled: true
rule_runner:
  enabled: true
  extraction_passes: 1
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="extraction_passes"):
        load_discovery_config(one_pass)
