from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from polybot.config import SETTINGS
from polybot.gamma import market_from_gamma

from .config import EnumerationView, UniverseConfig
from .types import MarketContext, OutcomeRecord


@dataclass(frozen=True)
class UniverseEnumeration:
    """One immutable view of a point-in-time Gamma universe scan."""

    events: list[dict[str, Any]]
    manifest: dict[str, Any]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def enumerate_active_events(
    universe: UniverseConfig,
    *,
    gamma_host: str = SETTINGS.gamma_host,
    fetch: Callable[[str, dict[str, Any]], Any] | None = None,
    scanned_at: str | None = None,
) -> UniverseEnumeration:
    """Fully enumerate configured Gamma views using keyset pagination.

    Production uses ``/events/keyset`` and its opaque ``next_cursor``. Injected
    list-returning fixtures retain the legacy offset contract so historical
    tests and offline captures remain replayable; production never sends an
    offset to the keyset endpoint.
    """

    fetcher = fetch or _http_fetch
    production_fetch = fetch is None
    started_at = scanned_at or datetime.now(timezone.utc).isoformat()
    url = f"{gamma_host.rstrip('/')}/events/keyset"
    first_payloads: dict[str, dict[str, Any]] = {}
    first_hashes: dict[str, str] = {}
    conflicts: dict[str, set[str]] = {}
    fallback_ids: set[str] = set()
    view_reports: dict[str, dict[str, Any]] = {}
    raw_events = 0
    duplicate_rows = 0
    truncated = False
    incomplete = False
    stop_all = False

    for view_index, view in enumerate(universe.enumeration_views):
        view_name = f"{view_index}:{view.order}:{'asc' if view.ascending else 'desc'}"
        pages = 0
        rows_seen = 0
        unique_seen: set[str] = set()
        page_hashes: list[str] = []
        cursor_history: list[dict[str, Any]] = []
        seen_page_hashes: set[str] = set()
        seen_cursors: set[str] = set()
        cursor = ""
        offset = 0
        mode = "keyset"
        state = "COMPLETE"
        failure = ""

        while not stop_all:
            if pages >= universe.pagination_safety_pages:
                state = "INCOMPLETE"
                failure = "pagination_safety_limit"
                incomplete = True
                break
            params: dict[str, Any] = {
                "active": "true",
                "closed": "false",
                "archived": "false",
                "order": view.order,
                "ascending": str(view.ascending).lower(),
                "limit": universe.page_size,
            }
            if cursor:
                params["after_cursor"] = cursor
            elif not production_fetch and mode == "offset" and offset:
                params["offset"] = offset
            try:
                response = fetcher(url, params)
            except Exception as exc:
                state = "INCOMPLETE"
                failure = f"fetch_error:{type(exc).__name__}:{exc}"
                incomplete = True
                break

            next_cursor = ""
            if isinstance(response, dict):
                rows_raw = response.get("events")
                if not isinstance(rows_raw, list):
                    state = "INCOMPLETE"
                    failure = "malformed_keyset_response"
                    incomplete = True
                    break
                next_raw = response.get("next_cursor")
                if next_raw is not None and not isinstance(next_raw, str):
                    state = "INCOMPLETE"
                    failure = "malformed_next_cursor"
                    incomplete = True
                    break
                next_cursor = str(next_raw or "")
                mode = "keyset"
            elif isinstance(response, list):
                rows_raw = response
                mode = "offset"
            else:
                state = "INCOMPLETE"
                failure = "malformed_page_response"
                incomplete = True
                break
            if any(not isinstance(item, dict) for item in rows_raw):
                state = "INCOMPLETE"
                failure = "malformed_event_row"
                incomplete = True
                break
            rows = [dict(item) for item in rows_raw]
            page_hash = _sha256(rows)
            if page_hash in seen_page_hashes and rows:
                state = "INCOMPLETE"
                failure = "repeated_page"
                incomplete = True
                break
            seen_page_hashes.add(page_hash)
            page_hashes.append(page_hash)
            cursor_history.append(
                {
                    "page": pages + 1,
                    "after_cursor": cursor or None,
                    "offset": (
                        offset if mode == "offset" else None
                    ),
                    "next_cursor": next_cursor or None,
                }
            )
            pages += 1
            rows_seen += len(rows)
            raw_events += len(rows)

            cap_reached = False
            for event in rows:
                identity, used_fallback = _event_identity(event)
                if not identity:
                    state = "INCOMPLETE"
                    failure = "event_missing_stable_identity"
                    incomplete = True
                    break
                if used_fallback:
                    fallback_ids.add(identity)
                payload_hash = _sha256(event)
                if identity in first_payloads:
                    duplicate_rows += 1
                    if first_hashes[identity] != payload_hash:
                        conflicts.setdefault(identity, set()).add(payload_hash)
                else:
                    first_payloads[identity] = event
                    first_hashes[identity] = payload_hash
                unique_seen.add(identity)
                if (
                    universe.max_events > 0
                    and len(first_payloads) >= universe.max_events
                ):
                    truncated = True
                    state = "TRUNCATED"
                    failure = "configured_event_cap"
                    stop_all = True
                    cap_reached = True
                    break
            if state == "INCOMPLETE" or stop_all:
                break
            if cap_reached:
                break
            if not rows:
                break
            if mode == "keyset":
                if not next_cursor:
                    break
                if next_cursor == cursor or next_cursor in seen_cursors:
                    state = "INCOMPLETE"
                    failure = "repeated_cursor"
                    incomplete = True
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            else:
                if len(rows) < universe.page_size:
                    break
                offset += len(rows)

        view_reports[view_name] = {
            "query": {
                "order": view.order,
                "ascending": view.ascending,
                "page_size": universe.page_size,
            },
            "pagination": mode,
            "pages_fetched": pages,
            "raw_rows": rows_seen,
            "unique_rows": len(unique_seen),
            "duplicate_rows": rows_seen - len(unique_seen),
            "page_sha256s": page_hashes,
            "cursor_history": cursor_history,
            "state": state,
            "failure": failure or None,
        }

    stable_manifest = {
        "schema_version": 1,
        "config": {
            "max_events": universe.max_events,
            "page_size": universe.page_size,
            "fail_on_truncation": universe.fail_on_truncation,
            "pagination_safety_pages": universe.pagination_safety_pages,
            "enumeration_views": [
                {"order": view.order, "ascending": view.ascending}
                for view in universe.enumeration_views
            ],
        },
        "views": view_reports,
        "raw_events": raw_events,
        "unique_events": len(first_payloads),
        "duplicate_rows": duplicate_rows,
        "event_payload_sha256s": {
            identity: first_hashes[identity]
            for identity in sorted(first_hashes)
        },
        "payload_conflicts": {
            identity: sorted({first_hashes[identity], *hashes})
            for identity, hashes in sorted(conflicts.items())
        },
        "slug_fallback_identities": sorted(fallback_ids),
        "truncated": truncated,
        "coverage_complete": not truncated and not incomplete,
    }
    coverage_sha256 = _sha256(stable_manifest)
    status = (
        "TRUNCATED"
        if truncated
        else "INCOMPLETE"
        if incomplete
        else "COMPLETE"
    )
    ended_at = (
        started_at
        if scanned_at is not None
        else datetime.now(timezone.utc).isoformat()
    )
    manifest = {
        **stable_manifest,
        "coverage_status": status,
        "coverage_sha256": coverage_sha256,
        "config_sha256": _sha256(stable_manifest["config"]),
        "scan_started_at": started_at,
        "scan_ended_at": ended_at,
    }
    manifest["manifest_sha256"] = _sha256(manifest)
    events = [
        first_payloads[identity]
        for identity in sorted(first_payloads)
    ]
    if universe.max_events > 0:
        events = events[: universe.max_events]
    return UniverseEnumeration(events=events, manifest=manifest)


def fetch_active_events(
    *,
    limit: int,
    page_size: int = 100,
    gamma_host: str = SETTINGS.gamma_host,
    fetch: Callable[[str, dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Backward-compatible list surface over the audited enumerator."""

    universe = UniverseConfig(
        max_events=max(0, int(limit)),
        page_size=page_size,
        fail_on_truncation=False,
        enumeration_views=[
            EnumerationView(order="liquidity", ascending=False)
        ],
    )
    return enumerate_active_events(
        universe,
        gamma_host=gamma_host,
        fetch=fetch,
    ).events


def _http_fetch(url: str, params: dict[str, Any]) -> Any:
    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def _event_identity(event: dict[str, Any]) -> tuple[str, bool]:
    event_id = str(event.get("id") or "").strip()
    if event_id:
        return f"id:{event_id}", False
    slug = str(event.get("slug") or "").strip()
    if slug:
        return f"slug:{slug}", True
    return "", False


def is_geopolitical_candidate(event: dict[str, Any], universe: UniverseConfig) -> tuple[bool, str]:
    """Category/tag + question/description keyword filter. Returns (candidate,
    reason) so rejections are explainable in the funnel report."""
    text_parts = [str(event.get("title") or ""), str(event.get("description") or "")]
    for market in event.get("markets") or []:
        if isinstance(market, dict):
            text_parts.append(str(market.get("question") or ""))
    text = "\n".join(text_parts).lower()
    tags = _event_tags(event)

    for term in universe.exclude_keywords:
        # Exclusions are category words, not arbitrary substrings. Raw
        # substring matching made "nfl" reject "conflict" and "stock" reject
        # "stockpile", silently dropping real geopolitical markets.
        if _contains_excluded_term(text, term):
            return False, f"excluded_keyword:{term}"
    if any(tag in universe.include_tags for tag in tags):
        return True, f"tag_match:{','.join(sorted(set(tags) & set(universe.include_tags)))}"
    for term in universe.include_keywords:
        if term.lower() in text:
            return True, f"keyword_match:{term}"
    return False, "no_geopolitical_signal"


def _contains_excluded_term(text: str, term: str) -> bool:
    normalized = term.strip().casefold()
    if not normalized:
        return False
    return re.search(
        rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])",
        text,
    ) is not None


def _event_tags(event: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    raw_tags = event.get("tags")
    if isinstance(raw_tags, list):
        for item in raw_tags:
            if isinstance(item, dict):
                label = str(item.get("label") or item.get("slug") or "").strip().lower()
                if label:
                    tags.append(label)
            elif isinstance(item, str):
                tags.append(item.strip().lower())
    category = str(event.get("category") or "").strip().lower()
    if category:
        tags.append(category)
    return tags


def context_from_event(event: dict[str, Any]) -> MarketContext | None:
    """Build the durable context skeleton (identity, outcomes, token mappings,
    rule text + hash, tradeability) from one raw Gamma event. Rule ANALYSIS is
    added separately by the analyzer."""
    markets_raw = [m for m in (event.get("markets") or []) if isinstance(m, dict)]
    if not markets_raw:
        return None
    now = datetime.now(timezone.utc).isoformat()
    metas = []
    for raw in markets_raw:
        try:
            metas.append(
                (
                    raw,
                    market_from_gamma(event, raw, observed_at=now),
                )
            )
        except ValueError:
            continue
    if not metas:
        return None

    grouped = len(metas) > 1
    event_slug = str(event.get("slug") or "")
    outcome_topology = _outcome_topology(event, metas)
    outcomes: list[OutcomeRecord] = []
    for raw, meta in metas:
        label = str(raw.get("groupItemTitle") or "").strip() if grouped else "Yes"
        name = _normalize(label or meta.question)
        outcome_rule_text = "\n\n".join(
            part
            for part in (
                meta.description.strip(),
                meta.resolution_source.strip(),
            )
            if part
        )
        outcome_rule_sha256 = (
            hashlib.sha256(outcome_rule_text.encode("utf-8")).hexdigest()
            if outcome_rule_text
            else ""
        )
        outcome_deadline = str(raw.get("endDate") or "")
        outcomes.append(
            OutcomeRecord(
                name=name,
                label=label or meta.question,
                market_slug=meta.market_slug,
                question=meta.question,
                condition_id=meta.condition_id,
                yes_token_id=meta.yes_token_id,
                no_token_id=meta.no_token_id,
                deadline_iso=outcome_deadline,
                rule_text=outcome_rule_text,
                rule_text_sha256=outcome_rule_sha256,
                resolution_source=meta.resolution_source.strip(),
                deadline_consistency=_deadline_consistency(
                    label=label,
                    question=meta.question,
                    deadline_iso=outcome_deadline,
                ),
                tick_size=meta.tick_size,
                neg_risk=meta.neg_risk,
                last_yes_price=meta.outcome_prices[0] if meta.outcome_prices else None,
                volume=meta.volume,
                liquidity=meta.liquidity,
                active=meta.active,
                closed=meta.closed,
                accepting_orders=meta.accepting_orders,
                fee_schedule=meta.fee_schedule,
                fee_schedule_error=meta.fee_schedule_error,
            )
        )

    if grouped:
        rule_text = str(event.get("description") or "").strip()
        market_id = event_slug
    else:
        _, meta = metas[0]
        rule_text = "\n\n".join(part for part in [meta.description, meta.resolution_source] if part).strip()
        market_id = meta.condition_id or event_slug
    digest = hashlib.sha256(rule_text.encode("utf-8")).hexdigest() if rule_text else ""

    deadline = str(event.get("endDate") or (markets_raw[0].get("endDate") if markets_raw else "") or "")
    return MarketContext(
        market_id=market_id,
        kind="grouped" if grouped else "binary",
        event_slug=event_slug,
        event_title=str(event.get("title") or ""),
        question=str(event.get("title") or metas[0][1].question),
        deadline_iso=deadline,
        outcomes=outcomes,
        rule_text=rule_text,
        rule_text_sha256=digest,
        rule_version=1,
        outcome_topology=outcome_topology,
        resolution_source=str(event.get("resolutionSource") or metas[0][1].resolution_source or ""),
        neg_risk=any(meta.neg_risk for _, meta in metas),
        category=str(event.get("category") or ""),
        tags=_event_tags(event),
        volume=sum(meta.volume for _, meta in metas),
        liquidity=sum(meta.liquidity for _, meta in metas),
        active=any(meta.active for _, meta in metas),
        closed=all(meta.closed for _, meta in metas),
        accepting_orders=any(meta.accepting_orders for _, meta in metas),
        state="DISCOVERED",
        discovered_at=now,
        updated_at=now,
    )


_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_DATE_LABEL = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:,\s*(\d{4}))?\b",
    re.IGNORECASE,
)


def _deadline_consistency(
    *,
    label: str,
    question: str,
    deadline_iso: str,
) -> str:
    match = _DATE_LABEL.search(label) or _DATE_LABEL.search(question)
    if match is None or not deadline_iso.strip():
        return "UNKNOWN"
    try:
        deadline = datetime.fromisoformat(
            deadline_iso.strip().replace("Z", "+00:00")
        )
    except ValueError:
        return "MISMATCH"
    expected_year = int(match.group(3)) if match.group(3) else deadline.year
    expected = (
        expected_year,
        _MONTHS[match.group(1).casefold()],
        int(match.group(2)),
    )
    observed = (deadline.year, deadline.month, deadline.day)
    return "MATCH" if observed == expected else "MISMATCH"


def _outcome_topology(
    event: dict[str, Any],
    metas: list[tuple[dict[str, Any], Any]],
) -> str:
    if len(metas) == 1:
        return "SINGLE_BINARY"
    if bool(event.get("negRisk")) or any(meta.neg_risk for _, meta in metas):
        return "EXCLUSIVE_ONE_OF_N"
    text = "\n".join(
        [
            str(event.get("title") or ""),
            *(str(meta.question or "") for _, meta in metas),
        ]
    ).casefold()
    if re.search(r"\bon(?:\.\.\.|\s+which|\s+[a-z]+\s+\d)", text):
        return "INDEPENDENT_MULTI"
    if re.search(r"\b(?:by|through)(?:\.\.\.|\s+[a-z]+\s+\d)", text):
        return "MONOTONE_DEADLINE_LADDER"
    return "UNCLASSIFIED"


def merge_refresh(existing: MarketContext, fresh: MarketContext) -> MarketContext:
    """Refresh live fields on an existing record; preserve analysis/state
    unless the rule text changed, in which case the analysis is dropped, the
    version bumps, and the market falls back to RULES_REVIEW_REQUIRED (the
    changed-rule-hash execution block)."""
    rule_changed = existing.rule_text_sha256 != fresh.rule_text_sha256
    merged = {
        **existing.as_dict(),
        "outcomes": [o.__dict__ for o in fresh.outcomes],
        "deadline_iso": fresh.deadline_iso,
        "volume": fresh.volume,
        "liquidity": fresh.liquidity,
        "active": fresh.active,
        "closed": fresh.closed,
        "accepting_orders": fresh.accepting_orders,
        "tags": fresh.tags,
        "category": fresh.category,
        "outcome_topology": fresh.outcome_topology,
        "resolution_source": fresh.resolution_source,
        "neg_risk": fresh.neg_risk,
    }
    if rule_changed:
        merged.update(
            {
                "rule_text": fresh.rule_text,
                "rule_text_sha256": fresh.rule_text_sha256,
                "rule_version": existing.rule_version + 1,
                "rule_analysis": None,
                "state": "RULES_REVIEW_REQUIRED",
                "state_reasons": ["rule_text_changed"],
            }
        )
    return MarketContext.from_dict(merged)


def _normalize(value: str) -> str:
    return value.strip().lower().replace(" ", "_")
