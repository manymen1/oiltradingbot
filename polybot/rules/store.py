from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import (
    DecisionProof,
    EvidenceClaim,
    RuleEvaluation,
    RuleSpec,
    canonical_json,
    sha256_json,
)


@dataclass(frozen=True)
class CompilationPass:
    market_id: str
    rule_text_sha256: str
    pass_index: int
    model: str
    raw_output: str
    normalized_output: dict[str, Any] | None
    error: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class ExtractionPass:
    market_id: str
    rule_spec_sha256: str
    article_id: str
    pass_index: int
    model: str
    raw_output: str
    normalized_output: dict[str, Any] | None
    error: str = ""
    created_at: str = ""


class RuleStore:
    """Immutable WAL store for compiled rules and extracted evidence.

    Rule versions are append-only. A current lookup is always scoped by both
    market id and the verbatim rule-text hash, so a changed oracle contract
    cannot accidentally reuse an older semantic interpretation.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(
                f"file:{self.path}?mode=ro",
                uri=True,
                timeout=5.0,
            )
        else:
            connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rule_specs (
                    market_id TEXT NOT NULL,
                    rule_text_sha256 TEXT NOT NULL,
                    spec_sha256 TEXT NOT NULL UNIQUE,
                    spec_json TEXT NOT NULL,
                    compiler_model TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(market_id, rule_text_sha256)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS compilation_passes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    rule_text_sha256 TEXT NOT NULL,
                    pass_index INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    raw_output TEXT NOT NULL,
                    normalized_output_json TEXT,
                    output_sha256 TEXT,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS compilation_pass_lookup
                ON compilation_passes(market_id, rule_text_sha256, id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_claims (
                    claim_sha256 TEXT PRIMARY KEY,
                    market_id TEXT NOT NULL,
                    rule_spec_sha256 TEXT NOT NULL,
                    article_id TEXT NOT NULL,
                    claim_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(market_id, rule_spec_sha256, article_id, claim_sha256)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS evidence_claim_lookup
                ON evidence_claims(rule_spec_sha256, article_id, created_at)
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS evidence_claim_identity
                ON evidence_claims(rule_spec_sha256, article_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS extraction_passes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    rule_spec_sha256 TEXT NOT NULL,
                    article_id TEXT NOT NULL,
                    pass_index INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    raw_output TEXT NOT NULL,
                    normalized_output_json TEXT,
                    output_sha256 TEXT,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS extraction_pass_lookup
                ON extraction_passes(rule_spec_sha256, article_id, id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rule_evaluations (
                    evaluation_sha256 TEXT PRIMARY KEY,
                    market_id TEXT NOT NULL,
                    rule_spec_sha256 TEXT NOT NULL,
                    evaluation_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS rule_evaluation_lookup
                ON rule_evaluations(rule_spec_sha256, created_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS decision_proofs (
                    proof_sha256 TEXT PRIMARY KEY,
                    market_id TEXT NOT NULL,
                    rule_spec_sha256 TEXT NOT NULL,
                    evaluation_sha256 TEXT NOT NULL,
                    proof_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS decision_proof_lookup
                ON decision_proofs(market_id, created_at)
                """
            )

    def save_spec(self, spec: RuleSpec) -> RuleSpec:
        validated = RuleSpec.from_dict(spec.as_dict())
        payload = canonical_json(validated.as_dict())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT spec_sha256, spec_json
                FROM rule_specs
                WHERE market_id=? AND rule_text_sha256=?
                """,
                (validated.market_id, validated.rule_text_sha256),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["spec_sha256"]) != validated.spec_sha256
                    or str(existing["spec_json"]) != payload
                ):
                    raise ValueError(
                        "immutable RuleSpec conflict for "
                        f"{validated.market_id}:{validated.rule_text_sha256}"
                    )
                return RuleSpec.from_dict(json.loads(str(existing["spec_json"])))
            connection.execute(
                """
                INSERT INTO rule_specs(
                    market_id, rule_text_sha256, spec_sha256, spec_json,
                    compiler_model, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    validated.market_id,
                    validated.rule_text_sha256,
                    validated.spec_sha256,
                    payload,
                    validated.compiler_model,
                    validated.compiled_at,
                ),
            )
        return validated

    def load_spec(
        self,
        market_id: str,
        rule_text_sha256: str | None = None,
    ) -> RuleSpec | None:
        query = "SELECT spec_json FROM rule_specs WHERE market_id=?"
        params: tuple[Any, ...] = (market_id,)
        if rule_text_sha256 is not None:
            query += " AND rule_text_sha256=?"
            params = (market_id, rule_text_sha256)
        query += " ORDER BY created_at DESC LIMIT 1"
        with self._connect(read_only=True) as connection:
            row = connection.execute(query, params).fetchone()
        if row is None:
            return None
        raw = json.loads(str(row["spec_json"]))
        return RuleSpec.from_dict(raw)

    def all_specs(self) -> list[RuleSpec]:
        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT spec_json FROM rule_specs ORDER BY market_id, created_at"
            ).fetchall()
        return [RuleSpec.from_dict(json.loads(str(row["spec_json"]))) for row in rows]

    def save_pass(self, item: CompilationPass) -> None:
        normalized_json = (
            canonical_json(item.normalized_output)
            if item.normalized_output is not None
            else None
        )
        created_at = item.created_at or _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO compilation_passes(
                    market_id, rule_text_sha256, pass_index, model, raw_output,
                    normalized_output_json, output_sha256, error, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.market_id,
                    item.rule_text_sha256,
                    item.pass_index,
                    item.model,
                    item.raw_output,
                    normalized_json,
                    sha256_json(item.normalized_output)
                    if item.normalized_output is not None
                    else None,
                    item.error,
                    created_at,
                ),
            )

    def compilation_passes(
        self,
        market_id: str,
        rule_text_sha256: str,
    ) -> list[dict[str, Any]]:
        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT pass_index, model, raw_output, normalized_output_json,
                       output_sha256, error, created_at
                FROM compilation_passes
                WHERE market_id=? AND rule_text_sha256=?
                ORDER BY id
                """,
                (market_id, rule_text_sha256),
            ).fetchall()
        return [
            {
                "pass_index": int(row["pass_index"]),
                "model": str(row["model"]),
                "raw_output": str(row["raw_output"]),
                "normalized_output": (
                    json.loads(str(row["normalized_output_json"]))
                    if row["normalized_output_json"]
                    else None
                ),
                "output_sha256": row["output_sha256"],
                "error": str(row["error"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def save_claim(self, claim: EvidenceClaim) -> EvidenceClaim:
        validated = EvidenceClaim.from_dict(claim.as_dict())
        payload = canonical_json(validated.as_dict())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT claim_sha256, claim_json
                FROM evidence_claims
                WHERE rule_spec_sha256=? AND article_id=?
                ORDER BY created_at
                LIMIT 1
                """,
                (validated.rule_spec_sha256, validated.article_id),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["claim_sha256"]) != validated.claim_sha256
                    or str(existing["claim_json"]) != payload
                ):
                    raise ValueError(
                        "immutable EvidenceClaim conflict for "
                        f"{validated.rule_spec_sha256}:{validated.article_id}"
                    )
                return EvidenceClaim.from_dict(
                    json.loads(str(existing["claim_json"]))
                )
            connection.execute(
                """
                INSERT INTO evidence_claims(
                    claim_sha256, market_id, rule_spec_sha256, article_id,
                    claim_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    validated.claim_sha256,
                    validated.market_id,
                    validated.rule_spec_sha256,
                    validated.article_id,
                    payload,
                    validated.extracted_at,
                ),
            )
        return validated

    def load_claim(
        self,
        spec_sha256: str,
        article_id: str,
    ) -> EvidenceClaim | None:
        with self._connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT claim_json FROM evidence_claims
                WHERE rule_spec_sha256=? AND article_id=?
                ORDER BY created_at
                LIMIT 1
                """,
                (spec_sha256, article_id),
            ).fetchone()
        if row is None:
            return None
        return EvidenceClaim.from_dict(json.loads(str(row["claim_json"])))

    def claims_for_spec(self, spec_sha256: str) -> list[EvidenceClaim]:
        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT claim_json FROM evidence_claims
                WHERE rule_spec_sha256=?
                ORDER BY created_at
                """,
                (spec_sha256,),
            ).fetchall()
        return [
            EvidenceClaim.from_dict(json.loads(str(row["claim_json"])))
            for row in rows
        ]

    def save_extraction_pass(self, item: ExtractionPass) -> None:
        normalized_json = (
            canonical_json(item.normalized_output)
            if item.normalized_output is not None
            else None
        )
        created_at = item.created_at or _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO extraction_passes(
                    market_id, rule_spec_sha256, article_id, pass_index,
                    model, raw_output, normalized_output_json, output_sha256,
                    error, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.market_id,
                    item.rule_spec_sha256,
                    item.article_id,
                    item.pass_index,
                    item.model,
                    item.raw_output,
                    normalized_json,
                    sha256_json(item.normalized_output)
                    if item.normalized_output is not None
                    else None,
                    item.error,
                    created_at,
                ),
            )

    def extraction_passes(
        self,
        spec_sha256: str,
        article_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT pass_index, model, raw_output, normalized_output_json,
                       output_sha256, error, created_at
                FROM extraction_passes
                WHERE rule_spec_sha256=? AND article_id=?
                ORDER BY id
                """,
                (spec_sha256, article_id),
            ).fetchall()
        return [
            {
                "pass_index": int(row["pass_index"]),
                "model": str(row["model"]),
                "raw_output": str(row["raw_output"]),
                "normalized_output": (
                    json.loads(str(row["normalized_output_json"]))
                    if row["normalized_output_json"]
                    else None
                ),
                "output_sha256": row["output_sha256"],
                "error": str(row["error"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def save_evaluation(self, evaluation: RuleEvaluation) -> RuleEvaluation:
        validated = RuleEvaluation.from_dict(evaluation.as_dict())
        payload = canonical_json(validated.as_dict())
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT evaluation_json FROM rule_evaluations
                WHERE evaluation_sha256=?
                """,
                (validated.evaluation_sha256,),
            ).fetchone()
            if existing is not None:
                if str(existing["evaluation_json"]) != payload:
                    raise ValueError(
                        "immutable RuleEvaluation conflict "
                        f"{validated.evaluation_sha256}"
                    )
                return RuleEvaluation.from_dict(
                    json.loads(str(existing["evaluation_json"]))
                )
            connection.execute(
                """
                INSERT INTO rule_evaluations(
                    evaluation_sha256, market_id, rule_spec_sha256,
                    evaluation_json, created_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    validated.evaluation_sha256,
                    validated.market_id,
                    validated.rule_spec_sha256,
                    payload,
                    validated.evaluated_at,
                ),
            )
        return validated

    def evaluations_for_spec(
        self,
        spec_sha256: str,
    ) -> list[RuleEvaluation]:
        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT evaluation_json FROM rule_evaluations
                WHERE rule_spec_sha256=?
                ORDER BY created_at
                """,
                (spec_sha256,),
            ).fetchall()
        return [
            RuleEvaluation.from_dict(json.loads(str(row["evaluation_json"])))
            for row in rows
        ]

    def save_proof(self, proof: DecisionProof) -> DecisionProof:
        validated = DecisionProof.from_dict(proof.as_dict())
        payload = canonical_json(validated.as_dict())
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT proof_json FROM decision_proofs WHERE proof_sha256=?
                """,
                (validated.proof_sha256,),
            ).fetchone()
            if existing is not None:
                if str(existing["proof_json"]) != payload:
                    raise ValueError(
                        f"immutable DecisionProof conflict {validated.proof_sha256}"
                    )
                return DecisionProof.from_dict(
                    json.loads(str(existing["proof_json"]))
                )
            connection.execute(
                """
                INSERT INTO decision_proofs(
                    proof_sha256, market_id, rule_spec_sha256,
                    evaluation_sha256, proof_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    validated.proof_sha256,
                    validated.market_id,
                    validated.rule_spec_sha256,
                    validated.evaluation_sha256,
                    payload,
                    validated.created_at,
                ),
            )
        return validated

    def proofs_for_market(self, market_id: str) -> list[DecisionProof]:
        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT proof_json FROM decision_proofs
                WHERE market_id=?
                ORDER BY created_at
                """,
                (market_id,),
            ).fetchall()
        return [
            DecisionProof.from_dict(json.loads(str(row["proof_json"])))
            for row in rows
        ]

    def status(self) -> dict[str, Any]:
        with self._connect(read_only=True) as connection:
            specs = connection.execute(
                "SELECT COUNT(*) AS count FROM rule_specs"
            ).fetchone()
            passes = connection.execute(
                "SELECT COUNT(*) AS count FROM compilation_passes"
            ).fetchone()
            claims = connection.execute(
                "SELECT COUNT(*) AS count FROM evidence_claims"
            ).fetchone()
            extraction_passes = connection.execute(
                "SELECT COUNT(*) AS count FROM extraction_passes"
            ).fetchone()
            evaluations = connection.execute(
                "SELECT COUNT(*) AS count FROM rule_evaluations"
            ).fetchone()
            proofs = connection.execute(
                "SELECT COUNT(*) AS count FROM decision_proofs"
            ).fetchone()
        return {
            "path": str(self.path),
            "specs": int(specs["count"]) if specs else 0,
            "compilation_passes": int(passes["count"]) if passes else 0,
            "evidence_claims": int(claims["count"]) if claims else 0,
            "extraction_passes": (
                int(extraction_passes["count"]) if extraction_passes else 0
            ),
            "evaluations": int(evaluations["count"]) if evaluations else 0,
            "decision_proofs": int(proofs["count"]) if proofs else 0,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["CompilationPass", "ExtractionPass", "RuleStore"]
