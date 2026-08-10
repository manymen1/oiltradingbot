from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from polybot.core.fees import FEE_POLICY_VERSION, FeeScheduleSnapshot

RULE_SPEC_SCHEMA_VERSION = 4
EVIDENCE_CLAIM_SCHEMA_VERSION = 2
RULE_EVALUATION_SCHEMA_VERSION = 1
TRADE_INTENT_SCHEMA_VERSION = 1
DECISION_PROOF_SCHEMA_VERSION = 2

RULE_FAMILIES = {
    "OCCURRENCE_BEFORE_DEADLINE",
    "CATEGORICAL_EXCLUSIVE",
    "SOURCE_LOCKED_ANNOUNCEMENT",
    "STATUS_AT_DEADLINE",
    "NUMERIC_THRESHOLD",
    "DURATION_REQUIREMENT",
    "SUBJECTIVE_DISCRETIONARY",
}
RULE_COMPARATORS = {
    "OCCURRED",
    "ANNOUNCED",
    "EQUALS",
    "STATUS_IS",
    "GREATER_THAN",
    "GREATER_THAN_OR_EQUAL",
    "LESS_THAN",
    "LESS_THAN_OR_EQUAL",
    "DURATION_AT_LEAST",
}
SOURCE_ROLES = {"SETTLEMENT", "CONFIRMATION", "CONTEXT"}
SOURCE_POLICY_TYPES = {
    "ANY_OF",
    "ALTERNATIVE_QUORUM",
    "ALL_OF",
    "QUORUM",
    "PRIMARY_WITH_FALLBACK",
    "CONDITIONAL_FALLBACK",
}
SOURCE_FALLBACK_CONDITIONS = {
    "SOURCE_UNAVAILABLE",
    "CONFLICT_UNRESOLVED",
    "PRIMARY_TEXT_UNAVAILABLE",
    "RULE_SPECIFIED",
}
EVIDENCE_STATES = {
    "TERMINAL_YES",
    "TERMINAL_NO",
    "STRONG_YES",
    "STRONG_NO",
    "PATHWAY_YES",
    "PATHWAY_NO",
    "RULE_IRRELEVANT",
    "AMBIGUOUS",
}
CLAIM_ASSERTIONS = {
    "PREDICATE_SATISFIED",
    "PREDICATE_FORECLOSED",
    "SCHEDULED",
    "CANCELLED",
    "PATHWAY_SUPPORT",
    "PATHWAY_OBSTACLE",
    "EXCLUDED_ACTIVITY",
    "STATUS_OBSERVED",
    "COUNT_OBSERVED",
    "DURATION_OBSERVED",
    "QUALIFYING_BREACH",
    "CONFLICTING",
    "NONE",
}
TEMPORAL_RELATIONS = {
    "BEFORE_WINDOW",
    "IN_WINDOW",
    "AFTER_WINDOW",
    "AT_DEADLINE",
    "UNKNOWN",
}
TRADE_ACTIONS = {
    "ENTER_YES",
    "ENTER_NO",
    "EXIT_YES",
    "EXIT_NO",
    "HOLD",
    "NO_ACTION",
}
TRADE_SIDES = {"YES", "NO", "NONE"}
STRICT_DEADLINE_AUTHORITY = "STRICT_GAMMA_MATCH_V1"
VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY = "VERBATIM_RULES_PAPER_ONLY_V1"
DEADLINE_AUTHORITY_POLICIES = {
    STRICT_DEADLINE_AUTHORITY,
    VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY,
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RuleClause:
    clause_id: str
    text: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RuleClause":
        data = _object(raw, "rule_clause")
        _known(data, cls.__dataclass_fields__, "rule_clause")
        text = _text(data.get("text"), "rule_clause.text")
        clause_id = _text(data.get("clause_id"), "rule_clause.clause_id")
        expected = rule_clause_id(text)
        if clause_id != expected:
            raise ValueError(
                f"rule_clause.clause_id mismatch: {clause_id!r} != {expected!r}"
            )
        return cls(clause_id=clause_id, text=text)


def rule_clause_id(text: str) -> str:
    normalized = " ".join(text.split())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
    return f"clause_{digest}"


def build_rule_clause_catalog(context: Any) -> list[RuleClause]:
    """Split verbatim rules deterministically into content-addressed clauses."""

    active_texts = {
        str(item.rule_text).strip()
        for item in context.outcomes
        if item.active and not item.closed and str(item.rule_text).strip()
    }
    texts = sorted(active_texts) or [str(context.rule_text).strip()]
    clauses: dict[str, RuleClause] = {}
    for rule_text in texts:
        for paragraph in re.split(r"\n\s*\n", rule_text.replace("\r", "\n")):
            for line in (item.strip() for item in paragraph.splitlines()):
                if not line:
                    continue
                pieces = re.split(
                    r"(?<=[.!?])\s+(?=[\"'\u201c\u2018(]*[A-Z0-9])|"
                    r";\s+(?=\(?[ivx0-9]+\))",
                    line,
                )
                for piece in pieces:
                    text = " ".join(piece.strip().split())
                    if not text:
                        continue
                    clause_id = rule_clause_id(text)
                    existing = clauses.get(clause_id)
                    if existing is not None and existing.text != text:
                        raise ValueError("rule clause id collision")
                    clauses[clause_id] = RuleClause(clause_id, text)
    return sorted(clauses.values(), key=lambda item: item.clause_id)


@dataclass(frozen=True)
class OutcomeBinding:
    name: str
    label: str
    market_slug: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    # The semantic cutoff is explicit; source deadlines remain immutable audit
    # evidence instead of being silently collapsed into one timestamp.
    deadline_iso: str
    gamma_deadline_iso: str
    rule_deadline_iso: str
    deadline_timezone: str
    deadline_consistency: str
    deadline_authority: str
    rule_text_sha256: str
    resolution_source: str
    # Gamma's per-leg createdAt, preserved so a ladder leg added after an
    # earlier announcement cannot be treated as retroactively qualified.
    start_iso: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OutcomeBinding":
        data = _object(raw, "outcome")
        _known(data, cls.__dataclass_fields__, "outcome")
        values = {
            name: (
                _iso_datetime(
                    data.get(name, ""),
                    f"outcome.{name}",
                    allow_empty=True,
                )
                if name in {
                    "deadline_iso",
                    "gamma_deadline_iso",
                    "rule_deadline_iso",
                    "start_iso",
                }
                else _sha256(
                    data.get(name),
                    f"outcome.{name}",
                )
                if name == "rule_text_sha256"
                else _choice(
                    data.get(name),
                    {"MATCH", "MISMATCH", "UNKNOWN"},
                    f"outcome.{name}",
                )
                if name == "deadline_consistency"
                else _choice(
                    data.get(name),
                    {"GAMMA", "VERBATIM_RULES"},
                    f"outcome.{name}",
                )
                if name == "deadline_authority"
                else _text(
                    data.get(name, ""),
                    f"outcome.{name}",
                    allow_empty=True,
                )
                if name in {"resolution_source", "deadline_timezone"}
                else _text(data.get(name), f"outcome.{name}")
            )
            for name in cls.__dataclass_fields__
        }
        return cls(**values)


@dataclass(frozen=True)
class RulePredicate:
    subjects: list[str]
    action: str
    object: str
    comparator: str
    value: str = ""
    unit: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RulePredicate":
        data = _object(raw, "predicate")
        _known(data, cls.__dataclass_fields__, "predicate")
        comparator = _choice(
            data.get("comparator"),
            RULE_COMPARATORS,
            "predicate.comparator",
        )
        subjects = _text_list(data.get("subjects"), "predicate.subjects", minimum=1)
        return cls(
            subjects=subjects,
            action=_text(data.get("action"), "predicate.action"),
            object=_text(data.get("object"), "predicate.object"),
            comparator=comparator,
            value=_text(data.get("value", ""), "predicate.value", allow_empty=True),
            unit=_text(data.get("unit", ""), "predicate.unit", allow_empty=True),
        )


@dataclass(frozen=True)
class RuleWindow:
    start_iso: str
    end_iso: str
    timezone: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RuleWindow":
        data = _object(raw, "window")
        _known(data, cls.__dataclass_fields__, "window")
        start = _iso_datetime(data.get("start_iso", ""), "window.start_iso", allow_empty=True)
        end = _iso_datetime(data.get("end_iso"), "window.end_iso")
        timezone_name = _text(data.get("timezone"), "window.timezone")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"window.timezone is unknown: {timezone_name!r}") from exc
        if start and end:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=ZoneInfo(timezone_name))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=ZoneInfo(timezone_name))
            if end_dt <= start_dt:
                raise ValueError("window.end_iso must be after window.start_iso")
        return cls(start_iso=start, end_iso=end, timezone=timezone_name)


@dataclass(frozen=True)
class SourceRequirement:
    requirement_id: str
    source_ref: str
    roles: list[str]
    required: bool = False
    rationale: str = ""
    # Anchors this source policy to the verbatim resolution-source clause(s)
    # it implements, so the requirement cannot silently drift from the rule
    # text that authorized it.
    clause_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SourceRequirement":
        data = _object(raw, "source_requirement")
        _known(data, cls.__dataclass_fields__, "source_requirement")
        roles = [
            _choice(role, SOURCE_ROLES, f"source_requirement.roles[{index}]")
            for index, role in enumerate(
                _list(data.get("roles"), "source_requirement.roles", minimum=1)
            )
        ]
        source_ref = _text(
            data.get("source_ref"),
            "source_requirement.source_ref",
        )
        unique_roles = _unique(roles, "source_requirement.roles")
        required = _boolean(
            data.get("required", False),
            "source_requirement.required",
        )
        requirement_id = _text(
            data.get("requirement_id"),
            "source_requirement.requirement_id",
        )
        expected = source_requirement_id(source_ref, unique_roles, required)
        if requirement_id != expected:
            raise ValueError(
                "source_requirement.requirement_id does not match its "
                "source identity, roles, and required flag"
            )
        return cls(
            requirement_id=requirement_id,
            source_ref=source_ref,
            roles=unique_roles,
            required=required,
            rationale=_text(
                data.get("rationale", ""),
                "source_requirement.rationale",
                allow_empty=True,
            ),
            clause_ids=_clause_ids(
                data.get("clause_ids", []),
                "source_requirement.clause_ids",
            ),
        )


def source_requirement_id(
    source_ref: str,
    roles: Iterable[str],
    required: bool,
) -> str:
    identity = {
        "source_ref": " ".join(source_ref.casefold().split()),
        "roles": sorted(set(roles)),
        "required": bool(required),
    }
    return f"source_{sha256_json(identity)[:20]}"


@dataclass(frozen=True)
class SourcePolicyBranch:
    requirement_ids: list[str]
    requirement_quorum: int
    minimum_independent_sources: int

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SourcePolicyBranch":
        data = _object(raw, "source_policy_branch")
        _known(data, cls.__dataclass_fields__, "source_policy_branch")
        branch = cls(
            requirement_ids=_source_ids(
                data.get("requirement_ids"),
                "source_policy_branch.requirement_ids",
                minimum=1,
            ),
            requirement_quorum=_integer(
                data.get("requirement_quorum"),
                "source_policy_branch.requirement_quorum",
                minimum=1,
                maximum=20,
            ),
            minimum_independent_sources=_integer(
                data.get("minimum_independent_sources"),
                "source_policy_branch.minimum_independent_sources",
                minimum=1,
                maximum=20,
            ),
        )
        if branch.requirement_quorum > len(branch.requirement_ids):
            raise ValueError(
                "source policy branch quorum exceeds requirement count"
            )
        return branch


@dataclass(frozen=True)
class SourcePolicy:
    policy_type: str
    requirement_ids: list[str]
    quorum: int
    primary_requirement_ids: list[str] = field(default_factory=list)
    fallback_requirement_ids: list[str] = field(default_factory=list)
    fallback_condition: str = ""
    branches: list[SourcePolicyBranch] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if not self.branches:
            payload.pop("branches")
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SourcePolicy":
        data = _object(raw, "source_policy")
        _known(data, cls.__dataclass_fields__, "source_policy")
        policy = cls(
            policy_type=_choice(
                data.get("policy_type"),
                SOURCE_POLICY_TYPES,
                "source_policy.policy_type",
            ),
            requirement_ids=_source_ids(
                data.get("requirement_ids"),
                "source_policy.requirement_ids",
                minimum=1,
            ),
            quorum=_integer(
                data.get("quorum"),
                "source_policy.quorum",
                minimum=1,
                maximum=20,
            ),
            primary_requirement_ids=_source_ids(
                data.get("primary_requirement_ids", []),
                "source_policy.primary_requirement_ids",
            ),
            fallback_requirement_ids=_source_ids(
                data.get("fallback_requirement_ids", []),
                "source_policy.fallback_requirement_ids",
            ),
            fallback_condition=(
                _choice(
                    data.get("fallback_condition"),
                    SOURCE_FALLBACK_CONDITIONS,
                    "source_policy.fallback_condition",
                )
                if str(data.get("fallback_condition") or "").strip()
                else ""
            ),
            branches=[
                SourcePolicyBranch.from_dict(item)
                for item in _list(
                    data.get("branches", []),
                    "source_policy.branches",
                )
            ],
        )
        policy._validate_shape()
        return policy

    def _validate_shape(self) -> None:
        ids = set(self.requirement_ids)
        primary = set(self.primary_requirement_ids)
        fallback = set(self.fallback_requirement_ids)
        branches = list(self.branches)
        if not primary.issubset(ids) or not fallback.issubset(ids):
            raise ValueError("source policy branches must reference requirement_ids")
        if primary & fallback:
            raise ValueError("source policy primary and fallback branches overlap")
        if self.quorum > len(ids):
            raise ValueError("source policy quorum exceeds requirement count")
        if self.policy_type == "ANY_OF":
            if (
                self.quorum != 1
                or primary
                or fallback
                or self.fallback_condition
                or branches
            ):
                raise ValueError("ANY_OF requires quorum=1 and no fallback branches")
        elif self.policy_type == "ALTERNATIVE_QUORUM":
            if (
                self.quorum != 1
                or primary
                or fallback
                or self.fallback_condition
                or len(branches) < 2
            ):
                raise ValueError(
                    "ALTERNATIVE_QUORUM requires at least two branches, "
                    "quorum=1, and no fallback branch"
                )
            branch_ids = [
                requirement_id
                for branch in branches
                for requirement_id in branch.requirement_ids
            ]
            if len(branch_ids) != len(set(branch_ids)):
                raise ValueError(
                    "ALTERNATIVE_QUORUM branches must not overlap"
                )
            if set(branch_ids) != ids:
                raise ValueError(
                    "ALTERNATIVE_QUORUM branches must cover every requirement"
                )
        elif self.policy_type == "ALL_OF":
            if (
                self.quorum != len(ids)
                or primary
                or fallback
                or self.fallback_condition
                or branches
            ):
                raise ValueError("ALL_OF requires every requirement and no fallback")
        elif self.policy_type == "QUORUM":
            if primary or fallback or self.fallback_condition or branches:
                raise ValueError("QUORUM cannot define fallback branches")
        else:
            if branches or not primary or not fallback or not self.fallback_condition:
                raise ValueError(
                    f"{self.policy_type} requires primary and fallback branches"
                )
            if ids != primary | fallback:
                raise ValueError(
                    "fallback policy requirement_ids must equal both branches"
                )
            if self.quorum > min(len(primary), len(fallback)):
                raise ValueError(
                    "fallback source policy quorum exceeds a branch size"
                )
            if (
                self.policy_type == "PRIMARY_WITH_FALLBACK"
                and self.fallback_condition != "SOURCE_UNAVAILABLE"
            ):
                raise ValueError(
                    "PRIMARY_WITH_FALLBACK requires SOURCE_UNAVAILABLE"
                )


@dataclass(frozen=True)
class ResolutionPolicy:
    cancellation_behavior: str
    postponement_behavior: str
    terminal_yes: list[str]
    terminal_no: list[str]
    cancellation_clause_ids: list[str]
    postponement_clause_ids: list[str]
    terminal_yes_clause_ids: list[str]
    terminal_no_clause_ids: list[str]
    terminal_yes_monotonic: bool
    terminal_no_monotonic: bool
    independent_confirmation_sources: int = 1

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ResolutionPolicy":
        data = _object(raw, "resolution_policy")
        _known(data, cls.__dataclass_fields__, "resolution_policy")
        independent = _integer(
            data.get("independent_confirmation_sources", 1),
            "resolution_policy.independent_confirmation_sources",
            minimum=1,
            maximum=5,
        )
        return cls(
            cancellation_behavior=_text(
                data.get("cancellation_behavior"),
                "resolution_policy.cancellation_behavior",
            ),
            postponement_behavior=_text(
                data.get("postponement_behavior"),
                "resolution_policy.postponement_behavior",
            ),
            terminal_yes=_text_list(
                data.get("terminal_yes"),
                "resolution_policy.terminal_yes",
                minimum=1,
            ),
            terminal_no=_text_list(
                data.get("terminal_no"),
                "resolution_policy.terminal_no",
                minimum=1,
            ),
            cancellation_clause_ids=_clause_ids(
                data.get("cancellation_clause_ids", []),
                "resolution_policy.cancellation_clause_ids",
            ),
            postponement_clause_ids=_clause_ids(
                data.get("postponement_clause_ids", []),
                "resolution_policy.postponement_clause_ids",
            ),
            terminal_yes_clause_ids=_clause_ids(
                data.get("terminal_yes_clause_ids"),
                "resolution_policy.terminal_yes_clause_ids",
                minimum=1,
            ),
            terminal_no_clause_ids=_clause_ids(
                data.get("terminal_no_clause_ids"),
                "resolution_policy.terminal_no_clause_ids",
                minimum=1,
            ),
            terminal_yes_monotonic=_boolean(
                data.get("terminal_yes_monotonic"),
                "resolution_policy.terminal_yes_monotonic",
            ),
            terminal_no_monotonic=_boolean(
                data.get("terminal_no_monotonic"),
                "resolution_policy.terminal_no_monotonic",
            ),
            independent_confirmation_sources=independent,
        )


@dataclass(frozen=True)
class RuleSemantics:
    rule_family: str
    predicate: RulePredicate
    window: RuleWindow
    qualifying_conditions: list[str]
    exclusions: list[str]
    qualifying_clause_ids: list[str]
    exclusion_clause_ids: list[str]
    source_requirements: list[SourceRequirement]
    source_policy: SourcePolicy
    resolution_policy: ResolutionPolicy
    subjective_terms: list[str] = field(default_factory=list)
    subjective_clause_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if not self.source_policy.branches:
            payload["source_policy"].pop("branches")
        return payload

    def normalized_dict(self) -> dict[str, Any]:
        raw = self.as_dict()
        for key in ("start_iso", "end_iso"):
            value = raw["window"][key]
            if value:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(
                        tzinfo=ZoneInfo(raw["window"]["timezone"])
                    )
                raw["window"][key] = (
                    parsed.astimezone(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
        for key in ("qualifying_conditions", "exclusions", "subjective_terms"):
            raw[key] = sorted({_normalize_text(item) for item in raw[key]})
        for key in (
            "qualifying_clause_ids",
            "exclusion_clause_ids",
            "subjective_clause_ids",
        ):
            raw[key] = sorted(set(raw[key]))
        raw["predicate"]["subjects"] = sorted(
            {_normalize_text(item) for item in raw["predicate"]["subjects"]}
        )
        raw["source_requirements"] = sorted(
            (
                {
                    **item,
                    "source_ref": _normalize_text(item["source_ref"]),
                    "roles": sorted(set(item["roles"])),
                    "rationale": _normalize_text(item["rationale"]),
                    "clause_ids": sorted(set(item["clause_ids"])),
                }
                for item in raw["source_requirements"]
            ),
            key=lambda item: (
                item["source_ref"],
                item["roles"],
                item["required"],
            ),
        )
        for key in ("terminal_yes", "terminal_no"):
            raw["resolution_policy"][key] = sorted(
                {_normalize_text(item) for item in raw["resolution_policy"][key]}
            )
        for key in (
            "cancellation_clause_ids",
            "postponement_clause_ids",
            "terminal_yes_clause_ids",
            "terminal_no_clause_ids",
        ):
            raw["resolution_policy"][key] = sorted(
                set(raw["resolution_policy"][key])
            )
        for key in (
            "requirement_ids",
            "primary_requirement_ids",
            "fallback_requirement_ids",
        ):
            raw["source_policy"][key] = sorted(
                set(raw["source_policy"][key])
            )
        raw["source_policy"]["branches"] = sorted(
            (
                {
                    **branch,
                    "requirement_ids": sorted(
                        set(branch["requirement_ids"])
                    ),
                }
                for branch in raw["source_policy"].get("branches", [])
            ),
            key=lambda branch: (
                branch["requirement_ids"],
                branch["requirement_quorum"],
                branch["minimum_independent_sources"],
            ),
        )
        return raw

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RuleSemantics":
        data = _object(raw, "rule_semantics")
        _known(data, cls.__dataclass_fields__, "rule_semantics")
        family = _choice(data.get("rule_family"), RULE_FAMILIES, "rule_family")
        semantics = cls(
            rule_family=family,
            predicate=RulePredicate.from_dict(data.get("predicate")),
            window=RuleWindow.from_dict(data.get("window")),
            qualifying_conditions=_text_list(
                data.get("qualifying_conditions"),
                "qualifying_conditions",
                minimum=1,
            ),
            exclusions=_text_list(data.get("exclusions", []), "exclusions"),
            qualifying_clause_ids=_clause_ids(
                data.get("qualifying_clause_ids"),
                "qualifying_clause_ids",
                minimum=1,
            ),
            exclusion_clause_ids=_clause_ids(
                data.get("exclusion_clause_ids", []),
                "exclusion_clause_ids",
            ),
            source_requirements=[
                SourceRequirement.from_dict(item)
                for item in _list(
                    data.get("source_requirements", []),
                    "source_requirements",
                )
            ],
            source_policy=SourcePolicy.from_dict(data.get("source_policy")),
            resolution_policy=ResolutionPolicy.from_dict(
                data.get("resolution_policy")
            ),
            subjective_terms=_text_list(
                data.get("subjective_terms", []),
                "subjective_terms",
            ),
            subjective_clause_ids=_clause_ids(
                data.get("subjective_clause_ids", []),
                "subjective_clause_ids",
            ),
        )
        requirement_ids = {
            item.requirement_id for item in semantics.source_requirements
        }
        if set(semantics.source_policy.requirement_ids) != requirement_ids:
            raise ValueError(
                "source_policy must reference every source requirement exactly"
            )
        semantics._validate_family()
        return semantics

    def _validate_family(self) -> None:
        comparator = self.predicate.comparator
        expected = {
            "OCCURRENCE_BEFORE_DEADLINE": {"OCCURRED"},
            "CATEGORICAL_EXCLUSIVE": {"EQUALS"},
            "SOURCE_LOCKED_ANNOUNCEMENT": {"ANNOUNCED"},
            "STATUS_AT_DEADLINE": {"STATUS_IS"},
            "NUMERIC_THRESHOLD": {
                "GREATER_THAN",
                "GREATER_THAN_OR_EQUAL",
                "LESS_THAN",
                "LESS_THAN_OR_EQUAL",
            },
            "DURATION_REQUIREMENT": {"DURATION_AT_LEAST"},
            "SUBJECTIVE_DISCRETIONARY": RULE_COMPARATORS,
        }[self.rule_family]
        if comparator not in expected:
            raise ValueError(
                f"{self.rule_family} cannot use comparator {comparator}"
            )
        if self.rule_family in {"NUMERIC_THRESHOLD", "DURATION_REQUIREMENT"}:
            if not self.predicate.value or not self.predicate.unit:
                raise ValueError(
                    f"{self.rule_family} requires predicate.value and predicate.unit"
                )
        if (
            self.rule_family == "SOURCE_LOCKED_ANNOUNCEMENT"
            and not any(
                item.required and "SETTLEMENT" in item.roles
                for item in self.source_requirements
            )
        ):
            raise ValueError(
                "SOURCE_LOCKED_ANNOUNCEMENT requires a named required SETTLEMENT source"
            )
        if (
            self.rule_family == "SUBJECTIVE_DISCRETIONARY"
            and (
                not self.subjective_terms
                or not self.subjective_clause_ids
            )
        ):
            raise ValueError(
                "SUBJECTIVE_DISCRETIONARY requires bound subjective clauses"
            )


@dataclass(frozen=True)
class RuleSpec:
    schema_version: int
    market_id: str
    kind: str
    event_slug: str
    question: str
    rule_text_sha256: str
    rule_version: int
    outcome_topology: str
    deadline_authority_policy: str
    rule_clauses: list[RuleClause]
    outcomes: list[OutcomeBinding]
    semantics: RuleSemantics
    compiler_model: str
    compiled_at: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if not self.semantics.source_policy.branches:
            payload["semantics"]["source_policy"].pop("branches", None)
        return payload

    def execution_dict(self) -> dict[str, Any]:
        raw = self.as_dict()
        raw.pop("compiled_at", None)
        raw.pop("compiler_model", None)
        return raw

    @property
    def spec_sha256(self) -> str:
        return sha256_json(self.execution_dict())

    @classmethod
    def from_context(
        cls,
        context: Any,
        semantics: RuleSemantics,
        *,
        compiler_model: str,
        compiled_at: str,
        deadline_authority_policy: str = STRICT_DEADLINE_AUTHORITY,
    ) -> "RuleSpec":
        semantics = RuleSemantics.from_dict(semantics.as_dict())
        deadline_policy = _choice(
            deadline_authority_policy,
            DEADLINE_AUTHORITY_POLICIES,
            "deadline_authority_policy",
        )
        topology = str(
            getattr(context, "outcome_topology", "") or ""
        ).strip().upper()
        context_outcomes = list(context.outcomes)
        if topology in {"EXCLUSIVE_ONE_OF_N", "TOP_K"}:
            # Gamma grouped events can retain large numbers of inactive or
            # already-resolved placeholder child markets. They are useful to
            # raw capture, but they are not candidates in the current
            # exclusive semantic instrument.
            context_outcomes = [
                item
                for item in context_outcomes
                if item.active and not item.closed
            ]
        outcomes = [
            OutcomeBinding(
                name=item.name,
                label=item.label,
                market_slug=item.market_slug,
                condition_id=item.condition_id,
                yes_token_id=item.yes_token_id,
                no_token_id=item.no_token_id,
                deadline_iso=(
                    item.rule_deadline_iso
                    if (
                        deadline_policy
                        == VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY
                        and item.rule_deadline_iso
                    )
                    else item.deadline_iso or context.deadline_iso
                ),
                gamma_deadline_iso=item.deadline_iso or context.deadline_iso,
                rule_deadline_iso=item.rule_deadline_iso,
                deadline_timezone=item.deadline_timezone,
                deadline_consistency=item.deadline_consistency,
                deadline_authority=(
                    "VERBATIM_RULES"
                    if (
                        deadline_policy
                        == VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY
                        and item.rule_deadline_iso
                    )
                    else "GAMMA"
                ),
                rule_text_sha256=(
                    item.rule_text_sha256 or context.rule_text_sha256
                ),
                resolution_source=(
                    item.resolution_source or context.resolution_source
                ).strip(),
                start_iso=(
                    item.start_iso or context.discovered_at
                ),
            )
            for item in context_outcomes
        ]
        if topology == "UNCLASSIFIED":
            if context.kind == "binary":
                topology = "SINGLE_BINARY"
            elif context.neg_risk:
                topology = "EXCLUSIVE_ONE_OF_N"
        spec = cls(
            schema_version=RULE_SPEC_SCHEMA_VERSION,
            market_id=_text(context.market_id, "market_id"),
            kind=_choice(
                context.kind,
                {"BINARY", "GROUPED"},
                "kind",
            ).lower(),
            event_slug=_text(context.event_slug, "event_slug"),
            question=_text(context.question, "question"),
            rule_text_sha256=_sha256(context.rule_text_sha256, "rule_text_sha256"),
            rule_version=_integer(
                context.rule_version,
                "rule_version",
                minimum=1,
            ),
            outcome_topology=_choice(
                topology,
                {
                    "SINGLE_BINARY",
                    "EXCLUSIVE_ONE_OF_N",
                    "INDEPENDENT_MULTI",
                    "MONOTONE_DEADLINE_LADDER",
                    "MONOTONE_THRESHOLD_LADDER",
                    "TOP_K",
                    "UNCLASSIFIED",
                },
                "outcome_topology",
            ),
            deadline_authority_policy=deadline_policy,
            rule_clauses=build_rule_clause_catalog(context),
            outcomes=outcomes,
            semantics=semantics,
            compiler_model=_text(compiler_model, "compiler_model"),
            compiled_at=_iso_datetime(compiled_at, "compiled_at"),
        )
        spec._validate_topology()
        spec._validate_deadline_authority()
        spec._validate_clause_bindings()
        spec._validate_window()
        return spec

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RuleSpec":
        data = _object(raw, "rule_spec")
        _known(data, cls.__dataclass_fields__, "rule_spec")
        schema = _integer(data.get("schema_version"), "schema_version", minimum=1)
        if schema != RULE_SPEC_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported RuleSpec schema_version {schema}; "
                f"expected {RULE_SPEC_SCHEMA_VERSION}"
            )
        outcomes = [
            OutcomeBinding.from_dict(item)
            for item in _list(data.get("outcomes"), "outcomes", minimum=1)
        ]
        _unique([item.name for item in outcomes], "outcome names")
        condition_ids = [item.condition_id for item in outcomes]
        token_ids = [
            token
            for item in outcomes
            for token in (item.yes_token_id, item.no_token_id)
        ]
        _unique(condition_ids, "outcome condition ids")
        _unique(token_ids, "outcome token ids")
        rule_clauses = [
            RuleClause.from_dict(item)
            for item in _list(
                data.get("rule_clauses"),
                "rule_clauses",
                minimum=1,
            )
        ]
        _unique(
            [item.clause_id for item in rule_clauses],
            "rule clause ids",
        )
        spec = cls(
            schema_version=schema,
            market_id=_text(data.get("market_id"), "market_id"),
            kind=_choice(
                data.get("kind"),
                {"BINARY", "GROUPED"},
                "kind",
            ).lower(),
            event_slug=_text(data.get("event_slug"), "event_slug"),
            question=_text(data.get("question"), "question"),
            rule_text_sha256=_sha256(
                data.get("rule_text_sha256"),
                "rule_text_sha256",
            ),
            rule_version=_integer(
                data.get("rule_version"),
                "rule_version",
                minimum=1,
            ),
            outcome_topology=_choice(
                data.get("outcome_topology"),
                {
                    "SINGLE_BINARY",
                    "EXCLUSIVE_ONE_OF_N",
                    "INDEPENDENT_MULTI",
                    "MONOTONE_DEADLINE_LADDER",
                    "MONOTONE_THRESHOLD_LADDER",
                    "TOP_K",
                    "UNCLASSIFIED",
                },
                "outcome_topology",
            ),
            deadline_authority_policy=_choice(
                data.get("deadline_authority_policy"),
                DEADLINE_AUTHORITY_POLICIES,
                "deadline_authority_policy",
            ),
            rule_clauses=rule_clauses,
            outcomes=outcomes,
            semantics=RuleSemantics.from_dict(data.get("semantics")),
            compiler_model=_text(data.get("compiler_model"), "compiler_model"),
            compiled_at=_iso_datetime(data.get("compiled_at"), "compiled_at"),
        )
        spec._validate_topology()
        spec._validate_deadline_authority()
        spec._validate_clause_bindings()
        spec._validate_window()
        return spec

    def _validate_topology(self) -> None:
        if self.outcome_topology == "SINGLE_BINARY" and len(self.outcomes) != 1:
            raise ValueError("SINGLE_BINARY requires exactly one outcome")
        if (
            self.outcome_topology
            in {
                "INDEPENDENT_MULTI",
                "MONOTONE_DEADLINE_LADDER",
                "MONOTONE_THRESHOLD_LADDER",
                "EXCLUSIVE_ONE_OF_N",
            }
            and len(self.outcomes) < 2
        ):
            raise ValueError(
                f"{self.outcome_topology} requires at least two outcomes"
            )
        if self.outcome_topology in {
            "INDEPENDENT_MULTI",
            "MONOTONE_DEADLINE_LADDER",
        }:
            missing = [
                outcome.name
                for outcome in self.outcomes
                if not outcome.deadline_iso
            ]
            if missing:
                raise ValueError(
                    f"{self.outcome_topology} requires per-outcome deadlines: "
                    + ",".join(missing)
                )
        if self.outcome_topology == "MONOTONE_DEADLINE_LADDER":
            missing_start = [
                outcome.name
                for outcome in self.outcomes
                if not outcome.start_iso
            ]
            if missing_start:
                raise ValueError(
                    "MONOTONE_DEADLINE_LADDER requires per-outcome start_iso: "
                    + ",".join(missing_start)
                )

    def _validate_deadline_authority(self) -> None:
        for outcome in self.outcomes:
            if outcome.deadline_authority == "VERBATIM_RULES":
                if (
                    self.deadline_authority_policy
                    != VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY
                ):
                    raise ValueError(
                        "verbatim rule deadline requires the paper-only policy"
                    )
                if not outcome.rule_deadline_iso:
                    raise ValueError(
                        "verbatim rule deadline authority requires an exact rule deadline"
                    )
                if outcome.deadline_iso != outcome.rule_deadline_iso:
                    raise ValueError(
                        "semantic deadline does not match verbatim rule deadline"
                    )
            elif outcome.deadline_iso != outcome.gamma_deadline_iso:
                raise ValueError("Gamma deadline authority has a mismatched cutoff")
            if (
                outcome.deadline_consistency == "MISMATCH"
                and self.deadline_authority_policy
                == VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY
                and outcome.deadline_authority != "VERBATIM_RULES"
            ):
                raise ValueError(
                    "deadline mismatch lacks an exact verbatim rule authority"
                )

    def _validate_window(self) -> None:
        if self.outcome_topology != "MONOTONE_DEADLINE_LADDER":
            return
        window_end = _to_utc_instant(
            self.semantics.window.end_iso,
            self.semantics.window.timezone,
        )
        latest_leg = max(
            _to_utc_instant(outcome.deadline_iso, outcome.deadline_timezone)
            for outcome in self.outcomes
        )
        if window_end != latest_leg:
            raise ValueError(
                "MONOTONE_DEADLINE_LADDER window.end_iso must be derived from "
                "the latest outcome deadline, not a stale parent-event cutoff"
            )

    def _validate_clause_bindings(self) -> None:
        available = {item.clause_id for item in self.rule_clauses}
        semantics = self.semantics
        bound = {
            *semantics.qualifying_clause_ids,
            *semantics.exclusion_clause_ids,
            *semantics.subjective_clause_ids,
            *semantics.resolution_policy.cancellation_clause_ids,
            *semantics.resolution_policy.postponement_clause_ids,
            *semantics.resolution_policy.terminal_yes_clause_ids,
            *semantics.resolution_policy.terminal_no_clause_ids,
            *(
                clause_id
                for item in semantics.source_requirements
                for clause_id in item.clause_ids
            ),
        }
        unknown = sorted(bound - available)
        if unknown:
            raise ValueError(
                "RuleSpec semantics reference unknown rule clauses: "
                + ",".join(unknown)
            )

    def validate_context_binding(self, context: Any) -> None:
        expected = RuleSpec.from_context(
            context,
            self.semantics,
            compiler_model=self.compiler_model,
            compiled_at=self.compiled_at,
            deadline_authority_policy=self.deadline_authority_policy,
        )
        fields = (
            "market_id",
            "kind",
            "event_slug",
            "question",
            "rule_text_sha256",
            "rule_version",
            "outcome_topology",
            "deadline_authority_policy",
            "rule_clauses",
        )
        mismatches = [
            name for name in fields if getattr(self, name) != getattr(expected, name)
        ]
        # Gamma may reorder the markets nested under an event without changing
        # any instrument. Preserve the reviewed RuleSpec order (it can encode
        # ladder chronology), but compare freshness by immutable leg identity
        # rather than transient API list position.
        def outcome_key(item: OutcomeBinding) -> tuple[str, str, str, str]:
            return (
                item.condition_id,
                item.yes_token_id,
                item.no_token_id,
                item.name,
            )
        if sorted(self.outcomes, key=outcome_key) != sorted(
            expected.outcomes,
            key=outcome_key,
        ):
            mismatches.append("outcomes")
        if mismatches:
            raise ValueError(
                "RuleSpec is stale or instrument binding changed: "
                + ",".join(mismatches)
            )


@dataclass(frozen=True)
class EvidenceClaim:
    schema_version: int
    market_id: str
    rule_spec_sha256: str
    article_id: str
    source_domain: str
    source_organization_id: str
    origin_organization_id: str
    independence_group: str
    source_roles: list[str]
    published_at: str
    extracted_at: str
    target_outcome: str
    assertion: str
    predicate_matches: bool
    temporal_relation: str
    event_at: str
    observed_value: str
    observed_value_upper: str
    observed_unit: str
    supporting_quote: str
    clauses_satisfied: list[str]
    clauses_violated: list[str]
    model: str
    extraction_passes: int
    # Deterministically mapped from SourcePlan records after extraction. The
    # model never chooses these ids. Legacy claims deserialize with an empty
    # list and therefore cannot satisfy a current terminal source policy.
    source_requirement_ids: list[str] = field(default_factory=list)
    # Duration evidence needs an explicit immutable coverage interval. Article
    # publication time is deliberately not a substitute: a newly published
    # story can describe a period that ended before a later clock reset.
    interval_start_at: str = ""
    interval_end_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.schema_version == 1:
            # Preserve the canonical payload and hash of persisted v1 claims.
            payload.pop("interval_start_at", None)
            payload.pop("interval_end_at", None)
        return payload

    @property
    def claim_sha256(self) -> str:
        return sha256_json(self.as_dict())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EvidenceClaim":
        data = _object(raw, "evidence_claim")
        _known(data, cls.__dataclass_fields__, "evidence_claim")
        schema = _integer(data.get("schema_version"), "schema_version", minimum=1)
        if schema not in {1, EVIDENCE_CLAIM_SCHEMA_VERSION}:
            raise ValueError(
                f"unsupported EvidenceClaim schema_version {schema}"
            )
        claim = cls(
            schema_version=schema,
            market_id=_text(data.get("market_id"), "market_id"),
            rule_spec_sha256=_sha256(
                data.get("rule_spec_sha256"),
                "rule_spec_sha256",
            ),
            article_id=_text(data.get("article_id"), "article_id"),
            source_domain=_text(data.get("source_domain"), "source_domain"),
            source_organization_id=_text(
                data.get("source_organization_id"),
                "source_organization_id",
            ),
            origin_organization_id=_text(
                data.get("origin_organization_id"),
                "origin_organization_id",
            ),
            independence_group=_text(
                data.get("independence_group"),
                "independence_group",
            ),
            source_roles=_unique(
                [
                    _choice(role, SOURCE_ROLES, f"source_roles[{index}]")
                    for index, role in enumerate(
                        _list(data.get("source_roles", []), "source_roles")
                    )
                ],
                "source_roles",
            ),
            published_at=_iso_datetime(
                data.get("published_at", ""),
                "published_at",
                allow_empty=True,
            ),
            extracted_at=_iso_datetime(
                data.get("extracted_at"),
                "extracted_at",
            ),
            target_outcome=_text(
                data.get("target_outcome", ""),
                "target_outcome",
                allow_empty=True,
            ),
            assertion=_choice(
                data.get("assertion"),
                CLAIM_ASSERTIONS,
                "assertion",
            ),
            predicate_matches=_boolean(
                data.get("predicate_matches"),
                "predicate_matches",
            ),
            temporal_relation=_choice(
                data.get("temporal_relation"),
                TEMPORAL_RELATIONS,
                "temporal_relation",
            ),
            event_at=_iso_datetime(
                data.get("event_at", ""),
                "event_at",
                allow_empty=True,
            ),
            observed_value=_text(
                data.get("observed_value", ""),
                "observed_value",
                allow_empty=True,
            ),
            observed_value_upper=_text(
                data.get("observed_value_upper", ""),
                "observed_value_upper",
                allow_empty=True,
            ),
            observed_unit=_text(
                data.get("observed_unit", ""),
                "observed_unit",
                allow_empty=True,
            ),
            supporting_quote=_text(
                data.get("supporting_quote"),
                "supporting_quote",
            ),
            clauses_satisfied=_text_list(
                data.get("clauses_satisfied", []),
                "clauses_satisfied",
            ),
            clauses_violated=_text_list(
                data.get("clauses_violated", []),
                "clauses_violated",
            ),
            model=_text(data.get("model"), "model"),
            extraction_passes=_integer(
                data.get("extraction_passes"),
                "extraction_passes",
                minimum=1,
                maximum=5,
            ),
            source_requirement_ids=_source_ids(
                data.get("source_requirement_ids", []),
                "source_requirement_ids",
            ),
            interval_start_at=_iso_datetime(
                data.get("interval_start_at", ""),
                "interval_start_at",
                allow_empty=True,
            ),
            interval_end_at=_iso_datetime(
                data.get("interval_end_at", ""),
                "interval_end_at",
                allow_empty=True,
            ),
        )
        if bool(claim.interval_start_at) != bool(claim.interval_end_at):
            raise ValueError(
                "interval_start_at and interval_end_at must both be set or empty"
            )
        if claim.interval_start_at and (
            datetime.fromisoformat(
                claim.interval_end_at.replace("Z", "+00:00")
            )
            <= datetime.fromisoformat(
                claim.interval_start_at.replace("Z", "+00:00")
            )
        ):
            raise ValueError("interval_end_at must be after interval_start_at")
        return claim

    def validate_spec_binding(self, spec: RuleSpec) -> None:
        mismatches: list[str] = []
        if self.market_id != spec.market_id:
            mismatches.append("market_id")
        if self.rule_spec_sha256 != spec.spec_sha256:
            mismatches.append("rule_spec_sha256")
        outcome_names = {item.name for item in spec.outcomes}
        if self.target_outcome and self.target_outcome not in outcome_names:
            mismatches.append("target_outcome")
        allowed_clause_ids = {item.clause_id for item in spec.rule_clauses}
        if not set(self.clauses_satisfied).issubset(allowed_clause_ids):
            mismatches.append("clauses_satisfied")
        if not set(self.clauses_violated).issubset(allowed_clause_ids):
            mismatches.append("clauses_violated")
        allowed_requirement_ids = {
            item.requirement_id for item in spec.semantics.source_requirements
        }
        if not set(self.source_requirement_ids).issubset(
            allowed_requirement_ids
        ):
            mismatches.append("source_requirement_ids")
        if (
            spec.kind == "grouped"
            and not self.target_outcome
            and self.assertion not in {"NONE", "CONFLICTING", "EXCLUDED_ACTIVITY"}
            and spec.outcome_topology != "MONOTONE_DEADLINE_LADDER"
        ):
            mismatches.append("target_outcome")
        if mismatches:
            raise ValueError(
                "EvidenceClaim is stale or instrument binding changed: "
                + ",".join(sorted(set(mismatches)))
            )


@dataclass(frozen=True)
class RuleEvaluation:
    schema_version: int
    market_id: str
    rule_spec_sha256: str
    rule_family: str
    outcome_name: str
    evidence_state: str
    terminal: bool
    claim_sha256s: list[str]
    independence_groups: list[str]
    independent_confirmations: int
    required_confirmations: int
    clauses_satisfied: list[str]
    clauses_violated: list[str]
    blockers: list[str]
    evaluator_version: str
    evaluated_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def execution_dict(self) -> dict[str, Any]:
        raw = self.as_dict()
        raw.pop("evaluated_at", None)
        return raw

    @property
    def evaluation_sha256(self) -> str:
        return sha256_json(self.as_dict())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RuleEvaluation":
        data = _object(raw, "rule_evaluation")
        _known(data, cls.__dataclass_fields__, "rule_evaluation")
        schema = _integer(data.get("schema_version"), "schema_version", minimum=1)
        if schema != RULE_EVALUATION_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported RuleEvaluation schema_version {schema}"
            )
        confirmations = _integer(
            data.get("independent_confirmations"),
            "independent_confirmations",
            minimum=0,
        )
        required = _integer(
            data.get("required_confirmations"),
            "required_confirmations",
            minimum=1,
            maximum=5,
        )
        return cls(
            schema_version=schema,
            market_id=_text(data.get("market_id"), "market_id"),
            rule_spec_sha256=_sha256(
                data.get("rule_spec_sha256"),
                "rule_spec_sha256",
            ),
            rule_family=_choice(
                data.get("rule_family"),
                RULE_FAMILIES,
                "rule_family",
            ),
            outcome_name=_text(
                data.get("outcome_name", ""),
                "outcome_name",
                allow_empty=True,
            ),
            evidence_state=_choice(
                data.get("evidence_state"),
                EVIDENCE_STATES,
                "evidence_state",
            ),
            terminal=_boolean(data.get("terminal"), "terminal"),
            claim_sha256s=_unique(
                [
                    _sha256(item, f"claim_sha256s[{index}]")
                    for index, item in enumerate(
                        _list(data.get("claim_sha256s", []), "claim_sha256s")
                    )
                ],
                "claim_sha256s",
            ),
            independence_groups=_unique(
                _text_list(
                    data.get("independence_groups", []),
                    "independence_groups",
                ),
                "independence_groups",
            ),
            independent_confirmations=confirmations,
            required_confirmations=required,
            clauses_satisfied=_unique(
                _text_list(
                    data.get("clauses_satisfied", []),
                    "clauses_satisfied",
                ),
                "clauses_satisfied",
            ),
            clauses_violated=_unique(
                _text_list(
                    data.get("clauses_violated", []),
                    "clauses_violated",
                ),
                "clauses_violated",
            ),
            blockers=_unique(
                _text_list(data.get("blockers", []), "blockers"),
                "blockers",
            ),
            evaluator_version=_text(
                data.get("evaluator_version"),
                "evaluator_version",
            ),
            evaluated_at=_iso_datetime(
                data.get("evaluated_at"),
                "evaluated_at",
            ),
        )

    def validate_spec_binding(self, spec: RuleSpec) -> None:
        mismatches: list[str] = []
        if self.market_id != spec.market_id:
            mismatches.append("market_id")
        if self.rule_spec_sha256 != spec.spec_sha256:
            mismatches.append("rule_spec_sha256")
        if self.rule_family != spec.semantics.rule_family:
            mismatches.append("rule_family")
        if self.outcome_name and self.outcome_name not in {
            item.name for item in spec.outcomes
        }:
            mismatches.append("outcome_name")
        if mismatches:
            raise ValueError(
                "RuleEvaluation is stale or instrument binding changed: "
                + ",".join(mismatches)
            )


@dataclass(frozen=True)
class TradeIntent:
    schema_version: int
    market_id: str
    rule_spec_sha256: str
    evaluation_sha256: str
    outcome_name: str
    action: str
    side: str
    token_id: str
    estimated_probability: float
    max_price: float
    allocation_usd: float
    paper_only: bool
    blockers: list[str]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def intent_sha256(self) -> str:
        return sha256_json(self.as_dict())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TradeIntent":
        data = _object(raw, "trade_intent")
        _known(data, cls.__dataclass_fields__, "trade_intent")
        schema = _integer(data.get("schema_version"), "schema_version", minimum=1)
        if schema != TRADE_INTENT_SCHEMA_VERSION:
            raise ValueError(f"unsupported TradeIntent schema_version {schema}")
        action = _choice(data.get("action"), TRADE_ACTIONS, "action")
        side = _choice(data.get("side"), TRADE_SIDES, "side")
        token_id = _text(
            data.get("token_id", ""),
            "token_id",
            allow_empty=True,
        )
        if action in {"ENTER_YES", "ENTER_NO", "EXIT_YES", "EXIT_NO"}:
            if side == "NONE" or not token_id:
                raise ValueError("trade action requires a side and token_id")
        return cls(
            schema_version=schema,
            market_id=_text(data.get("market_id"), "market_id"),
            rule_spec_sha256=_sha256(
                data.get("rule_spec_sha256"),
                "rule_spec_sha256",
            ),
            evaluation_sha256=_sha256(
                data.get("evaluation_sha256"),
                "evaluation_sha256",
            ),
            outcome_name=_text(
                data.get("outcome_name", ""),
                "outcome_name",
                allow_empty=True,
            ),
            action=action,
            side=side,
            token_id=token_id,
            estimated_probability=_number(
                data.get("estimated_probability"),
                "estimated_probability",
                minimum=0.0,
                maximum=1.0,
            ),
            max_price=_number(
                data.get("max_price"),
                "max_price",
                minimum=0.0,
                maximum=1.0,
            ),
            allocation_usd=_number(
                data.get("allocation_usd"),
                "allocation_usd",
                minimum=0.0,
            ),
            paper_only=_boolean(data.get("paper_only"), "paper_only"),
            blockers=_unique(
                _text_list(data.get("blockers", []), "blockers"),
                "blockers",
            ),
            created_at=_iso_datetime(data.get("created_at"), "created_at"),
        )


@dataclass(frozen=True)
class DecisionProof:
    schema_version: int
    market_id: str
    rule_text_sha256: str
    rule_spec_sha256: str
    source_plan_sha256: str
    evaluation_sha256: str
    intent_sha256: str
    claim_sha256s: list[str]
    article_ids: list[str]
    source_domains: list[str]
    independence_groups: list[str]
    supporting_quotes: list[str]
    clauses_satisfied: list[str]
    clauses_violated: list[str]
    outcome_name: str
    action: str
    side: str
    token_id: str
    estimated_probability: float
    executable_bid: float | None
    executable_ask: float | None
    fee_policy_version: str
    fee_schedule_status: str
    fee_schedule_sha256: str
    fee_schedule: dict[str, Any]
    fee_buffer: float
    slippage_buffer: float
    resolution_risk_buffer: float
    uncertainty_buffer: float
    minimum_edge: float
    net_edge: float | None
    allocation_usd: float
    paper_only: bool
    blockers: list[str]
    executed: bool
    execution_result: dict[str, Any]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def proof_sha256(self) -> str:
        return sha256_json(self.as_dict())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DecisionProof":
        data = _object(raw, "decision_proof")
        _known(data, cls.__dataclass_fields__, "decision_proof")
        schema = _integer(data.get("schema_version"), "schema_version", minimum=1)
        if schema != DECISION_PROOF_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported DecisionProof schema_version {schema}"
            )
        execution_result = data.get("execution_result", {})
        if not isinstance(execution_result, dict):
            raise ValueError("execution_result must be an object")
        fee_schedule = data.get("fee_schedule", {})
        if not isinstance(fee_schedule, dict):
            raise ValueError("fee_schedule must be an object")
        fee_schedule_status = _choice(
            data.get("fee_schedule_status"),
            {"VERIFIED", "UNAVAILABLE"},
            "fee_schedule_status",
        )
        fee_policy_version = _text(
            data.get("fee_policy_version"),
            "fee_policy_version",
        )
        if fee_policy_version != FEE_POLICY_VERSION:
            raise ValueError(
                f"unsupported fee_policy_version {fee_policy_version!r}"
            )
        fee_schedule_sha256 = _text(
            data.get("fee_schedule_sha256", ""),
            "fee_schedule_sha256",
            allow_empty=True,
        ).lower()
        if fee_schedule_status == "VERIFIED":
            validated_fee_schedule = FeeScheduleSnapshot.from_dict(
                fee_schedule
            )
            fee_schedule = validated_fee_schedule.as_dict()
            if fee_schedule_sha256 != validated_fee_schedule.schedule_sha256:
                raise ValueError("fee_schedule_sha256 does not match fee_schedule")
        elif fee_schedule or fee_schedule_sha256:
            raise ValueError(
                "unavailable fee schedule must have an empty snapshot and hash"
            )
        action = _choice(data.get("action"), TRADE_ACTIONS, "action")
        executed = _boolean(data.get("executed"), "executed")
        if (
            executed
            and action in {"ENTER_YES", "ENTER_NO"}
            and fee_schedule_status != "VERIFIED"
        ):
            raise ValueError(
                "executed entry requires a verified fee schedule"
            )
        return cls(
            schema_version=schema,
            market_id=_text(data.get("market_id"), "market_id"),
            rule_text_sha256=_sha256(
                data.get("rule_text_sha256"),
                "rule_text_sha256",
            ),
            rule_spec_sha256=_sha256(
                data.get("rule_spec_sha256"),
                "rule_spec_sha256",
            ),
            source_plan_sha256=_sha256(
                data.get("source_plan_sha256"),
                "source_plan_sha256",
            ),
            evaluation_sha256=_sha256(
                data.get("evaluation_sha256"),
                "evaluation_sha256",
            ),
            intent_sha256=_sha256(
                data.get("intent_sha256"),
                "intent_sha256",
            ),
            claim_sha256s=_unique(
                [
                    _sha256(item, f"claim_sha256s[{index}]")
                    for index, item in enumerate(
                        _list(data.get("claim_sha256s", []), "claim_sha256s")
                    )
                ],
                "claim_sha256s",
            ),
            article_ids=_unique(
                _text_list(data.get("article_ids", []), "article_ids"),
                "article_ids",
            ),
            source_domains=_unique(
                _text_list(
                    data.get("source_domains", []),
                    "source_domains",
                ),
                "source_domains",
            ),
            independence_groups=_unique(
                _text_list(
                    data.get("independence_groups", []),
                    "independence_groups",
                ),
                "independence_groups",
            ),
            supporting_quotes=_unique(
                _text_list(
                    data.get("supporting_quotes", []),
                    "supporting_quotes",
                ),
                "supporting_quotes",
            ),
            clauses_satisfied=_unique(
                _text_list(
                    data.get("clauses_satisfied", []),
                    "clauses_satisfied",
                ),
                "clauses_satisfied",
            ),
            clauses_violated=_unique(
                _text_list(
                    data.get("clauses_violated", []),
                    "clauses_violated",
                ),
                "clauses_violated",
            ),
            outcome_name=_text(
                data.get("outcome_name", ""),
                "outcome_name",
                allow_empty=True,
            ),
            action=action,
            side=_choice(data.get("side"), TRADE_SIDES, "side"),
            token_id=_text(
                data.get("token_id", ""),
                "token_id",
                allow_empty=True,
            ),
            estimated_probability=_number(
                data.get("estimated_probability"),
                "estimated_probability",
                minimum=0.0,
                maximum=1.0,
            ),
            executable_bid=_optional_number(
                data.get("executable_bid"),
                "executable_bid",
                minimum=0.0,
                maximum=1.0,
            ),
            executable_ask=_optional_number(
                data.get("executable_ask"),
                "executable_ask",
                minimum=0.0,
                maximum=1.0,
            ),
            fee_policy_version=fee_policy_version,
            fee_schedule_status=fee_schedule_status,
            fee_schedule_sha256=fee_schedule_sha256,
            fee_schedule=fee_schedule,
            fee_buffer=_number(
                data.get("fee_buffer"),
                "fee_buffer",
                minimum=0.0,
            ),
            slippage_buffer=_number(
                data.get("slippage_buffer"),
                "slippage_buffer",
                minimum=0.0,
            ),
            resolution_risk_buffer=_number(
                data.get("resolution_risk_buffer"),
                "resolution_risk_buffer",
                minimum=0.0,
            ),
            uncertainty_buffer=_number(
                data.get("uncertainty_buffer"),
                "uncertainty_buffer",
                minimum=0.0,
            ),
            minimum_edge=_number(
                data.get("minimum_edge"),
                "minimum_edge",
                minimum=0.0,
            ),
            net_edge=_optional_number(data.get("net_edge"), "net_edge"),
            allocation_usd=_number(
                data.get("allocation_usd"),
                "allocation_usd",
                minimum=0.0,
            ),
            paper_only=_boolean(data.get("paper_only"), "paper_only"),
            blockers=_unique(
                _text_list(data.get("blockers", []), "blockers"),
                "blockers",
            ),
            executed=executed,
            execution_result=dict(execution_result),
            created_at=_iso_datetime(data.get("created_at"), "created_at"),
        )


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _known(
    raw: dict[str, Any],
    allowed: Iterable[str],
    name: str,
) -> None:
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise ValueError(f"{name} contains unknown keys: {', '.join(unknown)}")


def _text(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _choice(value: Any, allowed: set[str], name: str) -> str:
    text = _text(value, name).upper()
    if text not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)!r}")
    return text


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _integer(
    value: Any,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _number(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    import math

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return parsed


def _optional_number(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if value is None:
        return None
    return _number(
        value,
        name,
        minimum=minimum,
        maximum=maximum,
    )


def _list(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    if len(value) < minimum:
        raise ValueError(f"{name} must contain at least {minimum} item(s)")
    return value


def _text_list(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
) -> list[str]:
    return [
        _text(item, f"{name}[{index}]")
        for index, item in enumerate(_list(value, name, minimum=minimum))
    ]


def _source_ids(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
) -> list[str]:
    ids = _unique(_text_list(value, name, minimum=minimum), name)
    for item in ids:
        if re.fullmatch(r"source_[0-9a-f]{20}", item) is None:
            raise ValueError(f"{name} contains invalid source requirement id")
    return ids


def _clause_ids(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
) -> list[str]:
    ids = _unique(_text_list(value, name, minimum=minimum), name)
    for item in ids:
        if re.fullmatch(r"clause_[0-9a-f]{20}", item) is None:
            raise ValueError(f"{name} contains invalid rule clause id")
    return ids


def _unique(values: list[Any], name: str) -> list[Any]:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must be unique")
    return values


def _iso_datetime(
    value: Any,
    name: str,
    *,
    allow_empty: bool = False,
) -> str:
    text = _text(value, name, allow_empty=allow_empty)
    if not text:
        return ""
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date-time") from exc
    return text


def _to_utc_instant(value: str, timezone_name: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(timezone.utc)


def _sha256(value: Any, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return text


__all__ = [
    "CLAIM_ASSERTIONS",
    "DEADLINE_AUTHORITY_POLICIES",
    "DECISION_PROOF_SCHEMA_VERSION",
    "DecisionProof",
    "EVIDENCE_CLAIM_SCHEMA_VERSION",
    "EVIDENCE_STATES",
    "EvidenceClaim",
    "OutcomeBinding",
    "RuleClause",
    "ResolutionPolicy",
    "RULE_FAMILIES",
    "RULE_EVALUATION_SCHEMA_VERSION",
    "RULE_SPEC_SCHEMA_VERSION",
    "RuleEvaluation",
    "RulePredicate",
    "RuleSemantics",
    "RuleSpec",
    "RuleWindow",
    "SOURCE_ROLES",
    "SOURCE_FALLBACK_CONDITIONS",
    "SOURCE_POLICY_TYPES",
    "STRICT_DEADLINE_AUTHORITY",
    "SourcePolicy",
    "SourcePolicyBranch",
    "SourceRequirement",
    "TEMPORAL_RELATIONS",
    "TRADE_ACTIONS",
    "TRADE_INTENT_SCHEMA_VERSION",
    "TRADE_SIDES",
    "VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY",
    "TradeIntent",
    "canonical_json",
    "build_rule_clause_catalog",
    "rule_clause_id",
    "sha256_json",
    "source_requirement_id",
]
