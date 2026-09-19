from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from .clock import instant, utc_now
from .market import atomic_json, read_archive
from .schema import digest
from .store import Journal


class ReplayReader:
    def __init__(self, records: list[dict]):
        self.records = sorted(records, key=lambda r: (instant(r["available_at"]), r["recorded_at"], r["id"]))
        by_id = {r["id"]: r for r in records}
        if len(by_id) != len(records):
            raise ValueError("duplicate replay identity")
        for row in records:
            for rid in row["payload"].get("input_revision_ids", []):
                if rid not in by_id:
                    raise ValueError("missing replay input: " + rid)
                if instant(by_id[rid]["available_at"]) > instant(row["available_at"]):
                    raise ValueError("future input in derived record")

    def through(self, at: str, kind: str | None = None) -> list[dict]:
        return [r for r in self.records if instant(r["available_at"]) <= instant(at) and (not kind or r["kind"] == kind)]

    def decision_inputs(self) -> list[dict]:
        by_id = {r["id"]: r for r in self.records}
        result = []
        for row in self.records:
            if row["kind"] != "decision":
                continue
            inputs, pending = {}, list(row["payload"]["input_revision_ids"])
            while pending:
                rid = pending.pop()
                if rid in inputs:
                    continue
                record = by_id[rid]
                if instant(record["available_at"]) > instant(row["available_at"]):
                    raise ValueError("future decision input")
                inputs[rid] = record
                pending.extend(record["payload"].get("input_revision_ids", []))
            result.append({"decision": row, "input_hash": digest([inputs[k] for k in sorted(inputs)])})
        return result


def export_manifest(config, destination: Path) -> Path:
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("snapshot destination already exists")
    destination.mkdir(parents=True)
    files = {}
    for component in ("analysis", "news", "runtime"):
        name = f"{component}.sqlite3"
        files[name] = Journal(config.db(component)).backup(destination / name)
    market_records, gaps = read_archive(config.root / "quotes")
    atomic_json(destination / "market.json", {"records": market_records, "gaps": gaps})
    files["market.json"] = hashlib.sha256((destination / "market.json").read_bytes()).hexdigest()
    records = []
    import sqlite3
    for name in ("news", "analysis", "runtime"):
        with sqlite3.connect(f"file:{destination / (name + '.sqlite3')}?mode=ro&immutable=1", uri=True) as db:
            db.row_factory = sqlite3.Row
            records.extend({**dict(row), "payload": json.loads(row["payload"])} for row in db.execute("SELECT * FROM records ORDER BY seq"))
    # Capturing news before analysis may miss a concurrent input. Fail rather than
    # export a superficially complete, causally invalid research snapshot.
    reader = ReplayReader(records)
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
    except subprocess.SubprocessError:
        commit, dirty = "unknown", True
    manifest = {"schema": "oil-manifest-v1", "created_at": utc_now(), "code_commit": commit, "dirty": dirty,
                "config": config.raw, "config_hash": digest(config.raw), "files": files,
                "record_count": len(records), "records_hash": digest(reader.records),
                "decision_inputs_hash": digest(reader.decision_inputs()),
                "dataset_role": "engineering_fixture" if not market_records or any(r["payload"].get("data_mode") == "fixture" for r in market_records) else "observation",
                "economic_evaluation": "unavailable"}
    atomic_json(destination / "manifest.json", manifest)
    return destination / "manifest.json"


def load_manifest(path: Path) -> tuple[dict, ReplayReader, dict]:
    path = Path(path).resolve()
    manifest = json.loads(path.read_text())
    for name, expected in manifest["files"].items():
        target = (path.parent / name).resolve()
        if target.parent != path.parent:
            raise ValueError("manifest path escapes snapshot")
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ValueError("snapshot checksum mismatch: " + name)
    # Read snapshots without initializing/migrating them or changing hash-bound bytes.
    import sqlite3
    records = []
    for name in ("news", "analysis", "runtime"):
        with sqlite3.connect(f"file:{path.parent / (name + '.sqlite3')}?mode=ro&immutable=1", uri=True) as db:
            db.row_factory = sqlite3.Row
            records.extend({**dict(row), "payload": json.loads(row["payload"])} for row in db.execute("SELECT * FROM records ORDER BY seq"))
    reader = ReplayReader(records)
    if digest(reader.records) != manifest["records_hash"] or digest(reader.decision_inputs()) != manifest["decision_inputs_hash"]:
        raise ValueError("replay reconstruction mismatch")
    return manifest, reader, json.loads((path.parent / "market.json").read_text())
