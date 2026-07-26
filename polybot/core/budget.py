from __future__ import annotations

import json
import sqlite3
import threading
import hashlib
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ClassifierConfig


class ClassifierBudgetExceeded(RuntimeError):
    """A classifier call group could not be reserved within its budget."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ClassifierBudgetStore:
    """Classifier attempt/error counters.

    Standalone bots retain their local JSON counter. Fleet-generated bots set
    ``classifier.budget_db_path`` and share a WAL SQLite counter, so the
    check-and-reserve operation is atomic across every market process.
    """

    def __init__(
        self,
        data_dir: Path,
        db_path: Path | None = None,
        *,
        priority_quotas: bool = False,
        exploitation_fraction: float = 0.70,
        exploration_fraction: float = 0.20,
        system_fraction: float = 0.10,
    ) -> None:
        self.path = data_dir / "classifier_budget.json"
        self.db_path = db_path
        self.priority_quotas = bool(priority_quotas)
        self.quota_fractions = {
            "exploitation": float(exploitation_fraction),
            "exploration": float(exploration_fraction),
            "system": float(system_fraction),
        }
        if any(
            not math.isfinite(value) or value < 0 or value > 1
            for value in self.quota_fractions.values()
        ) or abs(sum(self.quota_fractions.values()) - 1.0) > 1e-9:
            raise ValueError(
                "classifier budget quota fractions must be finite, between "
                "zero and one, and sum to one"
            )
        self._lock = threading.RLock()
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize_sqlite()

    def block_reason(self, config: ClassifierConfig) -> str | None:
        now = datetime.now(timezone.utc)
        if self.db_path is not None:
            with self._connect(read_only=True) as connection:
                return self._sqlite_block_reason(connection, config, now, attempts=1)
        with self._lock:
            return self._json_block_reason(self._read(), config, now, attempts=1)

    def reserve_attempts(
        self,
        config: ClassifierConfig,
        attempts: int = 1,
        *,
        market_id: str = "",
        purpose: str = "system",
        priority_score_sha256: str = "",
        reservation_id: str = "",
    ) -> str | None:
        """Atomically reserve all calls required for one classifier stage.

        Returning a reason instead of partially reserving is important for
        live two-pass agreement: one pass must never run when the second pass
        cannot also fit inside the same fleet-wide budget.
        """

        count = max(1, int(attempts))
        now = datetime.now(timezone.utc)
        normalized_purpose = str(purpose).strip().casefold() or "system"
        if normalized_purpose not in {
            "exploitation",
            "exploration",
            "system",
        }:
            normalized_purpose = "system"
        reservation_key = str(reservation_id).strip()
        if not reservation_key and market_id:
            reservation_key = hashlib.sha256(
                (
                    f"{market_id}:{normalized_purpose}:{count}:"
                    f"{priority_score_sha256}:{now.isoformat()}"
                ).encode("utf-8")
            ).hexdigest()
        if self.db_path is not None:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if reservation_key:
                    existing = connection.execute(
                        """
                        SELECT admitted FROM admissions
                        WHERE reservation_id=? AND admitted=1
                        ORDER BY id LIMIT 1
                        """,
                        (reservation_key,),
                    ).fetchone()
                    if existing is not None:
                        return None
                reason = self._sqlite_block_reason(connection, config, now, attempts=count)
                if reason is None and self.priority_quotas:
                    reason = self._sqlite_quota_reason(
                        connection,
                        config,
                        now,
                        market_id=market_id,
                        purpose=normalized_purpose,
                        attempts=count,
                    )
                if reason is not None:
                    self._sqlite_record_admission(
                        connection,
                        now=now,
                        reservation_id=reservation_key,
                        market_id=market_id,
                        purpose=normalized_purpose,
                        requested=count,
                        admitted=0,
                        priority_score_sha256=priority_score_sha256,
                        denial_reason=reason,
                    )
                    return reason
                self._sqlite_increment(connection, "attempts_by_hour", self._hour_key(now), count)
                self._sqlite_increment(connection, "attempts_by_day", self._day_key(now), count)
                if self.priority_quotas:
                    self._sqlite_increment(
                        connection,
                        f"quota_{normalized_purpose}_by_hour",
                        self._hour_key(now),
                        count,
                    )
                    self._sqlite_increment(
                        connection,
                        f"quota_{normalized_purpose}_by_day",
                        self._day_key(now),
                        count,
                    )
                self._sqlite_record_admission(
                    connection,
                    now=now,
                    reservation_id=reservation_key,
                    market_id=market_id,
                    purpose=normalized_purpose,
                    requested=count,
                    admitted=count,
                    priority_score_sha256=priority_score_sha256,
                    denial_reason="",
                )
                return None
        with self._lock:
            raw = self._read()
            reason = self._json_block_reason(raw, config, now, attempts=count)
            if reason is not None:
                return reason
            self._json_increment(raw, "attempts_by_hour", self._hour_key(now), count)
            self._json_increment(raw, "attempts_by_day", self._day_key(now), count)
            self._write(raw)
            return None

    def record_attempt(self) -> None:
        """Compatibility helper for older callers; new code reserves a stage."""

        now = datetime.now(timezone.utc)
        if self.db_path is not None:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._sqlite_increment(connection, "attempts_by_hour", self._hour_key(now), 1)
                self._sqlite_increment(connection, "attempts_by_day", self._day_key(now), 1)
            return
        with self._lock:
            raw = self._read()
            self._json_increment(raw, "attempts_by_hour", self._hour_key(now), 1)
            self._json_increment(raw, "attempts_by_day", self._day_key(now), 1)
            self._write(raw)

    def record_error(self) -> None:
        now = datetime.now(timezone.utc)
        if self.db_path is not None:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._sqlite_increment(connection, "errors_by_hour", self._hour_key(now), 1)
            return
        with self._lock:
            raw = self._read()
            self._json_increment(raw, "errors_by_hour", self._hour_key(now), 1)
            self._write(raw)

    def mark_notified_once(self, reason: str, window: str) -> bool:
        now = datetime.now(timezone.utc)
        key = self._hour_key(now) if window == "hour" else self._day_key(now)
        if self.db_path is not None:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO notifications(reason, window_key, notified_at)
                    VALUES(?, ?, ?)
                    """,
                    (reason, key, now.isoformat()),
                )
                return cursor.rowcount > 0
        with self._lock:
            raw = self._read()
            notified = raw.setdefault("notified", {})
            reason_notified = notified.setdefault(reason, [])
            if key in reason_notified:
                return False
            reason_notified.append(key)
            self._write(raw)
            return True

    def status(self, config: ClassifierConfig) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        hour_key = self._hour_key(now)
        day_key = self._day_key(now)
        if self.db_path is not None:
            with self._connect(read_only=True) as connection:
                attempts_hour = self._sqlite_value(connection, "attempts_by_hour", hour_key)
                attempts_day = self._sqlite_value(connection, "attempts_by_day", day_key)
                errors_hour = self._sqlite_value(connection, "errors_by_hour", hour_key)
        else:
            with self._lock:
                raw = self._read()
                attempts_hour = int(raw.get("attempts_by_hour", {}).get(hour_key, 0))
                attempts_day = int(raw.get("attempts_by_day", {}).get(day_key, 0))
                errors_hour = int(raw.get("errors_by_hour", {}).get(hour_key, 0))
        result = {
            "backend": "sqlite" if self.db_path is not None else "json",
            "path": str(self.db_path or self.path),
            "hour_key": hour_key,
            "day_key": day_key,
            "attempts_this_hour": attempts_hour,
            "attempts_today": attempts_day,
            "errors_this_hour": errors_hour,
            "max_escalations_per_hour": config.max_escalations_per_hour,
            "max_escalations_per_day": config.max_escalations_per_day,
            "max_classifier_errors_per_hour": config.max_classifier_errors_per_hour,
            "remaining_this_hour": _remaining(config.max_escalations_per_hour, attempts_hour),
            "remaining_today": _remaining(config.max_escalations_per_day, attempts_day),
            "block_reason": self.block_reason(config),
        }
        if self.db_path is not None and self.priority_quotas:
            with self._connect(read_only=True) as connection:
                result["priority_quotas"] = {
                    purpose: {
                        "fraction": self.quota_fractions[purpose],
                        "hour_limit": _quota_limit(
                            config.max_escalations_per_hour,
                            purpose,
                            self.quota_fractions,
                        ),
                        "hour_used": self._sqlite_value(
                            connection,
                            f"quota_{purpose}_by_hour",
                            hour_key,
                        ),
                        "day_limit": _quota_limit(
                            config.max_escalations_per_day,
                            purpose,
                            self.quota_fractions,
                        ),
                        "day_used": self._sqlite_value(
                            connection,
                            f"quota_{purpose}_by_day",
                            day_key,
                        ),
                    }
                    for purpose in (
                        "exploitation",
                        "exploration",
                        "system",
                    )
                }
        return result

    def admissions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if self.db_path is None:
            return []
        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT reservation_id, market_id, purpose, hour_key, day_key,
                       requested_calls, admitted_calls, admitted,
                       priority_score_sha256, denial_reason, created_at
                FROM admissions ORDER BY id DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def _initialize_sqlite(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS counters (
                    bucket TEXT NOT NULL,
                    window_key TEXT NOT NULL,
                    value INTEGER NOT NULL,
                    PRIMARY KEY(bucket, window_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS admissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reservation_id TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    hour_key TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    requested_calls INTEGER NOT NULL,
                    admitted_calls INTEGER NOT NULL,
                    admitted INTEGER NOT NULL,
                    priority_score_sha256 TEXT NOT NULL,
                    denial_reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS admitted_reservation_identity
                ON admissions(reservation_id)
                WHERE admitted=1 AND reservation_id != ''
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS admissions_window_lookup
                ON admissions(hour_key, day_key, purpose, admitted)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS allocation_windows (
                    hour_key TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    plan_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(hour_key, purpose)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS market_allocations (
                    hour_key TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    call_limit INTEGER NOT NULL,
                    rank INTEGER NOT NULL,
                    priority_score_sha256 TEXT NOT NULL,
                    PRIMARY KEY(hour_key, purpose, market_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    reason TEXT NOT NULL,
                    window_key TEXT NOT NULL,
                    notified_at TEXT NOT NULL,
                    PRIMARY KEY(reason, window_key)
                )
                """
            )

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        assert self.db_path is not None
        if read_only:
            connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5.0)
        else:
            connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _sqlite_block_reason(
        self,
        connection: sqlite3.Connection,
        config: ClassifierConfig,
        now: datetime,
        *,
        attempts: int,
    ) -> str | None:
        errors = self._sqlite_value(connection, "errors_by_hour", self._hour_key(now))
        hour_attempts = self._sqlite_value(connection, "attempts_by_hour", self._hour_key(now))
        day_attempts = self._sqlite_value(connection, "attempts_by_day", self._day_key(now))
        return _reason_for_counts(config, errors, hour_attempts, day_attempts, attempts)

    def _sqlite_quota_reason(
        self,
        connection: sqlite3.Connection,
        config: ClassifierConfig,
        now: datetime,
        *,
        market_id: str,
        purpose: str,
        attempts: int,
    ) -> str | None:
        if market_id and purpose != "system":
            plan_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM allocation_windows
                    WHERE hour_key=? AND purpose=?
                    """,
                    (self._hour_key(now), purpose),
                ).fetchone()[0]
            )
            if plan_count:
                allocation = connection.execute(
                    """
                    SELECT call_limit FROM market_allocations
                    WHERE hour_key=? AND purpose=? AND market_id=?
                    """,
                    (self._hour_key(now), purpose, market_id),
                ).fetchone()
                if allocation is None:
                    return f"classifier_market_not_allocated_{purpose}"
                admitted = int(
                    connection.execute(
                        """
                        SELECT COALESCE(SUM(admitted_calls), 0)
                        FROM admissions
                        WHERE hour_key=? AND purpose=? AND market_id=?
                          AND admitted=1
                        """,
                        (self._hour_key(now), purpose, market_id),
                    ).fetchone()[0]
                )
                if admitted + attempts > int(allocation["call_limit"]):
                    return f"classifier_market_{purpose}_allocation_exhausted"
        hour_used = self._sqlite_value(
            connection,
            f"quota_{purpose}_by_hour",
            self._hour_key(now),
        )
        day_used = self._sqlite_value(
            connection,
            f"quota_{purpose}_by_day",
            self._day_key(now),
        )
        hour_limit = _quota_limit(
            config.max_escalations_per_hour,
            purpose,
            self.quota_fractions,
        )
        day_limit = _quota_limit(
            config.max_escalations_per_day,
            purpose,
            self.quota_fractions,
        )
        if hour_limit is not None and hour_used + attempts > hour_limit:
            return f"classifier_{purpose}_quota_exhausted_hourly"
        if day_limit is not None and day_used + attempts > day_limit:
            return f"classifier_{purpose}_quota_exhausted_daily"
        return None

    def configure_priority_allocations(
        self,
        config: ClassifierConfig,
        *,
        exploitation: list[tuple[str, str]],
        exploration: list[tuple[str, str]],
        calls_per_group: int = 2,
    ) -> dict[str, Any]:
        """Install an immutable deterministic allocation plan for this hour.

        Each input tuple is ``(market_id, priority_score_sha256)`` in priority
        order. Whole two-pass groups are distributed round-robin; lower-ranked
        markets receive no token once the bucket is exhausted. A changed plan
        waits for the next hour instead of reassigning calls already promised
        to running processes.
        """

        if self.db_path is None:
            return {"configured": False, "reason": "sqlite_required"}
        group = max(1, int(calls_per_group))
        now = datetime.now(timezone.utc)
        hour_key = self._hour_key(now)
        result: dict[str, Any] = {"configured": True, "hour_key": hour_key}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for purpose, ranked in (
                ("exploitation", exploitation),
                ("exploration", exploration),
            ):
                unique: list[tuple[str, str]] = []
                seen: set[str] = set()
                for market_id, score_hash in ranked:
                    market = str(market_id)
                    if not market or market in seen:
                        continue
                    seen.add(market)
                    unique.append((market, str(score_hash)))
                bucket = _quota_limit(
                    config.max_escalations_per_hour,
                    purpose,
                    self.quota_fractions,
                )
                allocations = _allocate_groups(
                    unique,
                    max_calls=bucket or 0,
                    calls_per_group=group,
                )
                plan_payload = {
                    "hour_key": hour_key,
                    "purpose": purpose,
                    "calls_per_group": group,
                    "ranked": unique,
                    "allocations": allocations,
                }
                plan_sha = hashlib.sha256(
                    json.dumps(
                        plan_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                existing = connection.execute(
                    """
                    SELECT plan_sha256 FROM allocation_windows
                    WHERE hour_key=? AND purpose=?
                    """,
                    (hour_key, purpose),
                ).fetchone()
                if existing is not None:
                    if str(existing["plan_sha256"]) != plan_sha:
                        result[purpose] = {
                            "status": "DEFERRED_EXISTING_WINDOW_PLAN",
                            "plan_sha256": str(existing["plan_sha256"]),
                        }
                    else:
                        result[purpose] = {
                            "status": "UNCHANGED",
                            "plan_sha256": plan_sha,
                        }
                    continue
                connection.execute(
                    """
                    INSERT INTO allocation_windows(
                        hour_key, purpose, plan_sha256, created_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (hour_key, purpose, plan_sha, now.isoformat()),
                )
                for rank, (market_id, score_hash) in enumerate(unique):
                    call_limit = allocations.get(market_id, 0)
                    if call_limit <= 0:
                        continue
                    connection.execute(
                        """
                        INSERT INTO market_allocations(
                            hour_key, purpose, market_id, call_limit, rank,
                            priority_score_sha256
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            hour_key,
                            purpose,
                            market_id,
                            call_limit,
                            rank,
                            score_hash,
                        ),
                    )
                result[purpose] = {
                    "status": "CONFIGURED",
                    "plan_sha256": plan_sha,
                    "markets_ranked": len(unique),
                    "markets_allocated": sum(
                        1 for value in allocations.values() if value > 0
                    ),
                    "calls_allocated": sum(allocations.values()),
                }
        return result

    def _sqlite_record_admission(
        self,
        connection: sqlite3.Connection,
        *,
        now: datetime,
        reservation_id: str,
        market_id: str,
        purpose: str,
        requested: int,
        admitted: int,
        priority_score_sha256: str,
        denial_reason: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO admissions(
                reservation_id, market_id, purpose, hour_key, day_key,
                requested_calls, admitted_calls, admitted,
                priority_score_sha256, denial_reason, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reservation_id,
                str(market_id),
                purpose,
                self._hour_key(now),
                self._day_key(now),
                requested,
                admitted,
                int(admitted > 0),
                str(priority_score_sha256),
                str(denial_reason),
                now.isoformat(),
            ),
        )

    @staticmethod
    def _sqlite_value(connection: sqlite3.Connection, bucket: str, key: str) -> int:
        row = connection.execute(
            "SELECT value FROM counters WHERE bucket=? AND window_key=?",
            (bucket, key),
        ).fetchone()
        return int(row["value"]) if row is not None else 0

    @staticmethod
    def _sqlite_increment(connection: sqlite3.Connection, bucket: str, key: str, amount: int) -> None:
        connection.execute(
            """
            INSERT INTO counters(bucket, window_key, value) VALUES(?, ?, ?)
            ON CONFLICT(bucket, window_key) DO UPDATE SET value=value + excluded.value
            """,
            (bucket, key, amount),
        )

    def _json_block_reason(
        self,
        raw: dict[str, Any],
        config: ClassifierConfig,
        now: datetime,
        *,
        attempts: int,
    ) -> str | None:
        errors = int(raw.get("errors_by_hour", {}).get(self._hour_key(now), 0))
        hour_attempts = int(raw.get("attempts_by_hour", {}).get(self._hour_key(now), 0))
        day_attempts = int(raw.get("attempts_by_day", {}).get(self._day_key(now), 0))
        return _reason_for_counts(config, errors, hour_attempts, day_attempts, attempts)

    @staticmethod
    def _json_increment(raw: dict[str, Any], bucket: str, key: str, amount: int) -> None:
        values = raw.setdefault(bucket, {})
        values[key] = int(values.get(key, 0)) + amount

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}

    def _write(self, raw: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(raw, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    @staticmethod
    def _hour_key(now: datetime) -> str:
        return now.strftime("%Y%m%d%H")

    @staticmethod
    def _day_key(now: datetime) -> str:
        return now.strftime("%Y%m%d")


def _reason_for_counts(
    config: ClassifierConfig,
    errors: int,
    hour_attempts: int,
    day_attempts: int,
    requested_attempts: int,
) -> str | None:
    if config.max_classifier_errors_per_hour > 0 and errors >= config.max_classifier_errors_per_hour:
        return "classifier_error_cap_exceeded"
    if (
        config.max_escalations_per_hour > 0
        and hour_attempts + requested_attempts > config.max_escalations_per_hour
    ):
        return "classifier_budget_exhausted_hourly"
    if (
        config.max_escalations_per_day > 0
        and day_attempts + requested_attempts > config.max_escalations_per_day
    ):
        return "classifier_budget_exhausted_daily"
    return None


def _remaining(limit: int, used: int) -> int | None:
    return max(0, limit - used) if limit > 0 else None


def _quota_limit(
    global_limit: int,
    purpose: str,
    fractions: dict[str, float],
) -> int | None:
    if global_limit <= 0:
        return None
    exploitation = int(math.floor(global_limit * fractions["exploitation"]))
    exploration = int(math.floor(global_limit * fractions["exploration"]))
    system = max(0, global_limit - exploitation - exploration)
    return {
        "exploitation": exploitation,
        "exploration": exploration,
        "system": system,
    }[purpose]


def _allocate_groups(
    ranked: list[tuple[str, str]],
    *,
    max_calls: int,
    calls_per_group: int,
) -> dict[str, int]:
    allocations = {market_id: 0 for market_id, _score in ranked}
    if not ranked or max_calls < calls_per_group:
        return allocations
    groups = max_calls // calls_per_group
    for index in range(groups):
        market_id = ranked[index % len(ranked)][0]
        allocations[market_id] += calls_per_group
    return allocations


__all__ = [
    "ClassifierBudgetExceeded",
    "ClassifierBudgetStore",
]
