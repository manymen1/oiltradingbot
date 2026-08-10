from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from polybot.rules.contracts import RuleSpec
from polybot.rules.decision import ConfirmationDecisionEngine, ConfirmationPolicy
from polybot.rules.evaluators import evaluate_rule

from test_rule_evidence import _Quotes, _claim, _paper_adapter, _portfolio, _spec
from polybot.core.execution import PaperTradingAdapter

CANDIDATE_PATH = (
    Path(__file__).parent.parent
    / "data"
    / "discovery"
    / "reviews"
    / "us-iran-blockade.candidate.json"
)

# clause ids bound in the reviewed candidate's rule_clauses catalog
CLAUSE_SETTLEMENT_SOURCE = "clause_22d75a977b824e629bc9"
CLAUSE_TERMINAL_YES_DECLARATIVE = "clause_d083323989f3c60a36dd"
CLAUSE_TERMINAL_YES_SURVIVES_REVERSAL = "clause_3d45a884f3b8caba3fd2"
CLAUSE_PARTIAL_EXEMPTION_EXCLUDED = "clause_0598d59b13ece6394922"
CLAUSE_PROSPECTIVE_EXCLUDED = "clause_44ac5a865a08090389bd"
CLAUSE_PREVIOUSLY_UNANNOUNCED_QUALIFIES = "clause_887fd13abe067440e6fe"


def _candidate_spec() -> RuleSpec:
    raw = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
    return RuleSpec.from_dict(raw)


def _official_claim(
    spec: RuleSpec,
    *,
    article_id: str,
    event_at: str,
    target: str = "",
    assertion: str = "PREDICATE_SATISFIED",
    matches: bool = True,
    clauses_satisfied: list[str] | None = None,
    clauses_violated: list[str] | None = None,
):
    claim = _claim(
        spec,
        article_id=article_id,
        assertion=assertion,
        group="government:united_states",
        roles=["CONFIRMATION", "SETTLEMENT"],
        target=target,
        matches=matches,
        event_at=event_at,
    )
    if clauses_satisfied or clauses_violated:
        from polybot.rules.contracts import EvidenceClaim

        claim = EvidenceClaim.from_dict(
            {
                **claim.as_dict(),
                "clauses_satisfied": clauses_satisfied or [],
                "clauses_violated": clauses_violated or [],
            }
        )
    return claim


def _states(spec: RuleSpec, claims: list) -> dict[str, str]:
    return {item.outcome_name: item.evidence_state for item in evaluate_rule(spec, claims)}


def test_ladder_announcement_before_later_leg_created() -> None:
    """Legs created before the announcement qualify; legs Gamma added after
    the announcement (august_7/september_30/october_31/december_31, all
    created 2026-07-27) cannot be retroactively qualified by it."""
    spec = _candidate_spec()
    claim = _official_claim(spec, article_id="c1", event_at="2026-07-20T12:00:00Z")
    states = _states(spec, [claim])
    for name in ("july_24", "july_31", "august_15", "august_31"):
        assert states[name] == "TERMINAL_YES", name
    for name in ("august_7", "september_30", "october_31", "december_31"):
        assert states[name] != "TERMINAL_YES", name


def test_ladder_announcement_after_creation_before_only_some_deadlines() -> None:
    """An announcement on 2026-08-01 postdates every leg's creation, but
    july_24/july_31/july_14 have already passed their own deadlines by then
    and cannot be resolved YES by a later announcement."""
    spec = _candidate_spec()
    claim = _official_claim(spec, article_id="c2", event_at="2026-08-01T12:00:00Z")
    states = _states(spec, [claim])
    for name in ("july_24", "july_31", "july_14"):
        assert states[name] != "TERMINAL_YES", name
    for name in ("august_15", "august_31", "august_7", "september_30", "october_31", "december_31"):
        assert states[name] == "TERMINAL_YES", name


def test_ladder_announcement_after_all_deadlines_is_not_retroactive() -> None:
    spec = _candidate_spec()
    claim = _official_claim(spec, article_id="c3", event_at="2027-02-01T00:00:00Z")
    states = _states(spec, [claim])
    assert all(state != "TERMINAL_YES" for state in states.values())


def test_ladder_reuters_reporting_without_official_statement_is_not_terminal() -> None:
    spec = _candidate_spec()
    claim = _claim(
        spec,
        article_id="c4",
        assertion="PREDICATE_SATISFIED",
        group="reuters",
        roles=["CONFIRMATION"],
        target="",
        event_at="2026-07-20T12:00:00Z",
    )
    evaluations = evaluate_rule(spec, [claim])
    assert all(item.evidence_state != "TERMINAL_YES" for item in evaluations)
    eligible = [item for item in evaluations if item.outcome_name in {"july_24", "july_31"}]
    assert eligible
    assert all(
        "terminal_claim_missing_settlement_source" in item.blockers
        for item in eligible
    )


def test_ladder_official_statement_later_reversed_stays_terminal_yes() -> None:
    spec = _candidate_spec()
    announcement = _official_claim(
        spec,
        article_id="c5",
        event_at="2026-07-15T00:00:00Z",
        clauses_satisfied=[
            CLAUSE_TERMINAL_YES_DECLARATIVE,
            CLAUSE_TERMINAL_YES_SURVIVES_REVERSAL,
        ],
    )
    reversal_report = _claim(
        spec,
        article_id="c5-reversal",
        assertion="STATUS_OBSERVED",
        group="government:united_states",
        roles=["CONFIRMATION", "SETTLEMENT"],
        target="july_24",
        matches=False,
        event_at="2026-07-22T00:00:00Z",
    )
    states = _states(spec, [announcement, reversal_report])
    assert states["july_24"] == "TERMINAL_YES"


def test_ladder_partial_vessel_cargo_port_exemption_is_not_terminal() -> None:
    spec = _candidate_spec()
    claim = _official_claim(
        spec,
        article_id="c6",
        event_at="2026-07-20T12:00:00Z",
        assertion="EXCLUDED_ACTIVITY",
        matches=False,
        clauses_violated=[CLAUSE_PARTIAL_EXEMPTION_EXCLUDED],
    )
    states = _states(spec, [claim])
    assert all(state != "TERMINAL_YES" for state in states.values())


def test_ladder_conditional_or_prospective_announcement_is_not_terminal() -> None:
    spec = _candidate_spec()
    claim = _official_claim(
        spec,
        article_id="c7",
        event_at="2026-07-20T12:00:00Z",
        assertion="SCHEDULED",
        matches=False,
        clauses_violated=[CLAUSE_PROSPECTIVE_EXCLUDED],
    )
    states = _states(spec, [claim])
    assert all(state != "TERMINAL_YES" for state in states.values())


def test_ladder_previously_unannounced_prior_suspension_qualifies_open_legs() -> None:
    """Clause_887fd... lets an announcement cover a suspension that already
    happened quietly earlier; eligibility still keys off the announcement
    time (when the leg could learn about it), not the earlier private date."""
    spec = _candidate_spec()
    claim = _official_claim(
        spec,
        article_id="c8",
        event_at="2026-07-15T00:00:00Z",
        clauses_satisfied=[CLAUSE_PREVIOUSLY_UNANNOUNCED_QUALIFIES],
    )
    states = _states(spec, [claim])
    for name in ("july_24", "july_31", "august_15", "august_31"):
        assert states[name] == "TERMINAL_YES", name
    for name in ("august_7", "september_30", "october_31", "december_31"):
        assert states[name] != "TERMINAL_YES", name


def test_ladder_one_claim_satisfies_every_applicable_later_leg() -> None:
    """By 2026-07-28 every leg has been created (the last batch was created
    2026-07-27), and only july_14/july_24 have already expired. One claim
    must resolve the remaining seven legs simultaneously."""
    spec = _candidate_spec()
    claim = _official_claim(spec, article_id="c9", event_at="2026-07-28T00:00:00Z")
    states = _states(spec, [claim])
    for name in ("july_14", "july_24"):
        assert states[name] != "TERMINAL_YES", name
    for name in (
        "july_31",
        "august_15",
        "august_31",
        "august_7",
        "september_30",
        "october_31",
        "december_31",
    ):
        assert states[name] == "TERMINAL_YES", name


def test_closed_ladder_leg_is_retained_for_replay_but_blocked_from_new_execution(
    tmp_path,
) -> None:
    context, base = _spec("SOURCE_LOCKED_ANNOUNCEMENT")
    leg_open = replace(
        context.outcomes[0],
        name="leg_open",
        label="Leg Open",
        market_slug=f"{context.market_id}-leg-open",
        condition_id=f"{context.market_id}-leg-open-condition",
        yes_token_id=f"{context.market_id}-leg-open-yes",
        no_token_id=f"{context.market_id}-leg-open-no",
        deadline_iso="2026-12-31T23:59:59-05:00",
        start_iso="2026-07-01T00:00:00Z",
        closed=False,
        active=True,
    )
    leg_closed = replace(
        context.outcomes[0],
        name="leg_closed",
        label="Leg Closed",
        market_slug=f"{context.market_id}-leg-closed",
        condition_id=f"{context.market_id}-leg-closed-condition",
        yes_token_id=f"{context.market_id}-leg-closed-yes",
        no_token_id=f"{context.market_id}-leg-closed-no",
        deadline_iso="2026-08-31T23:59:59-04:00",
        start_iso="2026-07-01T00:00:00Z",
        closed=True,
        active=False,
    )
    new_context = replace(
        context,
        kind="grouped",
        outcome_topology="MONOTONE_DEADLINE_LADDER",
        outcomes=[leg_open, leg_closed],
    )
    spec = RuleSpec.from_context(
        new_context,
        base.semantics,
        compiler_model="anthropic:test",
        compiled_at="2026-07-25T00:00:00+00:00",
    )

    open_claim = _official_claim(spec, article_id="closed-1", target="leg_open", event_at="2026-07-10T00:00:00Z")
    closed_claim = _official_claim(spec, article_id="closed-2", target="leg_closed", event_at="2026-07-10T00:00:00Z")
    evaluations = {
        item.outcome_name: item
        for item in evaluate_rule(spec, [open_claim, closed_claim])
    }
    # Replay/evaluation still scores the closed leg: it is not silently dropped.
    assert evaluations["leg_open"].evidence_state == "TERMINAL_YES"
    assert evaluations["leg_closed"].evidence_state == "TERMINAL_YES"

    snapshots = {}
    for binding in spec.outcomes:
        snapshots[binding.yes_token_id] = {
            "asks": [[0.80, 1000]],
            "bids": [[0.78, 1000]],
            "staleness": 0.0,
            "revision": f"{binding.name}-yes-r1",
        }
        snapshots[binding.no_token_id] = {
            "asks": [[0.22, 1000]],
            "bids": [[0.20, 1000]],
            "staleness": 0.0,
            "revision": f"{binding.name}-no-r1",
        }
    adapter = PaperTradingAdapter(
        state_path=tmp_path / "paper.json",
        quote_provider=_Quotes(snapshots),
        token_pairs=[(b.yes_token_id, b.no_token_id) for b in spec.outcomes],
        fee_bps=0,
        slippage_bps=25,
        max_book_age_seconds=10,
    )
    engine = ConfirmationDecisionEngine(
        context=new_context,
        spec=spec,
        adapter=adapter,
        portfolio=_portfolio(tmp_path, new_context),
        policy=ConfirmationPolicy(
            min_edge=0.05,
            max_entry_price=0.90,
            requested_usd=25.0,
        ),
    )
    open_draft = engine.decide(evaluations["leg_open"], created_at="2026-07-25T00:02:00+00:00")
    assert open_draft.intent.action == "ENTER_YES"

    closed_draft = engine.decide(evaluations["leg_closed"], created_at="2026-07-25T00:02:00+00:00")
    assert closed_draft.intent.action == "NO_ACTION"
    assert "outcome_closed_for_new_execution" in closed_draft.intent.blockers
