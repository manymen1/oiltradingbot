from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Article:
    url: str
    domain: str
    title: str
    published_at: str | None
    fetched_at: str
    raw_text: str
    hash: str
    source_kind: str = "article"
    byline: str = ""
    origin_organization: str = ""
    # Point-in-time provenance for end-to-end latency accounting. Older
    # journals/SQLite rows omit these fields and continue to load through the
    # defaults. ``discovered_at`` is when a feed/listing/API first exposed the
    # item; ``fetch_started_at``/``fetched_at`` bound the publisher-page
    # request; ``parsed_at`` is when full text became classifier-ready.
    discovered_at: str = ""
    fetch_started_at: str = ""
    parsed_at: str = ""
    source_endpoint: str = ""
    source_adapter: str = ""

__all__ = ["Article"]
