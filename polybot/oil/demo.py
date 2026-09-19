"""Reproducible synthetic pilot. Does not call news, model, market or broker services."""
from __future__ import annotations

from pathlib import Path

from .clock import stamp
from .config import OilConfig
from .extract import AnalysisWorker, CodexExtractor
from .incidents import IncidentReducer
from .market import FixtureAdapter, record_adapter
from .replay import export_manifest, load_manifest
from .report import build_report, write_report
from .research import research_candidate
from .schema import Fact, NewsItem, digest, to_dict
from .store import Journal


def run_demo(config: OilConfig, destination: Path, quotes: Path):
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("demo destination already exists")
    demo_config = OilConfig(config.path, destination / "runtime", {**config.raw, "dataset_role": "engineering_fixture"})
    news, analysis = Journal(demo_config.db("news")), Journal(demo_config.db("analysis"))
    source = {**config.sources[0], "id": "synthetic_operator", "owner": "Synthetic fixture only",
              "rights": {"capture": "synthetic", "model_processing": "permitted", "retention": "synthetic"}}
    def output(text):
        value = "restored" if "restored" in text else "suspended"
        return {"relevant": True, "facts": [
            to_dict(Fact("asset", "Ras Tanura", 0, 10, "Ras Tanura", "asserted")),
            to_dict(Fact("operational_status", value, 0, len(text), text, "asserted"))]}
    extractor = CodexExtractor(config.extraction, runner=output, version="synthetic-fixture-no-model-call")
    extractor.identity["provider"] = "recorded_fixture"
    extractor.fingerprint = digest(extractor.identity)
    reducer = IncidentReducer(news, analysis, config.raw["assets"])
    worker = AnalysisWorker(news, analysis, extractor, config.extraction, reducer)
    for text, status in (("Ras Tanura loading suspended", "update"), ("Ras Tanura loading restored", "correction")):
        now = stamp()
        oid = news.capture(source, {"body": text.encode(), "url": "https://synthetic.invalid/fixture", "status": 200,
                                    "content_type": "text/plain", "headers": {}, "started": now,
                                    "first_byte": now, "received": now, "synthetic": True})
        news.accept_items(source, oid, [NewsItem("fixture-story", "https://synthetic.invalid/fixture", text, text, status=status)], {})
        worker.run_once()
        for incident in analysis.records("incident_revision"):
            research_candidate(analysis, incident)
    record_adapter(FixtureAdapter(quotes), demo_config.root / "quotes")
    manifest_path = export_manifest(demo_config, destination / "snapshot")
    manifest, reader, market = load_manifest(manifest_path)
    report = write_report(build_report(manifest, reader, market), destination / "report.json")
    return {"manifest": str(manifest_path), "report": str(report), "synthetic": True,
            "economic_evaluation": "unavailable", "decisions": len(reader.decision_inputs())}
