from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from polybot.book import BookCache
from polybot.core.holdings import _atomic_json_write
from polybot.core.types import Article
from polybot.discovery.config import (
    DiscoveryConfig,
    ForwardRecorderConfig,
    forward_recorder_db_path,
    load_discovery_config,
    rule_store_db_path,
)
from polybot.discovery.store import DiscoveryStore
from polybot.discovery.types import (
    MarketContext,
    SourcePlan,
    market_dir_slug,
)
from polybot.log import log_event

from .contracts import (
    DecisionProof,
    RuleEvaluation,
    RuleSpec,
    canonical_json,
    sha256_json,
)
from .store import RuleStore

FORWARD_RECORDER_SCHEMA_VERSION = 3
FORWARD_TIMELINE_SCHEMA_VERSION = 2
SEMANTIC_BINDING = "SEMANTIC"
BOOK_CAPTURE_BINDING = "BOOK_CAPTURE"


class ForwardRecorderStore:
    """WAL-backed, append-only evidence captured during forward paper runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(
                f"file:{self.path}?mode=ro",
                uri=True,
                timeout=5.0,
            )
        else:
            connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS bindings (
                    binding_sha256 TEXT PRIMARY KEY,
                    binding_kind TEXT NOT NULL DEFAULT 'SEMANTIC',
                    market_id TEXT NOT NULL,
                    event_slug TEXT NOT NULL,
                    rule_spec_sha256 TEXT NOT NULL,
                    source_plan_sha256 TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    rule_spec_json TEXT NOT NULL,
                    source_plan_json TEXT NOT NULL,
                    recorder_policy_sha256 TEXT NOT NULL,
                    evidence_policy_sha256 TEXT NOT NULL DEFAULT '',
                    evidence_policy_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_forward_bindings_market
                    ON bindings(market_id, created_at);

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    binding_sha256 TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    paper_only INTEGER NOT NULL,
                    close_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_forward_sessions_binding
                    ON sessions(binding_sha256, started_at);

                CREATE TABLE IF NOT EXISTS operational_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_sha256 TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_forward_ops_binding
                    ON operational_events(binding_sha256, observed_at);

                CREATE TABLE IF NOT EXISTS book_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_sha256 TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    source_at TEXT NOT NULL,
                    source_latency_ms REAL,
                    revision INTEGER NOT NULL,
                    book_hash TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_forward_books_binding_at
                    ON book_events(binding_sha256, received_at, id);
                CREATE INDEX IF NOT EXISTS idx_forward_books_token_at
                    ON book_events(binding_sha256, token_id, received_at);

                CREATE TABLE IF NOT EXISTS trade_prints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_sha256 TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    source_at TEXT NOT NULL,
                    source_latency_ms REAL,
                    price REAL NOT NULL,
                    size REAL NOT NULL,
                    side TEXT NOT NULL,
                    fee_rate_bps REAL NOT NULL,
                    transaction_hash TEXT NOT NULL,
                    event_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_forward_trades_binding_at
                    ON trade_prints(binding_sha256, received_at, id);
                CREATE INDEX IF NOT EXISTS idx_forward_trades_token_at
                    ON trade_prints(binding_sha256, token_id, received_at);

                CREATE TABLE IF NOT EXISTS articles (
                    binding_sha256 TEXT NOT NULL,
                    article_id TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    article_json TEXT NOT NULL,
                    first_observed_at TEXT NOT NULL,
                    cycle_id TEXT NOT NULL,
                    PRIMARY KEY(binding_sha256, article_id)
                );
                CREATE INDEX IF NOT EXISTS idx_forward_articles_binding_at
                    ON articles(binding_sha256, first_observed_at);

                CREATE TABLE IF NOT EXISTS extractions (
                    extraction_sha256 TEXT PRIMARY KEY,
                    binding_sha256 TEXT NOT NULL,
                    article_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    duration_ms REAL NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_forward_extract_binding
                    ON extractions(binding_sha256, completed_at);

                CREATE TABLE IF NOT EXISTS decision_proofs (
                    proof_sha256 TEXT PRIMARY KEY,
                    binding_sha256 TEXT NOT NULL,
                    evaluation_sha256 TEXT NOT NULL,
                    terminal INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    proof_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_forward_proofs_binding
                    ON decision_proofs(binding_sha256, observed_at);

                CREATE TABLE IF NOT EXISTS quote_anchors (
                    anchor_id TEXT PRIMARY KEY,
                    binding_sha256 TEXT NOT NULL,
                    proof_sha256 TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    anchor_at TEXT NOT NULL,
                    anchor_best_bid REAL,
                    anchor_best_ask REAL,
                    anchor_executable_usd REAL NOT NULL,
                    requested_usd REAL NOT NULL,
                    snapshot_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_forward_anchors_binding
                    ON quote_anchors(binding_sha256, anchor_at);

                CREATE TABLE IF NOT EXISTS quote_samples (
                    anchor_id TEXT NOT NULL,
                    horizon_ms INTEGER NOT NULL,
                    due_at TEXT NOT NULL,
                    sampled_at TEXT NOT NULL,
                    sample_lag_ms REAL NOT NULL,
                    status TEXT NOT NULL,
                    best_bid REAL,
                    best_ask REAL,
                    executable_usd_at_anchor_price REAL NOT NULL,
                    executable_usd_at_one_cent_shock REAL NOT NULL,
                    quote_survived INTEGER,
                    quote_survived_one_cent INTEGER,
                    snapshot_json TEXT NOT NULL,
                    PRIMARY KEY(anchor_id, horizon_ms)
                );

                CREATE TABLE IF NOT EXISTS resolutions (
                    resolution_sha256 TEXT PRIMARY KEY,
                    binding_sha256 TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    outcome_name TEXT NOT NULL,
                    resolved_yes INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    source_at TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_forward_resolution_binding
                    ON resolutions(binding_sha256, observed_at);
                """
            )
            _ensure_sqlite_column(
                connection,
                "bindings",
                "evidence_policy_sha256",
                "TEXT NOT NULL DEFAULT ''",
            )
            _ensure_sqlite_column(
                connection,
                "bindings",
                "evidence_policy_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            _ensure_sqlite_column(
                connection,
                "bindings",
                "binding_kind",
                "TEXT NOT NULL DEFAULT 'SEMANTIC'",
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_forward_bindings_kind_market
                    ON bindings(binding_kind, market_id, created_at)
                """
            )

    def ensure_binding(
        self,
        context: MarketContext,
        spec: RuleSpec,
        source_plan: SourcePlan,
        recorder_config: ForwardRecorderConfig,
        *,
        evidence_policy: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> str:
        spec.validate_context_binding(context)
        if source_plan.market_id != context.market_id:
            raise ValueError("forward source plan market binding mismatch")
        if source_plan.rule_spec_sha256 != spec.spec_sha256:
            raise ValueError("forward source plan RuleSpec binding mismatch")
        context_binding = _context_binding_payload(context)
        source_plan_binding = _source_plan_binding_payload(source_plan)
        context_json = canonical_json(context_binding)
        spec_json = canonical_json(spec.as_dict())
        source_plan_json = canonical_json(source_plan_binding)
        recorder_policy = _recorder_policy_payload(recorder_config)
        recorder_policy_sha256 = sha256_json(recorder_policy)
        evidence_policy_payload = dict(evidence_policy or {})
        evidence_policy_json = canonical_json(evidence_policy_payload)
        evidence_policy_sha256 = sha256_json(evidence_policy_payload)
        binding_payload = {
            "schema_version": FORWARD_RECORDER_SCHEMA_VERSION,
            "binding_kind": SEMANTIC_BINDING,
            "market_id": context.market_id,
            "context_sha256": sha256_json(context_binding),
            "rule_spec_sha256": spec.spec_sha256,
            "source_plan_sha256": sha256_json(source_plan_binding),
            "recorder_policy_sha256": recorder_policy_sha256,
            "evidence_policy_sha256": evidence_policy_sha256,
        }
        binding_sha256 = sha256_json(binding_payload)
        at = created_at or _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT binding_kind, context_json, rule_spec_json, source_plan_json,
                       recorder_policy_sha256, evidence_policy_sha256,
                       evidence_policy_json
                FROM bindings WHERE binding_sha256=?
                """,
                (binding_sha256,),
            ).fetchone()
            expected = (
                SEMANTIC_BINDING,
                context_json,
                spec_json,
                source_plan_json,
                recorder_policy_sha256,
                evidence_policy_sha256,
                evidence_policy_json,
            )
            if row is not None:
                actual = tuple(
                    str(row[index]) for index in range(len(expected))
                )
                if actual != expected:
                    raise ValueError("immutable forward binding conflict")
                return binding_sha256
            connection.execute(
                """
                INSERT INTO bindings(
                    binding_sha256, binding_kind, market_id, event_slug,
                    rule_spec_sha256, source_plan_sha256,
                    context_json, rule_spec_json, source_plan_json,
                    recorder_policy_sha256, evidence_policy_sha256,
                    evidence_policy_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding_sha256,
                    SEMANTIC_BINDING,
                    context.market_id,
                    context.event_slug,
                    spec.spec_sha256,
                    binding_payload["source_plan_sha256"],
                    context_json,
                    spec_json,
                    source_plan_json,
                    recorder_policy_sha256,
                    evidence_policy_sha256,
                    evidence_policy_json,
                    at,
                ),
            )
        return binding_sha256

    def ensure_book_binding(
        self,
        context: MarketContext,
        recorder_config: ForwardRecorderConfig,
        *,
        created_at: str | None = None,
    ) -> str:
        """Create the immutable market/token binding used by raw book capture.

        This binding deliberately excludes RuleSpec and SourcePlan data so the
        shared collector can start before either semantic asset exists.
        """
        context_binding = _context_binding_payload(context)
        context_json = canonical_json(context_binding)
        recorder_policy_sha256 = sha256_json(
            _recorder_policy_payload(recorder_config)
        )
        binding_payload = {
            "schema_version": FORWARD_RECORDER_SCHEMA_VERSION,
            "binding_kind": BOOK_CAPTURE_BINDING,
            "market_id": context.market_id,
            "context_sha256": sha256_json(context_binding),
            "recorder_policy_sha256": recorder_policy_sha256,
        }
        binding_sha256 = sha256_json(binding_payload)
        at = created_at or _now()
        empty_json = canonical_json({})
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT binding_kind, context_json, recorder_policy_sha256,
                       rule_spec_sha256, source_plan_sha256,
                       rule_spec_json, source_plan_json
                FROM bindings WHERE binding_sha256=?
                """,
                (binding_sha256,),
            ).fetchone()
            expected = (
                BOOK_CAPTURE_BINDING,
                context_json,
                recorder_policy_sha256,
                "",
                "",
                empty_json,
                empty_json,
            )
            if row is not None:
                actual = tuple(str(row[index]) for index in range(len(expected)))
                if actual != expected:
                    raise ValueError(
                        "immutable forward book capture binding conflict"
                    )
                return binding_sha256
            connection.execute(
                """
                INSERT INTO bindings(
                    binding_sha256, binding_kind, market_id, event_slug,
                    rule_spec_sha256, source_plan_sha256,
                    context_json, rule_spec_json, source_plan_json,
                    recorder_policy_sha256, evidence_policy_sha256,
                    evidence_policy_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding_sha256,
                    BOOK_CAPTURE_BINDING,
                    context.market_id,
                    context.event_slug,
                    "",
                    "",
                    context_json,
                    empty_json,
                    empty_json,
                    recorder_policy_sha256,
                    "",
                    empty_json,
                    at,
                ),
            )
        return binding_sha256

    def latest_binding(self, market_id: str) -> str | None:
        with self._connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT binding_sha256 FROM bindings
                WHERE market_id=? AND binding_kind=?
                ORDER BY created_at DESC, binding_sha256 DESC LIMIT 1
                """,
                (market_id, SEMANTIC_BINDING),
            ).fetchone()
        return str(row["binding_sha256"]) if row else None

    def latest_book_binding(self, market_id: str) -> str | None:
        with self._connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT binding_sha256 FROM bindings
                WHERE market_id=? AND binding_kind=?
                ORDER BY created_at DESC, binding_sha256 DESC LIMIT 1
                """,
                (market_id, BOOK_CAPTURE_BINDING),
            ).fetchone()
        return str(row["binding_sha256"]) if row else None

    def capture_status(self) -> dict[str, Any]:
        """Return fresh store-level health without needing the live service."""
        with self._connect(read_only=True) as connection:
            bindings = connection.execute(
                """
                SELECT COUNT(*) AS n FROM bindings WHERE binding_kind=?
                """,
                (BOOK_CAPTURE_BINDING,),
            ).fetchone()
            sessions = connection.execute(
                """
                SELECT COUNT(*) AS n FROM sessions s
                JOIN bindings b ON b.binding_sha256=s.binding_sha256
                WHERE b.binding_kind=? AND s.ended_at IS NULL
                """,
                (BOOK_CAPTURE_BINDING,),
            ).fetchone()
            books = connection.execute(
                """
                SELECT COUNT(*) AS n, MAX(received_at) AS latest
                FROM book_events e
                JOIN bindings b ON b.binding_sha256=e.binding_sha256
                WHERE b.binding_kind=?
                """,
                (BOOK_CAPTURE_BINDING,),
            ).fetchone()
            trades = connection.execute(
                """
                SELECT COUNT(*) AS n FROM trade_prints t
                JOIN bindings b ON b.binding_sha256=t.binding_sha256
                WHERE b.binding_kind=?
                """,
                (BOOK_CAPTURE_BINDING,),
            ).fetchone()
        return {
            "capture_bindings": int(bindings["n"] or 0),
            "active_capture_sessions": int(sessions["n"] or 0),
            "book_events": int(books["n"] or 0),
            "trade_prints": int(trades["n"] or 0),
            "latest_book_received_at": str(books["latest"] or ""),
        }

    def close_active_capture_sessions(
        self,
        *,
        ended_at: str,
        reason: str,
    ) -> int:
        """Recover sessions left open by a terminated singleton service."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sessions
                SET ended_at=?, close_reason=?
                WHERE ended_at IS NULL
                  AND binding_sha256 IN (
                    SELECT binding_sha256 FROM bindings
                    WHERE binding_kind=?
                  )
                """,
                (
                    ended_at,
                    reason[:200],
                    BOOK_CAPTURE_BINDING,
                ),
            )
            return max(0, int(cursor.rowcount))

    @staticmethod
    def _related_book_bindings(
        connection: sqlite3.Connection,
        binding_sha256: str,
    ) -> list[str]:
        """Return legacy semantic storage plus matching raw capture storage."""
        binding = connection.execute(
            """
            SELECT binding_kind, market_id, context_json,
                   recorder_policy_sha256
            FROM bindings WHERE binding_sha256=?
            """,
            (binding_sha256,),
        ).fetchone()
        if binding is None or str(binding["binding_kind"]) == BOOK_CAPTURE_BINDING:
            return [binding_sha256]
        rows = connection.execute(
            """
            SELECT binding_sha256 FROM bindings
            WHERE binding_kind=? AND market_id=? AND context_json=?
              AND recorder_policy_sha256=?
            ORDER BY created_at, binding_sha256
            """,
            (
                BOOK_CAPTURE_BINDING,
                str(binding["market_id"]),
                str(binding["context_json"]),
                str(binding["recorder_policy_sha256"]),
            ),
        ).fetchall()
        return [
            binding_sha256,
            *[
                str(row["binding_sha256"])
                for row in rows
                if str(row["binding_sha256"]) != binding_sha256
            ],
        ]

    def start_session(
        self,
        binding_sha256: str,
        *,
        session_id: str,
        started_at: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE sessions
                SET ended_at=?, close_reason='superseded_by_new_session'
                WHERE binding_sha256=? AND ended_at IS NULL
                  AND session_id<>?
                """,
                (started_at, binding_sha256, session_id),
            )
            connection.execute(
                """
                INSERT INTO sessions(
                    session_id, binding_sha256, started_at, paper_only
                ) VALUES(?, ?, ?, 1)
                """,
                (session_id, binding_sha256, started_at),
            )

    def end_session(
        self,
        session_id: str,
        *,
        ended_at: str,
        reason: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions SET ended_at=?, close_reason=?
                WHERE session_id=? AND ended_at IS NULL
                """,
                (ended_at, reason[:200], session_id),
            )

    def record_operational_event(
        self,
        *,
        session_id: str,
        binding_sha256: str,
        event_type: str,
        observed_at: str,
        payload: dict[str, Any],
    ) -> None:
        record = {
            "session_id": session_id,
            "binding_sha256": binding_sha256,
            "event_type": event_type,
            "observed_at": observed_at,
            "payload": payload,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO operational_events(
                    event_sha256, session_id, binding_sha256,
                    event_type, observed_at, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    sha256_json(record),
                    session_id,
                    binding_sha256,
                    event_type,
                    observed_at,
                    canonical_json(payload),
                ),
            )

    def record_book_event(
        self,
        *,
        session_id: str,
        binding_sha256: str,
        record: dict[str, Any],
    ) -> None:
        snapshot = record.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ValueError("forward book event requires a snapshot")
        token_id = str(record.get("token_id") or "")
        if not token_id:
            raise ValueError("forward book event requires token_id")
        received_at = _iso(record.get("received_at"), "received_at")
        source_at = _optional_iso(record.get("source_at"))
        source_latency_ms = _latency_ms(source_at, received_at)
        event_payload = (
            record.get("event")
            if isinstance(record.get("event"), dict)
            else {}
        )
        payload = {
            "session_id": session_id,
            "binding_sha256": binding_sha256,
            "token_id": token_id,
            "event_type": str(record.get("event_type") or ""),
            "received_at": received_at,
            "source_at": source_at,
            "event": event_payload,
            "snapshot": snapshot,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO book_events(
                    event_sha256, session_id, binding_sha256, token_id,
                    event_type, received_at, source_at, source_latency_ms,
                    revision, book_hash, event_json, snapshot_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sha256_json(payload),
                    session_id,
                    binding_sha256,
                    token_id,
                    payload["event_type"],
                    received_at,
                    source_at,
                    source_latency_ms,
                    int(snapshot.get("revision") or 0),
                    str(snapshot.get("book_hash") or ""),
                    canonical_json(event_payload),
                    canonical_json(snapshot),
                ),
            )

    def record_trade_print(
        self,
        *,
        session_id: str,
        binding_sha256: str,
        record: dict[str, Any],
    ) -> None:
        token_id = str(record.get("token_id") or "")
        event = (
            record.get("event")
            if isinstance(record.get("event"), dict)
            else {}
        )
        if not token_id:
            raise ValueError("forward trade print requires token_id")
        price = _float_or_none(event.get("price"))
        size = _float_or_none(event.get("size"))
        fee_rate_bps = _float_or_none(event.get("fee_rate_bps"))
        side = str(event.get("side") or "").strip().upper()
        if price is None or not 0 < price < 1:
            raise ValueError("forward trade print price must be between 0 and 1")
        if size is None or size <= 0:
            raise ValueError("forward trade print size must be positive")
        if fee_rate_bps is None:
            fee_rate_bps = 0.0
        if fee_rate_bps < 0:
            raise ValueError(
                "forward trade print fee_rate_bps must be non-negative"
            )
        if side not in {"BUY", "SELL"}:
            raise ValueError("forward trade print side must be BUY or SELL")
        received_at = _iso(record.get("received_at"), "received_at")
        source_at = _optional_iso(record.get("source_at"))
        identity = {
            "binding_sha256": binding_sha256,
            "token_id": token_id,
            "market_id": str(event.get("market") or ""),
            "source_at": source_at,
            "price": price,
            "size": size,
            "side": side,
            "fee_rate_bps": fee_rate_bps,
            "transaction_hash": str(
                event.get("transaction_hash") or ""
            ),
            "event": event,
        }
        trade_sha256 = sha256_json(identity)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO trade_prints(
                    trade_sha256, session_id, binding_sha256, token_id,
                    market_id, received_at, source_at, source_latency_ms,
                    price, size, side, fee_rate_bps, transaction_hash,
                    event_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_sha256,
                    session_id,
                    binding_sha256,
                    token_id,
                    identity["market_id"],
                    received_at,
                    source_at,
                    _latency_ms(source_at, received_at),
                    price,
                    size,
                    side,
                    fee_rate_bps,
                    identity["transaction_hash"],
                    canonical_json(event),
                ),
            )

    def record_article(
        self,
        *,
        binding_sha256: str,
        article: Article,
        first_observed_at: str,
        cycle_id: str,
    ) -> None:
        payload = asdict(article)
        if not article.hash or not article.raw_text.strip():
            raise ValueError("forward article requires id and full text")
        observed = _iso(first_observed_at, "first_observed_at")
        _iso(article.fetched_at, "article.fetched_at")
        if article.published_at:
            _iso(article.published_at, "article.published_at")
        payload_json = canonical_json(payload)
        payload_sha256 = sha256_json(payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT payload_sha256, article_json
                FROM articles
                WHERE binding_sha256=? AND article_id=?
                """,
                (binding_sha256, article.hash),
            ).fetchone()
            if row is not None:
                if (
                    str(row["payload_sha256"]) != payload_sha256
                    or str(row["article_json"]) != payload_json
                ):
                    raise ValueError(
                        f"immutable forward article conflict: {article.hash}"
                    )
                return
            connection.execute(
                """
                INSERT INTO articles(
                    binding_sha256, article_id, payload_sha256,
                    article_json, first_observed_at, cycle_id
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    binding_sha256,
                    article.hash,
                    payload_sha256,
                    payload_json,
                    observed,
                    cycle_id,
                ),
            )

    def latest_book_snapshot(
        self,
        *,
        binding_sha256: str,
        token_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        with self._connect(read_only=True) as connection:
            book_bindings = self._related_book_bindings(
                connection,
                binding_sha256,
            )
            placeholders = ",".join("?" for _ in book_bindings)
            row = connection.execute(
                f"""
                SELECT received_at, snapshot_json FROM book_events
                WHERE binding_sha256 IN ({placeholders}) AND token_id=?
                ORDER BY received_at DESC, id DESC LIMIT 1
                """,
                (*book_bindings, token_id),
            ).fetchone()
        if row is None:
            return {
                "token_id": token_id,
                "best_ask": None,
                "best_bid": None,
                "asks": [],
                "bids": [],
                "staleness": None,
                "revision": 0,
                "source": "forward_recorder_store_missing",
            }
        snapshot = json.loads(str(row["snapshot_json"]))
        now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
        snapshot["staleness"] = max(
            0.0,
            (now - _parse_at(str(row["received_at"]))).total_seconds(),
        )
        snapshot["source"] = "forward_recorder_shared_store"
        return snapshot

    def record_extraction(
        self,
        *,
        binding_sha256: str,
        article_id: str,
        started_at: str,
        completed_at: str,
        duration_ms: float,
        result: dict[str, Any],
    ) -> None:
        normalized_started = _iso(started_at, "started_at")
        normalized_completed = _iso(completed_at, "completed_at")
        if _parse_at(normalized_completed) < _parse_at(normalized_started):
            raise ValueError("forward extraction completed before it started")
        payload = {
            "binding_sha256": binding_sha256,
            "article_id": article_id,
            "started_at": normalized_started,
            "completed_at": normalized_completed,
            "duration_ms": round(max(0.0, float(duration_ms)), 3),
            "result": result,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO extractions(
                    extraction_sha256, binding_sha256, article_id,
                    started_at, completed_at, duration_ms, status, result_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sha256_json(payload),
                    binding_sha256,
                    article_id,
                    payload["started_at"],
                    payload["completed_at"],
                    payload["duration_ms"],
                    str(result.get("status") or "UNKNOWN"),
                    canonical_json(result),
                ),
            )

    def record_proof(
        self,
        *,
        binding_sha256: str,
        evaluation: RuleEvaluation,
        proof: DecisionProof,
        observed_at: str,
    ) -> None:
        payload = canonical_json(proof.as_dict())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT proof_json FROM decision_proofs WHERE proof_sha256=?
                """,
                (proof.proof_sha256,),
            ).fetchone()
            if row is not None and str(row["proof_json"]) != payload:
                raise ValueError("immutable forward proof conflict")
            connection.execute(
                """
                INSERT OR IGNORE INTO decision_proofs(
                    proof_sha256, binding_sha256, evaluation_sha256,
                    terminal, observed_at, proof_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    proof.proof_sha256,
                    binding_sha256,
                    evaluation.evaluation_sha256,
                    int(evaluation.terminal),
                    _iso(observed_at, "observed_at"),
                    payload,
                ),
            )

    def record_quote_anchor(
        self,
        *,
        binding_sha256: str,
        proof: DecisionProof,
        snapshot: dict[str, Any],
        anchor_at: str,
    ) -> str:
        anchor_payload = {
            "binding_sha256": binding_sha256,
            "proof_sha256": proof.proof_sha256,
            "token_id": proof.token_id,
        }
        anchor_id = sha256_json(anchor_payload)
        best_ask = _float_or_none(snapshot.get("best_ask"))
        best_bid = _float_or_none(snapshot.get("best_bid"))
        executable = _depth_usd(
            snapshot.get("asks"),
            cap=best_ask,
        )
        payload = (
            anchor_id,
            binding_sha256,
            proof.proof_sha256,
            proof.token_id,
            proof.side,
            _iso(anchor_at, "anchor_at"),
            best_bid,
            best_ask,
            executable,
            float(proof.allocation_usd),
            canonical_json(snapshot),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT binding_sha256, proof_sha256, token_id, side,
                       anchor_at, anchor_best_bid, anchor_best_ask,
                       anchor_executable_usd, requested_usd, snapshot_json
                FROM quote_anchors WHERE anchor_id=?
                """,
                (anchor_id,),
            ).fetchone()
            if row is not None and tuple(row) != payload[1:]:
                raise ValueError("immutable forward quote-anchor conflict")
            connection.execute(
                """
                INSERT OR IGNORE INTO quote_anchors(
                    anchor_id, binding_sha256, proof_sha256, token_id,
                    side, anchor_at, anchor_best_bid, anchor_best_ask,
                    anchor_executable_usd, requested_usd, snapshot_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
        return anchor_id

    def pending_quote_samples(
        self,
        horizons_ms: Iterable[int],
        *,
        due_before: str,
    ) -> list[dict[str, Any]]:
        horizons = sorted(set(int(item) for item in horizons_ms))
        with self._connect(read_only=True) as connection:
            anchors = connection.execute(
                """
                SELECT * FROM quote_anchors ORDER BY anchor_at, anchor_id
                """
            ).fetchall()
            existing = {
                (str(row["anchor_id"]), int(row["horizon_ms"]))
                for row in connection.execute(
                    "SELECT anchor_id, horizon_ms FROM quote_samples"
                ).fetchall()
            }
        cutoff = _parse_at(due_before)
        pending: list[dict[str, Any]] = []
        for row in anchors:
            anchor_at = _parse_at(str(row["anchor_at"]))
            for horizon in horizons:
                key = (str(row["anchor_id"]), horizon)
                if key in existing:
                    continue
                due_at = anchor_at + timedelta(milliseconds=horizon)
                if due_at <= cutoff:
                    pending.append(
                        {
                            **dict(row),
                            "horizon_ms": horizon,
                            "due_at": due_at.isoformat(),
                        }
                    )
        return pending

    def next_quote_sample_due(
        self,
        horizons_ms: Iterable[int],
    ) -> datetime | None:
        horizons = sorted(set(int(item) for item in horizons_ms))
        with self._connect(read_only=True) as connection:
            anchors = connection.execute(
                "SELECT anchor_id, anchor_at FROM quote_anchors"
            ).fetchall()
            existing = {
                (str(row["anchor_id"]), int(row["horizon_ms"]))
                for row in connection.execute(
                    "SELECT anchor_id, horizon_ms FROM quote_samples"
                ).fetchall()
            }
        due: list[datetime] = []
        for row in anchors:
            anchor_at = _parse_at(str(row["anchor_at"]))
            for horizon in horizons:
                if (str(row["anchor_id"]), horizon) not in existing:
                    due.append(
                        anchor_at + timedelta(milliseconds=horizon)
                    )
        return min(due) if due else None

    def record_quote_sample(
        self,
        *,
        anchor: dict[str, Any],
        sampled_at: str,
        status: str,
        snapshot: dict[str, Any],
        max_sample_lag_ms: int,
    ) -> None:
        due_at = _iso(anchor["due_at"], "due_at")
        sampled = _iso(sampled_at, "sampled_at")
        lag_ms = max(0.0, _latency_ms(due_at, sampled) or 0.0)
        anchor_ask = _float_or_none(anchor.get("anchor_best_ask"))
        requested = min(
            float(anchor.get("requested_usd") or 0.0),
            float(anchor.get("anchor_executable_usd") or 0.0),
        )
        best_ask = _float_or_none(snapshot.get("best_ask"))
        best_bid = _float_or_none(snapshot.get("best_bid"))
        at_anchor = _depth_usd(snapshot.get("asks"), cap=anchor_ask)
        shocked = _depth_usd(
            snapshot.get("asks"),
            cap=(
                min(0.999999, anchor_ask + 0.01)
                if anchor_ask is not None
                else None
            ),
        )
        sample_status = status
        survived: int | None = None
        survived_shock: int | None = None
        if lag_ms > max_sample_lag_ms:
            sample_status = "LATE"
        elif status == "OK" and anchor_ask is not None and requested > 0:
            survived = int(at_anchor + 1e-9 >= requested)
            survived_shock = int(shocked + 1e-9 >= requested)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO quote_samples(
                    anchor_id, horizon_ms, due_at, sampled_at, sample_lag_ms,
                    status, best_bid, best_ask,
                    executable_usd_at_anchor_price,
                    executable_usd_at_one_cent_shock,
                    quote_survived, quote_survived_one_cent, snapshot_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(anchor["anchor_id"]),
                    int(anchor["horizon_ms"]),
                    due_at,
                    sampled,
                    lag_ms,
                    sample_status,
                    best_bid,
                    best_ask,
                    at_anchor,
                    shocked,
                    survived,
                    survived_shock,
                    canonical_json(snapshot),
                ),
            )

    def record_resolution(
        self,
        *,
        binding_sha256: str,
        session_id: str,
        outcome_name: str,
        resolved_yes: bool,
        source: str,
        source_at: str,
        observed_at: str,
        payload: dict[str, Any],
    ) -> None:
        record = {
            "binding_sha256": binding_sha256,
            "outcome_name": outcome_name,
            "resolved_yes": bool(resolved_yes),
            "source": source,
            "source_at": _optional_iso(source_at),
            "payload": payload,
        }
        resolution_sha256 = sha256_json(record)
        with self._connect() as connection:
            conflicts = connection.execute(
                """
                SELECT resolved_yes FROM resolutions
                WHERE binding_sha256=? AND outcome_name=?
                """,
                (binding_sha256, outcome_name),
            ).fetchall()
            if any(bool(row["resolved_yes"]) != bool(resolved_yes) for row in conflicts):
                raise ValueError(
                    f"conflicting forward resolution for {outcome_name}"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO resolutions(
                    resolution_sha256, binding_sha256, session_id,
                    outcome_name, resolved_yes, source, source_at,
                    observed_at, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolution_sha256,
                    binding_sha256,
                    session_id,
                    outcome_name,
                    int(bool(resolved_yes)),
                    source,
                    record["source_at"],
                    _iso(observed_at, "observed_at"),
                    canonical_json(payload),
                ),
            )

    def rows_for_timeline(
        self,
        binding_sha256: str,
    ) -> dict[str, list[dict[str, Any]]]:
        with self._connect(read_only=True) as connection:
            book_bindings = self._related_book_bindings(
                connection,
                binding_sha256,
            )
            placeholders = ",".join("?" for _ in book_bindings)
            books = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT * FROM book_events
                    WHERE binding_sha256 IN ({placeholders})
                    ORDER BY received_at, id
                    """,
                    book_bindings,
                ).fetchall()
            ]
            trades = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT * FROM trade_prints
                    WHERE binding_sha256 IN ({placeholders})
                    ORDER BY received_at, id
                    """,
                    book_bindings,
                ).fetchall()
            ]
            articles = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM articles
                    WHERE binding_sha256=?
                    ORDER BY first_observed_at, article_id
                    """,
                    (binding_sha256,),
                ).fetchall()
            ]
            resolutions = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT * FROM resolutions
                    WHERE binding_sha256 IN ({placeholders})
                    ORDER BY observed_at, outcome_name
                    """,
                    book_bindings,
                ).fetchall()
            ]
        return {
            "books": books,
            "trades": trades,
            "articles": articles,
            "resolutions": resolutions,
        }

    def stream_available(
        self,
        *,
        binding_sha256: str,
        token_id: str,
        at: str,
    ) -> bool:
        """Return the last known socket state for the token at sample time."""
        sample_at = _iso(at, "stream availability time")
        with self._connect(read_only=True) as connection:
            book_bindings = self._related_book_bindings(
                connection,
                binding_sha256,
            )
            placeholders = ",".join("?" for _ in book_bindings)
            rows = connection.execute(
                f"""
                SELECT event_type, payload_json
                FROM operational_events
                WHERE binding_sha256 IN ({placeholders})
                  AND observed_at<=?
                  AND event_type IN (
                      'ws_open', 'ws_pong', 'ws_error', 'ws_close',
                      'ws_stop'
                  )
                ORDER BY observed_at DESC, id DESC
                """,
                (*book_bindings, sample_at),
            ).fetchall()
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            token_ids = payload.get("token_ids")
            if (
                isinstance(token_ids, list)
                and token_id in {str(item) for item in token_ids}
            ):
                return str(row["event_type"]) in {"ws_open", "ws_pong"}
        return False

    def completeness(
        self,
        *,
        binding_sha256: str,
        token_ids: list[str],
        horizons_ms: list[int],
        max_sample_lag_ms: int,
    ) -> dict[str, Any]:
        with self._connect(read_only=True) as connection:
            book_bindings = self._related_book_bindings(
                connection,
                binding_sha256,
            )
            placeholders = ",".join("?" for _ in book_bindings)
            sessions = connection.execute(
                f"""
                SELECT COUNT(*) AS n FROM sessions
                WHERE binding_sha256 IN ({placeholders})
                """,
                book_bindings,
            ).fetchone()
            books = connection.execute(
                f"""
                SELECT COUNT(*) AS n,
                       COUNT(DISTINCT token_id) AS tokens,
                       SUM(CASE WHEN source_at != '' THEN 1 ELSE 0 END) AS sourced
                FROM book_events
                WHERE binding_sha256 IN ({placeholders})
                """,
                book_bindings,
            ).fetchone()
            book_latency_rows = connection.execute(
                f"""
                SELECT source_latency_ms FROM book_events
                WHERE binding_sha256 IN ({placeholders})
                  AND source_latency_ms IS NOT NULL
                """,
                book_bindings,
            ).fetchall()
            trades = connection.execute(
                f"""
                SELECT COUNT(*) AS n,
                       COUNT(DISTINCT token_id) AS tokens,
                       SUM(CASE WHEN source_at != '' THEN 1 ELSE 0 END) AS sourced
                FROM trade_prints
                WHERE binding_sha256 IN ({placeholders})
                """,
                book_bindings,
            ).fetchone()
            trade_latency_rows = connection.execute(
                f"""
                SELECT source_latency_ms FROM trade_prints
                WHERE binding_sha256 IN ({placeholders})
                  AND source_latency_ms IS NOT NULL
                """,
                book_bindings,
            ).fetchall()
            seen_tokens = {
                str(row["token_id"])
                for row in connection.execute(
                    f"""
                    SELECT DISTINCT token_id FROM book_events
                    WHERE binding_sha256 IN ({placeholders})
                      AND event_type IN ('book', 'rest_book')
                    """,
                    book_bindings,
                ).fetchall()
            }
            articles = connection.execute(
                """
                SELECT COUNT(*) AS n,
                       SUM(CASE
                           WHEN json_extract(article_json, '$.raw_text') != ''
                           THEN 1 ELSE 0 END) AS full_text
                FROM articles WHERE binding_sha256=?
                """,
                (binding_sha256,),
            ).fetchone()
            article_rows = connection.execute(
                """
                SELECT article_id, article_json, first_observed_at
                FROM articles WHERE binding_sha256=?
                """,
                (binding_sha256,),
            ).fetchall()
            extracted = connection.execute(
                """
                SELECT COUNT(DISTINCT article_id) AS n
                FROM extractions WHERE binding_sha256=?
                """,
                (binding_sha256,),
            ).fetchone()
            extraction_rows = connection.execute(
                """
                SELECT duration_ms, status, result_json
                FROM extractions WHERE binding_sha256=?
                """,
                (binding_sha256,),
            ).fetchall()
            proofs = connection.execute(
                """
                SELECT COUNT(*) AS n,
                       SUM(CASE WHEN terminal=1 THEN 1 ELSE 0 END) AS terminal
                FROM decision_proofs WHERE binding_sha256=?
                """,
                (binding_sha256,),
            ).fetchone()
            proof_rows = connection.execute(
                """
                SELECT observed_at, proof_json
                FROM decision_proofs WHERE binding_sha256=?
                """,
                (binding_sha256,),
            ).fetchall()
            anchors = connection.execute(
                """
                SELECT COUNT(*) AS n FROM quote_anchors
                WHERE binding_sha256=?
                """,
                (binding_sha256,),
            ).fetchone()
            sample_rows = connection.execute(
                """
                SELECT s.horizon_ms, s.status, s.sample_lag_ms,
                       s.quote_survived, s.quote_survived_one_cent
                FROM quote_samples s
                JOIN quote_anchors a ON a.anchor_id=s.anchor_id
                WHERE a.binding_sha256=?
                """,
                (binding_sha256,),
            ).fetchall()
            resolutions = connection.execute(
                f"""
                SELECT COUNT(DISTINCT outcome_name) AS n
                FROM resolutions
                WHERE binding_sha256 IN ({placeholders})
                """,
                book_bindings,
            ).fetchone()
            ops = connection.execute(
                f"""
                SELECT event_type, COUNT(*) AS n FROM operational_events
                WHERE binding_sha256 IN ({placeholders})
                GROUP BY event_type
                """,
                book_bindings,
            ).fetchall()

        book_count = int(books["n"] or 0)
        trade_count = int(trades["n"] or 0)
        article_count = int(articles["n"] or 0)
        anchor_count = int(anchors["n"] or 0)
        samples: dict[str, dict[str, Any]] = {}
        for horizon in horizons_ms:
            rows = [
                row
                for row in sample_rows
                if int(row["horizon_ms"]) == int(horizon)
            ]
            on_time = [
                row
                for row in rows
                if str(row["status"]) == "OK"
                and float(row["sample_lag_ms"]) <= max_sample_lag_ms
            ]
            known = [
                row
                for row in on_time
                if row["quote_survived"] is not None
            ]
            samples[str(horizon)] = {
                "expected": anchor_count,
                "captured": len(rows),
                "on_time": len(on_time),
                "coverage": (
                    round(len(on_time) / anchor_count, 6)
                    if anchor_count
                    else None
                ),
                "quote_survival_rate": (
                    round(
                        sum(int(row["quote_survived"]) for row in known)
                        / len(known),
                        6,
                    )
                    if known
                    else None
                ),
                "one_cent_survival_rate": (
                    round(
                        sum(
                            int(row["quote_survived_one_cent"])
                            for row in known
                        )
                        / len(known),
                        6,
                    )
                    if known
                    else None
                ),
            }
        missing_tokens = sorted(set(token_ids) - seen_tokens)
        article_observed_at: dict[str, datetime] = {}
        article_published_at: dict[str, datetime] = {}
        article_discovered_at: dict[str, datetime] = {}
        publisher_to_fetch: list[float] = []
        publisher_to_discovery: list[float] = []
        discovery_to_fetch_start: list[float] = []
        publisher_fetch: list[float] = []
        publisher_parse: list[float] = []
        fetch_to_observe: list[float] = []
        parse_to_observe: list[float] = []
        future_published_articles = 0
        future_fetched_articles = 0
        future_discovered_articles = 0
        future_parsed_articles = 0
        fetched_before_published = 0
        fetch_started_before_discovery = 0
        parsed_before_fetch = 0
        for row in article_rows:
            article = json.loads(str(row["article_json"]))
            observed = _parse_at(str(row["first_observed_at"]))
            article_observed_at[str(row["article_id"])] = observed
            fetched_raw = str(article.get("fetched_at") or "")
            published_raw = str(article.get("published_at") or "")
            discovered_raw = str(
                article.get("discovered_at") or fetched_raw
            )
            fetch_started_raw = str(
                article.get("fetch_started_at") or ""
            )
            parsed_raw = str(article.get("parsed_at") or fetched_raw)
            discovered = (
                _parse_at(discovered_raw) if discovered_raw else None
            )
            fetch_started = (
                _parse_at(fetch_started_raw)
                if fetch_started_raw
                else None
            )
            parsed = _parse_at(parsed_raw) if parsed_raw else None
            if discovered is not None:
                article_discovered_at[str(row["article_id"])] = discovered
                if (observed - discovered).total_seconds() < -1.0:
                    future_discovered_articles += 1
            if parsed is not None:
                if (observed - parsed).total_seconds() < -1.0:
                    future_parsed_articles += 1
                parse_to_observe.append(
                    (observed - parsed).total_seconds() * 1000.0
                )
            if fetched_raw:
                fetched = _parse_at(fetched_raw)
                fetch_delay = (
                    observed - fetched
                ).total_seconds() * 1000.0
                fetch_to_observe.append(fetch_delay)
                if fetch_delay < -1_000.0:
                    future_fetched_articles += 1
                if fetch_started is not None:
                    publisher_fetch.append(
                        (fetched - fetch_started).total_seconds()
                        * 1000.0
                    )
                    if (
                        discovered is not None
                        and fetch_started < discovered
                    ):
                        fetch_started_before_discovery += 1
                    elif discovered is not None:
                        discovery_to_fetch_start.append(
                            (fetch_started - discovered).total_seconds()
                            * 1000.0
                        )
                if parsed is not None:
                    publisher_parse.append(
                        (parsed - fetched).total_seconds() * 1000.0
                    )
                    if parsed < fetched:
                        parsed_before_fetch += 1
                if published_raw:
                    published = _parse_at(published_raw)
                    article_published_at[str(row["article_id"])] = published
                    publication_delay = (
                        fetched - published
                    ).total_seconds() * 1000.0
                    publisher_to_fetch.append(publication_delay)
                    if discovered is not None:
                        publisher_to_discovery.append(
                            (discovered - published).total_seconds()
                            * 1000.0
                        )
                    if publication_delay < -1_000.0:
                        fetched_before_published += 1
                    if (
                        observed - published
                    ).total_seconds() < -1.0:
                        future_published_articles += 1
        extraction_durations = [
            float(row["duration_ms"])
            for row in extraction_rows
            if str(row["status"]) not in {"CACHED", "STALE"}
        ]
        deterministic_extraction_durations: list[float] = []
        model_extraction_durations: list[float] = []
        for row in extraction_rows:
            if str(row["status"]) in {"CACHED", "STALE"}:
                continue
            try:
                result = json.loads(str(row["result_json"]))
            except json.JSONDecodeError:
                result = {}
            target = (
                deterministic_extraction_durations
                if result.get("lane") == "deterministic"
                else model_extraction_durations
            )
            target.append(float(row["duration_ms"]))
        evidence_to_decision: list[float] = []
        publisher_to_submission: list[float] = []
        discovery_to_submission: list[float] = []
        decision_to_submission: list[float] = []
        submission_roundtrip: list[float] = []
        decisions_before_evidence = 0
        decision_submission_order_violations = 0
        for row in proof_rows:
            proof = json.loads(str(row["proof_json"]))
            evidence_times = [
                article_observed_at[article_id]
                for article_id in proof.get("article_ids", [])
                if article_id in article_observed_at
            ]
            if evidence_times:
                latency = (
                    _parse_at(str(row["observed_at"]))
                    - max(evidence_times)
                ).total_seconds() * 1000.0
                evidence_to_decision.append(latency)
                if latency < -1.0:
                    decisions_before_evidence += 1
            timing = proof.get("execution_result", {}).get(
                "_timing",
                {},
            )
            if not isinstance(timing, dict):
                timing = {}
            submission_raw = str(
                timing.get("submission_started_at")
                or row["observed_at"]
            )
            submission = _parse_at(submission_raw)
            decision_started_raw = str(
                timing.get("decision_started_at") or ""
            )
            decision_completed_raw = str(
                timing.get("decision_completed_at") or ""
            )
            submission_completed_raw = str(
                timing.get("submission_completed_at") or ""
            )
            ordered_stages = [
                _parse_at(value)
                for value in (
                    decision_started_raw,
                    decision_completed_raw,
                    submission_raw,
                    submission_completed_raw,
                )
                if value
            ]
            if any(
                later < earlier
                for earlier, later in zip(
                    ordered_stages,
                    ordered_stages[1:],
                )
            ):
                decision_submission_order_violations += 1
            if decision_started_raw:
                decision_to_submission.append(
                    (
                        submission - _parse_at(decision_started_raw)
                    ).total_seconds()
                    * 1000.0
                )
            if submission_completed_raw:
                submission_roundtrip.append(
                    (
                        _parse_at(submission_completed_raw) - submission
                    ).total_seconds()
                    * 1000.0
                )
            article_ids = [
                str(item) for item in proof.get("article_ids", [])
            ]
            published_times = [
                article_published_at[item]
                for item in article_ids
                if item in article_published_at
            ]
            discovered_times = [
                article_discovered_at[item]
                for item in article_ids
                if item in article_discovered_at
            ]
            if published_times:
                publisher_to_submission.append(
                    (submission - max(published_times)).total_seconds()
                    * 1000.0
                )
            if discovered_times:
                discovery_to_submission.append(
                    (submission - max(discovered_times)).total_seconds()
                    * 1000.0
                )
        future_book_timestamps = sum(
            1
            for row in book_latency_rows
            if float(row["source_latency_ms"]) < -1_000.0
        )
        future_trade_timestamps = sum(
            1
            for row in trade_latency_rows
            if float(row["source_latency_ms"]) < -1_000.0
        )
        blockers: list[str] = []
        if not int(sessions["n"] or 0):
            blockers.append("no_forward_session")
        if missing_tokens:
            blockers.append("missing_book_tokens")
        if article_count == 0:
            blockers.append("no_full_text_articles")
        if int(articles["full_text"] or 0) != article_count:
            blockers.append("article_full_text_incomplete")
        if int(extracted["n"] or 0) != article_count:
            blockers.append("article_extraction_incomplete")
        if anchor_count and any(
            item["on_time"] != anchor_count for item in samples.values()
        ):
            blockers.append("quote_survival_sampling_incomplete")
        if future_book_timestamps:
            blockers.append("future_book_source_timestamp")
        if future_trade_timestamps:
            blockers.append("future_trade_source_timestamp")
        if future_published_articles or future_fetched_articles:
            blockers.append("future_article_timestamp")
        if future_discovered_articles or future_parsed_articles:
            blockers.append("future_article_stage_timestamp")
        if fetched_before_published:
            blockers.append("article_fetched_before_publication")
        if fetch_started_before_discovery:
            blockers.append("article_fetch_started_before_discovery")
        if parsed_before_fetch:
            blockers.append("article_parsed_before_fetch")
        if decisions_before_evidence:
            blockers.append("decision_precedes_evidence")
        if decision_submission_order_violations:
            blockers.append("decision_submission_stage_order_invalid")
        return {
            "schema_version": FORWARD_RECORDER_SCHEMA_VERSION,
            "binding_sha256": binding_sha256,
            "status": "READY_FOR_REPLAY" if not blockers else "COLLECTING",
            "replay_ready": not blockers,
            "promotion_ready": False,
            "blockers": blockers,
            "sessions": int(sessions["n"] or 0),
            "book_events": book_count,
            "book_source_timestamp_rate": (
                round(int(books["sourced"] or 0) / book_count, 6)
                if book_count
                else None
            ),
            "book_tokens_expected": len(token_ids),
            "book_tokens_seen": len(seen_tokens),
            "missing_book_tokens": missing_tokens,
            "trade_prints": trade_count,
            "trade_print_tokens": int(trades["tokens"] or 0),
            "trade_source_timestamp_rate": (
                round(int(trades["sourced"] or 0) / trade_count, 6)
                if trade_count
                else None
            ),
            "articles": article_count,
            "articles_with_full_text": int(articles["full_text"] or 0),
            "articles_with_extraction": int(extracted["n"] or 0),
            "decision_proofs": int(proofs["n"] or 0),
            "terminal_decision_proofs": int(proofs["terminal"] or 0),
            "quote_anchors": anchor_count,
            "quote_survival": samples,
            "latency_ms": {
                "publisher_to_discovery": _latency_summary(
                    publisher_to_discovery
                ),
                "discovery_to_fetch_start": _latency_summary(
                    discovery_to_fetch_start
                ),
                "publisher_fetch": _latency_summary(publisher_fetch),
                "publisher_parse": _latency_summary(publisher_parse),
                "publisher_to_fetch": _latency_summary(
                    publisher_to_fetch
                ),
                "fetch_to_first_observation": _latency_summary(
                    fetch_to_observe
                ),
                "parse_to_first_observation": _latency_summary(
                    parse_to_observe
                ),
                "two_pass_extraction": _latency_summary(
                    extraction_durations
                ),
                "deterministic_extraction": _latency_summary(
                    deterministic_extraction_durations
                ),
                "model_extraction": _latency_summary(
                    model_extraction_durations
                ),
                "latest_evidence_to_decision": _latency_summary(
                    evidence_to_decision
                ),
                "publisher_to_submission": _latency_summary(
                    publisher_to_submission
                ),
                "discovery_to_submission": _latency_summary(
                    discovery_to_submission
                ),
                "decision_to_submission": _latency_summary(
                    decision_to_submission
                ),
                "simulated_submission_roundtrip": _latency_summary(
                    submission_roundtrip
                ),
                "book_source_to_receive": _latency_summary(
                    [
                        float(row["source_latency_ms"])
                        for row in book_latency_rows
                    ]
                ),
                "trade_source_to_receive": _latency_summary(
                    [
                        float(row["source_latency_ms"])
                        for row in trade_latency_rows
                    ]
                ),
            },
            "time_integrity": {
                "future_book_source_timestamps": future_book_timestamps,
                "future_trade_source_timestamps": (
                    future_trade_timestamps
                ),
                "future_published_articles": future_published_articles,
                "future_fetched_articles": future_fetched_articles,
                "future_discovered_articles": (
                    future_discovered_articles
                ),
                "future_parsed_articles": future_parsed_articles,
                "articles_fetched_before_publication": (
                    fetched_before_published
                ),
                "fetches_started_before_discovery": (
                    fetch_started_before_discovery
                ),
                "articles_parsed_before_fetch": parsed_before_fetch,
                "decisions_before_evidence": decisions_before_evidence,
                "decision_submission_order_violations": (
                    decision_submission_order_violations
                ),
            },
            "resolved_outcomes": int(resolutions["n"] or 0),
            "operational_events": {
                str(row["event_type"]): int(row["n"])
                for row in ops
            },
            "generated_at": _now(),
        }


class ForwardBookService:
    """Fleet-wide sharded market stream feeding every paper worker."""

    def __init__(
        self,
        config: DiscoveryConfig,
        *,
        notifier: Any | None = None,
    ) -> None:
        if not config.forward_recorder.enabled:
            raise ValueError("forward book service requires recorder.enabled")
        if not config.forward_recorder.shared_book_service:
            raise ValueError(
                "forward book service requires shared_book_service"
            )
        self.config = config
        self.recorder_config = config.forward_recorder
        self._notifier = notifier
        self.store = ForwardRecorderStore(forward_recorder_db_path(config))
        self.store.close_active_capture_sessions(
            ended_at=_now(),
            reason="service_startup_orphan_recovery",
        )
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._caches: list[BookCache] = []
        self._bindings_by_token: dict[str, list[str]] = {}
        self._contexts_by_binding: dict[str, MarketContext] = {}
        self._sessions_by_binding: dict[str, str] = {}
        self._fingerprint = ""
        self._streaming = False
        self._last_sync_at = ""
        self._generation = 0
        self._seed_cancel = threading.Event()
        self._seed_thread: threading.Thread | None = None
        self._seed_total = 0
        self._seed_completed = 0
        self._seed_errors = 0
        self._seed_in_progress = False
        self._seed_max_pending = 0
        self._storage_errors = 0
        self._status_path = (
            config.data_dir / "forward_books_status.json"
        )
        self._status_stop = threading.Event()
        self._status_thread: threading.Thread | None = None
        self._service_started_monotonic = time.monotonic()
        self._sync_started_monotonic = self._service_started_monotonic
        self._book_events_at_sync = 0
        self._last_health_signature: tuple[str, ...] = ()
        self._last_health_alert_monotonic = 0.0

    def sync(
        self,
        contexts: Iterable[MarketContext],
        *,
        start_websocket: bool = True,
    ) -> dict[str, Any]:
        (
            bindings_by_token,
            contexts_by_binding,
        ) = self._resolve_bindings(contexts)
        fingerprint = sha256_json(
            {
                "bindings_by_token": bindings_by_token,
                "streaming": start_websocket,
                "recorder_policy": _recorder_policy_payload(
                    self.recorder_config
                ),
            }
        )
        with self._lock:
            if fingerprint == self._fingerprint:
                return self.status()
        self._shutdown_streams(reason="universe_refresh")
        if not bindings_by_token:
            capture = self.store.capture_status()
            with self._lock:
                self._fingerprint = fingerprint
                self._last_sync_at = _now()
                self._sync_started_monotonic = time.monotonic()
                self._book_events_at_sync = int(
                    capture.get("book_events", 0)
                )
            self._start_status_publisher()
            return self.status()

        started_at = _now()
        capture = self.store.capture_status()
        sessions: dict[str, str] = {}
        for binding in contexts_by_binding:
            session_id = f"shared-books-{uuid.uuid4().hex}"
            self.store.start_session(
                binding,
                session_id=session_id,
                started_at=started_at,
            )
            sessions[binding] = session_id
        with self._lock:
            self._bindings_by_token = bindings_by_token
            self._contexts_by_binding = contexts_by_binding
            self._sessions_by_binding = sessions
            generation = self._generation

        tokens = sorted(bindings_by_token)
        shard_size = self.recorder_config.max_tokens_per_connection
        caches: list[BookCache] = []
        for index in range(0, len(tokens), shard_size):
            cache = BookCache(
                tokens[index : index + shard_size],
                ws_url=self.recorder_config.websocket_url,
                heartbeat_seconds=self.recorder_config.heartbeat_seconds,
                reconnect_min_seconds=(
                    self.recorder_config.reconnect_min_seconds
                ),
                reconnect_max_seconds=(
                    self.recorder_config.reconnect_max_seconds
                ),
                max_snapshot_levels=self.recorder_config.max_book_levels,
            )
            cache.add_listener(
                lambda record, generation=generation: self._on_stream_event(
                    record,
                    generation=generation,
                )
            )
            caches.append(cache)
        with self._lock:
            self._caches = caches
            self._streaming = start_websocket
            self._fingerprint = fingerprint
            self._last_sync_at = started_at
            self._sync_started_monotonic = time.monotonic()
            self._book_events_at_sync = int(
                capture.get("book_events", 0)
            )

        if start_websocket:
            for cache in caches:
                cache.start_ws()
        if self.recorder_config.rest_seed:
            if start_websocket:
                self._start_seed_books(caches, generation=generation)
            else:
                cancel = threading.Event()
                with self._lock:
                    self._seed_total = len(tokens)
                    self._seed_completed = 0
                    self._seed_errors = 0
                    self._seed_in_progress = bool(tokens)
                self._seed_books(
                    caches,
                    generation=generation,
                    cancel=cancel,
                )
        self._start_status_publisher()
        return self.status()

    def poll_once(
        self,
        contexts: Iterable[MarketContext],
    ) -> dict[str, Any]:
        return self.sync(contexts, start_websocket=False)

    def stop(self) -> None:
        self._shutdown_streams(reason="service_stop")
        self._status_stop.set()
        if self._status_thread is not None:
            self._status_thread.join(timeout=2)
        self._publish_status_file()

    def status(self) -> dict[str, Any]:
        capture = self.store.capture_status()
        now_monotonic = time.monotonic()
        with self._lock:
            connections = [
                cache.connection_state() for cache in self._caches
            ]
            status = {
                "enabled": True,
                "paper_only": True,
                "shared": True,
                "reason": (
                    ""
                    if self._contexts_by_binding
                    else "no_recordable_contexts"
                ),
                "selected_contexts": len(self._contexts_by_binding),
                "bindings": len(self._contexts_by_binding),
                "tokens": len(self._bindings_by_token),
                "connections": len(self._caches),
                "connected": sum(
                    1 for item in connections if item["connected"]
                ),
                "reconnects": sum(
                    int(item["reconnects"]) for item in connections
                ),
                "rest_seed_enabled": self.recorder_config.rest_seed,
                "rest_seed_in_progress": self._seed_in_progress,
                "rest_seed_total": self._seed_total,
                "rest_seed_completed": self._seed_completed,
                "rest_seed_errors": self._seed_errors,
                "rest_seed_max_pending": self._seed_max_pending,
                "storage_errors": self._storage_errors,
                "streaming": self._streaming,
                "last_sync_at": self._last_sync_at,
            }
            sync_started = self._sync_started_monotonic
            service_started = self._service_started_monotonic
            book_events_at_sync = self._book_events_at_sync
        elapsed = max(0.0, now_monotonic - sync_started)
        events_since_sync = max(
            0,
            int(capture.get("book_events", 0)) - book_events_at_sync,
        )
        latest_age = _timestamp_age_seconds(
            str(capture.get("latest_book_received_at") or "")
        )
        health_blockers: list[str] = []
        selected = int(status["selected_contexts"])
        in_grace = (
            elapsed
            < self.recorder_config.health_startup_grace_seconds
        )
        if not selected:
            health_blockers.append("no_recordable_contexts")
        else:
            if not status["streaming"]:
                health_blockers.append("streaming_disabled")
            if not in_grace:
                if int(status["connected"]) != int(status["connections"]):
                    health_blockers.append(
                        "disconnected_shards:"
                        f"{status['connected']}/{status['connections']}"
                    )
                if int(capture["active_capture_sessions"]) != selected:
                    health_blockers.append(
                        "active_session_mismatch:"
                        f"{capture['active_capture_sessions']}/{selected}"
                    )
                if latest_age is None:
                    health_blockers.append("no_book_events")
                elif (
                    latest_age
                    > self.recorder_config.health_stale_after_seconds
                ):
                    health_blockers.append(
                        f"latest_book_stale:{latest_age:.1f}s"
                    )
                if (
                    elapsed
                    >= self.recorder_config.health_growth_window_seconds
                    and events_since_sync == 0
                ):
                    health_blockers.append("book_event_growth_stalled")
            if int(status["storage_errors"]) > 0:
                health_blockers.append(
                    f"storage_errors:{status['storage_errors']}"
                )
        soak = {
            "healthy": not health_blockers,
            "health_blockers": health_blockers,
            "health_in_startup_grace": in_grace,
            "uptime_seconds": round(
                max(0.0, now_monotonic - service_started),
                3,
            ),
            "sync_uptime_seconds": round(elapsed, 3),
            "latest_book_age_seconds": (
                round(latest_age, 3) if latest_age is not None else None
            ),
            "book_events_at_sync": book_events_at_sync,
            "book_events_since_sync": events_since_sync,
            "book_event_rate_per_minute": round(
                events_since_sync * 60.0 / elapsed,
                3,
            )
            if elapsed > 0
            else 0.0,
        }
        return {**status, **capture, **soak}

    def _resolve_bindings(
        self,
        contexts: Iterable[MarketContext],
    ) -> tuple[dict[str, list[str]], dict[str, MarketContext]]:
        bindings_by_token: dict[str, list[str]] = {}
        contexts_by_binding: dict[str, MarketContext] = {}
        for context in contexts:
            try:
                binding = self.store.ensure_book_binding(
                    context,
                    self.recorder_config,
                )
            except ValueError as exc:
                log_event(
                    "forward_book_binding_rejected",
                    market_id=context.market_id,
                    error=str(exc),
                )
                continue
            contexts_by_binding[binding] = context
            for outcome in context.outcomes:
                for token_id in (
                    outcome.yes_token_id,
                    outcome.no_token_id,
                ):
                    if token_id:
                        bindings_by_token.setdefault(token_id, []).append(
                            binding
                        )
        return (
            {
                token: sorted(set(bindings))
                for token, bindings in sorted(bindings_by_token.items())
            },
            contexts_by_binding,
        )

    def _start_status_publisher(self) -> None:
        with self._lock:
            if (
                self._status_thread is not None
                and self._status_thread.is_alive()
            ):
                return
            self._status_stop.clear()
            thread = threading.Thread(
                target=self._status_publisher_loop,
                name="polybot-forward-status",
                daemon=True,
            )
            self._status_thread = thread
        thread.start()

    def _status_publisher_loop(self) -> None:
        while not self._status_stop.wait(5.0):
            self._publish_status_file()

    def _publish_status_file(self) -> None:
        try:
            from polybot.core.holdings import _atomic_json_write

            status = self.status()
            _atomic_json_write(
                self._status_path,
                {
                    **status,
                    "published_at": _now(),
                },
            )
            self._maybe_alert_health(status)
        except Exception as exc:
            log_event(
                "forward_book_status_publish_failed",
                error=str(exc),
            )

    def _maybe_alert_health(self, status: dict[str, Any]) -> None:
        if int(status.get("selected_contexts", 0)) <= 0:
            return
        signature = tuple(
            str(item) for item in status.get("health_blockers", [])
        )
        now_monotonic = time.monotonic()
        with self._lock:
            previous = self._last_health_signature
            changed = signature != previous
            cooldown_elapsed = (
                now_monotonic - self._last_health_alert_monotonic
                >= self.recorder_config.health_alert_cooldown_seconds
            )
            if not changed and (not signature or not cooldown_elapsed):
                return
            self._last_health_signature = signature
            if signature:
                self._last_health_alert_monotonic = now_monotonic
        fields = {
            "blockers": list(signature),
            "selected_contexts": status.get("selected_contexts"),
            "tokens": status.get("tokens"),
            "connections": status.get("connections"),
            "connected": status.get("connected"),
            "active_capture_sessions": status.get(
                "active_capture_sessions"
            ),
            "book_events": status.get("book_events"),
            "latest_book_age_seconds": status.get(
                "latest_book_age_seconds"
            ),
            "storage_errors": status.get("storage_errors"),
        }
        event = (
            "forward_book_health_alert"
            if signature
            else "forward_book_health_recovered"
        )
        log_event(event, **fields)
        if self._notifier is None:
            return
        try:
            self._notifier.notify(
                (
                    "Forward recorder unhealthy"
                    if signature
                    else "Forward recorder recovered"
                ),
                **fields,
            )
        except Exception as exc:
            log_event(
                "forward_book_health_notify_failed",
                error=str(exc),
            )

    def _start_seed_books(
        self,
        caches: list[BookCache],
        *,
        generation: int,
    ) -> None:
        cancel = threading.Event()
        jobs = sum(len(cache.token_ids) for cache in caches)
        with self._lock:
            self._seed_cancel = cancel
            self._seed_total = jobs
            self._seed_completed = 0
            self._seed_errors = 0
            self._seed_in_progress = bool(jobs)
            self._seed_max_pending = 0
        if not jobs:
            return
        thread = threading.Thread(
            target=self._seed_books,
            args=(caches,),
            kwargs={"generation": generation, "cancel": cancel},
            name="polybot-book-rest-seed",
            daemon=True,
        )
        with self._lock:
            self._seed_thread = thread
        thread.start()

    def _seed_books(
        self,
        caches: list[BookCache],
        *,
        generation: int,
        cancel: threading.Event,
    ) -> None:
        jobs: list[tuple[BookCache, str]] = [
            (cache, token_id)
            for cache in caches
            for token_id in cache.token_ids
        ]
        if not jobs:
            with self._lock:
                if generation == self._generation:
                    self._seed_in_progress = False
            return
        worker_count = min(
            self.recorder_config.rest_seed_workers,
            len(jobs),
        )
        pool = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="polybot-book-rest",
        )
        pending = {}
        remaining = iter(jobs)

        def submit_available() -> None:
            while (
                not cancel.is_set()
                and len(pending) < worker_count * 2
            ):
                try:
                    cache, token_id = next(remaining)
                except StopIteration:
                    return
                pending[pool.submit(cache.rest_snapshot, token_id)] = (
                    token_id
                )
                with self._lock:
                    if generation == self._generation:
                        self._seed_max_pending = max(
                            self._seed_max_pending,
                            len(pending),
                        )

        try:
            submit_available()
            while pending and not cancel.is_set():
                completed, _ = wait(
                    pending,
                    return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    token_id = pending.pop(future)
                    try:
                        future.result()
                    except Exception as exc:
                        with self._lock:
                            if generation != self._generation:
                                return
                            self._seed_errors += 1
                        self._record_token_operational(
                            token_id,
                            event_type="rest_seed_error",
                            payload={"error": str(exc)},
                            generation=generation,
                        )
                    finally:
                        # Removing completed futures promptly releases any
                        # requests traceback/Response they retained.
                        with self._lock:
                            if generation == self._generation:
                                self._seed_completed += 1
                submit_available()
        finally:
            for future in pending:
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            with self._lock:
                if generation == self._generation:
                    self._seed_in_progress = False

    def _shutdown_streams(self, *, reason: str) -> None:
        with self._lock:
            self._seed_cancel.set()
            self._generation += 1
            caches = list(self._caches)
            sessions = dict(self._sessions_by_binding)
        for cache in caches:
            cache.stop_ws()
        ended_at = _now()
        for session_id in sessions.values():
            self.store.end_session(
                session_id,
                ended_at=ended_at,
                reason=reason,
            )
        with self._lock:
            self._caches = []
            self._bindings_by_token = {}
            self._contexts_by_binding = {}
            self._sessions_by_binding = {}
            self._streaming = False
            self._fingerprint = ""
            self._seed_in_progress = False

    def _on_stream_event(
        self,
        record: dict[str, Any],
        *,
        generation: int,
    ) -> None:
        event_type = str(record.get("event_type") or "")
        token_id = str(record.get("token_id") or "")
        with self._lock:
            if generation != self._generation:
                return
            bindings = list(self._bindings_by_token.get(token_id, []))
            bindings_by_token = {
                item: list(bound)
                for item, bound in self._bindings_by_token.items()
            }
            sessions = dict(self._sessions_by_binding)
            contexts = dict(self._contexts_by_binding)
        if (
            token_id
            and isinstance(record.get("snapshot"), dict)
            and event_type in {"book", "rest_book", "price_change"}
        ):
            try:
                with self._write_lock:
                    for binding in bindings:
                        self.store.record_book_event(
                            session_id=sessions[binding],
                            binding_sha256=binding,
                            record=record,
                        )
            except sqlite3.Error as exc:
                self._note_storage_error(
                    event_type=event_type,
                    token_id=token_id,
                    error=exc,
                )
            return
        if (
            token_id
            and event_type == "last_trade_price"
            and isinstance(record.get("event"), dict)
        ):
            try:
                with self._write_lock:
                    for binding in bindings:
                        self.store.record_trade_print(
                            session_id=sessions[binding],
                            binding_sha256=binding,
                            record=record,
                        )
            except sqlite3.Error as exc:
                self._note_storage_error(
                    event_type=event_type,
                    token_id=token_id,
                    error=exc,
                )
            return
        if event_type == "market_resolved":
            try:
                with self._write_lock:
                    for binding, context in contexts.items():
                        _record_stream_resolution(
                            store=self.store,
                            binding_sha256=binding,
                            session_id=sessions[binding],
                            context=context,
                            record=record,
                        )
            except sqlite3.Error as exc:
                self._note_storage_error(
                    event_type=event_type,
                    token_id=token_id,
                    error=exc,
                )
            return
        payload = record
        observed_at = str(record.get("received_at") or _now())
        event_tokens = record.get("token_ids")
        if isinstance(event_tokens, list):
            target_bindings = {
                binding
                for item in event_tokens
                for binding in bindings_by_token.get(str(item), [])
            }
        else:
            target_bindings = set(sessions)
        try:
            with self._write_lock:
                for binding in sorted(target_bindings):
                    self.store.record_operational_event(
                        session_id=sessions[binding],
                        binding_sha256=binding,
                        event_type=event_type or "market_stream_event",
                        observed_at=observed_at,
                        payload=payload,
                    )
        except sqlite3.Error as exc:
            self._note_storage_error(
                event_type=event_type or "market_stream_event",
                token_id=token_id,
                error=exc,
            )

    def _record_token_operational(
        self,
        token_id: str,
        *,
        event_type: str,
        payload: dict[str, Any],
        generation: int,
    ) -> None:
        with self._lock:
            if generation != self._generation:
                return
            bindings = list(self._bindings_by_token.get(token_id, []))
            sessions = dict(self._sessions_by_binding)
        try:
            with self._write_lock:
                for binding in bindings:
                    self.store.record_operational_event(
                        session_id=sessions[binding],
                        binding_sha256=binding,
                        event_type=event_type,
                        observed_at=_now(),
                        payload={"token_id": token_id, **payload},
                    )
        except sqlite3.Error as exc:
            self._note_storage_error(
                event_type=event_type,
                token_id=token_id,
                error=exc,
            )

    def _note_storage_error(
        self,
        *,
        event_type: str,
        token_id: str,
        error: sqlite3.Error,
    ) -> None:
        with self._lock:
            self._storage_errors += 1
        log_event(
            "forward_book_store_write_error",
            event_type=event_type,
            token_id=token_id,
            error=str(error),
        )


class RecordedClobQuoteAdapter:
    """Public quotes plus forward evidence capture; no trading credentials."""

    def __init__(
        self,
        *,
        config: DiscoveryConfig,
        context: MarketContext,
        spec: RuleSpec,
        source_plan: SourcePlan,
    ) -> None:
        self.config = config
        self.context = context
        self.spec = spec
        self.source_plan = source_plan
        self.recorder_config = config.forward_recorder
        self.store = ForwardRecorderStore(forward_recorder_db_path(config))
        self.binding_sha256 = self.store.ensure_binding(
            context,
            spec,
            source_plan,
            self.recorder_config,
            evidence_policy=_evidence_policy_payload(config),
        )
        self.session_id = uuid.uuid4().hex
        self.started_at = _now()
        self.token_ids = [
            token
            for outcome in spec.outcomes
            for token in (outcome.yes_token_id, outcome.no_token_id)
        ]
        self.cache: BookCache | None = None
        if not self.recorder_config.shared_book_service:
            self.cache = BookCache(
                self.token_ids,
                ws_url=self.recorder_config.websocket_url,
                heartbeat_seconds=self.recorder_config.heartbeat_seconds,
                reconnect_min_seconds=(
                    self.recorder_config.reconnect_min_seconds
                ),
                reconnect_max_seconds=(
                    self.recorder_config.reconnect_max_seconds
                ),
                max_snapshot_levels=self.recorder_config.max_book_levels,
            )
            self.cache.add_listener(self._on_stream_event)
        self._started = False
        self._stopped = threading.Event()
        self._sample_wakeup = threading.Event()
        self._sampler_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.started_at = _now()
        self.store.start_session(
            self.binding_sha256,
            session_id=self.session_id,
            started_at=self.started_at,
        )
        if self.cache is not None and self.recorder_config.rest_seed:
            for token_id in self.token_ids:
                try:
                    self.cache.rest_snapshot(token_id)
                except Exception as exc:
                    self.store.record_operational_event(
                        session_id=self.session_id,
                        binding_sha256=self.binding_sha256,
                        event_type="rest_seed_error",
                        observed_at=_now(),
                        payload={
                            "token_id": token_id,
                            "error": str(exc),
                        },
                    )
        if self.cache is not None:
            self.cache.start_ws()
        else:
            self.store.record_operational_event(
                session_id=self.session_id,
                binding_sha256=self.binding_sha256,
                event_type="shared_book_reader_start",
                observed_at=_now(),
                payload={"token_ids": self.token_ids},
            )
        self._sampler_thread = threading.Thread(
            target=self._sampler_loop,
            name="polybot-quote-survival",
            daemon=True,
        )
        self._sampler_thread.start()

    def stop(self, *, reason: str = "normal_stop") -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._sample_wakeup.set()
        if self.cache is not None:
            self.cache.stop_ws()
        if self._sampler_thread:
            self._sampler_thread.join(timeout=2)
        self.sample_due()
        self.store.end_session(
            self.session_id,
            ended_at=_now(),
            reason=reason,
        )

    def quote_snapshot(self, token_id: str) -> dict[str, Any]:
        if self.cache is None:
            return self.store.latest_book_snapshot(
                binding_sha256=self.binding_sha256,
                token_id=token_id,
            )
        snapshot = self.cache.snapshot_state(token_id)
        snapshot["source"] = "public_clob_websocket_recorded"
        return snapshot

    def yes_best_ask(self, token_id: str) -> float | None:
        return _float_or_none(self.quote_snapshot(token_id).get("best_ask"))

    def yes_best_bid(self, token_id: str) -> float | None:
        return _float_or_none(self.quote_snapshot(token_id).get("best_bid"))

    def record_article(
        self,
        article: Article,
        *,
        observed_at: str,
        cycle_id: str,
    ) -> None:
        self.store.record_article(
            binding_sha256=self.binding_sha256,
            article=article,
            first_observed_at=observed_at,
            cycle_id=cycle_id,
        )

    def record_extraction(
        self,
        article_id: str,
        *,
        started_at: str,
        completed_at: str,
        duration_ms: float,
        result: dict[str, Any],
    ) -> None:
        self.store.record_extraction(
            binding_sha256=self.binding_sha256,
            article_id=article_id,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            result=result,
        )

    def record_decision(
        self,
        evaluation: RuleEvaluation,
        proof: DecisionProof,
        *,
        observed_at: str,
    ) -> None:
        self.store.record_proof(
            binding_sha256=self.binding_sha256,
            evaluation=evaluation,
            proof=proof,
            observed_at=observed_at,
        )
        if (
            evaluation.terminal
            and proof.action in {"ENTER_YES", "ENTER_NO"}
            and proof.token_id
            and proof.executable_ask is not None
        ):
            snapshot = self.quote_snapshot(proof.token_id)
            self.store.record_quote_anchor(
                binding_sha256=self.binding_sha256,
                proof=proof,
                snapshot=snapshot,
                anchor_at=observed_at,
            )
            self._sample_wakeup.set()

    def record_cycle(self, summary: dict[str, Any], *, observed_at: str) -> None:
        self.store.record_operational_event(
            session_id=self.session_id,
            binding_sha256=self.binding_sha256,
            event_type="runner_cycle",
            observed_at=observed_at,
            payload=summary,
        )

    def sample_due(self, *, now: datetime | None = None) -> int:
        sampled = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        sampled_at = sampled.isoformat()
        pending = self.store.pending_quote_samples(
            self.recorder_config.quote_survival_horizons_ms,
            due_before=sampled_at,
        )
        for anchor in pending:
            snapshot = self.quote_snapshot(str(anchor["token_id"]))
            has_book = (
                snapshot.get("best_ask") is not None
                and bool(snapshot.get("asks"))
            )
            stream_available = self.store.stream_available(
                binding_sha256=str(anchor["binding_sha256"]),
                token_id=str(anchor["token_id"]),
                at=sampled_at,
            )
            if not has_book:
                status = "MISSING_BOOK"
            elif not stream_available:
                status = "STREAM_UNAVAILABLE"
            else:
                status = "OK"
            self.store.record_quote_sample(
                anchor=anchor,
                sampled_at=sampled_at,
                status=status,
                snapshot=snapshot,
                max_sample_lag_ms=self.recorder_config.max_sample_lag_ms,
            )
        return len(pending)

    def _sampler_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                next_due = self.store.next_quote_sample_due(
                    self.recorder_config.quote_survival_horizons_ms
                )
                if next_due is None:
                    self._sample_wakeup.wait(60.0)
                    self._sample_wakeup.clear()
                    continue
                delay = max(
                    0.0,
                    (
                        next_due - datetime.now(timezone.utc)
                    ).total_seconds(),
                )
                if delay > 0:
                    self._sample_wakeup.wait(delay)
                    self._sample_wakeup.clear()
                    continue
                self.sample_due()
            except Exception as exc:
                log_event(
                    "forward_quote_sampler_failed",
                    market_id=self.context.market_id,
                    error=str(exc),
                )
                self._stopped.wait(1.0)

    def _on_stream_event(self, record: dict[str, Any]) -> None:
        event_type = str(record.get("event_type") or "")
        if (
            event_type == "last_trade_price"
            and isinstance(record.get("event"), dict)
        ):
            self.store.record_trade_print(
                session_id=self.session_id,
                binding_sha256=self.binding_sha256,
                record=record,
            )
            return
        if (
            isinstance(record.get("snapshot"), dict)
            and event_type in {"book", "rest_book", "price_change"}
        ):
            self.store.record_book_event(
                session_id=self.session_id,
                binding_sha256=self.binding_sha256,
                record=record,
            )
            return
        if isinstance(record.get("snapshot"), dict):
            self.store.record_operational_event(
                session_id=self.session_id,
                binding_sha256=self.binding_sha256,
                event_type=event_type or "market_metadata",
                observed_at=_now(),
                payload=record,
            )
            return
        if event_type == "market_resolved":
            self._record_market_resolution(record)
            return
        self.store.record_operational_event(
            session_id=self.session_id,
            binding_sha256=self.binding_sha256,
            event_type=event_type or "unknown_stream_event",
            observed_at=str(record.get("received_at") or _now()),
            payload=record,
        )

    def _record_market_resolution(self, record: dict[str, Any]) -> None:
        _record_stream_resolution(
            store=self.store,
            binding_sha256=self.binding_sha256,
            session_id=self.session_id,
            context=self.context,
            record=record,
        )


def _record_stream_resolution(
    *,
    store: ForwardRecorderStore,
    binding_sha256: str,
    session_id: str,
    context: MarketContext,
    record: dict[str, Any],
) -> None:
    event = (
        record.get("event")
        if isinstance(record.get("event"), dict)
        else {}
    )
    winner = str(
        event.get("winning_asset_id")
        or event.get("winning_token_id")
        or ""
    )
    condition_id = str(event.get("market") or "")
    if not winner:
        return
    candidates = [
        outcome
        for outcome in context.outcomes
        if not condition_id or outcome.condition_id == condition_id
    ]
    if not candidates:
        return
    all_yes_tokens = {
        outcome.yes_token_id for outcome in context.outcomes
    }
    for outcome in candidates:
        if winner == outcome.yes_token_id:
            resolved_yes = True
        elif winner == outcome.no_token_id:
            resolved_yes = False
        elif context.neg_risk and winner in all_yes_tokens:
            resolved_yes = False
        else:
            continue
        store.record_resolution(
            binding_sha256=binding_sha256,
            session_id=session_id,
            outcome_name=outcome.name,
            resolved_yes=resolved_yes,
            source="polymarket_market_stream",
            source_at=str(record.get("source_at") or ""),
            observed_at=str(record.get("received_at") or _now()),
            payload=event,
        )


def record_forward_resolutions(
    config: DiscoveryConfig,
    records: Iterable[dict[str, Any]],
    *,
    observed_at: str | None = None,
) -> int:
    """Mirror Gamma-finalized outcomes into the immutable forward dataset."""
    if not config.forward_recorder.enabled:
        return 0
    discovery = DiscoveryStore(config.data_dir)
    rule_store = RuleStore(rule_store_db_path(config))
    forward = ForwardRecorderStore(forward_recorder_db_path(config))
    count = 0
    at = observed_at or _now()
    for record in records:
        market_id = str(record.get("market_id") or "")
        outcome_name = str(record.get("outcome") or "")
        if not market_id or not outcome_name:
            continue
        context = discovery.load_context(market_id)
        plan = discovery.load_source_plan(market_id)
        if context is None or plan is None:
            continue
        spec = rule_store.load_spec(market_id, context.rule_text_sha256)
        if spec is None:
            continue
        binding = forward.ensure_binding(
            context,
            spec,
            plan,
            config.forward_recorder,
            evidence_policy=_evidence_policy_payload(config),
            created_at=at,
        )
        forward.record_resolution(
            binding_sha256=binding,
            session_id="gamma-resolution-sync",
            outcome_name=outcome_name,
            resolved_yes=bool(record.get("resolved_yes")),
            source="gamma_finalization",
            source_at=at,
            observed_at=at,
            payload=record,
        )
        count += 1
    return count


def forward_completeness_report(
    config_path: Path,
    market_id: str,
) -> dict[str, Any]:
    config, context, spec, plan = _load_forward_context(
        config_path,
        market_id,
    )
    store = ForwardRecorderStore(forward_recorder_db_path(config))
    binding = store.ensure_binding(
        context,
        spec,
        plan,
        config.forward_recorder,
        evidence_policy=_evidence_policy_payload(config),
    )
    token_ids = [
        token
        for outcome in spec.outcomes
        for token in (outcome.yes_token_id, outcome.no_token_id)
    ]
    return {
        "market_id": market_id,
        "paper_only": True,
        **store.completeness(
            binding_sha256=binding,
            token_ids=token_ids,
            horizons_ms=(
                config.forward_recorder.quote_survival_horizons_ms
            ),
            max_sample_lag_ms=(
                config.forward_recorder.max_sample_lag_ms
            ),
        ),
    }


def forward_completeness_command(
    config_path: Path,
    market_id: str,
) -> int:
    print(
        json.dumps(
            forward_completeness_report(config_path, market_id),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def build_forward_timeline(
    config_path: Path,
    market_id: str,
    *,
    out: Path | None = None,
) -> dict[str, Any]:
    config, context, spec, plan = _load_forward_context(
        config_path,
        market_id,
    )
    store = ForwardRecorderStore(forward_recorder_db_path(config))
    binding = store.ensure_binding(
        context,
        spec,
        plan,
        config.forward_recorder,
        evidence_policy=_evidence_policy_payload(config),
    )
    report = forward_completeness_report(config_path, market_id)
    if not report["replay_ready"]:
        raise ValueError(
            "forward dataset is incomplete: "
            + ",".join(report["blockers"])
        )
    rows = store.rows_for_timeline(binding)
    outcome_by_token: dict[str, tuple[str, str]] = {}
    tokens_by_outcome: dict[str, tuple[str, str]] = {}
    for outcome in spec.outcomes:
        outcome_by_token[outcome.yes_token_id] = (outcome.name, "yes")
        outcome_by_token[outcome.no_token_id] = (outcome.name, "no")
        tokens_by_outcome[outcome.name] = (
            outcome.yes_token_id,
            outcome.no_token_id,
        )

    latest: dict[str, dict[str, Any]] = {}
    events: list[tuple[datetime, int, dict[str, Any]]] = []
    last_book_payload: dict[str, str] = {}
    for row in rows["books"]:
        token_id = str(row["token_id"])
        if token_id not in outcome_by_token:
            continue
        snapshot = json.loads(str(row["snapshot_json"]))
        latest[token_id] = snapshot
        outcome_name, _side = outcome_by_token[token_id]
        yes_token, no_token = tokens_by_outcome[outcome_name]
        if yes_token not in latest or no_token not in latest:
            continue
        yes = _timeline_book(latest[yes_token])
        no = _timeline_book(latest[no_token])
        _validate_timeline_book(yes, outcome_name, "yes")
        _validate_timeline_book(no, outcome_name, "no")
        event = {
            "type": "BOOK",
            "at": str(row["received_at"]),
            "outcome": outcome_name,
            "yes": yes,
            "no": no,
        }
        fingerprint = sha256_json(
            {"outcome": outcome_name, "yes": yes, "no": no}
        )
        if last_book_payload.get(outcome_name) == fingerprint:
            continue
        last_book_payload[outcome_name] = fingerprint
        events.append((_parse_at(event["at"]), 0, event))

    for row in rows["trades"]:
        token_id = str(row["token_id"])
        if token_id not in outcome_by_token:
            continue
        outcome_name, outcome_side = outcome_by_token[token_id]
        event = {
            "type": "TRADE",
            "at": str(row["received_at"]),
            "outcome": outcome_name,
            "outcome_side": outcome_side.upper(),
            "token_id": token_id,
            "price": float(row["price"]),
            "size": float(row["size"]),
            "reported_side": str(row["side"]),
            "fee_rate_bps": float(row["fee_rate_bps"]),
            "transaction_hash": str(row["transaction_hash"]),
        }
        events.append((_parse_at(event["at"]), 1, event))

    for row in rows["articles"]:
        article = json.loads(str(row["article_json"]))
        article.setdefault("source_kind", "article")
        event = {
            "type": "ARTICLE",
            "at": str(row["first_observed_at"]),
            "article": article,
        }
        events.append((_parse_at(event["at"]), 2, event))

    seen_resolutions: set[tuple[str, bool]] = set()
    for row in rows["resolutions"]:
        key = (str(row["outcome_name"]), bool(row["resolved_yes"]))
        if key in seen_resolutions:
            continue
        seen_resolutions.add(key)
        event = {
            "type": "RESOLUTION",
            "at": str(row["observed_at"]),
            "outcome": key[0],
            "resolved_yes": key[1],
        }
        events.append((_parse_at(event["at"]), 3, event))

    events.sort(key=lambda item: (item[0], item[1], canonical_json(item[2])))
    lines = [canonical_json(item[2]) for item in events]
    if not lines:
        raise ValueError("forward timeline has no events")
    timeline_content = "\n".join(lines) + "\n"
    timeline_sha256 = hashlib.sha256(
        timeline_content.encode("utf-8")
    ).hexdigest()
    output_path = out or (
        config.data_dir
        / "forward_timelines"
        / market_dir_slug(market_id)
        / f"{timeline_sha256}.jsonl"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        if output_path.read_text(encoding="utf-8") != timeline_content:
            raise ValueError(
                f"refusing to overwrite different forward timeline: {output_path}"
            )
    else:
        _atomic_text_write(output_path, timeline_content)
    manifest_path = output_path.with_suffix(
        output_path.suffix + ".manifest.json"
    )
    if manifest_path.exists():
        try:
            existing_manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("corrupt forward timeline manifest") from exc
        if (
            not isinstance(existing_manifest, dict)
            or existing_manifest.get("timeline_sha256")
            != timeline_sha256
            or existing_manifest.get("binding_sha256") != binding
            or existing_manifest.get("market_id") != market_id
        ):
            raise ValueError("forward timeline manifest binding conflict")
        return existing_manifest
    manifest = {
        "schema_version": FORWARD_TIMELINE_SCHEMA_VERSION,
        "dataset_role": "forward",
        "paper_only": True,
        "market_id": market_id,
        "binding_sha256": binding,
        "rule_spec_sha256": spec.spec_sha256,
        "timeline_sha256": timeline_sha256,
        "event_count": len(lines),
        "book_events": sum(1 for line in events if line[2]["type"] == "BOOK"),
        "trade_events": sum(
            1 for line in events if line[2]["type"] == "TRADE"
        ),
        "article_events": sum(
            1 for line in events if line[2]["type"] == "ARTICLE"
        ),
        "resolution_events": sum(
            1 for line in events if line[2]["type"] == "RESOLUTION"
        ),
        "timeline_path": str(output_path),
        "completeness": report,
        "generated_at": _now(),
    }
    _atomic_json_write(manifest_path, manifest)
    return manifest


def build_forward_timeline_command(
    config_path: Path,
    market_id: str,
    *,
    out: Path | None = None,
) -> int:
    print(
        json.dumps(
            build_forward_timeline(
                config_path,
                market_id,
                out=out,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _load_forward_context(
    config_path: Path,
    market_id: str,
) -> tuple[DiscoveryConfig, MarketContext, RuleSpec, SourcePlan]:
    config = load_discovery_config(config_path)
    if not config.forward_recorder.enabled:
        raise ValueError("forward_recorder.enabled is false")
    discovery = DiscoveryStore(config.data_dir)
    context = discovery.load_context(market_id)
    if context is None:
        raise ValueError(f"unknown market_id {market_id!r}")
    plan = discovery.load_source_plan(market_id)
    if plan is None:
        raise ValueError(f"market {market_id!r} has no source plan")
    spec = RuleStore(rule_store_db_path(config)).load_spec(
        market_id,
        context.rule_text_sha256,
    )
    if spec is None:
        raise ValueError(f"market {market_id!r} has no current RuleSpec")
    return config, context, spec, plan


def _context_binding_payload(context: MarketContext) -> dict[str, Any]:
    """Exclude discovery scores and timestamps that do not change a contract."""
    return {
        "market_id": context.market_id,
        "kind": context.kind,
        "event_slug": context.event_slug,
        "event_title": context.event_title,
        "question": context.question,
        "deadline_iso": context.deadline_iso,
        "rule_text_sha256": context.rule_text_sha256,
        "rule_version": context.rule_version,
        "resolution_source": context.resolution_source,
        "neg_risk": context.neg_risk,
        "outcomes": [
            {
                "name": outcome.name,
                "label": outcome.label,
                "market_slug": outcome.market_slug,
                "condition_id": outcome.condition_id,
                "yes_token_id": outcome.yes_token_id,
                "no_token_id": outcome.no_token_id,
                "tick_size": outcome.tick_size,
                "neg_risk": outcome.neg_risk,
                "fee_schedule": (
                    _fee_policy_binding(outcome.fee_schedule.as_dict())
                    if outcome.fee_schedule is not None
                    else None
                ),
                "fee_schedule_error": outcome.fee_schedule_error,
            }
            for outcome in context.outcomes
        ],
    }


def _source_plan_binding_payload(
    source_plan: SourcePlan,
) -> dict[str, Any]:
    payload = source_plan.as_dict()
    payload.pop("created_at", None)
    return payload


def _recorder_policy_payload(
    recorder_config: ForwardRecorderConfig,
) -> dict[str, Any]:
    payload = asdict(recorder_config)
    for operational_field in (
        "db_path",
        "health_startup_grace_seconds",
        "health_stale_after_seconds",
        "health_growth_window_seconds",
        "health_alert_cooldown_seconds",
    ):
        payload.pop(operational_field, None)
    return payload


def _evidence_policy_payload(config: DiscoveryConfig) -> dict[str, Any]:
    from .evidence import (
        EVIDENCE_EXTRACTOR_VERSION,
        EVIDENCE_PROMPT_VERSION,
    )
    from .fast_evidence import OFFICIAL_CLAIM_ADAPTER_VERSION

    return {
        "extractor_version": EVIDENCE_EXTRACTOR_VERSION,
        "prompt_version": EVIDENCE_PROMPT_VERSION,
        "classifier": {
            "provider": config.classifier.provider,
            "model": config.classifier.model,
            "screen_model": config.classifier.screen_model,
            "temperature": config.classifier.temperature,
        },
        "model_passes": config.rule_runner.extraction_passes,
        "deterministic": {
            "enabled": (
                config.rule_runner.deterministic_evidence_enabled
            ),
            "configured_policy_version": (
                config.rule_runner.deterministic_evidence_policy_version
            ),
            "implementation_version": OFFICIAL_CLAIM_ADAPTER_VERSION,
            "families": sorted(
                {
                    item.strip().upper()
                    for item in (
                        config.rule_runner.deterministic_evidence_families
                    )
                }
            ),
        },
        "direct_source_discovery": {
            "enabled": config.central_feed.direct_sources_enabled,
            "adapters": [
                "html_listing_v1",
                "json_discovery_v1",
                "rss_atom_v1",
                "sitemap_urlset_v1",
            ],
        },
    }


def _fee_policy_binding(schedule: dict[str, Any]) -> dict[str, Any]:
    """Freshness observations change often; the fee curve/policy does not."""
    return {
        key: value
        for key, value in schedule.items()
        if key != "observed_at"
    }


def _timeline_book(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "bids": [
            [float(level[0]), float(level[1])]
            for level in snapshot.get("bids", [])
            if isinstance(level, (list, tuple)) and len(level) >= 2
        ],
        "asks": [
            [float(level[0]), float(level[1])]
            for level in snapshot.get("asks", [])
            if isinstance(level, (list, tuple)) and len(level) >= 2
        ],
    }


def _validate_timeline_book(
    book: dict[str, Any],
    outcome_name: str,
    side: str,
) -> None:
    bids = book["bids"]
    asks = book["asks"]
    for price, size in [*bids, *asks]:
        if not 0 < price < 1 or size <= 0:
            raise ValueError(
                f"invalid recorded {side} book for {outcome_name}"
            )
    best_bid = max((item[0] for item in bids), default=None)
    best_ask = min((item[0] for item in asks), default=None)
    if (
        best_bid is not None
        and best_ask is not None
        and best_bid >= best_ask
    ):
        raise ValueError(
            f"crossed recorded {side} book for {outcome_name}"
        )


def _depth_usd(raw: Any, *, cap: float | None) -> float:
    if cap is None or not isinstance(raw, list):
        return 0.0
    total = 0.0
    for level in raw:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price = _float_or_none(level[0])
        size = _float_or_none(level[1])
        if price is not None and size is not None and price <= cap:
            total += price * max(0.0, size)
    return round(total, 8)


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _latency_ms(start: str, end: str) -> float | None:
    if not start:
        return None
    return round(
        (_parse_at(end) - _parse_at(start)).total_seconds() * 1000.0,
        3,
    )


def _latency_summary(values: Iterable[float]) -> dict[str, Any]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {
            "samples": 0,
            "p50": None,
            "p95": None,
            "max": None,
        }

    def percentile(fraction: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(len(ordered) - 1, lower + 1)
        weight = position - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    return {
        "samples": len(ordered),
        "p50": round(percentile(0.50), 3),
        "p95": round(percentile(0.95), 3),
        "max": round(ordered[-1], 3),
    }


def _parse_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("forward timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp_age_seconds(value: str) -> float | None:
    if not value:
        return None
    try:
        return max(
            0.0,
            (datetime.now(timezone.utc) - _parse_at(value)).total_seconds(),
        )
    except (TypeError, ValueError):
        return None


def _iso(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    try:
        return _parse_at(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"{name} must be a timezone-aware ISO timestamp") from exc


def _optional_iso(value: Any) -> str:
    text = str(value or "").strip()
    return _iso(text, "timestamp") if text else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_sqlite_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }
    if column not in existing:
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def _atomic_text_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "FORWARD_RECORDER_SCHEMA_VERSION",
    "FORWARD_TIMELINE_SCHEMA_VERSION",
    "ForwardBookService",
    "ForwardRecorderStore",
    "RecordedClobQuoteAdapter",
    "build_forward_timeline",
    "build_forward_timeline_command",
    "forward_completeness_command",
    "forward_completeness_report",
    "record_forward_resolutions",
]
