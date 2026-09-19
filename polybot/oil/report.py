from __future__ import annotations

import html
import json
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from .clock import instant, utc_now
from .market import qualify
from .research import quote_at
from .schema import InstrumentDefinition, MarketEvent, digest


def distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    return {"n": len(ordered), **{f"p{p}": ordered[min(len(ordered) - 1, int((len(ordered) - 1) * p / 100))]
                                 if ordered else None for p in (50, 95, 99)}}


def markouts(reader, market: dict) -> list[dict]:
    definitions = {r["payload"]["instrument_id"]: InstrumentDefinition(**r["payload"])
                   for r in market["records"] if r["kind"] == "instrument"}
    quotes = [MarketEvent(**r["payload"]) for r in market["records"] if r["kind"] == "market"]
    by_id = {r["id"]: r for r in reader.records}
    output = []
    for decision in reader.through("9999-01-01T00:00:00Z", "decision"):
        incident = by_id[decision["payload"]["input_revision_ids"][0]]
        story = by_id[incident["payload"]["input_revision_ids"][0]]
        received = instant(story["payload"]["observed_at"])
        at = instant(decision["available_at"])
        for definition in definitions.values():
            anchors = {"anchor": received - timedelta(seconds=60), "receipt": received, "decision": at,
                       **{f"after_{m}m": at + timedelta(minutes=m) for m in (5, 30, 60, 240)}}
            mids, quote_ids = {}, {}
            for label, timestamp in anchors.items():
                try:
                    quote = quote_at(quotes, timestamp.isoformat(), definition, 2)
                except ValueError:
                    quote = None
                mids[label] = (Decimal(quote.bid) + Decimal(quote.ask)) / 2 if quote else None
                quote_ids[label] = digest(["market", quote.__dict__]) if quote else None
            def difference(end, start):
                return str(mids[end] - mids[start]) if mids[end] is not None and mids[start] is not None else None
            output.append({"decision_id": decision["id"], "instrument": definition.instrument_id,
                           "data_mode": definition.data_mode, "metric": "descriptive_mid_price_response_not_pnl",
                           "pre_receipt": difference("receipt", "anchor"), "during_processing": difference("decision", "receipt"),
                           "after": {str(m): difference(f"after_{m}m", "decision") for m in (5, 30, 60, 240)},
                           "quote_ids": quote_ids, "missing_anchors": [key for key, value in mids.items() if value is None]})
    return output


def build_report(manifest: dict, reader, market: dict) -> dict:
    rows = reader.records
    sources = manifest["config"]["sources"]
    health = {}
    for row in rows:
        if row["kind"] == "source_health":
            health[row["payload"]["source_id"]] = {**row["payload"], "available_at": row["available_at"]}
    counts = Counter(r["kind"] for r in rows)
    extractions = [r for r in rows if r["kind"] == "extraction"]
    incidents = [r for r in rows if r["kind"] == "incident_revision"]
    comparable = {}
    for row in rows:
        if row["kind"] == "story_revision":
            p = row["payload"]
            comparable.setdefault(digest(p["text"]), {}).setdefault(p["source_id"], p["observed_at"])
    lag = {s["id"]: [] for s in sources}
    for group in comparable.values():
        if len(group) < 2:
            continue
        earliest = min(instant(at) for at in group.values())
        for name, at in group.items():
            lag.setdefault(name, []).append((instant(at) - earliest).total_seconds() * 1000)
    return {
        "generated_at": utc_now(), "economic_evaluation": "unavailable",
        "headline": "Economic evaluation unavailable: no qualified live CL/MCL feed.",
        "manifest": {k: manifest[k] for k in ("code_commit", "dirty", "dataset_role", "records_hash")},
        "counts": dict(counts),
        "sources": [{"id": s["id"], "enabled": s["enabled"], "role": s["role"], "rights": s["rights"],
                     "health": health.get(s["id"], {"status": "NOT_OBSERVED" if s["enabled"] else "DISABLED"}),
                     "revisions": sum(r["kind"] == "story_revision" and r["payload"]["source_id"] == s["id"] for r in rows)} for s in sources],
        "capture_latency_ms": distribution([r["payload"]["receive_to_commit_ms"] for r in rows if r["kind"] == "commit_receipt"]),
        "matched_source_lead_lag_ms": {name: distribution(values) for name, values in lag.items()},
        "source_comparison_method": "Exact text matches only; lead/lag versus earliest local comparable receipt, not global publication. Copies do not imply independent confirmation.",
        "extraction": {"latency_ms": distribution([r["payload"]["latency_ms"] for r in extractions]),
                       "late": sum(r["payload"]["late"] for r in extractions),
                       "failures": counts["extraction_failure"], "accuracy": "Not measured: requires independently labeled corpus",
                       "quota": "Attempt counts available through status; CLI subscription use is not a dollar-price estimate"},
        "incident_examples": incidents[-20:], "decisions": [r for r in rows if r["kind"] == "decision"],
        "market_qualification": qualify(market["records"], market["gaps"]),
        "response": markouts(reader, market),
        "gaps": [r for r in rows if r["kind"] in {"runtime_gap", "source_health"} and
                 (r["kind"] == "runtime_gap" or r["payload"].get("status") not in {"OK", "UNCHANGED", "EMPTY"})] + market["gaps"],
        "next_budget_review": {
            "current_incremental_subscription_spend": 0,
            "options": [
                {"provider": "IBKR", "price": None, "status": "Account, real-time permissions and all-in cost unverified"},
                {"provider": "Databento", "published_reference_usd_month": 199, "verified_date": "2026-09-19",
                 "source": "https://databento.com/pricing", "status": "Planning reference only; recheck entitlements and price before purchase"}],
            "decision": "Select and fund a live feed separately; buy no news subscription yet."},
        "promotion": {"A": "engineering review required", "B": "blocked: live data unavailable", "C": "offline tools only",
                      "D": "insufficient prospective evidence", "E": "not enabled", "F": "not enabled"},
    }


def write_report(report: dict, destination: Path):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    view = destination.with_suffix(".html")
    # Source text is escaped; captured prompt/HTML content is never executable UI.
    sections = []
    for key, value in report.items():
        sections.append(f"<details open><summary>{html.escape(key.replace('_', ' ').title())}</summary><pre>{html.escape(json.dumps(value, indent=2, ensure_ascii=False))}</pre></details>")
    view.write_text("<!doctype html><meta charset='utf-8'><title>Oil observation pilot</title>"
                   "<style>body{max-width:1100px;margin:40px auto;padding:0 24px;font:16px system-ui;background:#f5f7fa;color:#172735}"
                   "h1{font-size:28px}details{background:white;padding:16px;margin:12px 0;border:1px solid #d7dfe5}"
                   "summary{font-weight:600;cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}</style>"
                   "<h1>Oil observation pilot</h1><p>Economic evaluation unavailable. Observation only; no broker execution.</p>"
                   + "".join(sections))
    return view
