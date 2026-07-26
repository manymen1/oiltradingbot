from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from polybot.core.central_feed import (
    CentralFeedReader,
    CentralFeedPromotionCache,
    CentralFeedService,
    CentralFeedStore,
    CentralFeedUnavailable,
)
from polybot.core.types import Article
from polybot.core.source_fetcher import fetch_direct_source_articles


def _article(name: str, text: str) -> Article:
    return Article(
        url=f"https://example.com/{name}",
        domain="example.com",
        title=text,
        published_at="2026-07-24T00:00:00+00:00",
        fetched_at="2026-07-24T00:00:01+00:00",
        raw_text=text,
        hash=name,
        source_kind="feed",
    )


def test_service_fetches_union_once_and_deduplicates_restart_rows(tmp_path) -> None:
    calls: list[str] = []

    def fetcher(url, user_agent, **kwargs):
        calls.append(url)
        return [_article("iran", "Iran talks begin")]

    store = CentralFeedStore(tmp_path / "central.sqlite3")
    service = CentralFeedService(
        store=store,
        feed_urls_provider=lambda: ["https://feed.example/rss", "https://feed.example/rss"],
        poll_seconds=2,
        max_workers=4,
        max_entries_per_feed=20,
        retention_hours=72,
        fetcher=fetcher,
    )

    assert service.poll_once(force=True) == {
        "feeds": 1,
        "polled": 1,
        "inserted": 1,
        "errors": 0,
        "pruned": 0,
    }
    assert service.poll_once(force=True) == {
        "feeds": 1,
        "polled": 1,
        "inserted": 0,
        "errors": 0,
        "pruned": 0,
    }
    assert calls == ["https://feed.example/rss", "https://feed.example/rss"]
    status = store.status()
    assert status["active_feeds"] == 1
    assert status["known_feeds"] == 1
    assert status["stored_rows"] == 1
    assert status["inserted_total"] == 1


def test_service_routes_required_direct_sources_through_separate_adapter(
    tmp_path,
) -> None:
    feed_calls: list[str] = []
    direct_calls: list[str] = []

    def feed_fetcher(url, user_agent, **kwargs):
        feed_calls.append(url)
        return []

    def direct_fetcher(url, user_agent, **kwargs):
        direct_calls.append(url)
        return [_article("official", "Official announcement")]

    service = CentralFeedService(
        store=CentralFeedStore(tmp_path / "central.sqlite3"),
        feed_urls_provider=lambda: ["https://feed.example/rss"],
        direct_urls_provider=lambda: ["https://official.example/releases"],
        poll_seconds=2,
        direct_poll_seconds=2,
        max_workers=4,
        max_entries_per_feed=20,
        max_entries_per_direct_source=5,
        retention_hours=72,
        fetcher=feed_fetcher,
        direct_fetcher=direct_fetcher,
    )

    summary = service.poll_once(force=True)
    assert summary == {
        "feeds": 1,
        "direct_sources": 1,
        "polled": 2,
        "direct_polled": 1,
        "inserted": 1,
        "errors": 0,
        "pruned": 0,
    }
    assert feed_calls == ["https://feed.example/rss"]
    assert direct_calls == ["https://official.example/releases"]


def test_unchanged_direct_source_adapts_poll_interval(
    tmp_path,
    monkeypatch,
) -> None:
    direct_url = "https://official.example/releases"
    service = CentralFeedService(
        store=CentralFeedStore(tmp_path / "central.sqlite3"),
        feed_urls_provider=lambda: [],
        direct_urls_provider=lambda: [direct_url],
        poll_seconds=2,
        direct_poll_seconds=2,
        direct_idle_max_seconds=8,
        max_workers=1,
        max_entries_per_feed=20,
        max_entries_per_direct_source=5,
        retention_hours=72,
        direct_fetcher=lambda *args, **kwargs: [],
    )

    monkeypatch.setattr(
        "polybot.core.central_feed.time.monotonic",
        lambda: 100.0,
    )
    assert service.poll_once(force=True)["direct_polled"] == 1
    assert service._direct_interval(direct_url) == 4.0

    monkeypatch.setattr(
        "polybot.core.central_feed.time.monotonic",
        lambda: 103.0,
    )
    assert service.poll_once()["direct_polled"] == 0
    monkeypatch.setattr(
        "polybot.core.central_feed.time.monotonic",
        lambda: 104.0,
    )
    assert service.poll_once()["direct_polled"] == 1
    assert service._direct_interval(direct_url) == 8.0


def test_direct_source_priority_breaks_same_domain_contention(
    tmp_path,
) -> None:
    calls: list[str] = []
    low = "https://official.example/releases/low"
    high = "https://official.example/releases/high"
    service = CentralFeedService(
        store=CentralFeedStore(tmp_path / "central.sqlite3"),
        feed_urls_provider=lambda: [],
        direct_urls_provider=lambda: [low, high],
        direct_priorities_provider=lambda: {low: 1.0, high: 9.0},
        poll_seconds=2,
        direct_poll_seconds=2,
        max_workers=1,
        max_entries_per_feed=20,
        max_entries_per_direct_source=5,
        max_urls_per_domain_per_cycle=1,
        retention_hours=72,
        direct_fetcher=lambda url, *args, **kwargs: (
            calls.append(url) or []
        ),
    )

    assert service.poll_once(force=True)["direct_polled"] == 1
    assert calls == [high]


def test_direct_json_adapter_requires_explicit_claim_envelope(
    monkeypatch,
) -> None:
    payload = {
        "url": "https://official.example/releases/1",
        "title": "Official result",
        "published_at": "2026-07-25T00:00:00Z",
        "polybot_claim": {
            "market_id": "market",
            "rule_spec_sha256": "a" * 64,
            "fact": {"assertion": "PREDICATE_SATISFIED"},
        },
    }
    encoded = json.dumps(payload).encode()
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/json"},
        content=encoded,
        text=encoded.decode(),
        encoding="utf-8",
        raise_for_status=lambda: None,
    )
    monkeypatch.setattr(
        "polybot.core.source_fetcher.requests.get",
        lambda *args, **kwargs: response,
    )

    articles = fetch_direct_source_articles(
        "https://official.example/api/releases",
        limit=5,
    )
    assert len(articles) == 1
    assert articles[0].source_kind == "official_claim_json"
    assert articles[0].source_adapter == "official_claim_json_v1"
    assert articles[0].discovered_at
    assert json.loads(articles[0].raw_text)["market_id"] == "market"


def test_direct_json_adapter_ignores_cross_domain_targets(
    monkeypatch,
) -> None:
    payload = {
        "url": "http://127.0.0.1/internal",
        "title": "Untrusted redirect target",
        "polybot_claim": {
            "market_id": "market",
            "rule_spec_sha256": "a" * 64,
            "fact": {"assertion": "PREDICATE_SATISFIED"},
        },
    }
    encoded = json.dumps(payload).encode()
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/json"},
        content=encoded,
        text=encoded.decode(),
        encoding="utf-8",
        raise_for_status=lambda: None,
    )
    monkeypatch.setattr(
        "polybot.core.source_fetcher.requests.get",
        lambda *args, **kwargs: response,
    )

    assert fetch_direct_source_articles(
        "https://official.example/api/releases",
        limit=5,
    ) == []


def test_readers_fan_out_with_independent_filters_and_per_feed_cursors(tmp_path) -> None:
    store = CentralFeedStore(tmp_path / "central.sqlite3")
    store.touch_heartbeat()
    feed_a = "https://feed.example/a"
    feed_b = "https://feed.example/b"
    store.record_success(
        feed_a,
        [
            _article("iran", "Iran negotiations scheduled"),
            _article("sport", "World Cup result"),
        ],
    )
    store.record_success(feed_b, [_article("israel", "Israel ceasefire update")])

    iran_reader = CentralFeedReader(
        store.path,
        tmp_path / "iran-cursor.json",
        stale_after_seconds=60,
    )
    first = iran_reader.read(
        [feed_a],
        include_terms=["iran"],
        exclude_terms=["world cup"],
        limit_per_feed=20,
    )
    assert [article.hash for article in first.articles] == ["iran"]
    iran_reader.ack_pending()
    assert iran_reader.read(
        [feed_a],
        include_terms=["iran"],
        exclude_terms=[],
        limit_per_feed=20,
    ).articles == []

    # Adding a feed later starts that feed at cursor zero. A single global
    # cursor would incorrectly skip this already-ingested row.
    added = iran_reader.read(
        [feed_a, feed_b],
        include_terms=["israel"],
        exclude_terms=[],
        limit_per_feed=20,
    )
    assert [article.hash for article in added.articles] == ["israel"]

    israel_reader = CentralFeedReader(
        store.path,
        tmp_path / "israel-cursor.json",
        stale_after_seconds=60,
    )
    independent = israel_reader.read(
        [feed_b],
        include_terms=["ceasefire"],
        exclude_terms=[],
        limit_per_feed=20,
    )
    assert [article.hash for article in independent.articles] == ["israel"]


def test_reader_fails_closed_on_stale_producer_without_advancing_cursor(tmp_path) -> None:
    store = CentralFeedStore(tmp_path / "central.sqlite3")
    store.record_success("https://feed.example/rss", [_article("x", "Iran update")])
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='service_heartbeat'",
            (stale,),
        )

    cursor = tmp_path / "cursor.json"
    reader = CentralFeedReader(store.path, cursor, stale_after_seconds=30)
    with pytest.raises(CentralFeedUnavailable, match="heartbeat stale"):
        reader.read(
            ["https://feed.example/rss"],
            include_terms=["iran"],
            exclude_terms=[],
            limit_per_feed=20,
        )
    assert not cursor.exists()


def test_service_serializes_requests_per_domain(tmp_path) -> None:
    active: dict[str, int] = {}
    peak: dict[str, int] = {}
    lock = threading.Lock()

    def fetcher(url, user_agent, **kwargs):
        domain = url.split("/", 3)[2]
        with lock:
            active[domain] = active.get(domain, 0) + 1
            peak[domain] = max(peak.get(domain, 0), active[domain])
        time.sleep(0.02)
        with lock:
            active[domain] -= 1
        return []

    service = CentralFeedService(
        store=CentralFeedStore(tmp_path / "central.sqlite3"),
        feed_urls_provider=lambda: [
            "https://aggregator.example/query-a",
            "https://aggregator.example/query-b",
            "https://publisher.example/world",
        ],
        poll_seconds=2,
        max_workers=8,
        max_entries_per_feed=20,
        retention_hours=72,
        fetcher=fetcher,
    )
    assert service.poll_once(force=True)["errors"] == 0
    assert peak["aggregator.example"] == 1


def test_service_rotates_bounded_aggregator_queries_across_cycles(tmp_path) -> None:
    calls: list[str] = []
    urls = [f"https://news.google.com/rss/search?q=query-{index}" for index in range(7)]

    def fetcher(url, user_agent, **kwargs):
        calls.append(url)
        return []

    service = CentralFeedService(
        store=CentralFeedStore(tmp_path / "central.sqlite3"),
        feed_urls_provider=lambda: urls,
        poll_seconds=2,
        aggregator_poll_seconds=30,
        max_urls_per_domain_per_cycle=3,
        max_workers=8,
        max_entries_per_feed=20,
        retention_hours=72,
        fetcher=fetcher,
    )
    assert service.poll_once(force=True)["polled"] == 3
    assert service.poll_once(force=True)["polled"] == 3
    assert service.poll_once(force=True)["polled"] == 3
    assert set(calls[:7]) == set(urls)


def test_publisher_promotion_is_single_flight_across_market_caches(tmp_path) -> None:
    db_path = tmp_path / "central.sqlite3"
    CentralFeedStore(db_path)
    caches = [
        CentralFeedPromotionCache(db_path),
        CentralFeedPromotionCache(db_path),
    ]
    feed_article = _article("shared-story", "Iran talks scheduled")
    barrier = threading.Barrier(2)
    calls = 0
    lock = threading.Lock()

    def promoter(article: Article, user_agent: str) -> Article:
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.1)
        return Article(
            url=article.url,
            domain=article.domain,
            title=article.title,
            published_at=article.published_at,
            fetched_at=article.fetched_at,
            raw_text="full publisher text",
            hash="promoted-shared-story",
            source_kind="article",
        )

    def run(cache: CentralFeedPromotionCache) -> Article | None:
        barrier.wait()
        return cache.promote(feed_article, "ua", promoter=promoter)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, caches))

    assert calls == 1
    assert [result.hash if result else None for result in results] == [
        "promoted-shared-story",
        "promoted-shared-story",
    ]
    status = CentralFeedStore(db_path).status()
    assert status["promotion_cached_rows"] == 1
    assert status["promotion_cache_hits"] == 1


def test_publisher_promotion_failure_is_briefly_shared(tmp_path) -> None:
    db_path = tmp_path / "central.sqlite3"
    CentralFeedStore(db_path)
    cache = CentralFeedPromotionCache(db_path, failure_ttl_seconds=60)
    calls = 0

    def promoter(article: Article, user_agent: str) -> None:
        nonlocal calls
        calls += 1
        return None

    article = _article("blocked-story", "Iran talks update")
    assert cache.promote(article, "ua", promoter=promoter) is None
    assert cache.promote(article, "ua", promoter=promoter) is None
    assert calls == 1
