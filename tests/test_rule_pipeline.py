from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from polybot.discovery.config import (
    ScoringConfig,
    load_discovery_config,
    rule_store_db_path,
)
from polybot.discovery.runner import (
    compile_rules_command,
    emit_bot_config_command,
    grade_markets_command,
    inspect_rule_command,
    plan_sources_command,
    validate_rule_command,
)
from polybot.discovery.sources import build_source_plan
from polybot.discovery.scorer import grade_market
from polybot.discovery.store import DiscoveryStore
from polybot.discovery.types import MarketContext
from polybot.core.config import ClassifierConfig
from polybot.rules.compiler import (
    CompilationResult,
    RuleCompiler,
    fixture_semantics,
    rule_compilation_blocker,
)
from polybot.rules.contracts import (
    RuleSpec,
    STRICT_DEADLINE_AUTHORITY,
    VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY,
)
from polybot.rules.store import RuleStore
from test_rule_contracts import _golden_rules, context_for_case


def _config(
    tmp_path: Path,
    *,
    max_per_cycle: int = 10,
    priority_market_ids: list[str] | None = None,
    deadline_authority_policy: str = STRICT_DEADLINE_AUTHORITY,
    deadline_authority_market_ids: list[str] | None = None,
) -> Path:
    path = tmp_path / "discovery.yaml"
    priority_yaml = (
        "\n" + "\n".join(
            f"    - {market_id}"
            for market_id in priority_market_ids
        )
        if priority_market_ids
        else " []"
    )
    deadline_market_yaml = (
        "\n" + "\n".join(
            f"    - {market_id}"
            for market_id in deadline_authority_market_ids
        )
        if deadline_authority_market_ids
        else " []"
    )
    path.write_text(
        f"""
data_dir: {tmp_path / "data"}
logs_dir: {tmp_path / "logs"}
classifier:
  provider: rule_based
classifier_budget:
  enabled: false
rule_compiler:
  enabled: true
  max_per_cycle: {max_per_cycle}
  priority_market_ids:{priority_yaml}
  deadline_authority_policy: {deadline_authority_policy}
  deadline_authority_market_ids:{deadline_market_yaml}
  db_path: {tmp_path / "rules.sqlite3"}
  paper_families:
    - OCCURRENCE_BEFORE_DEADLINE
    - CATEGORICAL_EXCLUSIVE
    - SOURCE_LOCKED_ANNOUNCEMENT
    - STATUS_AT_DEADLINE
    - NUMERIC_THRESHOLD
    - DURATION_REQUIREMENT
  live_confirmation_families: []
scoring:
  min_rule_text_chars: 1
  allow_fixture_analysis_live: true
""",
        encoding="utf-8",
    )
    return path


def test_explicit_priority_gets_first_attempt_without_starving_unattempted(
    tmp_path: Path,
    capsys,
) -> None:
    contexts = [
        context_for_case(case, strong_analysis=True)
        for case in _golden_rules()[:3]
    ]
    priority_id = contexts[2].market_id
    config_path = _config(
        tmp_path,
        max_per_cycle=1,
        priority_market_ids=[priority_id],
    )
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    for context in contexts:
        store.save_context(context)

    assert compile_rules_command(config_path) == 0
    first = json.loads(capsys.readouterr().out)
    compiled = [
        item["market_id"]
        for item in first["results"]
        if item["status"] in {"COMPILED", "CACHED"}
    ]
    assert compiled == [priority_id]

    # Once attempted, the pin no longer outranks never-attempted work.
    assert compile_rules_command(config_path) == 0
    second = json.loads(capsys.readouterr().out)
    assert any(
        item["market_id"] != priority_id
        and item["status"] == "COMPILED"
        for item in second["results"]
    )


def test_unsupported_multi_outcome_topology_skips_model_calls(
    tmp_path: Path,
    capsys,
) -> None:
    context = replace(
        context_for_case(_golden_rules()[0], strong_analysis=True),
        outcome_topology="TOP_K",
    )
    config_path = _config(tmp_path)
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    store.save_context(context)

    assert (
        rule_compilation_blocker(context)
        == "unsupported_rule_topology:TOP_K"
    )
    assert compile_rules_command(config_path) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["compiled"] == 0
    assert summary["failed"] == 1
    assert summary["results"][0]["status"] == "UNSUPPORTED"
    assert (
        RuleStore(rule_store_db_path(config)).compilation_passes(
            context.market_id,
            context.rule_text_sha256,
        )
        == []
    )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda outcomes: [
                replace(outcomes[0], deadline_iso=""),
                outcomes[1],
            ],
            "outcome_deadline_missing:yes",
        ),
        (
            lambda outcomes: [
                replace(outcomes[0], rule_text_sha256="f" * 64),
                outcomes[1],
            ],
            "outcome_rule_text_mismatch",
        ),
        (
            lambda outcomes: [
                replace(
                    outcomes[0],
                    deadline_consistency="MISMATCH",
                ),
                outcomes[1],
            ],
            "outcome_deadline_mismatch:yes",
        ),
        (
            lambda outcomes: [
                replace(
                    outcomes[0],
                    resolution_source="https://one.example/result",
                ),
                replace(
                    outcomes[1],
                    resolution_source="https://two.example/result",
                ),
            ],
            "outcome_resolution_source_mismatch",
        ),
    ],
)
def test_multi_outcome_compilation_preflight_fails_closed(
    mutation,
    expected: str,
) -> None:
    case = next(
        item
        for item in _golden_rules()
        if item["kind"] == "grouped"
    )
    context = replace(
        context_for_case(case, strong_analysis=True),
        outcome_topology="INDEPENDENT_MULTI",
    )
    context = replace(context, outcomes=mutation(context.outcomes))
    assert rule_compilation_blocker(context) == expected


def test_paper_deadline_authority_requires_an_exact_rule_clock() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    mismatch = replace(
        context,
        outcomes=[
            replace(
                context.outcomes[0],
                deadline_consistency="MISMATCH",
                rule_deadline_iso="2026-12-31T23:59:00-05:00",
                deadline_timezone="America/New_York",
            )
        ],
    )

    assert rule_compilation_blocker(mismatch) == (
        "outcome_deadline_mismatch:yes"
    )
    assert rule_compilation_blocker(
        mismatch,
        deadline_authority_policy=(
            VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY
        ),
    ) == ""

    incomplete = replace(
        mismatch,
        outcomes=[
            replace(mismatch.outcomes[0], deadline_timezone="")
        ],
    )
    assert rule_compilation_blocker(
        incomplete,
        deadline_authority_policy=(
            VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY
        ),
    ) == "outcome_rule_deadline_missing:yes"


def test_allowlisted_rule_deadline_compiles_but_remains_paper_only(
    tmp_path: Path,
    capsys,
) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    gamma_deadline = context.outcomes[0].deadline_iso
    rule_deadline = "2026-12-31T23:59:00-05:00"
    context = replace(
        context,
        outcomes=[
            replace(
                context.outcomes[0],
                deadline_consistency="MISMATCH",
                rule_deadline_iso=rule_deadline,
                deadline_timezone="America/New_York",
            )
        ],
    )
    config_path = _config(
        tmp_path,
        priority_market_ids=[context.market_id],
        deadline_authority_policy=(
            VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY
        ),
        deadline_authority_market_ids=[context.market_id],
    )
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    store.save_context(context)

    assert compile_rules_command(config_path) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["compiled"] == 1
    spec = RuleStore(rule_store_db_path(config)).load_spec(
        context.market_id,
        context.rule_text_sha256,
    )
    assert spec is not None
    assert (
        spec.deadline_authority_policy
        == VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY
    )
    assert spec.outcomes[0].deadline_authority == "VERBATIM_RULES"
    assert spec.outcomes[0].deadline_iso == rule_deadline
    assert spec.outcomes[0].rule_deadline_iso == rule_deadline
    assert spec.outcomes[0].gamma_deadline_iso == gamma_deadline
    assert spec.outcomes[0].deadline_consistency == "MISMATCH"
    tampered = spec.as_dict()
    tampered["outcomes"][0]["deadline_iso"] = gamma_deadline
    with pytest.raises(
        ValueError,
        match="semantic deadline does not match verbatim rule deadline",
    ):
        RuleSpec.from_dict(tampered)

    plan = build_source_plan(context, spec)
    graded = grade_market(
        context,
        ScoringConfig(
            allow_fixture_analysis_live=True,
            min_rule_text_chars=1,
        ),
        rule_spec=spec,
        source_plan=plan,
        require_rule_spec=True,
        paper_families={spec.semantics.rule_family},
        live_confirmation_families={spec.semantics.rule_family},
    )
    assert graded.state == "PAPER_ELIGIBLE"
    assert "gamma_rule_deadline_mismatch_paper_only:yes" in (
        graded.state_reasons
    )


def test_deadline_authority_config_is_explicit_and_scoped(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="cannot define market overrides"):
        load_discovery_config(
            _config(
                tmp_path,
                priority_market_ids=["market-a"],
                deadline_authority_market_ids=["market-a"],
            )
        )

    with pytest.raises(ValueError, match="must also be explicit priority"):
        load_discovery_config(
            _config(
                tmp_path,
                priority_market_ids=["market-a"],
                deadline_authority_policy=(
                    VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY
                ),
                deadline_authority_market_ids=["market-b"],
            )
        )


def test_compile_cycle_caps_only_new_specs_and_cached_specs_are_free(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _config(tmp_path, max_per_cycle=1)
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    contexts = [
        context_for_case(case, strong_analysis=True)
        for case in _golden_rules()[:2]
    ]
    for context in contexts:
        store.save_context(context)

    assert compile_rules_command(config_path) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["compiled"] == 1
    assert first["deferred"] == 1
    deferred_id = next(
        item["market_id"]
        for item in first["results"]
        if item["status"] == "DEFERRED"
    )
    # Hitting a batch limit is scheduling, not a semantic failure: it must
    # not mutate the market's descriptive state.
    assert store.load_context(deferred_id).state == contexts[
        [item.market_id for item in contexts].index(deferred_id)
    ].state

    assert compile_rules_command(config_path) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["cached"] == 1
    assert second["compiled"] == 1
    assert second["deferred"] == 0


def test_compile_cycle_rotates_failed_markets_behind_unattempted_markets(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _config(tmp_path, max_per_cycle=1)
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    contexts = [
        context_for_case(case, strong_analysis=True)
        for case in _golden_rules()[:3]
    ]
    for context in contexts:
        store.save_context(context)
    invalid = fixture_semantics(contexts[0]).as_dict()
    invalid["window"]["end_iso"] = "not-an-iso-date"
    compiler = RuleCompiler(
        ClassifierConfig(provider="codex_cli"),
        RuleStore(rule_store_db_path(config)),
        cli_runner=lambda _prompt: json.dumps(invalid),
    )
    market_ids = {context.market_id for context in contexts}

    compile_rules_command(
        config_path,
        compiler=compiler,
        market_ids=market_ids,
    )
    first = json.loads(capsys.readouterr().out)
    first_attempted = next(
        item["market_id"]
        for item in first["results"]
        if item["status"] != "DEFERRED"
    )

    compile_rules_command(
        config_path,
        compiler=compiler,
        market_ids=market_ids,
    )
    second = json.loads(capsys.readouterr().out)
    second_attempted = next(
        item["market_id"]
        for item in second["results"]
        if item["status"] != "DEFERRED"
    )

    assert second_attempted != first_attempted
    assert next(
        item
        for item in second["results"]
        if item["market_id"] == second_attempted
    )["prior_compilation_passes"] == 0


def test_compile_cycle_stops_after_transport_unavailable(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _config(tmp_path)
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    contexts = [
        replace(
            context_for_case(case, strong_analysis=True),
            state="DISCOVERED",
        )
        for case in _golden_rules()[:3]
    ]
    for context in contexts:
        store.save_context(context)

    class UnavailableCompiler:
        def __init__(self):
            self.calls = 0

        def compile(self, context, **_kwargs):
            self.calls += 1
            return CompilationResult(
                market_id=context.market_id,
                status="UNAVAILABLE",
                reason="codex CLI exited 1: 401 Unauthorized",
            )

    compiler = UnavailableCompiler()
    compile_rules_command(
        config_path,
        compiler=compiler,
        market_ids={context.market_id for context in contexts},
    )
    result = json.loads(capsys.readouterr().out)

    assert compiler.calls == 1
    assert result["failed"] == 1
    assert result["deferred"] == 2
    assert {
        item["selection_reason"]
        for item in result["results"]
        if item["status"] == "DEFERRED"
    } == {"compiler_unavailable_circuit_breaker"}
    assert all(
        store.load_context(context.market_id).state == "DISCOVERED"
        for context in contexts
    )


def test_semantic_pregrade_is_scoped_and_does_not_authorize_without_assets(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _config(tmp_path)
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    selected = replace(
        context_for_case(_golden_rules()[0], strong_analysis=True),
        state="DISCOVERED",
    )
    untouched = replace(
        context_for_case(_golden_rules()[1], strong_analysis=True),
        state="MONITOR_ONLY",
        state_reasons=["manual_review"],
    )
    store.save_context(selected)
    store.save_context(untouched)

    grade_markets_command(
        config_path,
        require_semantic_assets=False,
        market_ids={selected.market_id},
    )
    capsys.readouterr()
    assert store.load_context(selected.market_id).state in {
        "PAPER_ELIGIBLE",
        "LIVE_CONFIRMATION_ELIGIBLE",
    }
    assert store.load_context(untouched.market_id).state == "MONITOR_ONLY"

    grade_markets_command(
        config_path,
        require_semantic_assets=True,
        market_ids={selected.market_id},
    )
    capsys.readouterr()
    strict = store.load_context(selected.market_id)
    assert strict.state == "RULES_REVIEW_REQUIRED"
    assert strict.state_reasons == ["valid_rule_spec_missing"]


def test_semantic_source_planning_is_scoped(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _config(tmp_path)
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    selected = replace(
        context_for_case(_golden_rules()[0], strong_analysis=True),
        state="DISCOVERED",
    )
    untouched = replace(
        context_for_case(_golden_rules()[1], strong_analysis=True),
        state="MONITOR_ONLY",
        state_reasons=["manual_review"],
    )
    store.save_context(selected)
    store.save_context(untouched)

    plan_sources_command(
        config_path,
        market_ids={selected.market_id},
    )
    capsys.readouterr()

    assert store.load_context(selected.market_id).state == (
        "RULES_REVIEW_REQUIRED"
    )
    assert store.load_context(untouched.market_id).state == "MONITOR_ONLY"
    assert store.load_context(untouched.market_id).state_reasons == [
        "manual_review"
    ]


def test_compile_plan_grade_roundtrip_is_paper_only(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _config(tmp_path)
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    store.save_context(context)

    compile_rules_command(config_path, market_id=context.market_id)
    capsys.readouterr()
    plan_sources_command(config_path, market_id=context.market_id)
    capsys.readouterr()
    grade_markets_command(config_path)
    capsys.readouterr()

    graded = store.load_context(context.market_id)
    assert graded is not None
    assert graded.state == "PAPER_ELIGIBLE"
    assert any(
        reason.startswith("rule_family_not_live_promoted")
        for reason in graded.state_reasons
    )
    plan = store.load_source_plan(context.market_id)
    spec = RuleStore(rule_store_db_path(config)).load_spec(
        context.market_id,
        context.rule_text_sha256,
    )
    assert plan is not None and spec is not None
    assert plan.rule_spec_sha256 == spec.spec_sha256


def test_compile_failure_demotes_previously_tradeable_market(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _config(tmp_path)
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    context = replace(
        context_for_case(_golden_rules()[0], strong_analysis=True),
        state="LIVE_CONFIRMATION_ELIGIBLE",
    )
    store.save_context(context)

    class BrokenCompiler:
        def compile(self, context):
            raise RuntimeError("compiler database unavailable")

    compile_rules_command(
        config_path,
        market_id=context.market_id,
        compiler=BrokenCompiler(),
    )
    capsys.readouterr()
    saved = store.load_context(context.market_id)
    assert saved is not None
    assert saved.state == "RULES_REVIEW_REQUIRED"
    assert saved.state_reasons == [
        "rule_compiler_error:compiler database unavailable"
    ]


def test_rule_change_invalidates_current_spec_and_plan(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _config(tmp_path)
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    store.save_context(context)
    compile_rules_command(config_path, market_id=context.market_id)
    capsys.readouterr()
    plan_sources_command(config_path, market_id=context.market_id)
    capsys.readouterr()

    changed = replace(
        context,
        rule_text=context.rule_text + " Material amendment.",
        rule_text_sha256="a" * 64,
        rule_version=2,
        state="DISCOVERED",
    )
    store.save_context(changed)
    grade_markets_command(config_path)
    capsys.readouterr()
    graded = store.load_context(context.market_id)
    assert graded is not None
    assert graded.state == "RULES_REVIEW_REQUIRED"
    assert "valid_rule_spec_missing" in graded.state_reasons


def test_emit_refuses_stale_rule_spec_source_plan(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _config(tmp_path)
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    semantics = fixture_semantics(context)
    spec = RuleSpec.from_context(
        context,
        semantics,
        compiler_model="anthropic:test",
        compiled_at="2026-07-25T00:00:00+00:00",
    )
    RuleStore(rule_store_db_path(config)).save_spec(spec)
    stale_plan = replace(
        build_source_plan(context, spec),
        rule_spec_sha256="0" * 64,
    )
    store.save_source_plan(stale_plan)
    store.save_context(replace(context, state="PAPER_ELIGIBLE"))

    with pytest.raises(SystemExit, match="stale semantic assets"):
        emit_bot_config_command(config_path, context.market_id)
    capsys.readouterr()


def test_inspect_and_validate_rule_cli_roundtrip(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _config(tmp_path)
    config = load_discovery_config(config_path)
    store = DiscoveryStore(config.data_dir)
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    store.save_context(context)
    compile_rules_command(config_path, market_id=context.market_id)
    capsys.readouterr()

    assert inspect_rule_command(config_path, context.market_id) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["spec"]["market_id"] == context.market_id
    assert len(inspected["passes"]) == 2

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(
        json.dumps(inspected["spec"]),
        encoding="utf-8",
    )
    assert validate_rule_command(spec_path) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["valid"] is True
    assert validated["spec_sha256"] == inspected["spec_sha256"]


def test_rule_compiler_config_rejects_unsafe_family_promotions(
    tmp_path: Path,
) -> None:
    path = _config(tmp_path)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "live_confirmation_families: []",
            "live_confirmation_families:\n"
            "    - SUBJECTIVE_DISCRETIONARY",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="subset|never"):
        load_discovery_config(path)
