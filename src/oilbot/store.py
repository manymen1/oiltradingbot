from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .clock import instant, stamp, utc_now
from .schema import canonical, digest, to_dict


class Journal:
    """Append-only records with transactional cursors. SQLite serializes writers.

    Each component has its own database; BEGIN IMMEDIATE also protects budget
    reservations from simultaneous workers. Read connections never take ownership.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
                raise ValueError("unsupported oil journal schema version")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS record_kind ON records(kind,seq);
                CREATE TABLE IF NOT EXISTS cursors (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS budget (day TEXT PRIMARY KEY, attempts INTEGER NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_update BEFORE UPDATE ON records
                    BEGIN SELECT RAISE(ABORT, 'records are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_delete BEFORE DELETE ON records
                    BEGIN SELECT RAISE(ABORT, 'records are immutable'); END;
                PRAGMA user_version=1;
            """)

    def _open(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    @contextmanager
    def connect(self):
        db = self._open()
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        db = self._open()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def append(self, kind: str, payload: dict, *, available_at: str | None = None,
               record_id: str | None = None, db=None) -> str:
        at = instant(available_at or utc_now()).isoformat(timespec="microseconds")
        rid = record_id or str(uuid.uuid4())
        if db is None:
            with self.transaction() as connection:
                return self.append(kind, payload, available_at=at, record_id=rid, db=connection)
        body = canonical(payload)
        old = db.execute("SELECT kind,payload,available_at FROM records WHERE id=?", (rid,)).fetchone()
        if old:
            if old["kind"] != kind or old["payload"] != body or old["available_at"] != at:
                raise ValueError("immutable identity collision")
            return rid
        db.execute("INSERT INTO records(id,kind,available_at,payload,recorded_at) VALUES(?,?,?,?,?)",
                   (rid, kind, at, body, utc_now()))
        return rid

    def records(self, kind: str | None = None, *, through: str | None = None) -> list[dict]:
        conditions, args = [], []
        if kind:
            conditions.append("kind=?")
            args.append(kind)
        if through:
            conditions.append("available_at<=?")
            args.append(instant(through).isoformat(timespec="microseconds"))
        sql = "SELECT * FROM records" + (" WHERE " + " AND ".join(conditions) if conditions else "") + " ORDER BY seq"
        with self.connect() as db:
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in db.execute(sql, args)]

    def get(self, rid: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def cursor(self, key: str, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM cursors WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def set_cursor(db, key: str, value) -> None:
        db.execute("INSERT INTO cursors VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, canonical(value)))

    def reserve_attempt(self, limit: int, at: str | None = None) -> bool:
        day = instant(at or utc_now()).date().isoformat()
        with self.transaction() as db:
            row = db.execute("SELECT attempts FROM budget WHERE day=?", (day,)).fetchone()
            count = row[0] if row else 0
            if count >= limit:
                return False
            db.execute("INSERT INTO budget VALUES(?,?) ON CONFLICT(day) DO UPDATE SET attempts=excluded.attempts",
                       (day, count + 1))
        return True

    def budget(self) -> dict:
        with self.connect() as db:
            return {row[0]: row[1] for row in db.execute("SELECT day,attempts FROM budget ORDER BY day")}

    def capture(self, source: dict, response: dict) -> str:
        """Durable raw bytes before any parser sees them. Repeated receipts persist."""
        body = response["body"]
        payload = {key: value for key, value in response.items() if key != "body"}
        payload.update(source_id=source["id"], source_role=source["role"],
                       body_b64=base64.b64encode(body).decode(),
                       sha256=hashlib.sha256(body).hexdigest(), source_policy=digest(source))
        rid = self.append("observation", payload, available_at=response["received"]["utc"])
        committed = stamp()
        self.append("commit_receipt", {"input_revision_ids": [rid], "commit_observed": committed,
                                     "receive_to_commit_ms": (committed["monotonic_ns"] - response["received"]["monotonic_ns"]) / 1e6})
        return rid

    def accept_items(self, source: dict, observation_id: str, items: list, cursor: dict) -> list[str]:
        observation = self.get(observation_id)
        if observation is None:
            raise ValueError("missing raw observation")
        at = utc_now()
        output = []
        with self.transaction() as db:
            initial_snapshot = db.execute("SELECT 1 FROM cursors WHERE key=?", ("source:" + source["id"],)).fetchone() is None
            for item in items:
                story = digest([source["id"], item.native_id])
                content = digest(to_dict(item))
                previous = db.execute("SELECT value FROM cursors WHERE key=?", ("story:" + story,)).fetchone()
                previous = json.loads(previous[0]) if previous else None
                if previous and previous["content_hash"] == content:
                    self.append("story_receipt", {"input_revision_ids": [observation_id, previous["revision_id"]],
                                                  "source_id": source["id"]}, db=db)
                    continue
                rid = digest([story, content, previous["revision_id"] if previous else None])
                payload = {**to_dict(item), "source_id": source["id"], "source_role": source["role"],
                           "story_id": story, "content_hash": content,
                           "revision": previous["revision"] + 1 if previous else 1,
                           "supersedes_id": previous["revision_id"] if previous else None,
                           "input_revision_ids": [observation_id], "transform": "source-v1",
                           "observed_at": observation["available_at"],
                           "initial_snapshot": initial_snapshot,
                           "origin_status": "attributed" if item.origin else "unknown",
                           "model_processing": source["rights"]["model_processing"]}
                self.append("story_revision", payload, available_at=at, record_id=rid, db=db)
                self.set_cursor(db, "story:" + story, {"content_hash": content, "revision_id": rid, "revision": payload["revision"]})
                output.append(rid)
            self.append("parse_receipt", {"input_revision_ids": [observation_id], "revision_ids": output,
                                          "transform": "source-v1"}, db=db)
            self.set_cursor(db, "source:" + source["id"], cursor)
        return output

    def backup(self, destination: Path) -> str:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ValueError("backup destination already exists")
        with self.connect() as src, sqlite3.connect(destination) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("backup integrity check failed")
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
        return hashlib.sha256(destination.read_bytes()).hexdigest()


@contextmanager
def component_lock(root: Path, name: str):
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{name}.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"{name} already has an active writer") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
