from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

from polybot.core.holdings import _atomic_json_write
from polybot.core.types import Article
from polybot.log import log_event

from .source_fetcher import (
    fetch_direct_source_articles,
    fetch_feed_articles,
    promote_feed_article,
)


class CentralFeedUnavailable(RuntimeError):
    """The shared feed is missing or its producer heartbeat is stale."""


@dataclass(frozen=True)
class CentralFeedBatch:
    articles: list[Article]
    cursors: dict[str, int]


class CentralFeedStore:
    """WAL-backed fan-out store for one fetcher and many market readers.

    A row is unique per (feed URL, article hash), rather than globally by
    article hash, because the same publisher item can satisfy several
    market-specific aggregator queries. Per-feed consumer cursors preserve
    that routing relationship while each bot's ArticleStore still removes
    content duplicates before classification.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5.0)
        else:
            connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feed_url TEXT NOT NULL,
                    article_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    UNIQUE(feed_url, article_hash)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_central_articles_feed_id ON articles(feed_url, id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS feeds (
                    feed_url TEXT PRIMARY KEY,
                    last_polled_at TEXT NOT NULL,
                    last_success_at TEXT,
                    last_error TEXT,
                    inserted_total INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            _initialize_promotion_cache(connection)

    def touch_heartbeat(self) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('service_heartbeat', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (now,),
            )

    def set_active_feeds(self, feed_urls: Iterable[str]) -> None:
        payload = json.dumps(sorted(set(feed_urls)), separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('active_feeds', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (payload,),
            )

    def record_success(self, feed_url: str, articles: Iterable[Article]) -> int:
        now = _now()
        inserted = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for article in articles:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO articles(feed_url, article_hash, payload_json, ingested_at)
                    VALUES(?, ?, ?, ?)
                    """,
                    (
                        feed_url,
                        article.hash,
                        json.dumps(asdict(article), sort_keys=True, separators=(",", ":")),
                        now,
                    ),
                )
                inserted += max(0, cursor.rowcount)
            connection.execute(
                """
                INSERT INTO feeds(feed_url, last_polled_at, last_success_at, last_error, inserted_total)
                VALUES(?, ?, ?, NULL, ?)
                ON CONFLICT(feed_url) DO UPDATE SET
                    last_polled_at=excluded.last_polled_at,
                    last_success_at=excluded.last_success_at,
                    last_error=NULL,
                    inserted_total=feeds.inserted_total + excluded.inserted_total
                """,
                (feed_url, now, now, inserted),
            )
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('service_heartbeat', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (now,),
            )
        return inserted

    def record_error(self, feed_url: str, error: str) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO feeds(feed_url, last_polled_at, last_success_at, last_error, inserted_total)
                VALUES(?, ?, NULL, ?, 0)
                ON CONFLICT(feed_url) DO UPDATE SET
                    last_polled_at=excluded.last_polled_at,
                    last_error=excluded.last_error
                """,
                (feed_url, now, error[:1000]),
            )
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('service_heartbeat', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (now,),
            )

    def prune(self, retention_hours: float) -> int:
        if retention_hours <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=retention_hours)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM articles WHERE ingested_at < ?", (cutoff,))
            connection.execute(
                "DELETE FROM promotion_cache WHERE expires_at < ? AND state != 'pending'",
                (time.time() - 3600.0,),
            )
            return max(0, cursor.rowcount)

    def status(self) -> dict[str, object]:
        with self._connect(read_only=True) as connection:
            heartbeat_row = connection.execute(
                "SELECT value FROM metadata WHERE key='service_heartbeat'"
            ).fetchone()
            active_row = connection.execute(
                "SELECT value FROM metadata WHERE key='active_feeds'"
            ).fetchone()
            totals = connection.execute(
                """
                SELECT
                    COUNT(*) AS configured_feeds,
                    SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END) AS feeds_in_error,
                    COALESCE(SUM(inserted_total), 0) AS inserted_total
                FROM feeds
                """
            ).fetchone()
            stored = connection.execute("SELECT COUNT(*) AS count FROM articles").fetchone()
            promotion = connection.execute(
                """
                SELECT
                    COUNT(*) AS cached_rows,
                    COALESCE(SUM(hit_count), 0) AS cache_hits,
                    SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END) AS pending_rows
                FROM promotion_cache
                """
            ).fetchone()
        return {
            "path": str(self.path),
            "heartbeat": str(heartbeat_row["value"]) if heartbeat_row else None,
            "active_feeds": len(_json_list(active_row["value"])) if active_row else 0,
            "known_feeds": int(totals["configured_feeds"] or 0),
            "feeds_in_error": int(totals["feeds_in_error"] or 0),
            "inserted_total": int(totals["inserted_total"] or 0),
            "stored_rows": int(stored["count"] or 0),
            "promotion_cached_rows": int(promotion["cached_rows"] or 0),
            "promotion_cache_hits": int(promotion["cache_hits"] or 0),
            "promotion_pending_rows": int(promotion["pending_rows"] or 0),
        }


class CentralFeedPromotionCache:
    """Cross-process single-flight cache for publisher-page promotion.

    Several related markets often consume the same aggregator item at once.
    Exactly one process resolves/fetches the publisher page; peers wait for and
    reuse its normalized Article instead of producing a request burst.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        success_ttl_seconds: float = 21600.0,
        failure_ttl_seconds: float = 30.0,
        lease_seconds: float = 60.0,
        wait_timeout_seconds: float = 65.0,
    ) -> None:
        self.db_path = db_path
        self.success_ttl_seconds = max(1.0, float(success_ttl_seconds))
        self.failure_ttl_seconds = max(1.0, float(failure_ttl_seconds))
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.wait_timeout_seconds = max(self.lease_seconds, float(wait_timeout_seconds))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            _initialize_promotion_cache(connection)

    def promote(
        self,
        article: Article,
        user_agent: str,
        *,
        promoter: Callable[[Article, str], Article | None] = promote_feed_article,
    ) -> Article | None:
        owner = uuid.uuid4().hex
        deadline = time.monotonic() + self.wait_timeout_seconds
        while True:
            action, payload = self._claim_or_read(article.hash, owner)
            if action == "hit":
                cached = _article_from_json(payload)
                if cached is not None:
                    return cached
                self._invalidate_corrupt(article.hash)
                continue
            if action == "failure":
                return None
            if action == "claim":
                try:
                    promoted = promoter(article, user_agent)
                except Exception as exc:
                    self._finish(article.hash, owner, None, error=str(exc))
                    raise
                self._finish(article.hash, owner, promoted, error=None)
                return promoted
            while time.monotonic() < deadline:
                wait_action, wait_payload = self._read_waiting(article.hash)
                if wait_action == "hit":
                    cached = _article_from_json(wait_payload)
                    if cached is not None:
                        self._record_hit(article.hash)
                        return cached
                    self._invalidate_corrupt(article.hash)
                    break
                if wait_action == "failure":
                    self._record_hit(article.hash)
                    return None
                if wait_action == "retry":
                    break
                time.sleep(0.1)
            else:
                log_event("central_promotion_wait_timeout", article_hash=article.hash, url=article.url)
                return None

    def _invalidate_corrupt(self, article_hash: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM promotion_cache WHERE article_hash=? AND state='success'",
                (article_hash,),
            )

    def _read_waiting(self, article_hash: str) -> tuple[str, str | None]:
        now = time.time()
        with self._connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT state, payload_json, lease_until, expires_at
                FROM promotion_cache WHERE article_hash=?
                """,
                (article_hash,),
            ).fetchone()
        if row is None:
            return "retry", None
        if float(row["expires_at"] or 0.0) > now:
            if row["state"] == "success" and row["payload_json"]:
                return "hit", str(row["payload_json"])
            if row["state"] == "failure":
                return "failure", None
        if row["state"] == "pending" and float(row["lease_until"] or 0.0) > now:
            return "wait", None
        return "retry", None

    def _record_hit(self, article_hash: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE promotion_cache SET hit_count=hit_count+1 WHERE article_hash=?",
                (article_hash,),
            )

    def _claim_or_read(self, article_hash: str, owner: str) -> tuple[str, str | None]:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT state, payload_json, owner, lease_until, expires_at
                FROM promotion_cache WHERE article_hash=?
                """,
                (article_hash,),
            ).fetchone()
            if row is not None and float(row["expires_at"] or 0.0) > now:
                if row["state"] == "success" and row["payload_json"]:
                    connection.execute(
                        "UPDATE promotion_cache SET hit_count=hit_count+1 WHERE article_hash=?",
                        (article_hash,),
                    )
                    return "hit", str(row["payload_json"])
                if row["state"] == "failure":
                    connection.execute(
                        "UPDATE promotion_cache SET hit_count=hit_count+1 WHERE article_hash=?",
                        (article_hash,),
                    )
                    return "failure", None
            if (
                row is not None
                and row["state"] == "pending"
                and str(row["owner"] or "") != owner
                and float(row["lease_until"] or 0.0) > now
            ):
                return "wait", None
            connection.execute(
                """
                INSERT INTO promotion_cache(
                    article_hash, state, payload_json, owner, lease_until,
                    expires_at, updated_at, last_error, hit_count
                )
                VALUES(?, 'pending', NULL, ?, ?, 0, ?, NULL, 0)
                ON CONFLICT(article_hash) DO UPDATE SET
                    state='pending',
                    payload_json=NULL,
                    owner=excluded.owner,
                    lease_until=excluded.lease_until,
                    expires_at=0,
                    updated_at=excluded.updated_at,
                    last_error=NULL
                """,
                (article_hash, owner, now + self.lease_seconds, _now()),
            )
            return "claim", None

    def _finish(
        self,
        article_hash: str,
        owner: str,
        promoted: Article | None,
        *,
        error: str | None,
    ) -> None:
        now = time.time()
        state = "success" if promoted is not None else "failure"
        ttl = self.success_ttl_seconds if promoted is not None else self.failure_ttl_seconds
        payload = (
            json.dumps(asdict(promoted), sort_keys=True, separators=(",", ":"))
            if promoted is not None
            else None
        )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE promotion_cache
                SET state=?, payload_json=?, lease_until=0, expires_at=?,
                    updated_at=?, last_error=?
                WHERE article_hash=? AND owner=? AND state='pending'
                """,
                (
                    state,
                    payload,
                    now + ttl,
                    _now(),
                    (error or "")[:1000] or None,
                    article_hash,
                    owner,
                ),
            )

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5.0)
        else:
            connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection


class CentralFeedReader:
    """Read subscribed feed rows with durable, per-feed acknowledgements."""

    def __init__(
        self,
        db_path: Path,
        cursor_path: Path,
        *,
        stale_after_seconds: float,
    ) -> None:
        self.db_path = db_path
        self.cursor_path = cursor_path
        self.stale_after_seconds = stale_after_seconds
        self._pending: dict[str, int] = {}

    def read(
        self,
        feed_urls: Iterable[str],
        *,
        include_terms: list[str] | None,
        exclude_terms: list[str] | None,
        limit_per_feed: int,
    ) -> CentralFeedBatch:
        urls = sorted(set(str(url).strip() for url in feed_urls if str(url).strip()))
        if not urls:
            self._pending = {}
            return CentralFeedBatch([], {})
        if not self.db_path.exists():
            raise CentralFeedUnavailable(f"central feed database missing: {self.db_path}")

        try:
            connection = sqlite3.connect(
                f"file:{self.db_path}?mode=ro",
                uri=True,
                timeout=5.0,
            )
        except sqlite3.Error as exc:
            raise CentralFeedUnavailable(f"central feed open failed: {exc}") from exc
        connection.row_factory = sqlite3.Row
        try:
            self._require_fresh_heartbeat(connection)
            current = self._load_cursors()
            rows: list[tuple[int, str, Article]] = []
            next_cursors: dict[str, int] = {}
            for feed_url in urls:
                after = int(current.get(feed_url, 0))
                fetched = connection.execute(
                    """
                    SELECT id, payload_json
                    FROM articles
                    WHERE feed_url=? AND id>?
                    ORDER BY id
                    LIMIT ?
                    """,
                    (feed_url, after, max(1, int(limit_per_feed))),
                ).fetchall()
                if not fetched:
                    continue
                next_cursors[feed_url] = int(fetched[-1]["id"])
                for row in fetched:
                    try:
                        raw = json.loads(str(row["payload_json"]))
                        article = Article(**raw)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
                    if not _article_matches(article, include_terms, exclude_terms):
                        continue
                    rows.append((int(row["id"]), feed_url, article))
        except sqlite3.Error as exc:
            raise CentralFeedUnavailable(f"central feed read failed: {exc}") from exc
        finally:
            connection.close()

        rows.sort(key=lambda item: (item[0], item[1], item[2].hash))
        self._pending = next_cursors
        return CentralFeedBatch([item[2] for item in rows], next_cursors)

    def ack_pending(self) -> None:
        if not self._pending:
            return
        current = self._load_cursors()
        for feed_url, cursor in self._pending.items():
            current[feed_url] = max(int(current.get(feed_url, 0)), int(cursor))
        _atomic_json_write(
            self.cursor_path,
            {"feeds": current, "updated_at": _now()},
        )
        self._pending = {}

    def _require_fresh_heartbeat(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='service_heartbeat'"
        ).fetchone()
        if row is None:
            raise CentralFeedUnavailable("central feed has no producer heartbeat")
        if self.stale_after_seconds <= 0:
            return
        try:
            heartbeat = datetime.fromisoformat(str(row["value"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise CentralFeedUnavailable("central feed heartbeat is invalid") from exc
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
        if age > self.stale_after_seconds:
            raise CentralFeedUnavailable(
                f"central feed heartbeat stale: {age:.1f}s > {self.stale_after_seconds:.1f}s"
            )

    def _load_cursors(self) -> dict[str, int]:
        if not self.cursor_path.exists():
            return {}
        try:
            raw = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        feeds = raw.get("feeds") if isinstance(raw, dict) else None
        if not isinstance(feeds, dict):
            return {}
        out: dict[str, int] = {}
        for feed_url, value in feeds.items():
            try:
                out[str(feed_url)] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        return out


class CentralFeedService:
    """Fetch the current fleet's union of feeds once and fan rows into SQLite."""

    def __init__(
        self,
        *,
        store: CentralFeedStore,
        feed_urls_provider: Callable[[], Iterable[str]],
        poll_seconds: float,
        max_workers: int,
        max_entries_per_feed: int,
        retention_hours: float,
        direct_urls_provider: Callable[[], Iterable[str]] | None = None,
        direct_priorities_provider: (
            Callable[[], dict[str, float]] | None
        ) = None,
        direct_poll_seconds: float = 2.0,
        direct_idle_max_seconds: float = 30.0,
        max_entries_per_direct_source: int = 20,
        aggregator_poll_seconds: float = 30.0,
        max_urls_per_domain_per_cycle: int = 8,
        user_agent: str = "polybot/0.1",
        fetcher: Callable[..., list[Article]] = fetch_feed_articles,
        direct_fetcher: Callable[..., list[Article]] = (
            fetch_direct_source_articles
        ),
    ) -> None:
        self.store = store
        self.feed_urls_provider = feed_urls_provider
        self._direct_provider_configured = direct_urls_provider is not None
        self.direct_urls_provider = direct_urls_provider or (lambda: ())
        self.direct_priorities_provider = (
            direct_priorities_provider or (lambda: {})
        )
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.direct_poll_seconds = max(
            0.1,
            float(direct_poll_seconds),
        )
        self.direct_idle_max_seconds = max(
            self.direct_poll_seconds,
            float(direct_idle_max_seconds),
        )
        self.max_workers = max(1, int(max_workers))
        self.max_entries_per_feed = max(1, int(max_entries_per_feed))
        self.max_entries_per_direct_source = max(
            1,
            int(max_entries_per_direct_source),
        )
        self.retention_hours = float(retention_hours)
        self.aggregator_poll_seconds = max(
            self.poll_seconds,
            float(aggregator_poll_seconds),
        )
        self.max_urls_per_domain_per_cycle = max(
            1,
            int(max_urls_per_domain_per_cycle),
        )
        self.user_agent = user_agent
        self.fetcher = fetcher
        self.direct_fetcher = direct_fetcher
        self._last_attempt: dict[str, float] = {}
        self._direct_idle_streak: dict[str, int] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.store.touch_heartbeat()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever,
            name="polybot-central-feed",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                self.store.touch_heartbeat()
                log_event("central_feed_cycle_error", error=str(exc))
            self._stop.wait(self.poll_seconds)

    def poll_once(self, *, force: bool = False) -> dict[str, int]:
        feed_urls = {
            str(url).strip()
            for url in self.feed_urls_provider()
            if str(url).strip()
        }
        direct_urls = {
            str(url).strip()
            for url in self.direct_urls_provider()
            if str(url).strip()
        } - feed_urls
        direct_priorities = {
            str(url): float(value)
            for url, value in self.direct_priorities_provider().items()
            if str(url) in direct_urls
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        }
        urls = sorted(feed_urls | direct_urls)
        self.store.touch_heartbeat()
        self.store.set_active_feeds(urls)
        if not urls:
            return self._summary({
                "feeds": 0,
                "direct_sources": 0,
                "polled": 0,
                "direct_polled": 0,
                "inserted": 0,
                "errors": 0,
                "pruned": 0,
            })

        inserted = 0
        errors = 0

        def fetch(feed_url: str) -> tuple[str, list[Article], str | None]:
            try:
                is_direct = feed_url in direct_urls
                source_fetcher = (
                    self.direct_fetcher if is_direct else self.fetcher
                )
                articles = source_fetcher(
                    feed_url,
                    self.user_agent,
                    include_terms=None,
                    exclude_terms=None,
                    limit=(
                        self.max_entries_per_direct_source
                        if is_direct
                        else self.max_entries_per_feed
                    ),
                )
                return feed_url, articles, None
            except Exception as exc:
                return feed_url, [], str(exc)

        # One sequential lane per domain prevents dozens of market-specific
        # Google/Bing queries from hitting the same publisher concurrently.
        # After two domain failures in one cycle, short-circuit the remaining
        # URLs for that domain so an unreachable aggregator cannot stretch a
        # two-second cycle into many minutes.
        active_by_domain: dict[str, list[str]] = {}
        for url in urls:
            domain = urlparse(url).netloc.lower().removeprefix("www.") or url
            active_by_domain.setdefault(domain, []).append(url)

        # Market-specific aggregator queries can number in the hundreds.
        # Poll a bounded, least-recently-attempted slice per domain each
        # cycle; direct publisher feeds remain due at the fast base cadence.
        # This keeps the central loop bounded while rotating full query
        # coverage instead of letting one domain monopolize every worker.
        now_monotonic = time.monotonic()
        by_domain: dict[str, list[str]] = {}
        for domain, domain_urls in active_by_domain.items():
            due: list[str] = []
            for feed_url in sorted(
                domain_urls,
                key=lambda item: (
                    self._last_attempt.get(item, 0.0),
                    -direct_priorities.get(item, 0.0),
                    item,
                ),
            ):
                interval = (
                    self.aggregator_poll_seconds
                    if domain in {"news.google.com", "bing.com", "www.bing.com"}
                    else self._direct_interval(feed_url)
                    if feed_url in direct_urls
                    else self.poll_seconds
                )
                elapsed = now_monotonic - self._last_attempt.get(feed_url, 0.0)
                if force or elapsed >= interval:
                    due.append(feed_url)
                if len(due) >= self.max_urls_per_domain_per_cycle:
                    break
            if due:
                by_domain[domain] = due
                for feed_url in due:
                    self._last_attempt[feed_url] = now_monotonic

        if not by_domain:
            return self._summary({
                "feeds": len(feed_urls),
                "direct_sources": len(direct_urls),
                "polled": 0,
                "direct_polled": 0,
                "inserted": 0,
                "errors": 0,
                "pruned": 0,
            })

        def fetch_domain(domain_urls: list[str]) -> list[tuple[str, list[Article], str | None]]:
            results: list[tuple[str, list[Article], str | None]] = []
            consecutive_errors = 0
            last_error = ""
            for index, feed_url in enumerate(domain_urls):
                if consecutive_errors >= 2:
                    for skipped in domain_urls[index:]:
                        results.append(
                            (
                                skipped,
                                [],
                                f"domain_cycle_short_circuit:{last_error}"[:1000],
                            )
                        )
                    break
                result = fetch(feed_url)
                results.append(result)
                if result[2] is None:
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    last_error = str(result[2])
            return results

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(by_domain))) as pool:
            futures = [pool.submit(fetch_domain, domain_urls) for domain_urls in by_domain.values()]
            for future in as_completed(futures):
                for feed_url, articles, error in future.result():
                    if error is not None:
                        errors += 1
                        self.store.record_error(feed_url, error)
                        log_event("central_feed_fetch_error", url=feed_url, error=error)
                        continue
                    inserted_now = self.store.record_success(
                        feed_url,
                        articles,
                    )
                    inserted += inserted_now
                    if feed_url in direct_urls:
                        if inserted_now:
                            self._direct_idle_streak[feed_url] = 0
                        else:
                            self._direct_idle_streak[feed_url] = min(
                                30,
                                self._direct_idle_streak.get(feed_url, 0)
                                + 1,
                            )

        pruned = self.store.prune(self.retention_hours)
        summary = {
            "feeds": len(feed_urls),
            "direct_sources": len(direct_urls),
            "polled": sum(len(domain_urls) for domain_urls in by_domain.values()),
            "direct_polled": sum(
                1
                for domain_urls in by_domain.values()
                for url in domain_urls
                if url in direct_urls
            ),
            "inserted": inserted,
            "errors": errors,
            "pruned": pruned,
        }
        log_event("central_feed_cycle_complete", **summary)
        return self._summary(summary)

    def _direct_interval(self, feed_url: str) -> float:
        streak = min(30, self._direct_idle_streak.get(feed_url, 0))
        return min(
            self.direct_idle_max_seconds,
            self.direct_poll_seconds * (2**streak),
        )

    def _summary(self, value: dict[str, int]) -> dict[str, int]:
        if not self._direct_provider_configured:
            value.pop("direct_sources", None)
            value.pop("direct_polled", None)
        return value

    def status(self) -> dict[str, object]:
        status = self.store.status()
        status["running"] = bool(self._thread is not None and self._thread.is_alive())
        return status


def _article_matches(
    article: Article,
    include_terms: list[str] | None,
    exclude_terms: list[str] | None,
) -> bool:
    text = f"{article.domain}\n{article.title}\n{article.raw_text}".lower()
    if include_terms and not any(term.lower() in text for term in include_terms):
        return False
    if exclude_terms and any(term.lower() in text for term in exclude_terms):
        return False
    return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_list(value: object) -> list[object]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _initialize_promotion_cache(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS promotion_cache (
            article_hash TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            payload_json TEXT,
            owner TEXT,
            lease_until REAL NOT NULL DEFAULT 0,
            expires_at REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            last_error TEXT,
            hit_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def _article_from_json(payload: str | None) -> Article | None:
    if not payload:
        return None
    try:
        raw = json.loads(payload)
        return Article(**raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


__all__ = [
    "CentralFeedBatch",
    "CentralFeedPromotionCache",
    "CentralFeedReader",
    "CentralFeedService",
    "CentralFeedStore",
    "CentralFeedUnavailable",
]
