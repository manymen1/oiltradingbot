from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from polybot.rules.contracts import RuleSpec

from .registry import (
    GENERAL_FAST_FEEDS,
    WIRE_DOMAINS,
    actor_sources,
    bing_news_rss,
    direct_feeds,
    google_news_rss,
    official_domains,
    publisher_sources,
    resolve_source_reference,
)
from .types import (
    SOURCE_PLAN_CURRENT,
    SOURCE_PLAN_LEGACY,
    SOURCE_PLAN_SCHEMA_VERSION,
    MarketContext,
    PlannedSource,
    SourcePlan,
)


def build_source_plan(
    context: MarketContext,
    rule_spec: RuleSpec | None = None,
) -> SourcePlan:
    """Derive a per-market source plan FROM the context package.

    The system watches these sources because this market requires them: the
    parties and mediators whose behavior decides the market, wire services,
    the market's stated resolution source, and Google News discovery queries
    built from the decisive-event vocabulary. Requires a rule analysis --
    'no authoritative source plan -> no autonomous entry' is enforced by
    refusing to build a plan for an unanalyzed market.
    """
    if rule_spec is not None:
        rule_spec.validate_context_binding(context)
        return _build_rule_spec_source_plan(context, rule_spec)

    analysis = context.rule_analysis
    if analysis is None:
        raise ValueError(f"market {context.market_id} has no rule analysis; refusing to build a source plan")

    rationale: dict[str, list[str]] = {}
    actors = sorted(set(analysis.parties) | set(analysis.mediators))
    actor_domains = official_domains(actors)
    for domain in actor_domains:
        rationale.setdefault(domain, []).append("official domain of a deciding party/mediator")
    for domain in WIRE_DOMAINS:
        rationale.setdefault(domain, []).append("wire service; decisive events are wire-reported")

    resolution_domain = _domain_from_source(context.resolution_source)
    if resolution_domain:
        rationale.setdefault(resolution_domain, []).append("resolution source named by the market")

    # Discovery feeds: one Google News query per (actors x event vocabulary)
    # theme, plus one wire-scoped query. Stable public RSS does not exist for
    # most wires, so Google News RSS is the discovery layer (same approach as
    # the hand-built Iran/Qatar configs).
    actor_terms = [actor.replace("_", " ") for actor in (analysis.parties or actors)][:4]
    keyword_terms = analysis.keywords[:6]
    # Ordered by MEASURED latency, not authority. These markets resolve on "a
    # consensus of credible reporting", and probing (scripts/probe_feeds.sh)
    # found credible outlets publishing 0-30 minutes after an event while the
    # official government feeds ran 2.7-5.6 DAYS behind. So the fast credible
    # feeds lead, official feeds follow (authoritative confirmation, not
    # speed), and aggregators trail (5-15 min indexing lag on top of source).
    official_feeds = direct_feeds(actors)
    fast_feeds = GENERAL_FAST_FEEDS + official_feeds
    for url in GENERAL_FAST_FEEDS:
        rationale.setdefault(url, []).append("credible-reporting feed; measured minutes-fresh, leads the poll order")
    for url in official_feeds:
        rationale.setdefault(url, []).append("official source; authoritative confirmation but measured days behind")
    queries: list[str] = []
    if actor_terms and keyword_terms:
        queries.append(" ".join(actor_terms[:2] + keyword_terms[:2]))
        queries.append(" ".join(actor_terms[:2] + keyword_terms[2:4]) if len(keyword_terms) > 2 else "")
    elif actor_terms:
        queries.append(" ".join(actor_terms[:3]))
    for wire in ("reuters", "AP"):
        if actor_terms:
            queries.append(f"{wire} " + " ".join(actor_terms[:2] + keyword_terms[:1]))
    # Every query runs on BOTH aggregators: they sit on separate
    # infrastructure (Google vs Microsoft), so one outage or degraded route
    # cannot blind the market's discovery layer.
    aggregator_feeds: list[str] = []
    for q in queries:
        text = q.strip()
        if not text:
            continue
        aggregator_feeds.append(google_news_rss(text))
        aggregator_feeds.append(bing_news_rss(text))
    for url in aggregator_feeds:
        label = "google news" if "news.google.com" in url else "bing news"
        rationale.setdefault(url, []).append(f"{label} discovery query from parties + decisive-event vocabulary")
    feed_urls = fast_feeds + aggregator_feeds

    auto_trade = sorted(set(WIRE_DOMAINS) | set(actor_domains) | ({resolution_domain} if resolution_domain else set()))
    escalate_terms = sorted(set(term.lower() for term in keyword_terms + actor_terms if term))

    return SourcePlan(
        market_id=context.market_id,
        rule_text_sha256=context.rule_text_sha256,
        feed_urls=feed_urls,
        poll_urls=[],
        auto_trade_domains=auto_trade,
        alert_only_domains=["x.com", "twitter.com", "t.me"],
        escalate_terms=escalate_terms,
        rationale=rationale,
        created_at=datetime.now(timezone.utc).isoformat(),
        schema_version=1,
        semantic_status=SOURCE_PLAN_LEGACY,
    )


def _build_rule_spec_source_plan(
    context: MarketContext,
    rule_spec: RuleSpec,
) -> SourcePlan:
    analysis = context.rule_analysis
    semantics = rule_spec.semantics
    actors = sorted(
        set(semantics.predicate.subjects)
        | set(analysis.mediators if analysis else [])
    )
    rationale: dict[str, list[str]] = {}
    records: list[PlannedSource] = []

    # Baseline context/confirmation coverage required by every supported
    # geopolitical family.
    for item in publisher_sources():
        if item.domain in WIRE_DOMAINS or item.feed_urls:
            records.append(item)
    records.extend(actor_sources(actors))

    requirements = list(semantics.source_requirements)
    bound_resolution_sources = _ordered_unique(
        [
            outcome.resolution_source.strip()
            for outcome in rule_spec.outcomes
            if outcome.resolution_source.strip()
        ]
        + (
            [context.resolution_source.strip()]
            if context.resolution_source.strip()
            else []
        )
    )
    existing_requirement_refs = {
        requirement.source_ref.casefold()
        for requirement in requirements
    }
    for resolution_source in bound_resolution_sources:
        if resolution_source.casefold() in existing_requirement_refs:
            continue
        from polybot.rules.contracts import SourceRequirement
        from polybot.rules.contracts import source_requirement_id

        requirement_id = source_requirement_id(
            resolution_source,
            ["SETTLEMENT"],
            True,
        )
        requirements.append(
            SourceRequirement(
                requirement_id=requirement_id,
                source_ref=resolution_source,
                roles=["SETTLEMENT"],
                required=True,
                rationale="resolution source bound to a market outcome",
            )
        )
        existing_requirement_refs.add(resolution_source.casefold())

    required_refs: list[str] = []
    missing_required: list[str] = []
    for requirement in requirements:
        if requirement.required:
            required_refs.append(requirement.source_ref)
        resolved = _resolve_requirement(
            requirement.source_ref,
            actors=actors,
            mediator_actors=list(analysis.mediators if analysis else []),
            roles=requirement.roles,
            required=requirement.required,
            requirement_id=requirement.requirement_id,
        )
        if requirement.required and not resolved:
            missing_required.append(requirement.source_ref)
        records.extend(resolved)
        for item in resolved:
            rationale.setdefault(item.domain, []).append(
                requirement.rationale
                or f"RuleSpec requires {','.join(requirement.roles)} role"
            )

    records = _merge_source_records(records)
    for item in records:
        if item.source_tier == "official":
            rationale.setdefault(item.domain, []).append(
                "official source for a RuleSpec predicate actor"
            )
        elif item.source_tier in {"wire", "tier_one_press"}:
            rationale.setdefault(item.domain, []).append(
                "independent confirmation/context source"
            )

    actor_terms = [
        actor.replace("_", " ")
        for actor in semantics.predicate.subjects[:4]
    ]
    keyword_terms = _rule_terms(rule_spec)
    direct_urls = [
        url
        for item in records
        for url in item.feed_urls
    ]
    queries = _source_queries(actor_terms, keyword_terms)
    aggregator_urls = [
        url
        for query in queries
        for url in (google_news_rss(query), bing_news_rss(query))
    ]
    for url in aggregator_urls:
        label = "google news" if "news.google.com" in url else "bing news"
        rationale.setdefault(url, []).append(
            f"{label} query derived from RuleSpec predicate"
        )
    records.extend(
        [
            PlannedSource(
                source_id="aggregator:google_news",
                organization_id="aggregator:google_news",
                independence_group="aggregator:google_news",
                domain="news.google.com",
                source_tier="aggregator",
                feed_urls=[
                    url
                    for url in aggregator_urls
                    if "news.google.com" in url
                ],
                roles=["DISCOVERY_ONLY"],
                timestamp_quality="indexed",
            ),
            PlannedSource(
                source_id="aggregator:bing_news",
                organization_id="aggregator:bing_news",
                independence_group="aggregator:bing_news",
                domain="bing.com",
                source_tier="aggregator",
                feed_urls=[
                    url
                    for url in aggregator_urls
                    if "bing.com" in url
                ],
                roles=["DISCOVERY_ONLY"],
                timestamp_quality="indexed",
            ),
        ]
    )
    records = _merge_source_records(records)

    feed_urls = _ordered_unique(direct_urls + aggregator_urls)
    poll_urls = _ordered_unique(
        [url for item in records for url in item.poll_urls]
    )
    auto_trade_domains = sorted(
        {
            item.domain
            for item in records
            if set(item.roles) & {"SETTLEMENT", "CONFIRMATION"}
            and item.source_tier != "state_affiliated_press"
        }
    )
    return SourcePlan(
        market_id=context.market_id,
        rule_text_sha256=context.rule_text_sha256,
        rule_spec_sha256=rule_spec.spec_sha256,
        source_records=records,
        feed_urls=feed_urls,
        poll_urls=poll_urls,
        auto_trade_domains=auto_trade_domains,
        alert_only_domains=["x.com", "twitter.com", "t.me"],
        escalate_terms=keyword_terms,
        rationale=rationale,
        required_source_refs=sorted(set(required_refs)),
        missing_required_source_refs=sorted(set(missing_required)),
        minimum_independent_confirmations=(
            semantics.resolution_policy.independent_confirmation_sources
        ),
        source_policy=semantics.source_policy.as_dict(),
        created_at=datetime.now(timezone.utc).isoformat(),
        schema_version=SOURCE_PLAN_SCHEMA_VERSION,
        semantic_status=SOURCE_PLAN_CURRENT,
    )


def _resolve_requirement(
    source_ref: str,
    *,
    actors: list[str],
    mediator_actors: list[str],
    roles: list[str],
    required: bool,
    requirement_id: str,
) -> list[PlannedSource]:
    normalized = " ".join(source_ref.casefold().split())
    if normalized in {
        "wire",
        "wires",
        "credible reporting",
        "consensus of credible reporting",
    }:
        return [
            _with_requirement(item, roles, False, requirement_id)
            for item in publisher_sources()
            if item.source_tier in {"wire", "tier_one_press"}
        ]
    if normalized in {
        "official government",
        "official_government",
        "government",
    }:
        return [
            _with_requirement(item, roles, False, requirement_id)
            for item in actor_sources(actors)
        ]
    if normalized in {
        "mediator government",
        "mediator_government",
        "mediator",
    }:
        return [
            _with_requirement(item, roles, False, requirement_id)
            for item in actor_sources(mediator_actors)
        ]
    return [
        _with_requirement(item, roles, required, requirement_id)
        for item in resolve_source_reference(
            source_ref,
            roles=roles,
            required=required,
        )
    ]


def _with_requirement(
    item: PlannedSource,
    roles: list[str],
    required: bool,
    requirement_id: str,
) -> PlannedSource:
    from dataclasses import replace

    return replace(
        item,
        roles=sorted(set(item.roles) | set(roles)),
        requirement_ids=sorted(
            set(item.requirement_ids) | {requirement_id}
        ),
        required=item.required or required,
    )


def _merge_source_records(
    records: list[PlannedSource],
) -> list[PlannedSource]:
    from dataclasses import replace

    merged: dict[tuple[str, str], PlannedSource] = {}
    for item in records:
        key = (item.organization_id, item.domain)
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            continue
        merged[key] = replace(
            existing,
            feed_urls=_ordered_unique(existing.feed_urls + item.feed_urls),
            poll_urls=_ordered_unique(existing.poll_urls + item.poll_urls),
            roles=sorted(set(existing.roles) | set(item.roles)),
            requirement_ids=sorted(
                set(existing.requirement_ids) | set(item.requirement_ids)
            ),
            required=existing.required or item.required,
        )
    return sorted(
        merged.values(),
        key=lambda item: (item.organization_id, item.domain),
    )


def _rule_terms(rule_spec: RuleSpec) -> list[str]:
    semantics = rule_spec.semantics
    terms = (
        list(semantics.predicate.subjects)
        + [semantics.predicate.action, semantics.predicate.object]
        + list(semantics.qualifying_conditions)
    )
    normalized = [
        " ".join(item.casefold().replace("_", " ").split())
        for item in terms
        if item.strip()
    ]
    return sorted(set(normalized))[:20]


def _source_queries(
    actor_terms: list[str],
    keyword_terms: list[str],
) -> list[str]:
    queries: list[str] = []
    if actor_terms and keyword_terms:
        queries.append(" ".join(actor_terms[:2] + keyword_terms[:2]))
        if len(keyword_terms) > 2:
            queries.append(" ".join(actor_terms[:2] + keyword_terms[2:4]))
    elif actor_terms:
        queries.append(" ".join(actor_terms[:3]))
    for wire in ("Reuters", "AP"):
        if actor_terms:
            queries.append(
                " ".join([wire] + actor_terms[:2] + keyword_terms[:1])
            )
    return _ordered_unique([query.strip() for query in queries if query.strip()])


def _ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _domain_from_source(resolution_source: str) -> str | None:
    text = resolution_source.strip().lower()
    if not text:
        return None
    for token in text.replace(",", " ").split():
        cleaned = token.strip(".,;:()[]\"'")
        if "." in cleaned and not cleaned.startswith("http"):
            parts = cleaned.split(".")
            if len(parts) >= 2 and all(parts):
                return cleaned.removeprefix("www.")
        if cleaned.startswith("http"):
            from urllib.parse import urlparse

            netloc = urlparse(cleaned).netloc
            if netloc:
                return netloc.removeprefix("www.")
    return None


def source_plan_sha256(plan: SourcePlan) -> str:
    execution_fields = {
        "market_id": plan.market_id,
        "rule_text_sha256": plan.rule_text_sha256,
        "rule_spec_sha256": plan.rule_spec_sha256,
        "source_records": [
            item.as_dict() for item in plan.source_records
        ],
        "feed_urls": plan.feed_urls,
        "poll_urls": plan.poll_urls,
        "auto_trade_domains": plan.auto_trade_domains,
        "alert_only_domains": plan.alert_only_domains,
        "escalate_terms": plan.escalate_terms,
        "required_source_refs": plan.required_source_refs,
        "missing_required_source_refs": plan.missing_required_source_refs,
        "minimum_independent_confirmations": (
            plan.minimum_independent_confirmations
        ),
        "source_policy": plan.source_policy,
        "schema_version": plan.schema_version,
        "semantic_status": plan.semantic_status,
    }
    encoded = json.dumps(
        execution_fields,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_source_plan_freshness(
    context: MarketContext,
    plan: SourcePlan,
    rule_spec: RuleSpec,
) -> None:
    rule_spec.validate_context_binding(context)
    if plan.schema_version != SOURCE_PLAN_SCHEMA_VERSION:
        raise ValueError(
            "source plan schema is legacy or unsupported"
        )
    if plan.semantic_status != SOURCE_PLAN_CURRENT:
        raise ValueError("source plan is not semantically current")
    if plan.market_id != context.market_id:
        raise ValueError("source plan market_id does not match context")
    if plan.rule_text_sha256 != context.rule_text_sha256:
        raise ValueError("source plan is stale for current rule text")
    if plan.rule_spec_sha256 != rule_spec.spec_sha256:
        raise ValueError("source plan is stale for current RuleSpec")
    if plan.source_policy != rule_spec.semantics.source_policy.as_dict():
        raise ValueError("source plan source policy does not match RuleSpec")
    if not plan.source_records:
        raise ValueError("source plan has no structured source records")
    if plan.missing_required_source_refs:
        raise ValueError(
            "source plan has unresolved required sources: "
            + ",".join(plan.missing_required_source_refs)
        )
    policy_ids = set(
        rule_spec.semantics.source_policy.requirement_ids
    )
    resolved_policy_ids = {
        requirement_id
        for source in plan.source_records
        for requirement_id in source.requirement_ids
    }
    missing_policy_ids = sorted(policy_ids - resolved_policy_ids)
    if missing_policy_ids:
        raise ValueError(
            "source plan has unresolved policy requirements: "
            + ",".join(missing_policy_ids)
        )

    feed_domains = {
        _url_domain(url)
        for url in plan.feed_urls
        if _url_domain(url)
    }
    recorded_feed_domains = {
        _url_domain(url)
        for source in plan.source_records
        for url in source.feed_urls
        if _url_domain(url)
    }
    if not feed_domains.issubset(recorded_feed_domains):
        raise ValueError(
            "source plan contains feeds without structured source records"
        )

    aggregator_domains = {"news.google.com", "bing.com", "www.bing.com"}
    semantic_domains = {
        source.domain.casefold().removeprefix("www.")
        for source in plan.source_records
        if set(source.roles) & {"CONFIRMATION", "SETTLEMENT"}
        and source.source_tier
        not in {"aggregator", "state_affiliated_press"}
    }
    auto_trade_domains = {
        domain.casefold().removeprefix("www.")
        for domain in plan.auto_trade_domains
    }
    if not auto_trade_domains.issubset(semantic_domains):
        raise ValueError(
            "auto-trade domains lack structured semantic authority"
        )
    for source in plan.source_records:
        normalized_domain = source.domain.casefold().removeprefix("www.")
        if (
            source.source_tier == "aggregator"
            or normalized_domain in {"news.google.com", "bing.com"}
        ) and set(source.roles) != {"DISCOVERY_ONLY"}:
            raise ValueError(
                "aggregator sources must be DISCOVERY_ONLY"
            )
        if source.required and not (
            source.feed_urls or source.poll_urls
        ):
            raise ValueError(
                f"required source has no usable endpoint:{source.source_id}"
            )
    if auto_trade_domains & {
        domain.removeprefix("www.")
        for domain in aggregator_domains
    }:
        raise ValueError(
            "aggregator domains cannot authorize semantic execution"
        )

    confirmation_groups = {
        source.independence_group
        for source in plan.source_records
        if set(source.roles) & {"CONFIRMATION", "SETTLEMENT"}
        and source.source_tier
        not in {"aggregator", "state_affiliated_press"}
    }
    if (
        len(confirmation_groups)
        < plan.minimum_independent_confirmations
    ):
        raise ValueError(
            "source plan lacks required independent confirmations"
        )


def _url_domain(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc.casefold().removeprefix("www.")
