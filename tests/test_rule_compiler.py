from __future__ import annotations

import json
from pathlib import Path

import pytest

from polybot.core.budget import ClassifierBudgetStore
from polybot.core.config import ClassifierConfig
from polybot.rules.compiler import (
    _SEMANTIC_SCHEMA,
    RuleCompiler,
    _critical_consensus_payload,
    _repair_semantic_payload,
    compilation_prompt,
    fixture_semantics,
)
from polybot.rules.contracts import (
    RuleSemantics,
    build_rule_clause_catalog,
    source_requirement_id,
)
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


def test_codex_output_schema_uses_supported_strict_json_keywords() -> None:
    # The Responses structured-output dialect rejects uniqueItems even though
    # it is valid general JSON Schema. Runtime validation still enforces role
    # uniqueness after the model returns.
    assert '"uniqueItems"' not in json.dumps(_SEMANTIC_SCHEMA)


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


def test_codex_cli_compiler_accepts_schema_json(tmp_path: Path) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    semantic = fixture_semantics(context).as_dict()
    compiler = RuleCompiler(
        ClassifierConfig(
            provider="codex_cli",
            model="gpt-5.5",
            cli_binary="codex",
        ),
        RuleStore(tmp_path / "rules.sqlite3"),
        cli_runner=lambda _prompt: json.dumps(semantic),
    )

    result = compiler.compile(context)

    assert result.status == "COMPILED"
    assert result.spec is not None
    assert result.spec.compiler_model == "codex_cli:gpt-5.5"


def test_two_pass_disagreement_fails_closed_and_retains_diagnostics(
    tmp_path: Path,
) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    base = fixture_semantics(context).as_dict()

    def runner(prompt: str) -> str:
        payload = json.loads(json.dumps(base))
        if "pass: 2 of 2" in prompt:
            alternative = next(
                item.clause_id
                for item in build_rule_clause_catalog(context)
                if item.clause_id
                not in payload["qualifying_clause_ids"]
            )
            payload["qualifying_clause_ids"] = [alternative]
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


def test_compiler_replaces_model_clause_prose_with_catalog_text(
    tmp_path: Path,
) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    base = fixture_semantics(context).as_dict()

    def runner(prompt: str) -> str:
        payload = json.loads(json.dumps(base))
        payload["qualifying_conditions"] = [
            "invented explanation from "
            + ("pass two" if "pass: 2 of 2" in prompt else "pass one")
        ]
        payload["exclusions"] = [
            "different invented exclusion " + prompt[-1:]
        ]
        payload["resolution_policy"]["terminal_yes"] = [
            "invented terminal prose"
        ]
        return _envelope(payload)

    result = RuleCompiler(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "rules.sqlite3"),
        cli_runner=runner,
    ).compile(context)

    assert result.status == "COMPILED"
    assert result.spec is not None
    catalog = {
        item.clause_id: item.text for item in result.spec.rule_clauses
    }
    assert result.spec.semantics.qualifying_conditions == [
        catalog[item]
        for item in result.spec.semantics.qualifying_clause_ids
    ]
    assert "invented explanation" not in json.dumps(
        result.spec.semantics.as_dict()
    )


def test_compiler_rejects_invented_clause_id(tmp_path: Path) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    payload = fixture_semantics(context).as_dict()
    payload["qualifying_clause_ids"] = ["clause_not_in_verbatim_rules"]

    result = RuleCompiler(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "rules.sqlite3"),
        cli_runner=lambda _prompt: _envelope(payload),
    ).compile(context)

    assert result.status == "INVALID"
    assert "unknown rule clauses" in result.reason


def test_compiler_canonicalizes_only_long_unique_clause_prefixes(
    tmp_path: Path,
) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    payload = fixture_semantics(context).as_dict()
    canonical = payload["qualifying_clause_ids"][0]
    payload["qualifying_clause_ids"] = [canonical[:-2]]

    result = RuleCompiler(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "rules.sqlite3"),
        cli_runner=lambda _prompt: _envelope(payload),
    ).compile(context)

    assert result.status == "COMPILED"
    assert result.spec is not None
    assert result.spec.semantics.qualifying_clause_ids == [canonical]

    too_short = fixture_semantics(context).as_dict()
    too_short["qualifying_clause_ids"] = [canonical[:-3]]
    blocked = RuleCompiler(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "short.sqlite3"),
        cli_runner=lambda _prompt: _envelope(too_short),
    ).compile(context)
    assert blocked.status == "INVALID"
    assert "unknown rule clauses" in blocked.reason


def test_source_policy_disagreement_fails_closed(tmp_path: Path) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    base = fixture_semantics(context).as_dict()

    def runner(prompt: str) -> str:
        payload = json.loads(json.dumps(base))
        if "pass: 2 of 2" in prompt:
            payload["source_policy"]["policy_type"] = "ALL_OF"
        return _envelope(payload)

    result = RuleCompiler(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "rules.sqlite3"),
        cli_runner=runner,
    ).compile(context)

    assert result.status == "DISAGREEMENT"
    assert result.spec is None


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


def test_transport_failure_is_unavailable_not_semantic_invalid(
    tmp_path: Path,
) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    limits = ClassifierConfig(
        provider="codex_cli",
        max_escalations_per_hour=2,
        max_escalations_per_day=2,
    )
    budget = ClassifierBudgetStore(
        tmp_path,
        tmp_path / "budget.sqlite3",
    )

    def unavailable(_prompt: str) -> str:
        raise RuntimeError(
            "codex CLI exited 1: 401 Unauthorized: session has ended"
        )

    result = RuleCompiler(
        limits,
        RuleStore(tmp_path / "rules.sqlite3"),
        budget_store=budget,
        budget_limits=limits,
        cli_runner=unavailable,
    ).compile(context)

    assert result.status == "UNAVAILABLE"
    assert result.spec is None
    assert budget.status(limits)["errors_this_hour"] == 0


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


@pytest.mark.parametrize(
    ("market_id", "comparator", "start_iso", "timezone_name"),
    [
        (
            "democratic-presidential-nominee-2028",
            "OCCURRED",
            "unspecified",
            "UTC",
        ),
        (
            "republican-presidential-nominee-2028",
            "OCCURRED",
            "",
            "UTC",
        ),
        (
            "next-french-presidential-election",
            "EQUALS",
            "unbounded",
            "America/New_York",
        ),
        (
            "next-uk-prime-minister-in-2026-122",
            "EQUALS",
            "",
            "ET",
        ),
        (
            "who-will-be-the-next-prime-minister-of-israel-after-the-next-election",
            "EQUALS",
            "unknown",
            "UTC",
        ),
    ],
)
def test_observed_real_market_payload_repairs_are_bounded(
    market_id: str,
    comparator: str,
    start_iso: str,
    timezone_name: str,
) -> None:
    context = context_for_case(_golden_rules()[1], strong_analysis=True)
    payload = fixture_semantics(context).as_dict()
    payload["rule_family"] = "CATEGORICAL_EXCLUSIVE"
    payload["predicate"]["comparator"] = comparator
    payload["window"]["start_iso"] = start_iso
    payload["window"]["timezone"] = timezone_name

    repaired, notes = _repair_semantic_payload(payload)
    semantics = RuleSemantics.from_dict(repaired)

    assert semantics.rule_family == "CATEGORICAL_EXCLUSIVE", market_id
    assert semantics.predicate.comparator == "EQUALS"
    assert semantics.window.start_iso == ""
    if timezone_name == "ET":
        assert semantics.window.timezone == "America/New_York"
    assert notes


def test_observed_venezuela_inverted_window_is_not_repaired() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    payload = fixture_semantics(context).as_dict()
    payload["window"]["start_iso"] = "2029-01-01T00:00:00+00:00"
    payload["window"]["end_iso"] = "2026-12-31T23:59:59+00:00"

    repaired, notes = _repair_semantic_payload(payload)

    assert notes == []
    with pytest.raises(ValueError, match="end_iso must be after"):
        RuleSemantics.from_dict(repaired)


@pytest.mark.parametrize(
    ("market_id", "field"),
    [
        ("presidential-election-winner-2028", "qualifying_conditions"),
        ("brazil-presidential-election", "exclusions"),
        (
            "california-governor-election-2026",
            "terminal_yes",
        ),
        ("nobel-peace-prize-winner-2026-139", "source_roles"),
    ],
)
def test_observed_real_market_critical_disagreements_remain_blocking(
    market_id: str,
    field: str,
) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    left = fixture_semantics(context).normalized_dict()
    right = json.loads(json.dumps(left))
    alternative = next(
        item.clause_id
        for item in build_rule_clause_catalog(context)
        if item.clause_id not in left["qualifying_clause_ids"]
    )
    if field == "qualifying_conditions":
        right["qualifying_clause_ids"] = [alternative]
    elif field == "exclusions":
        right["exclusion_clause_ids"] = [alternative]
    elif field == "terminal_yes":
        right["resolution_policy"]["terminal_yes_clause_ids"] = [
            alternative
        ]
    else:
        right["source_requirements"] = [
            {
                "source_ref": "example authority",
                "roles": ["SETTLEMENT"],
                "required": True,
                "rationale": "pass two wording",
            }
        ]

    assert (
        _critical_consensus_payload(left)
        != _critical_consensus_payload(right)
    ), market_id


def test_compilation_prompt_and_schema_state_source_rules() -> None:
    """The prompt lost its anti-paraphrase sentence mid-edit and the schema
    shipped decision-critical enums with no meaning at all."""
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    prompt = compilation_prompt(context, pass_index=1)

    assert "Copy exact rule conditions and terminal criteria" in prompt
    assert "Copy exact rule Select" not in prompt
    assert "one source requirement per distinct organisation" in prompt
    assert "ANY_OF uses quorum 1" in prompt
    assert "structural list heading ending in a colon" in prompt
    assert "not monotonic before its window irreversibly closes" in prompt
    assert "geographic boundary definition are not by themselves subjective" in prompt

    sources = _SEMANTIC_SCHEMA["properties"]["source_requirements"]
    item = sources["items"]
    assert "clause_ids" in item["required"]
    assert "SETTLEMENT" in item["properties"]["roles"]["description"]
    assert "indispensable" in item["properties"]["required"]["description"]
    assert item["properties"]["roles"]["minItems"] == 1
    for field in ("rule_family", "source_policy"):
        assert _SEMANTIC_SCHEMA["properties"][field]["description"]


def test_source_clause_ids_bind_to_catalog_and_reject_inventions(
    tmp_path: Path,
) -> None:
    """Source policy is only auditable if it names the clause that authorized
    it, so those ids get the same catalog treatment as every other clause."""
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    catalog = build_rule_clause_catalog(context)
    real_clause = catalog[0].clause_id
    base = fixture_semantics(context).as_dict()

    def runner_with(clause_id: str):
        def runner(_prompt: str) -> str:
            payload = json.loads(json.dumps(base))
            for item in payload["source_requirements"]:
                item["clause_ids"] = [clause_id]
            return _envelope(payload)

        return runner

    result = RuleCompiler(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "bound.sqlite3"),
        cli_runner=runner_with(real_clause),
    ).compile(context)
    assert result.status == "COMPILED"
    assert result.spec is not None
    assert result.spec.semantics.source_requirements[0].clause_ids == [
        real_clause
    ]

    invented = RuleCompiler(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "invented.sqlite3"),
        cli_runner=runner_with("clause_" + "f" * 20),
    ).compile(context)
    assert invented.status == "INVALID"
    assert invented.spec is None


def test_source_policy_quorum_is_derived_not_guessed() -> None:
    """ANY_OF/ALL_OF fully determine quorum and forbid branches."""
    any_of, repairs = _repair_semantic_payload(
        {
            "source_policy": {
                "policy_type": "ANY_OF",
                "requirement_ids": ["a", "b", "c"],
                "quorum": 3,
                "primary_requirement_ids": ["a"],
                "fallback_requirement_ids": ["b"],
                "fallback_condition": "SOURCE_UNAVAILABLE",
            }
        }
    )
    assert any_of["source_policy"]["quorum"] == 1
    assert any_of["source_policy"]["primary_requirement_ids"] == []
    assert any_of["source_policy"]["fallback_condition"] == ""
    assert any("source_policy.quorum:3->1" in item for item in repairs)

    all_of, _ = _repair_semantic_payload(
        {
            "source_policy": {
                "policy_type": "ALL_OF",
                "requirement_ids": ["a", "b", "c"],
                "quorum": 1,
                "primary_requirement_ids": [],
                "fallback_requirement_ids": [],
                "fallback_condition": "",
            }
        }
    )
    assert all_of["source_policy"]["quorum"] == 3

    # A genuine quorum policy is left alone.
    untouched, quorum_repairs = _repair_semantic_payload(
        {
            "source_policy": {
                "policy_type": "QUORUM",
                "requirement_ids": ["a", "b", "c"],
                "quorum": 2,
                "primary_requirement_ids": [],
                "fallback_requirement_ids": [],
                "fallback_condition": "",
            }
        }
    )
    assert untouched["source_policy"]["quorum"] == 2
    assert quorum_repairs == []


def _requirement(source_ref: str, clause_id: str, *, required: bool = True):
    return {
        "requirement_id": f"tmp-{source_ref}",
        "source_ref": source_ref,
        "clause_ids": [clause_id],
        "roles": ["CONFIRMATION", "SETTLEMENT"],
        "required": required,
        "rationale": "why",
    }


def test_consensus_ignores_source_granularity_but_not_source_meaning() -> None:
    """The blockade market disagreed only because one pass split a single
    authorizing clause into six named publishers and the other did not."""
    clause = "clause_" + "a" * 20
    other_clause = "clause_" + "b" * 20
    split = {
        "source_requirements": [
            _requirement(name, clause)
            for name in ("white house", "state", "defense", "centcom")
        ],
        "source_policy": {"policy_type": "ANY_OF", "quorum": 1},
    }
    folded = {
        "source_requirements": [_requirement("us government", clause)],
        "source_policy": {"policy_type": "ANY_OF", "quorum": 1},
    }
    assert _critical_consensus_payload(split) == _critical_consensus_payload(
        folded
    )

    # Same clause, but one pass makes the source dispensable: a real conflict.
    optional = {
        "source_requirements": [
            _requirement("us government", clause, required=False)
        ],
        "source_policy": {"policy_type": "ANY_OF", "quorum": 1},
    }
    assert _critical_consensus_payload(folded) != _critical_consensus_payload(
        optional
    )

    # A different authorizing clause is also a real conflict.
    elsewhere = {
        "source_requirements": [_requirement("us government", other_clause)],
        "source_policy": {"policy_type": "ANY_OF", "quorum": 1},
    }
    assert _critical_consensus_payload(folded) != _critical_consensus_payload(
        elsewhere
    )


def test_unbound_sources_fall_back_to_prose_identity() -> None:
    """Passes predating clause binding carry no clause ids. Collapsing them
    on roles/required alone would make two different outlets compare equal."""
    def unbound(source_ref: str) -> dict:
        return {
            "source_requirements": [
                {
                    "requirement_id": "tmp",
                    "source_ref": source_ref,
                    "clause_ids": [],
                    "roles": ["SETTLEMENT"],
                    "required": True,
                    "rationale": "why",
                }
            ]
        }

    assert _critical_consensus_payload(
        unbound("associated press")
    ) != _critical_consensus_payload(unbound("reuters"))
    # Wording-only variance still agrees.
    assert _critical_consensus_payload(
        unbound("Associated  Press")
    ) == _critical_consensus_payload(unbound("associated press"))


def test_predicate_prose_is_noncritical_but_thresholds_still_gate(
    tmp_path: Path,
) -> None:
    """Paraphrase of the question must not block; a threshold must."""
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    base = fixture_semantics(context).as_dict()

    def runner_prose(prompt: str) -> str:
        payload = json.loads(json.dumps(base))
        if "pass: 2 of 2" in prompt:
            predicate = payload["predicate"]
            predicate["object"] = "the " + predicate["object"]
            predicate["action"] = predicate["action"] + ", formally"
        return _envelope(payload)

    result = RuleCompiler(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "rules-prose.sqlite3"),
        cli_runner=runner_prose,
    ).compile(context)
    assert result.status == "COMPILED"
    assert result.spec is not None

    numeric_case = next(
        item
        for item in _golden_rules()
        if item["expected_family"] == "NUMERIC_THRESHOLD"
    )
    numeric_context = context_for_case(numeric_case, strong_analysis=True)
    numeric_base = fixture_semantics(numeric_context).as_dict()
    assert numeric_base["rule_family"] == "NUMERIC_THRESHOLD"

    def runner_threshold(prompt: str) -> str:
        payload = json.loads(json.dumps(numeric_base))
        if "pass: 2 of 2" in prompt:
            payload["predicate"]["value"] = str(
                int(payload["predicate"]["value"] or 0) + 1
            )
        return _envelope(payload)

    numeric_result = RuleCompiler(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "rules-threshold.sqlite3"),
        cli_runner=runner_threshold,
    ).compile(numeric_context)
    assert numeric_result.status == "DISAGREEMENT"
    assert numeric_result.spec is None


def test_source_rationale_wording_is_noncritical_consensus(
    tmp_path: Path,
) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    base = fixture_semantics(context).as_dict()
    base["source_requirements"] = [
        {
            "requirement_id": source_requirement_id(
                "example authority",
                ["SETTLEMENT"],
                True,
            ),
            "source_ref": "example authority",
            "roles": ["SETTLEMENT"],
            "required": True,
            "rationale": "first explanatory wording",
        }
    ]
    base["source_policy"] = {
        "policy_type": "ANY_OF",
        "requirement_ids": [
            base["source_requirements"][0]["requirement_id"]
        ],
        "quorum": 1,
        "primary_requirement_ids": [],
        "fallback_requirement_ids": [],
        "fallback_condition": "",
    }

    def runner(prompt: str) -> str:
        payload = json.loads(json.dumps(base))
        if "pass: 2 of 2" in prompt:
            payload["source_requirements"][0]["rationale"] = (
                "second explanatory wording"
            )
        return _envelope(payload)

    result = RuleCompiler(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "rules.sqlite3"),
        cli_runner=runner,
    ).compile(context)

    assert result.status == "COMPILED"
    assert result.spec is not None


def test_equivalent_iso_instants_reach_consensus(tmp_path: Path) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    base = fixture_semantics(context).as_dict()
    base["window"]["end_iso"] = "2026-09-30T23:59:00-04:00"

    def runner(prompt: str) -> str:
        payload = json.loads(json.dumps(base))
        if "pass: 2 of 2" in prompt:
            payload["window"]["end_iso"] = "2026-10-01T03:59:00Z"
        return _envelope(payload)

    result = RuleCompiler(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "rules.sqlite3"),
        cli_runner=runner,
    ).compile(context)

    assert result.status == "COMPILED"
    assert result.spec is not None
    assert result.spec.semantics.window.end_iso == "2026-10-01T03:59:00Z"


def test_different_iso_instants_remain_blocking(tmp_path: Path) -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)
    base = fixture_semantics(context).as_dict()
    base["window"]["end_iso"] = "2026-08-31T23:59:00-04:00"

    def runner(prompt: str) -> str:
        payload = json.loads(json.dumps(base))
        if "pass: 2 of 2" in prompt:
            payload["window"]["end_iso"] = "2026-08-31T23:59:00Z"
        return _envelope(payload)

    result = RuleCompiler(
        ClassifierConfig(provider="claude_cli"),
        RuleStore(tmp_path / "rules.sqlite3"),
        cli_runner=runner,
    ).compile(context)

    assert result.status == "DISAGREEMENT"
    assert result.spec is None


def test_compilation_prompt_names_closed_validation_constraints() -> None:
    context = context_for_case(_golden_rules()[0], strong_analysis=True)

    prompt = compilation_prompt(context, pass_index=1)

    assert "CATEGORICAL_EXCLUSIVE=EQUALS" in prompt
    assert "empty string" in prompt
    assert "IANA" in prompt
    assert "never ET/EST/EDT" in prompt
