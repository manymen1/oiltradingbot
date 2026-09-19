from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml


@dataclass(frozen=True)
class OilConfig:
    path: Path
    root: Path
    raw: dict

    @property
    def sources(self) -> list[dict]:
        return self.raw["sources"]

    @property
    def extraction(self) -> dict:
        return self.raw["extraction"]

    def db(self, name: str) -> Path:
        if name not in {"news", "analysis", "runtime"}:
            raise ValueError("unknown journal")
        return self.root / f"{name}.sqlite3"


def load_config(path: str | Path) -> OilConfig:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text())
    if raw.get("mode") != "observe" or raw.get("broker_execution") != "disabled":
        raise ValueError("oil supports observation only; broker execution must be disabled")
    if raw.get("market", {}).get("provider") != "fixture":
        raise ValueError("live provider not qualified; only fixture adapter is implemented")
    extraction = raw["extraction"]
    if extraction["provider"] != "codex_cli" or extraction.get("fallback") is not None:
        raise ValueError("Codex CLI only; no automatic fallback")
    for key, maximum in (("workers", 2), ("daily_attempts", 100), ("timeout_seconds", 180)):
        if type(extraction[key]) is not int or not 1 <= extraction[key] <= maximum:
            raise ValueError(f"invalid bounded extraction setting: {key}")
    if not 0 < extraction["deadline_seconds"] <= extraction["timeout_seconds"]:
        raise ValueError("invalid analysis deadline")
    ids = set()
    for source in raw["sources"]:
        if source["id"] in ids:
            raise ValueError("duplicate source")
        ids.add(source["id"])
        if source["poll_seconds"] < 60:
            raise ValueError("minimum pilot polling interval is 60 seconds")
        if urlparse(source["url"]).scheme != "https":
            raise ValueError("HTTPS source required")
        if source["adapter"] not in {"rss", "adnoc", "fujairah", "ukmto"}:
            raise ValueError("unknown source adapter")
        for key in ("owner", "role", "allowed_hosts", "rights", "revision_policy", "timestamp_precision"):
            if not source.get(key):
                raise ValueError(f"missing source registration: {key}")
        if source["rights"].get("model_processing") not in {"permitted", "pending", "prohibited"}:
            raise ValueError("explicit model processing rights required")
    root = Path(raw["storage"]["root"])
    if not root.is_absolute():
        root = path.parent / root
    return OilConfig(path, root.resolve(), raw)
