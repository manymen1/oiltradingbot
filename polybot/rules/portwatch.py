"""Deterministic IMF PortWatch evidence for numeric resolution markets."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from polybot.core.holdings import _atomic_json_write
from polybot.core.storage import append_jsonl
from polybot.discovery.portwatch import (
    Fetcher,
    chokepoint_series,
    match_chokepoint,
    moving_average_series,
)
from polybot.discovery.types import market_dir_slug

from .contracts import (
    EVIDENCE_CLAIM_SCHEMA_VERSION,
    EvidenceClaim,
    OutcomeBinding,
    RuleSpec,
    canonical_json,
)
from .store import RuleStore

PORTWATCH_ORGANIZATION_ID = "imf_portwatch"
PORTWATCH_DOMAIN = "portwatch.imf.org"
PORTWATCH_ADAPTER_VERSION = "portwatch-evidence-v1"


def is_portwatch_spec(spec: RuleSpec) -> bool:
    return spec.semantics.rule_family == "NUMERIC_THRESHOLD" and any(
        _normalized(requirement.source_ref) == "imf portwatch"
        for requirement in spec.semantics.source_requirements
    )


def sync_portwatch_claims(
    *,
    spec: RuleSpec,
    rule_store: RuleStore,
    data_dir: Path,
    as_of: datetime | None = None,
    fetcher: Fetcher | None = None,
) -> list[EvidenceClaim]:
    """Snapshot PortWatch and persist one current deterministic claim per leg.

    The state freezes a leg when its listed-date row first appears (or when
    the rule's 14-day missing-data grace expires).  A qualifying value seen
    before that point is permanent even if a later PortWatch revision removes
    it.  Conversely, a first observation after a cutoff is not treated as a
    historical oracle: without our own earlier snapshot that leg fails closed.
    """

    if not is_portwatch_spec(spec):
        return []
    observed_at = _aware(as_of or datetime.now(timezone.utc))
    portname = _portname(spec)
    if not portname:
        raise ValueError("PortWatch RuleSpec does not identify a chokepoint")
    rows = chokepoint_series(portname, fetcher=fetcher)
    averages = moving_average_series(rows)
    root = data_dir / "portwatch" / market_dir_slug(spec.market_id)
    state_path = root / f"{spec.spec_sha256}.json"
    history_path = root / f"{spec.spec_sha256}.snapshots.jsonl"
    state = _load_state(state_path, spec)
    snapshot_sha = hashlib.sha256(
        canonical_json(rows).encode("utf-8")
    ).hexdigest()
    if snapshot_sha != state.get("last_snapshot_sha256"):
        append_jsonl(
            history_path,
            {
                "schema_version": 1,
                "market_id": spec.market_id,
                "rule_spec_sha256": spec.spec_sha256,
                "portname": portname,
                "fetched_at": observed_at.isoformat(),
                "snapshot_sha256": snapshot_sha,
                "series": rows,
            },
        )
        state["last_snapshot_sha256"] = snapshot_sha
        state["last_snapshot_at"] = observed_at.isoformat()

    threshold = Decimal(spec.semantics.predicate.value)
    comparator = spec.semantics.predicate.comparator
    outcomes_state = state.setdefault("outcomes", {})
    claims: list[EvidenceClaim] = []
    published_dates = {stamp for stamp, _value in rows}
    for outcome in spec.outcomes:
        outcome_state = outcomes_state.setdefault(outcome.name, {})
        existing = outcome_state.get("claim")
        if outcome_state.get("frozen"):
            if isinstance(existing, dict):
                claims.append(
                    rule_store.save_claim(EvidenceClaim.from_dict(existing))
                )
            continue

        deadline = _deadline(outcome)
        deadline_date = _local_date(deadline, outcome.deadline_timezone)
        start_date = _start_date(outcome, deadline)
        listed_date_published = deadline_date.isoformat() in published_dates
        grace_expired = _grace_expired(
            observed_at,
            deadline,
            outcome.deadline_timezone,
        )
        first_observation = not outcome_state.get("tracking_started_at")
        if first_observation and (listed_date_published or grace_expired):
            outcome_state.update(
                {
                    "tracking_started_at": observed_at.isoformat(),
                    "status": "HISTORICAL_SNAPSHOT_UNAVAILABLE",
                    "frozen": True,
                    "cutoff_reason": (
                        "LISTED_DATE_ALREADY_PUBLISHED"
                        if listed_date_published
                        else "MISSING_DATA_GRACE_ALREADY_EXPIRED"
                    ),
                }
            )
            continue
        outcome_state.setdefault("tracking_started_at", observed_at.isoformat())

        eligible = [
            (stamp, value)
            for stamp, value in averages
            if start_date <= date.fromisoformat(stamp) <= deadline_date
        ]
        qualifying = [
            item
            for item in eligible
            if _compare(Decimal(str(item[1])), threshold, comparator)
        ]
        if qualifying:
            decisive_date, decisive_value = min(
                qualifying,
                key=lambda item: (item[1], item[0])
                if comparator.startswith("LESS")
                else (-item[1], item[0]),
            )
            claim = _claim(
                spec,
                outcome,
                observed_at=observed_at,
                measurement_date=decisive_date,
                value=decisive_value,
                predicate_matches=True,
                temporal_relation="IN_WINDOW",
                terminal=True,
            )
            outcome_state.update(
                {
                    "status": "TERMINAL_YES",
                    "frozen": True,
                    "cutoff_reason": "QUALIFYING_VALUE_PUBLISHED",
                    "qualifying_date": decisive_date,
                    "qualifying_value": _number(decisive_value),
                    "claim": claim.as_dict(),
                }
            )
            claims.append(rule_store.save_claim(claim))
            continue

        cutoff = listed_date_published or grace_expired
        if not eligible:
            outcome_state["status"] = (
                "NO_COMPLETE_MOVING_AVERAGE_AT_CUTOFF"
                if cutoff
                else "WAITING_FOR_COMPLETE_MOVING_AVERAGE"
            )
            if cutoff:
                outcome_state.update(
                    {
                        "frozen": True,
                        "cutoff_reason": (
                            "LISTED_DATE_PUBLISHED"
                            if listed_date_published
                            else "MISSING_DATA_GRACE_EXPIRED"
                        ),
                    }
                )
            continue

        measurement_date, value = _least_favorable(
            eligible,
            comparator,
        )
        claim = _claim(
            spec,
            outcome,
            observed_at=observed_at,
            measurement_date=measurement_date,
            value=value,
            predicate_matches=False,
            temporal_relation="AT_DEADLINE" if cutoff else "IN_WINDOW",
            terminal=cutoff,
        )
        outcome_state.update(
            {
                "status": "TERMINAL_NO" if cutoff else "TRACKING",
                "frozen": cutoff,
                "cutoff_reason": (
                    "LISTED_DATE_PUBLISHED"
                    if listed_date_published
                    else "MISSING_DATA_GRACE_EXPIRED"
                    if grace_expired
                    else ""
                ),
                "latest_measurement_date": measurement_date,
                "latest_measurement_value": _number(value),
                "claim": claim.as_dict(),
            }
        )
        claims.append(rule_store.save_claim(claim))

    state["updated_at"] = observed_at.isoformat()
    _atomic_json_write(state_path, state)
    return claims


def _load_state(path: Path, spec: RuleSpec) -> dict[str, Any]:
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("PortWatch state must be a JSON object")
        if raw.get("rule_spec_sha256") != spec.spec_sha256:
            raise ValueError("PortWatch state is bound to another RuleSpec")
        return raw
    return {
        "schema_version": 1,
        "market_id": spec.market_id,
        "rule_spec_sha256": spec.spec_sha256,
        "outcomes": {},
    }


def _claim(
    spec: RuleSpec,
    outcome: OutcomeBinding,
    *,
    observed_at: datetime,
    measurement_date: str,
    value: float,
    predicate_matches: bool,
    temporal_relation: str,
    terminal: bool,
) -> EvidenceClaim:
    requirements = [
        item
        for item in spec.semantics.source_requirements
        if _normalized(item.source_ref) == "imf portwatch"
    ]
    requirement_ids = [item.requirement_id for item in requirements]
    roles = sorted({role for item in requirements for role in item.roles})
    clauses = (
        spec.semantics.resolution_policy.terminal_yes_clause_ids
        if terminal and predicate_matches
        else spec.semantics.resolution_policy.terminal_no_clause_ids
        if terminal
        else spec.semantics.qualifying_clause_ids
    )
    payload_key = {
        "spec": spec.spec_sha256,
        "outcome": outcome.name,
        "observed_at": observed_at.isoformat(),
        "measurement_date": measurement_date,
        "value": _number(value),
        "temporal_relation": temporal_relation,
    }
    article_id = "portwatch:" + hashlib.sha256(
        canonical_json(payload_key).encode("utf-8")
    ).hexdigest()
    event_at = datetime.combine(
        date.fromisoformat(measurement_date),
        time.min,
        tzinfo=timezone.utc,
    ).isoformat()
    return EvidenceClaim.from_dict(
        EvidenceClaim(
            schema_version=EVIDENCE_CLAIM_SCHEMA_VERSION,
            market_id=spec.market_id,
            rule_spec_sha256=spec.spec_sha256,
            article_id=article_id,
            source_domain=PORTWATCH_DOMAIN,
            source_organization_id=PORTWATCH_ORGANIZATION_ID,
            origin_organization_id=PORTWATCH_ORGANIZATION_ID,
            independence_group=PORTWATCH_ORGANIZATION_ID,
            source_roles=roles,
            published_at=observed_at.isoformat(),
            extracted_at=observed_at.isoformat(),
            target_outcome=outcome.name,
            assertion="COUNT_OBSERVED",
            predicate_matches=predicate_matches,
            temporal_relation=temporal_relation,
            event_at=event_at,
            observed_value=_number(value),
            observed_value_upper="",
            observed_unit=spec.semantics.predicate.unit,
            supporting_quote=(
                f"IMF PortWatch {outcome.label}: published 7-day moving "
                f"average {_number(value)} for {measurement_date}."
            ),
            clauses_satisfied=list(clauses) if predicate_matches else [],
            clauses_violated=[],
            model=f"deterministic:{PORTWATCH_ADAPTER_VERSION}",
            extraction_passes=1,
            source_requirement_ids=requirement_ids,
        ).as_dict()
    )


def _portname(spec: RuleSpec) -> str | None:
    text = " ".join(
        [spec.question]
        + [clause.text for clause in spec.rule_clauses]
        + [spec.semantics.predicate.object]
    )
    return match_chokepoint(text)


def _deadline(outcome: OutcomeBinding) -> datetime:
    value = datetime.fromisoformat(outcome.deadline_iso.replace("Z", "+00:00"))
    if value.tzinfo is None:
        try:
            zone = ZoneInfo(outcome.deadline_timezone or "UTC")
        except ZoneInfoNotFoundError:
            zone = timezone.utc
        value = value.replace(tzinfo=zone)
    return value.astimezone(timezone.utc)


def _local_date(value: datetime, timezone_name: str) -> date:
    try:
        zone = ZoneInfo(timezone_name or "UTC")
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    return value.astimezone(zone).date()


def _grace_expired(
    observed_at: datetime,
    deadline: datetime,
    timezone_name: str,
) -> bool:
    """Apply the rule's 14 *calendar day* grace in the named local clock."""

    try:
        zone = ZoneInfo(timezone_name or "UTC")
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    local_cutoff = deadline.astimezone(zone) + timedelta(days=14)
    return observed_at >= local_cutoff.astimezone(timezone.utc)


def _start_date(outcome: OutcomeBinding, deadline: datetime) -> date:
    if not outcome.start_iso:
        return deadline.date()
    value = datetime.fromisoformat(outcome.start_iso.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return _local_date(value.astimezone(timezone.utc), outcome.deadline_timezone)


def _least_favorable(
    values: list[tuple[str, float]],
    comparator: str,
) -> tuple[str, float]:
    if comparator.startswith("LESS"):
        return min(values, key=lambda item: (item[1], item[0]))
    return max(values, key=lambda item: (item[1], item[0]))


def _compare(value: Decimal, threshold: Decimal, comparator: str) -> bool:
    return {
        "LESS_THAN": value < threshold,
        "LESS_THAN_OR_EQUAL": value <= threshold,
        "GREATER_THAN": value > threshold,
        "GREATER_THAN_OR_EQUAL": value >= threshold,
    }[comparator]


def _number(value: float) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "PORTWATCH_ADAPTER_VERSION",
    "PORTWATCH_DOMAIN",
    "PORTWATCH_ORGANIZATION_ID",
    "is_portwatch_spec",
    "sync_portwatch_claims",
]
