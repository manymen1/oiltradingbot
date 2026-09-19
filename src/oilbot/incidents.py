from __future__ import annotations

from .clock import instant, utc_now
from .schema import digest
from .store import Journal


class IncidentReducer:
    def __init__(self, news: Journal, analysis: Journal, assets: list[dict]):
        self.news, self.analysis, self.assets = news, analysis, assets
        registry_hash = digest(assets)
        self.registry_id = analysis.cursor("asset_registry:" + registry_hash)
        if not self.registry_id:
            with analysis.transaction() as db:
                self.registry_id = analysis.append("asset_registry", {"assets": assets, "version": registry_hash}, db=db)
                analysis.set_cursor(db, "asset_registry:" + registry_hash, self.registry_id)

    def apply(self, story: dict, extraction: dict) -> str | None:
        key = "reduced:" + extraction["id"]
        if self.analysis.cursor(key):
            return self.analysis.cursor(key)
        data = story["payload"]
        latest = self.news.cursor("story:" + data["story_id"])
        if latest and latest["revision_id"] != story["id"]:
            with self.analysis.transaction() as db:
                rid = self.analysis.append("stale_extraction", {"input_revision_ids": [story["id"], extraction["id"]],
                                           "reason": "SUPERSEDED_STORY_REVISION"}, db=db)
                self.analysis.set_cursor(db, key, rid)
            return rid
        facts = extraction["payload"]["facts"]
        asset_ids = []
        for asset in self.assets:
            if any(f["field"] in {"asset", "location"} and any(alias.casefold() in f["value"].casefold()
                    for alias in asset["aliases"]) for f in facts):
                asset_ids.append(asset["id"])
        association = self.analysis.cursor("association:" + data["story_id"])
        # Legacy cursors contained only an incident ID. New associations retain
        # the manual decision that caused the assignment for causal replay.
        incident_id = (association["incident_id"] if isinstance(association, dict)
                       else association or digest(["incident", data["story_id"]]))
        relations = self.analysis.cursor("links:" + data["story_id"], [])
        adjudication_ids = [relation["adjudication_id"] for relation in relations]
        if isinstance(association, dict):
            adjudication_ids.append(association["adjudication_id"])
        old_id = self.analysis.cursor("incident:" + incident_id)
        old = self.analysis.get(old_id) if old_id else None
        operational = [f["value"] for f in facts if f["field"] == "operational_status" and f["assertion"] == "asserted"]
        denied = [f for f in facts if f["assertion"] == "denied"]
        status = operational[-1] if len(set(operational)) == 1 else "unknown"
        contradictions = [f for f in facts if f["assertion"] == "denied"]
        if len(set(operational)) > 1:
            contradictions.extend(f for f in facts if f["field"] == "operational_status")
        if data["status"] == "withdrawal":
            status = "unknown"
        origin = data.get("origin")
        if not origin and data["source_role"] in {"operator", "port_authority", "maritime_authority"}:
            origin = data["source_id"]
        semantic = {"assets": sorted(asset_ids), "operational_status": status,
                    "facts": sorted([{k: f[k] for k in ("field", "value", "assertion", "unit", "quantity_kind")} for f in facts], key=digest),
                    "withdrawn": data["status"] == "withdrawal"}
        novelty = (old["payload"]["semantic_hash"] != digest(semantic) if old
                   else not extraction["payload"].get("cached", False))
        candidates = []
        for row in self.analysis.records("incident_revision"):
            if row["payload"]["incident_id"] != incident_id and set(asset_ids) & set(row["payload"]["asset_ids"]):
                candidates.append(row["payload"]["incident_id"])
        # Matching assets suggest candidates only; similarity never establishes a merge.
        payload = {"incident_id": incident_id, "episode_id": None, "story_id": data["story_id"],
                   "revision": old["payload"]["revision"] + 1 if old else 1,
                   "supersedes_id": old_id, "input_revision_ids": [story["id"], extraction["id"], self.registry_id] + ([old_id] if old_id else []) + adjudication_ids,
                   "transform": "incident-v1", "asset_ids": sorted(asset_ids),
                   "asset_types": sorted({a["type"] for a in self.assets if a["id"] in asset_ids}),
                   "operational_status": status, "action": [f for f in facts if f["field"] == "action"],
                   "evidence_status": "withdrawn" if data["status"] == "withdrawal" else "disputed" if contradictions
                       else "primary_operational_report" if origin == data["source_id"] and operational else "attributed_claim",
                   "origin_groups": [origin] if origin else [], "origin_uncertain": origin is None,
                   "contradictions": contradictions, "facts": facts, "semantic_hash": digest(semantic),
                   "novel": novelty, "candidate_links": sorted(set(candidates)),
                   "adjudicated_links": relations, "net_lost_supply": None,
                   "economic_effect": "unknown", "late": extraction["payload"]["late"]}
        payload["initial_snapshot"] = data.get("initial_snapshot", True)
        with self.analysis.transaction() as db:
            rid = self.analysis.append("incident_revision", payload, db=db)
            self.analysis.set_cursor(db, "incident:" + incident_id, rid)
            self.analysis.set_cursor(db, key, rid)
        return rid

    def adjudicate(self, *, story_ids: list[str], target_incident: str, operation: str, reason: str) -> str:
        if operation not in {"merge", "split", "link"} or not story_ids or not reason.strip():
            raise ValueError("explicit relation and reason required")
        known_stories = {r["payload"]["story_id"] for r in self.news.records("story_revision")}
        if not set(story_ids) <= known_stories:
            raise ValueError("unknown story")
        if not self.analysis.cursor("incident:" + target_incident) and operation != "split":
            raise ValueError("unknown target incident")
        with self.analysis.transaction() as db:
            rid = self.analysis.append("adjudication", {"operation": operation, "story_ids": story_ids,
                "target_incident": target_incident, "reason": reason, "transform": "manual-v1",
                "input_revision_ids": [r["id"] for r in self.news.records("story_revision") if r["payload"]["story_id"] in story_ids]}, db=db)
            for story_id in story_ids:
                relation = {"incident_id": target_incident, "adjudication_id": rid}
                if operation == "link":
                    links = self.analysis.cursor("links:" + story_id, [])
                    self.analysis.set_cursor(db, "links:" + story_id, links + [relation])
                else:
                    self.analysis.set_cursor(db, "association:" + story_id, relation)
        return rid
