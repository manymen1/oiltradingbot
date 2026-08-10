from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from polybot.discovery.sources import build_source_plan
from polybot.rules.contracts import (
    RulePredicate,
    RuleSpec,
    SourcePolicy,
    SourceRequirement,
    source_requirement_id,
)
from polybot.rules.evaluators import evaluate_rule
from polybot.rules.portwatch import sync_portwatch_claims
from polybot.rules.store import RuleStore
from test_rule_evidence import _spec


def _portwatch_spec(*, deadline: str = "2026-08-31T23:59:00-04:00"):
    context, base = _spec("NUMERIC_THRESHOLD")
    requirement = SourceRequirement(
        requirement_id=source_requirement_id(
            "IMF PortWatch",
            ["SETTLEMENT"],
            True,
        ),
        source_ref="IMF PortWatch",
        roles=["SETTLEMENT"],
        required=True,
        rationale="verbatim resolution source",
    )
    predicate = RulePredicate(
        subjects=["imf_portwatch"],
        action="publish",
        object="Bab el-Mandeb Strait 7-day moving average",
        comparator="LESS_THAN_OR_EQUAL",
        value="10",
        unit="transit calls",
    )
    semantics = replace(
        base.semantics,
        predicate=predicate,
        source_requirements=[requirement],
        source_policy=SourcePolicy(
            policy_type="ALL_OF",
            requirement_ids=[requirement.requirement_id],
            quorum=1,
        ),
        resolution_policy=replace(
            base.semantics.resolution_policy,
            terminal_yes_monotonic=True,
            independent_confirmation_sources=1,
        ),
    )
    question = "Bab el-Mandeb Strait effectively closed by August 31?"
    context_outcome = replace(
        context.outcomes[0],
        deadline_iso=deadline,
        rule_deadline_iso=deadline,
        deadline_timezone="America/New_York",
        start_iso="2026-07-01T00:00:00-04:00",
        resolution_source="IMF PortWatch",
    )
    context = replace(
        context,
        question=question,
        outcomes=[context_outcome],
        resolution_source="IMF PortWatch",
    )
    spec = RuleSpec.from_context(
        context,
        semantics,
        compiler_model=base.compiler_model,
        compiled_at=base.compiled_at,
    )
    return context, spec


def _payload(value: int, *, end_day: int = 7) -> dict:
    return {
        "features": [
            {
                "attributes": {
                    "date": f"2026-07-{day:02d}",
                    "n_total": value,
                    "portname": "Bab el-Mandeb Strait",
                }
            }
            for day in range(1, end_day + 1)
        ]
    }


def _august_payload(value: int, *, through: int) -> dict:
    rows = []
    for month, final_day in ((7, 31), (8, through)):
        rows.extend(
            {
                "attributes": {
                    "date": f"2026-{month:02d}-{day:02d}",
                    "n_total": value,
                    "portname": "Bab el-Mandeb Strait",
                }
            }
            for day in range(1, final_day + 1)
        )
    return {"features": rows}


def test_source_plan_resolves_required_portwatch_oracle() -> None:
    context, spec = _portwatch_spec()
    plan = build_source_plan(context, spec)
    oracle = next(
        item for item in plan.source_records if item.source_id == "imf_portwatch"
    )
    assert oracle.required is True
    assert oracle.roles == ["SETTLEMENT"]
    assert oracle.requirement_ids == [
        spec.semantics.source_requirements[0].requirement_id
    ]
    assert oracle.poll_urls and "arcgis" in oracle.poll_urls[0]
    assert plan.missing_required_source_refs == []


def test_qualifying_value_is_terminal_and_survives_revision(
    tmp_path: Path,
) -> None:
    _context, spec = _portwatch_spec()
    store = RuleStore(tmp_path / "rules.sqlite3")
    first = sync_portwatch_claims(
        spec=spec,
        rule_store=store,
        data_dir=tmp_path,
        as_of=datetime(2026, 7, 8, tzinfo=timezone.utc),
        fetcher=lambda _url: _payload(10),
    )
    assert len(first) == 1
    assert first[0].predicate_matches is True
    evaluation = evaluate_rule(spec, first)[0]
    assert evaluation.evidence_state == "TERMINAL_YES"
    assert evaluation.terminal is True

    revised = sync_portwatch_claims(
        spec=spec,
        rule_store=store,
        data_dir=tmp_path,
        as_of=datetime(2026, 7, 9, tzinfo=timezone.utc),
        fetcher=lambda _url: _payload(20),
    )
    assert revised == first
    assert len(store.claims_for_spec(spec.spec_sha256)) == 1


def test_first_post_cutoff_snapshot_fails_closed(tmp_path: Path) -> None:
    _context, spec = _portwatch_spec()
    store = RuleStore(tmp_path / "rules.sqlite3")
    claims = sync_portwatch_claims(
        spec=spec,
        rule_store=store,
        data_dir=tmp_path,
        as_of=datetime(2026, 9, 2, tzinfo=timezone.utc),
        fetcher=lambda _url: _august_payload(20, through=31),
    )
    assert claims == []
    state_path = next((tmp_path / "portwatch").rglob("*.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    outcome_state = next(iter(state["outcomes"].values()))
    assert outcome_state["status"] == "HISTORICAL_SNAPSHOT_UNAVAILABLE"
    assert outcome_state["frozen"] is True
    repeated = sync_portwatch_claims(
        spec=spec,
        rule_store=store,
        data_dir=tmp_path,
        as_of=datetime(2026, 9, 3, tzinfo=timezone.utc),
        fetcher=lambda _url: _august_payload(20, through=31),
    )
    assert repeated == []
    assert store.claims_for_spec(spec.spec_sha256) == []


def test_pre_cutoff_tracking_freezes_no_when_listed_date_appears(
    tmp_path: Path,
) -> None:
    _context, spec = _portwatch_spec()
    store = RuleStore(tmp_path / "rules.sqlite3")
    tracking = sync_portwatch_claims(
        spec=spec,
        rule_store=store,
        data_dir=tmp_path,
        as_of=datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc),
        fetcher=lambda _url: _august_payload(20, through=30),
    )
    assert tracking[0].temporal_relation == "IN_WINDOW"
    assert evaluate_rule(spec, tracking)[0].evidence_state == "STRONG_NO"

    frozen = sync_portwatch_claims(
        spec=spec,
        rule_store=store,
        data_dir=tmp_path,
        as_of=datetime(2026, 9, 2, tzinfo=timezone.utc),
        fetcher=lambda _url: _august_payload(20, through=31),
    )
    assert frozen[0].temporal_relation == "AT_DEADLINE"
    evaluation = evaluate_rule(spec, store.claims_for_spec(spec.spec_sha256))[0]
    assert evaluation.evidence_state == "TERMINAL_NO"
    assert evaluation.terminal is True


def test_missing_listed_date_freezes_at_fourteen_day_grace(
    tmp_path: Path,
) -> None:
    _context, spec = _portwatch_spec()
    store = RuleStore(tmp_path / "rules.sqlite3")
    sync_portwatch_claims(
        spec=spec,
        rule_store=store,
        data_dir=tmp_path,
        as_of=datetime(2026, 8, 30, tzinfo=timezone.utc),
        fetcher=lambda _url: _august_payload(20, through=30),
    )
    frozen = sync_portwatch_claims(
        spec=spec,
        rule_store=store,
        data_dir=tmp_path,
        as_of=datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc),
        fetcher=lambda _url: _august_payload(20, through=30),
    )
    assert frozen[0].temporal_relation == "AT_DEADLINE"
    assert evaluate_rule(spec, store.claims_for_spec(spec.spec_sha256))[0].terminal


def test_calendar_day_grace_preserves_local_clock_across_dst(
    tmp_path: Path,
) -> None:
    _context, spec = _portwatch_spec(
        deadline="2026-10-31T23:59:00-04:00"
    )
    store = RuleStore(tmp_path / "rules.sqlite3")
    sync_portwatch_claims(
        spec=spec,
        rule_store=store,
        data_dir=tmp_path,
        as_of=datetime(2026, 10, 30, tzinfo=timezone.utc),
        fetcher=lambda _url: _august_payload(20, through=30),
    )
    # New York falls back on Nov 1. Fourteen local calendar days therefore
    # ends one UTC hour later than a fixed 14*24-hour interval.
    not_yet = sync_portwatch_claims(
        spec=spec,
        rule_store=store,
        data_dir=tmp_path,
        as_of=datetime(2026, 11, 15, 4, 30, tzinfo=timezone.utc),
        fetcher=lambda _url: _august_payload(20, through=30),
    )
    assert not_yet[0].temporal_relation == "IN_WINDOW"
    frozen = sync_portwatch_claims(
        spec=spec,
        rule_store=store,
        data_dir=tmp_path,
        as_of=datetime(2026, 11, 15, 5, 0, tzinfo=timezone.utc),
        fetcher=lambda _url: _august_payload(20, through=30),
    )
    assert frozen[0].temporal_relation == "AT_DEADLINE"
