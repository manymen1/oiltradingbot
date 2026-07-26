from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .types import Article


def is_feed_summary(article: Article) -> bool:
    return article.source_kind in {"feed", "feed_item", "promoted_feed_summary"}


def article_age_hours(
    article: Article,
    *,
    as_of: datetime | None = None,
) -> float | None:
    if not article.published_at:
        return None
    try:
        published = parsedate_to_datetime(article.published_at)
    except (TypeError, ValueError):
        try:
            published = datetime.fromisoformat(article.published_at)
        except ValueError:
            return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    now = as_of or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - published).total_seconds() / 3600.0
