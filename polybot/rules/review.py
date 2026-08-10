from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.discovery.config import (
    load_discovery_config,
    rule_store_db_path,
)
from polybot.discovery.store import DiscoveryStore
from polybot.discovery.types import MarketContext
from polybot.log import log_event

from .compiler import (
    _critical_consensus_payload,
    effective_deadline_authority_policy,
    rule_compilation_blocker,
)
from .contracts import RuleSemantics, RuleSpec
from .store import RuleReviewApproval, RuleStore


def prepare_rule_review_command(
    config_path: Path,
    market_id: str,
    output_sha256: str,
    *,
    out: Path | None = None,
) -> int:
    """Build an exact review candidate from one stored normalized pass.

    This command does not approve or persist a RuleSpec. The output remains an
    ordinary JSON file that an operator can edit, validate, and approve by its
    resulting execution hash.
    """
    config = load_discovery_config(config_path)
    context = _reviewable_context(config, market_id)
    if re.fullmatch(r"[0-9a-f]{64}", output_sha256) is None:
        raise SystemExit("--pass-sha256 must be a lowercase SHA-256")
    rule_store = RuleStore(rule_store_db_path(config))
    stored_pass = rule_store.compilation_pass_by_output_sha256(
        market_id,
        context.rule_text_sha256,
        output_sha256,
    )
    if stored_pass is None:
        raise SystemExit(
            "no normalized compiler pass matches the current market/rule hash "
            f"and output SHA-256 {output_sha256}"
        )
    policy = _review_policy(config, context)
    blocker = rule_compilation_blocker(
        context,
        deadline_authority_policy=policy,
    )
    if blocker:
        raise SystemExit(
            "deterministic rule preflight blocks review preparation: " + blocker
        )
    semantics = RuleSemantics.from_dict(stored_pass["normalized_output"])
    spec = RuleSpec.from_context(
        context,
        semantics,
        compiler_model="operator_review_draft",
        compiled_at=_now(),
        deadline_authority_policy=policy,
    )
    candidate = spec.as_dict()
    if out is not None:
        destination = out.expanduser().resolve()
        if destination.exists():
            raise SystemExit(
                f"refusing to overwrite existing review candidate {destination}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(candidate, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        destination = None
    payload: dict[str, Any] = {
        "candidate_spec_sha256": spec.spec_sha256,
        "deadline_authority_policy": spec.deadline_authority_policy,
        "market_id": market_id,
        "paper_only": True,
        "rule_family": spec.semantics.rule_family,
        "rule_text_sha256": spec.rule_text_sha256,
        "source_pass": {
            "created_at": stored_pass["created_at"],
            "model": stored_pass["model"],
            "output_sha256": stored_pass["output_sha256"],
            "pass_index": stored_pass["pass_index"],
        },
        # A market reaches review precisely because its passes disagreed, so
        # the unexported pass is a competing reading of the same rules, not
        # noise. Reviewing one pass blind has already shipped the weaker
        # reading of a source policy; list the alternatives and where they
        # diverge so the choice is deliberate.
        "alternate_passes": _alternate_passes(
            rule_store.compilation_passes(market_id, context.rule_text_sha256),
            selected_output_sha256=stored_pass["output_sha256"],
        ),
    }
    if destination is None:
        payload["candidate_spec"] = candidate
    else:
        payload["out"] = str(destination)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def import_reviewed_rule_command(
    config_path: Path,
    market_id: str,
    spec_path: Path,
    *,
    reviewer: str,
    note: str,
    approved_spec_sha256: str,
) -> int:
    """Import exactly one explicitly approved, context-bound RuleSpec.

    The supplied file's provenance is retained by byte hash. Instrument,
    clause, deadline, topology, and rule-version fields are reconstructed from
    the current context before the spec and approval are atomically stored.
    """
    config = load_discovery_config(config_path)
    context = _reviewable_context(config, market_id)
    raw_bytes = spec_path.read_bytes()
    input_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    try:
        raw = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"reviewed RuleSpec is not valid UTF-8 JSON: {exc}") from exc
    candidate = RuleSpec.from_dict(raw)
    if candidate.market_id != market_id:
        raise SystemExit(
            "reviewed RuleSpec market does not match --market: "
            f"{candidate.market_id!r}"
        )
    if re.fullmatch(r"[0-9a-f]{64}", approved_spec_sha256) is None:
        raise SystemExit("--approve-spec-sha256 must be a lowercase SHA-256")
    if candidate.spec_sha256 != approved_spec_sha256:
        raise SystemExit(
            "approved hash does not match reviewed RuleSpec: "
            f"expected {candidate.spec_sha256}"
        )
    policy = _review_policy(config, context)
    blocker = rule_compilation_blocker(
        context,
        deadline_authority_policy=policy,
    )
    if blocker:
        raise SystemExit(
            "deterministic rule preflight blocks reviewed import: " + blocker
        )
    if candidate.deadline_authority_policy != policy:
        raise SystemExit(
            "reviewed RuleSpec deadline authority differs from maintained config"
        )
    try:
        candidate.validate_context_binding(context)
    except ValueError as exc:
        raise SystemExit(f"reviewed RuleSpec context binding failed: {exc}") from exc

    approved_at = _now()
    reviewed = RuleSpec.from_context(
        context,
        candidate.semantics,
        compiler_model=f"reviewed:{reviewer.strip()}",
        compiled_at=approved_at,
        deadline_authority_policy=policy,
    )
    if reviewed.execution_dict() != candidate.execution_dict():
        raise SystemExit(
            "reviewed RuleSpec contains fields that cannot be reconstructed "
            "from current context and approved semantics"
        )
    approval = RuleReviewApproval.create(
        market_id=market_id,
        rule_text_sha256=reviewed.rule_text_sha256,
        spec_sha256=reviewed.spec_sha256,
        reviewer=reviewer,
        review_note=note,
        input_sha256=input_sha256,
        approved_at=approved_at,
    )
    rule_store = RuleStore(rule_store_db_path(config))
    saved = rule_store.save_reviewed_spec(reviewed, approval)

    discovery_store = DiscoveryStore(config.data_dir)
    discovery_store.save_context(
        MarketContext.from_dict(
            {
                **context.as_dict(),
                "state": "RULES_REVIEW_REQUIRED",
                "state_reasons": [
                    "reviewed_rule_spec_imported_source_plan_required"
                ],
            }
        )
    )
    log_event(
        "reviewed_rule_spec_imported",
        approval_sha256=approval.approval_sha256,
        input_sha256=input_sha256,
        market_id=market_id,
        reviewer=approval.reviewer,
        rule_family=saved.semantics.rule_family,
        rule_text_sha256=saved.rule_text_sha256,
        spec_sha256=saved.spec_sha256,
    )
    print(
        json.dumps(
            {
                "approval_sha256": approval.approval_sha256,
                "imported": True,
                "input_sha256": input_sha256,
                "market_id": market_id,
                "next_step": "plan-sources then grade-markets",
                "paper_only": True,
                "reviewer": approval.reviewer,
                "rule_family": saved.semantics.rule_family,
                "rule_text_sha256": saved.rule_text_sha256,
                "source_plan_ready": False,
                "spec_sha256": saved.spec_sha256,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def consensus_report_command(
    config_path: Path,
    *,
    market_id: str = "",
) -> int:
    """Score two-pass agreement across every stored compilation pass.

    Compilation reaching zero stored specs is invisible from the pass table
    alone, because a pass can be clean and still be discarded at consensus.
    This scores the same comparison the compiler uses, so a change to
    consensus or to the prompt can be judged by a number instead of a
    hypothesis. It reads stored passes only and compiles nothing.
    """
    config = load_discovery_config(config_path)
    rule_store = RuleStore(rule_store_db_path(config))
    discovery_store = DiscoveryStore(config.data_dir)

    market_ids = (
        [market_id]
        if market_id
        else sorted(
            {item for item, _rule in rule_store.compilation_pass_counts()}
        )
    )
    markets: list[dict[str, Any]] = []
    field_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {
        "AGREE": 0,
        "DISAGREE": 0,
        "NO_CLEAN_PAIR": 0,
    }
    for item in market_ids:
        context = discovery_store.load_context(item)
        if context is None:
            continue
        stored = rule_store.compilation_passes(item, context.rule_text_sha256)
        usable = [
            entry
            for entry in stored
            if entry.get("normalized_output") is not None
        ]
        errored = [entry for entry in stored if entry.get("error")]
        latest: dict[int, dict[str, Any]] = {}
        for entry in usable:
            latest[int(entry["pass_index"])] = entry
        row: dict[str, Any] = {
            "market_id": item,
            "clean_passes": len(usable),
            "errored_passes": len(errored),
        }
        if len(latest) < 2:
            row["status"] = "NO_CLEAN_PAIR"
            if errored:
                row["last_error"] = errored[-1]["error"][:200]
        else:
            first, second = (latest[index] for index in sorted(latest)[:2])
            differing = _differing_fields(
                _critical_consensus_payload(first["normalized_output"]),
                _critical_consensus_payload(second["normalized_output"]),
            )
            row["status"] = "AGREE" if not differing else "DISAGREE"
            if differing:
                row["differing_fields"] = differing
                for field in differing:
                    field_counts[field] = field_counts.get(field, 0) + 1
        status_counts[row["status"]] += 1
        markets.append(row)

    payload = {
        "markets": sorted(markets, key=lambda entry: entry["market_id"]),
        "summary": {
            **status_counts,
            "markets_scored": len(markets),
            "differing_fields": dict(
                sorted(
                    field_counts.items(),
                    key=lambda pair: (-pair[1], pair[0]),
                )
            ),
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _alternate_passes(
    stored_passes: list[dict[str, Any]],
    *,
    selected_output_sha256: str,
) -> list[dict[str, Any]]:
    """Describe every other usable pass and how it differs from the export."""

    selected = next(
        (
            item
            for item in stored_passes
            if item.get("output_sha256") == selected_output_sha256
            and item.get("normalized_output") is not None
        ),
        None,
    )
    alternates: list[dict[str, Any]] = []
    for item in stored_passes:
        if item.get("output_sha256") == selected_output_sha256:
            continue
        entry: dict[str, Any] = {
            "created_at": item["created_at"],
            "model": item["model"],
            "output_sha256": item["output_sha256"] or "",
            "pass_index": item["pass_index"],
            "usable": item.get("normalized_output") is not None,
        }
        if item.get("error"):
            entry["error"] = item["error"]
        if selected is not None and item.get("normalized_output") is not None:
            entry["differing_fields"] = _differing_fields(
                _critical_consensus_payload(selected["normalized_output"]),
                _critical_consensus_payload(item["normalized_output"]),
            )
        alternates.append(entry)
    return alternates


def _differing_fields(
    selected: Any,
    other: Any,
    path: str = "",
) -> list[str]:
    if isinstance(selected, dict) and isinstance(other, dict):
        fields: list[str] = []
        for key in sorted(set(selected) | set(other)):
            child = f"{path}.{key}" if path else key
            if key in selected and key in other:
                fields.extend(
                    _differing_fields(selected[key], other[key], child)
                )
            else:
                fields.append(child)
        return fields
    if selected != other:
        return [path or "<root>"]
    return []


def _reviewable_context(config: Any, market_id: str) -> MarketContext:
    allowed = {
        item.strip()
        for item in config.rule_compiler.reviewed_rule_market_ids
    }
    if market_id not in allowed:
        raise SystemExit(
            f"market {market_id!r} is not in rule_compiler.reviewed_rule_market_ids"
        )
    context = DiscoveryStore(config.data_dir).load_context(market_id)
    if context is None:
        raise SystemExit(f"unknown market_id {market_id!r}")
    return context


def _review_policy(config: Any, context: MarketContext) -> str:
    return effective_deadline_authority_policy(
        context,
        config.rule_compiler.deadline_authority_policy,
        set(config.rule_compiler.deadline_authority_market_ids),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "import_reviewed_rule_command",
    "prepare_rule_review_command",
]
