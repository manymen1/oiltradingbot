from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from polybot.core.fees import FeeScheduleSnapshot

# Lifecycle of a discovered market. Transitions are computed by the scorer
# from the durable context record plus live tradeability; nothing downstream
# (source plans, opportunity scans, config emission) runs for a market whose
# state does not allow it.
MARKET_STATES = {
    "DISCOVERED",  # enumerated and matched the geopolitical filter; no context yet
    "RULES_REVIEW_REQUIRED",  # missing/short rule text or rule analysis not yet run
    "PAPER_ELIGIBLE",  # rules understandable; evidence/liquidity only good enough for paper
    "LIVE_CONFIRMATION_ELIGIBLE",  # clean rules + observable evidence + executable book
    "MONITOR_ONLY",  # rules ambiguous/discretionary or evidence unobservable
    "REJECTED",  # not geopolitical / unsuitable for this system
    "CLOSED",  # resolved, closed, or no longer accepting orders
}

TRADEABLE_STATES = {"PAPER_ELIGIBLE", "LIVE_CONFIRMATION_ELIGIBLE"}
OUTCOME_TOPOLOGIES = {
    "SINGLE_BINARY",
    "EXCLUSIVE_ONE_OF_N",
    "INDEPENDENT_MULTI",
    "MONOTONE_DEADLINE_LADDER",
    "MONOTONE_THRESHOLD_LADDER",
    "TOP_K",
    "UNCLASSIFIED",
}
SOURCE_PLAN_SCHEMA_VERSION = 2
SOURCE_PLAN_CURRENT = "CURRENT"
SOURCE_PLAN_LEGACY = "LEGACY_PRE_RULESPEC"


def market_dir_slug(market_id: str) -> str:
    """Filesystem-safe market key. Shared by config emission (executor
    data_dir) and the forecast-state lookup so both resolve the same path."""
    import re

    return re.sub(r"[^A-Za-z0-9_-]", "-", market_id)[:120]


@dataclass(frozen=True)
class OutcomeRecord:
    """One tradeable leg: for a binary market this is the single YES/NO pair;
    for a grouped neg-risk event, one leg per outcome."""

    name: str  # normalized key, e.g. "qatar" or "yes"
    label: str
    market_slug: str
    question: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    # Immutable semantic inputs from the individual Gamma market. Grouped
    # events cannot safely reuse the parent event deadline/rules for every
    # leg.
    deadline_iso: str = ""
    rule_text: str = ""
    rule_text_sha256: str = ""
    resolution_source: str = ""
    # MATCH/MISMATCH compares an explicit date in the leg label/question with
    # Gamma's leg endDate. UNKNOWN means there was no deterministic date to
    # compare; it is not an assertion that the metadata is correct.
    deadline_consistency: str = "UNKNOWN"
    tick_size: str = "0.01"
    neg_risk: bool = False
    last_yes_price: float | None = None
    volume: float = 0.0
    liquidity: float = 0.0
    active: bool = True
    closed: bool = False
    accepting_orders: bool = True
    fee_schedule: FeeScheduleSnapshot | None = None
    fee_schedule_error: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OutcomeRecord":
        if not isinstance(raw, dict):
            raise ValueError("outcome record must be an object")
        known = {
            name: raw.get(name)
            for name in cls.__dataclass_fields__  # type: ignore[attr-defined]
            if name in raw and name != "fee_schedule"
        }
        schedule_raw = raw.get("fee_schedule")
        schedule = (
            FeeScheduleSnapshot.from_dict(schedule_raw)
            if isinstance(schedule_raw, dict)
            else None
        )
        return cls(fee_schedule=schedule, **known)  # type: ignore[arg-type]


@dataclass(frozen=True)
class RuleAnalysis:
    """Structured reading of the market's verbatim resolution rules.

    Produced once per rule-text version (LLM or fixture); every judgement is
    about the RULES, not about the world. The classifier downstream must never
    have to infer these from the question alone.
    """

    counts: list[str] = field(default_factory=list)  # what explicitly counts
    does_not_count: list[str] = field(default_factory=list)
    cancellation_behavior: str = ""  # cancellation/postponement/replacement/no-event handling
    ambiguous_terms: list[str] = field(default_factory=list)
    discretionary: bool = False  # resolution depends on judgement calls, not observable facts
    parties: list[str] = field(default_factory=list)  # governments/actors whose behavior decides it
    mediators: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)  # decisive-event vocabulary
    decisive_sources: list[str] = field(default_factory=list)  # who would credibly report the decisive event
    rule_clarity: float = 0.0  # 0-1: can the outcome be classified reliably from the rules?
    evidence_observability: float = 0.0  # 0-1: will credible sources report the decisive event?
    resolution_risk: float = 1.0  # 0-1: wording/oracle interpretation risk (1 = worst)
    automation_suitability: float = 0.0  # 0-1: can evidence become deterministic actions?
    summary: str = ""
    model: str = ""  # provider/model that produced this analysis

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RuleAnalysis":
        known = {f: raw.get(f) for f in cls.__dataclass_fields__ if f in raw}  # type: ignore[attr-defined]
        return cls(**known)  # type: ignore[arg-type]


@dataclass(frozen=True)
class MarketContext:
    """Durable market context package: everything the system must know about
    a market before any forecasting, monitoring, or trading."""

    market_id: str  # stable key: event_slug for grouped events, condition_id for binaries
    kind: str  # "binary" | "grouped"
    event_slug: str
    event_title: str
    question: str
    deadline_iso: str  # market/event end date, ISO-8601 (UTC when known)
    outcomes: list[OutcomeRecord]
    rule_text: str
    rule_text_sha256: str
    rule_version: int  # bumped every time the rule hash changes
    outcome_topology: str = "UNCLASSIFIED"
    resolution_source: str = ""
    neg_risk: bool = False
    category: str = ""
    tags: list[str] = field(default_factory=list)
    volume: float = 0.0
    liquidity: float = 0.0
    active: bool = True
    closed: bool = False
    accepting_orders: bool = True
    rule_analysis: RuleAnalysis | None = None
    state: str = "DISCOVERED"
    state_reasons: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    correlation_group: str = ""  # derived from parties/locations; portfolio concentration key
    discovered_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["rule_analysis"] = self.rule_analysis.as_dict() if self.rule_analysis else None
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MarketContext":
        outcomes = [
            OutcomeRecord.from_dict(item)
            for item in raw.get("outcomes", [])
            if isinstance(item, dict)
        ]
        analysis_raw = raw.get("rule_analysis")
        analysis = RuleAnalysis.from_dict(analysis_raw) if isinstance(analysis_raw, dict) else None
        known = {
            f: raw.get(f)
            for f in cls.__dataclass_fields__  # type: ignore[attr-defined]
            if f in raw and f not in {"outcomes", "rule_analysis"}
        }
        return cls(outcomes=outcomes, rule_analysis=analysis, **known)  # type: ignore[arg-type]


@dataclass(frozen=True)
class PlannedSource:
    """One canonical source identity inside a market-specific source plan."""

    source_id: str
    organization_id: str
    independence_group: str
    domain: str
    source_tier: str
    feed_urls: list[str] = field(default_factory=list)
    poll_urls: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    timestamp_quality: str = "unknown"
    syndication_notes: str = ""
    required: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PlannedSource":
        known = {
            name: raw.get(name)
            for name in cls.__dataclass_fields__  # type: ignore[attr-defined]
            if name in raw
        }
        return cls(**known)  # type: ignore[arg-type]


@dataclass(frozen=True)
class SourcePlan:
    """Per-market source plan derived from the context package: the system
    watches these sources because THIS market requires them."""

    market_id: str
    rule_text_sha256: str
    rule_spec_sha256: str = ""
    source_records: list[PlannedSource] = field(default_factory=list)
    feed_urls: list[str] = field(default_factory=list)
    poll_urls: list[str] = field(default_factory=list)
    auto_trade_domains: list[str] = field(default_factory=list)
    alert_only_domains: list[str] = field(default_factory=list)
    escalate_terms: list[str] = field(default_factory=list)
    rationale: dict[str, list[str]] = field(default_factory=dict)  # source -> why it was chosen
    required_source_refs: list[str] = field(default_factory=list)
    missing_required_source_refs: list[str] = field(default_factory=list)
    minimum_independent_confirmations: int = 1
    created_at: str = ""
    schema_version: int = SOURCE_PLAN_SCHEMA_VERSION
    semantic_status: str = SOURCE_PLAN_CURRENT

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SourcePlan":
        records = [
            PlannedSource.from_dict(item)
            for item in raw.get("source_records", [])
            if isinstance(item, dict)
        ]
        known = {
            name: raw.get(name)
            for name in cls.__dataclass_fields__  # type: ignore[attr-defined]
            if name in raw and name != "source_records"
        }
        if "schema_version" not in raw:
            known["schema_version"] = 1
        if "semantic_status" not in raw:
            known["semantic_status"] = (
                SOURCE_PLAN_CURRENT
                if raw.get("rule_spec_sha256") and records
                else SOURCE_PLAN_LEGACY
            )
        return cls(source_records=records, **known)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Opportunity:
    """One outcome whose estimated probability clears the executable price by
    enough margin to survive costs and uncertainty."""

    market_id: str
    outcome: str
    side: str  # "YES" (v1 only prices YES legs)
    estimated_probability: float
    probability_source: str  # "config_estimate" | "forecast_state" | ...
    executable_price: float | None
    spread: float | None
    tradable_edge: float | None
    blockers: list[str] = field(default_factory=list)
    allocation_usd: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
