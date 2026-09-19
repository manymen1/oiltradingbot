from __future__ import annotations

import argparse
import json
import shutil
import signal
import threading
from pathlib import Path

from .clock import seconds, stamp, utc_now
from .config import load_config
from .extract import AnalysisWorker, CodexExtractor
from .incidents import IncidentReducer
from .market import FixtureAdapter, atomic_json, qualify, read_archive, record_adapter
from .replay import export_manifest, load_manifest
from .report import build_report, write_report
from .research import freeze_protocol, research_candidate
from .schema import digest
from .sources import NewsCollector
from .store import Journal, component_lock


def preflight(config):
    return {"mode": "observe", "broker_execution": "disabled", "pilot_ready": True,
            "economic_evaluation": "unavailable", "storage": str(config.root),
            "codex_available": bool(shutil.which(config.extraction.get("binary", "codex"))),
            "sources": [{"id": s["id"], "enabled": s["enabled"], "model_processing": s["rights"]["model_processing"],
                         "endpoint_qualification": s["qualification"]} for s in config.sources],
            "blockers": ["LIVE_MARKET_DATA_UNQUALIFIED", "NO_BROKER_ADAPTER", "SOURCE_MODEL_RIGHTS_REQUIRE_QUALIFICATION"],
            "next": "Capture public source payloads and fixtures; review quality and data budget."}


def status(config):
    output = preflight(config)
    output["journals"] = {}
    for name in ("news", "analysis", "runtime"):
        store = Journal(config.db(name))
        from collections import Counter
        output["journals"][name] = dict(Counter(r["kind"] for r in store.records()))
    output["attempts_by_day"] = Journal(config.db("analysis")).budget()
    records, gaps = read_archive(config.root / "quotes")
    output["market"] = qualify(records, gaps)
    output["heartbeats"] = [r for r in Journal(config.db("runtime")).records("heartbeat")][-3:]
    return output


def record(config, component: str, *, once: bool, fixture: Path | None, stop=None):
    stop = stop or threading.Event()
    runtime = Journal(config.db("runtime"))
    with component_lock(config.root, component):
        prior = runtime.cursor("runtime:" + component)
        current = stamp()
        if prior:
            runtime.append("runtime_gap", {"component": component, "reason": "RESTART_OR_SLEEP",
                                          "previous": prior, "current": current})
        runtime.append("runtime_start", {"component": component, "clock": current, "config_hash": digest(config.raw)})
        if component == "news":
            collector = NewsCollector(Journal(config.db("news")), config.sources)
            work = lambda: collector.poll_once()
        elif component == "analysis":
            news, analysis = Journal(config.db("news")), Journal(config.db("analysis"))
            reducer = IncidentReducer(news, analysis, config.raw["assets"])
            worker = AnalysisWorker(news, analysis, CodexExtractor(config.extraction), config.extraction, reducer,
                                    {s["id"]: s["rights"]["model_processing"] for s in config.sources})
            def work():
                result = worker.run_once()
                for incident in analysis.records("incident_revision"):
                    research_candidate(analysis, incident)
                return result
        else:
            done = False
            def work():
                nonlocal done
                if not fixture or done:
                    return {"status": "WAITING_FOR_QUALIFIED_LIVE_FEED", "economic_evaluation": "unavailable"}
                result = record_adapter(FixtureAdapter(fixture), config.root / "quotes")
                done = True
                return result
        last = current
        try:
            while not stop.is_set():
                current = stamp()
                if seconds(current["utc"], last["utc"]) > 90:
                    runtime.append("runtime_gap", {"component": component, "reason": "HEARTBEAT_DELAY_OR_SLEEP",
                                                  "previous": last, "current": current})
                if current["boot_id"] == last["boot_id"]:
                    drift = seconds(current["utc"], last["utc"]) - (current["monotonic_ns"] - last["monotonic_ns"]) / 1e9
                    if abs(drift) > 5:
                        runtime.append("runtime_gap", {"component": component, "reason": "CLOCK_STEP_OR_SUSPEND",
                                                      "wall_minus_monotonic_seconds": drift, "previous": last, "current": current})
                result = work()
                last = stamp()
                with runtime.transaction() as db:
                    runtime.append("heartbeat", {"component": component, "clock": last, "result": result}, db=db)
                    runtime.set_cursor(db, "runtime:" + component, last)
                if once:
                    return result
                # Poll well within the receipt-to-analysis deadline, including
                # when the previous iteration found no pending stories.
                stop.wait(min(1, config.extraction["deadline_seconds"] / 4)
                          if component == "analysis" else 30)
        finally:
            runtime.append("runtime_stop", {"component": component, "clock": stamp()})
    return {"stopped": component}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Oil observation and offline research; no broker execution")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "status", "record", "snapshot", "freeze-protocol", "adjudicate", "demo"):
        child = sub.add_parser(command)
        child.add_argument("--config", default="configs/oil/observe.yaml")
        if command == "record":
            child.add_argument("--component", choices=("news", "market", "analysis"), required=True)
            child.add_argument("--once", action="store_true")
            child.add_argument("--fixture", type=Path)
        if command in {"snapshot", "demo"}:
            child.add_argument("--out", type=Path, required=True)
        if command == "demo":
            child.add_argument("--quotes", type=Path, default=Path("tests/fixtures/oil/market.json"))
        if command == "freeze-protocol":
            child.add_argument("--protocol", type=Path, required=True)
        if command == "adjudicate":
            child.add_argument("--operation", choices=("merge", "split", "link"), required=True)
            child.add_argument("--story", action="append", required=True)
            child.add_argument("--target-incident", required=True)
            child.add_argument("--reason", required=True)
    for command in ("replay", "report"):
        child = sub.add_parser(command)
        child.add_argument("--manifest", type=Path, required=True)
        if command == "report":
            child.add_argument("--out", type=Path, required=True)
    child = sub.add_parser("paper-replay", help="local paper orders from a verified snapshot; no broker connection")
    child.add_argument("--manifest", type=Path, required=True)
    child.add_argument("--instrument", required=True, help="explicit archived contract ID")
    child.add_argument("--limits", type=Path, required=True, help="JSON paper execution and risk assumptions")
    child.add_argument("--out", type=Path, required=True, help="new output directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "paper-replay":
            from .paper import PaperLimits, paper_replay
            result = paper_replay(args.manifest, args.out, args.instrument,
                                  PaperLimits(**json.loads(args.limits.read_text())))
        elif args.command in {"replay", "report"}:
            manifest, reader, market = load_manifest(args.manifest)
            if args.command == "replay":
                result = {"verified": True, "records": len(reader.records), "decisions": len(reader.decision_inputs()),
                          "decision_inputs_hash": digest(reader.decision_inputs()), "economic_evaluation": "unavailable"}
            else:
                view = write_report(build_report(manifest, reader, market), args.out)
                result = {"json": str(args.out.resolve()), "html": str(view.resolve()), "economic_evaluation": "unavailable"}
        else:
            config = load_config(args.config)
            if args.command == "preflight":
                result = preflight(config)
            elif args.command == "status":
                result = status(config)
            elif args.command == "snapshot":
                result = {"manifest": str(export_manifest(config, args.out))}
            elif args.command == "demo":
                from .demo import run_demo
                result = run_demo(config, args.out, args.quotes)
            elif args.command == "freeze-protocol":
                result = {"protocol_id": freeze_protocol(Journal(config.db("analysis")), json.loads(args.protocol.read_text()))}
            elif args.command == "adjudicate":
                reducer = IncidentReducer(Journal(config.db("news")), Journal(config.db("analysis")), config.raw["assets"])
                result = {"adjudication_id": reducer.adjudicate(story_ids=args.story, target_incident=args.target_incident,
                                                              operation=args.operation, reason=args.reason)}
            else:
                stop = threading.Event()
                signal.signal(signal.SIGTERM, lambda *_: stop.set())
                signal.signal(signal.SIGINT, lambda *_: stop.set())
                result = record(config, args.component, once=args.once, fixture=args.fixture, stop=stop)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, TypeError, RuntimeError, OSError, KeyError) as exc:
        print(json.dumps({"error": str(exc), "mode": "observe"}))
        return 2
