from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from polybot.core.types import Article
from polybot.discovery.registry import source_identity
from polybot.discovery.types import MarketContext, SourcePlan

from .contracts import (
    EVIDENCE_CLAIM_SCHEMA_VERSION,
    EvidenceClaim,
    RuleSpec,
    canonical_json,
)
from .store import ExtractionPass, RuleStore

OFFICIAL_CLAIM_ADAPTER_VERSION = "official-claim-envelope-v1"
_ENVELOPE_FIELDS = {
    "schema_version",
    "market_id",
    "rule_spec_sha256",
    "fact",
}


@dataclass(frozen=True)
class FastEvidenceAttempt:
    applicable: bool
    claim: EvidenceClaim | None = None
    error: str = ""
    adapter_id: str = ""


def extract_fast_evidence(
    *,
    context: MarketContext,
    spec: RuleSpec,
    source_plan: SourcePlan,
    article: Article,
    store: RuleStore,
    extracted_at: str,
    allowed_families: set[str],
    policy_version: str,
) -> FastEvidenceAttempt:
    """Consume a strict source-specific normalization envelope.

    Generic HTML, RSS text, and arbitrary JSON are never interpreted here.
    A source adapter must explicitly emit ``official_claim_json`` with a
    ``polybot_claim`` envelope already bound to this market and RuleSpec.
    This keeps the fast lane deterministic without pretending that free-form
    press-release prose can be evaluated safely with keyword matching.
    """

    if (
        article.source_kind != "official_claim_json"
        or article.source_adapter != "official_claim_json_v1"
    ):
        return FastEvidenceAttempt(applicable=False)
    if policy_version != OFFICIAL_CLAIM_ADAPTER_VERSION:
        return FastEvidenceAttempt(
            applicable=True,
            error="unsupported_deterministic_evidence_policy",
            adapter_id=policy_version,
        )
    family = spec.semantics.rule_family
    if family not in allowed_families:
        return FastEvidenceAttempt(
            applicable=True,
            error=f"deterministic_family_not_enabled:{family}",
            adapter_id=policy_version,
        )
    try:
        raw = json.loads(article.raw_text)
    except json.JSONDecodeError:
        return FastEvidenceAttempt(
            applicable=True,
            error="official_claim_envelope_invalid_json",
            adapter_id=policy_version,
        )
    if not isinstance(raw, dict) or set(raw) != _ENVELOPE_FIELDS:
        return FastEvidenceAttempt(
            applicable=True,
            error="official_claim_envelope_fields_invalid",
            adapter_id=policy_version,
        )
    if raw.get("schema_version") != 1:
        return FastEvidenceAttempt(
            applicable=True,
            error="official_claim_envelope_schema_unsupported",
            adapter_id=policy_version,
        )
    if raw.get("market_id") != context.market_id:
        return FastEvidenceAttempt(
            applicable=True,
            error="official_claim_market_binding_mismatch",
            adapter_id=policy_version,
        )
    if raw.get("rule_spec_sha256") != spec.spec_sha256:
        return FastEvidenceAttempt(
            applicable=True,
            error="official_claim_rulespec_binding_mismatch",
            adapter_id=policy_version,
        )
    if not article.published_at:
        return FastEvidenceAttempt(
            applicable=True,
            error="official_claim_published_at_missing",
            adapter_id=policy_version,
        )

    organization, independence_group = source_identity(
        article.domain,
        origin_organization=article.origin_organization,
        byline=article.byline,
    )
    # Import at call time to avoid a module cycle: evidence.py invokes this
    # adapter, while these shared validators remain the canonical ones.
    from .evidence import (
        _domain,
        _source_requirement_ids,
        _source_roles,
        normalize_fact,
    )

    roles = _source_roles(
        source_plan,
        article.domain,
        organization,
        independence_group,
    )
    required_role = (
        "SETTLEMENT"
        if family == "SOURCE_LOCKED_ANNOUNCEMENT"
        else "CONFIRMATION"
    )
    if required_role not in roles:
        return FastEvidenceAttempt(
            applicable=True,
            error=f"official_claim_missing_{required_role.casefold()}_role",
            adapter_id=policy_version,
        )
    fact_raw = raw.get("fact")
    if not isinstance(fact_raw, dict):
        return FastEvidenceAttempt(
            applicable=True,
            error="official_claim_fact_not_object",
            adapter_id=policy_version,
        )
    fact_input = dict(fact_raw)
    try:
        fact_input["temporal_relation"] = _temporal_relation(
            event_at=str(fact_input.get("event_at") or ""),
            published_at=article.published_at,
            spec=spec,
        )
        fact = normalize_fact(spec, article, fact_input)
    except (TypeError, ValueError) as exc:
        return FastEvidenceAttempt(
            applicable=True,
            error=f"official_claim_fact_invalid:{exc}",
            adapter_id=policy_version,
        )
    claim = EvidenceClaim.from_dict(
        EvidenceClaim(
            schema_version=EVIDENCE_CLAIM_SCHEMA_VERSION,
            market_id=context.market_id,
            rule_spec_sha256=spec.spec_sha256,
            article_id=article.hash,
            source_domain=_domain(article.domain),
            source_organization_id=organization,
            origin_organization_id=organization,
            independence_group=independence_group,
            source_roles=roles,
            source_requirement_ids=_source_requirement_ids(
                source_plan,
                article.domain,
                organization,
                independence_group,
                allowed_requirement_ids={
                    item.requirement_id
                    for item in spec.semantics.source_requirements
                },
            ),
            published_at=_iso(article.published_at),
            extracted_at=_iso(extracted_at),
            target_outcome=str(fact["target_outcome"]),
            assertion=str(fact["assertion"]),
            predicate_matches=bool(fact["predicate_matches"]),
            temporal_relation=str(fact["temporal_relation"]),
            event_at=str(fact["event_at"]),
            interval_start_at=str(fact["interval_start_at"]),
            interval_end_at=str(fact["interval_end_at"]),
            observed_value=str(fact["observed_value"]),
            observed_value_upper=str(fact["observed_value_upper"]),
            observed_unit=str(fact["observed_unit"]),
            supporting_quote=str(fact["supporting_quote"]),
            clauses_satisfied=list(fact["clauses_satisfied"]),
            clauses_violated=list(fact["clauses_violated"]),
            model=f"deterministic:{policy_version}",
            extraction_passes=1,
        ).as_dict()
    )
    claim.validate_spec_binding(spec)
    store.save_extraction_pass(
        ExtractionPass(
            market_id=context.market_id,
            rule_spec_sha256=spec.spec_sha256,
            article_id=article.hash,
            pass_index=1,
            model=f"deterministic:{policy_version}",
            raw_output=canonical_json(raw),
            normalized_output=fact,
            created_at=_iso(extracted_at),
        )
    )
    return FastEvidenceAttempt(
        applicable=True,
        claim=store.save_claim(claim),
        adapter_id=policy_version,
    )


def _temporal_relation(
    *,
    event_at: str,
    published_at: str,
    spec: RuleSpec,
) -> str:
    observed = _parse(event_at or published_at)
    start = (
        _parse(spec.semantics.window.start_iso)
        if spec.semantics.window.start_iso
        else None
    )
    end = _parse(spec.semantics.window.end_iso)
    if start is not None and observed < start:
        return "BEFORE_WINDOW"
    if observed > end:
        return "AFTER_WINDOW"
    return "IN_WINDOW"


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: str) -> str:
    return _parse(value).isoformat()


__all__ = [
    "FastEvidenceAttempt",
    "OFFICIAL_CLAIM_ADAPTER_VERSION",
    "extract_fast_evidence",
]
