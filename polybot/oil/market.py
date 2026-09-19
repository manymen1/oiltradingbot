from __future__ import annotations

import hashlib
import fcntl
import json
import os
import threading
import time
import uuid
from pathlib import Path

from .clock import instant, utc_now
from .schema import InstrumentDefinition, MarketEvent, canonical, digest, to_dict


def atomic_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class FixtureAdapter:
    def __init__(self, path: Path):
        self.data = json.loads(Path(path).read_text())

    def definitions(self):
        for row in self.data["definitions"]:
            definition = InstrumentDefinition(**row)
            definition.validate()
            if definition.data_mode != "fixture":
                raise ValueError("fixture adapter cannot declare real-time data")
            yield definition

    def events(self):
        for row in self.data["events"]:
            event = MarketEvent(**row)
            if event.data_mode != "fixture":
                raise ValueError("fixture adapter cannot declare real-time data")
            yield event


class QuoteArchive:
    """Single-writer, checksummed segments; incomplete sessions retain explicit gaps."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.owner = (self.root / "writer.lock").open("a+")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.owner.close()
            raise RuntimeError("quote archive already has a writer") from exc
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.failure: Exception | None = None
        self.recover()
        self.path = self.root / f"{time.time_ns()}-{uuid.uuid4().hex}.open"
        self.stream = self.path.open("wb", buffering=0)
        self.count = 0
        self.dirty = False
        self.thread = threading.Thread(target=self._flusher, daemon=True)
        self.thread.start()

    def recover(self):
        for path in sorted(self.root.glob("*.open")):
            body = path.read_bytes()
            valid = []
            for line in body.splitlines(keepends=True):
                try:
                    if not line.endswith(b"\n"):
                        break
                    row = json.loads(line)
                    if row["checksum"] != digest(row["record"]):
                        break
                    valid.append(line)
                except (ValueError, KeyError):
                    break
            recovered = path.with_suffix(".jsonl")
            with recovered.open("wb") as stream:
                stream.write(b"".join(valid))
                stream.flush()
                os.fsync(stream.fileno())
            atomic_json(recovered.with_suffix(".manifest.json"), {
                "file": recovered.name, "sha256": hashlib.sha256(recovered.read_bytes()).hexdigest(),
                "records": len(valid), "recovered": True,
                "gap": {"reason": "UNCLEAN_RESTART", "detected_at": utc_now(),
                        "discarded_bytes": len(body) - sum(map(len, valid)),
                        "unobserved_tail": True},
            })
            path.unlink()

    def append(self, kind: str, payload: dict):
        with self.lock:
            if self.failure:
                raise RuntimeError("quote writer failed") from self.failure
            record = {"kind": kind, "payload": payload, "id": digest([kind, payload])}
            self.stream.write((canonical({"record": record, "checksum": digest(record)}) + "\n").encode())
            self.count += 1
            self.dirty = True

    def flush(self):
        with self.lock:
            if self.dirty:
                os.fsync(self.stream.fileno())
                self.dirty = False

    def _flusher(self):
        while not self.stop.wait(1):
            try:
                self.flush()
            except Exception as exc:
                self.failure = exc
                return

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self.flush()
        self.stream.close()
        if self.failure:
            raise RuntimeError("quote flush failed; segment left for recovery") from self.failure
        closed = self.path.with_suffix(".jsonl")
        os.replace(self.path, closed)
        atomic_json(closed.with_suffix(".manifest.json"), {
            "file": closed.name, "sha256": hashlib.sha256(closed.read_bytes()).hexdigest(),
            "records": self.count, "recovered": False, "gap": None,
        })
        self.owner.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.stop.set()
            self.thread.join(timeout=2)
            self.stream.close()  # Leave .open to expose the interrupted session.
            self.owner.close()
        else:
            self.close()


def read_archive(root: Path) -> tuple[list[dict], list[dict]]:
    records, gaps, seen = [], [], set()
    for path in sorted(Path(root).glob("*.jsonl")):
        manifest_path = path.with_suffix(".manifest.json")
        if not manifest_path.exists():
            gaps.append({"reason": "MISSING_MANIFEST", "file": path.name})
            continue
        manifest = json.loads(manifest_path.read_text())
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
            raise ValueError("market segment checksum mismatch")
        if manifest.get("gap"):
            gaps.append(manifest["gap"])
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if digest(row["record"]) != row["checksum"]:
                raise ValueError("market event checksum mismatch")
            if row["record"]["id"] not in seen:
                seen.add(row["record"]["id"])
                records.append(row["record"])
    for path in Path(root).glob("*.open"):
        gaps.append({"reason": "OPEN_SEGMENT", "file": path.name})
    return records, gaps


def record_adapter(adapter, root: Path) -> dict:
    definitions = {row.instrument_id: row for row in adapter.definitions()}
    sequences = {}
    count = 0
    with QuoteArchive(root) as archive:
        for definition in definitions.values():
            archive.append("instrument", to_dict(definition))
        for event in adapter.events():
            if event.instrument_id not in definitions:
                raise ValueError("unqualified instrument")
            event.validate(definitions[event.instrument_id])
            prior = sequences.get(event.instrument_id)
            if prior is not None and event.sequence != prior + 1:
                archive.append("gap", {"instrument_id": event.instrument_id, "reason": "SEQUENCE_GAP",
                                       "prior": prior, "next": event.sequence, "available_at": event.available_at})
            sequences[event.instrument_id] = event.sequence
            archive.append("market", to_dict(event))
            count += 1
    return {"events": count, "instruments": len(definitions), "economic_evaluation": "unavailable"}


def qualify(records: list[dict], gaps: list[dict]) -> dict:
    definitions = [InstrumentDefinition(**r["payload"]) for r in records if r["kind"] == "instrument"]
    quotes = [r["payload"] for r in records if r["kind"] == "market"]
    products = {d.product for d in definitions}
    matching = bool({d.month for d in definitions if d.product == "CL"} & {d.month for d in definitions if d.product == "MCL"})
    return {
        "provider_qualified": False, "economic_evaluation": "unavailable",
        "checks": {"CL_and_MCL": products == {"CL", "MCL"}, "matching_months": matching,
                   "quotes_present": bool(quotes),
                   "all_realtime": bool(quotes) and all(q["data_mode"] == "realtime" for q in quotes),
                   "no_recorded_gaps": not gaps and not any(r["kind"] == "gap" for r in records),
                   "entitlements_verified": False, "retention_rights_verified": False,
                   "seven_day_capture_verified": False},
        "reason": "No live provider or account entitlement has been qualified. Fixture data is engineering evidence only.",
    }
