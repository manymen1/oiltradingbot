from __future__ import annotations

from dataclasses import dataclass

from .config import UniverseConfig
from .types import MarketContext


@dataclass(frozen=True)
class ScopeDecision:
    status: str
    reason: str


def market_scope_decision(
    context: MarketContext,
    universe: UniverseConfig,
) -> ScopeDecision:
    """Re-evaluate a persisted context with the current universe policy."""
    if context.closed or context.state == "CLOSED":
        return ScopeDecision("TERMINAL", "market_closed")
    if context.state == "REJECTED":
        return ScopeDecision("OUT_OF_SCOPE", "state_rejected")

    text = "\n".join(
        [
            context.event_title,
            context.question,
            context.category,
            " ".join(context.tags),
        ]
    ).casefold()
    for term in universe.exclude_keywords:
        if term.casefold() in text:
            return ScopeDecision(
                "OUT_OF_SCOPE",
                f"excluded_keyword:{term}",
            )

    normalized_tags = {
        tag.casefold().strip() for tag in context.tags if tag.strip()
    }
    if context.category.strip():
        normalized_tags.add(context.category.casefold().strip())
    matching_tags = normalized_tags & {
        tag.casefold() for tag in universe.include_tags
    }
    if matching_tags:
        return ScopeDecision(
            "IN_SCOPE",
            "tag_match:" + ",".join(sorted(matching_tags)),
        )
    for term in universe.include_keywords:
        if term.casefold() in text:
            return ScopeDecision(
                "IN_SCOPE",
                f"keyword_match:{term}",
            )
    return ScopeDecision("REVIEW_REQUIRED", "no_geopolitical_signal")


__all__ = ["ScopeDecision", "market_scope_decision"]
