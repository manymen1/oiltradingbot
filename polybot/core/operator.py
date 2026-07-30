from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.config import SETTINGS
from polybot.gamma import MarketMeta
from polybot.log import log_event

from .execution import LivePosition, TradingAdapter


VALID_MODES = {"off", "alert_only", "dry_run", "live"}
TRADE_ACTIONS = {
    "SELL_NO_CONDITIONAL_BUY_YES",
    "SELL_NO_BUY_YES",
    "TRIM_YES",
    "EXIT_YES_ONLY",
    "EXIT_YES_OPTIONAL_BUY_NO",
    "ROTATE_YES",
    "ENTER_YES",
    "ENTER_NO",
    "TRIM_HELD",
    "EXIT_HELD",
}


@dataclass(frozen=True)
class OperatorStatus:
    position_id: str
    operator_dir: str
    config_hash: str
    config_acknowledged: bool
    config_ack_path: str
    global_mode: str
    position_mode: str
    effective_mode: str
    blockers: list[str]
    warnings: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    mode: str
    reason: str


def default_operator_dir(config: Any) -> Path:
    return config.data_dir.parent / "operator"


def position_id_from_config_path(config_path: Path) -> str:
    return config_path.stem


def config_hash(config_path: Path) -> str:
    return hashlib.sha256(config_path.read_bytes()).hexdigest()


class OperatorGate:
    def __init__(self, config_path: Path, config: Any, *, operator_dir: Path | None = None, position_id: str | None = None) -> None:
        self.config_path = config_path
        self.config = config
        self.operator_dir = operator_dir or default_operator_dir(config)
        self.position_id = position_id or position_id_from_config_path(config_path)
        self._last_block_reason: str | None = None

    def status(self, *, live_requested: bool) -> OperatorStatus:
        digest = config_hash(self.config_path)
        global_mode = _read_mode(self.operator_dir / "global_mode.json", default="live")
        position_mode = _read_mode(self.operator_dir / "positions" / f"{self.position_id}.mode", default="alert_only")
        effective_mode = _combine_modes(global_mode, position_mode)
        ack_path = self.ack_path(digest)
        blockers: list[str] = []
        warnings: list[str] = []
        if live_requested:
            try:
                classifier_passes = int(getattr(self.config.classifier, "passes", 1))
            except (TypeError, ValueError):
                classifier_passes = 0
            if effective_mode != "live":
                blockers.append(f"operator_mode_{effective_mode}")
            if not ack_path.exists():
                blockers.append("live_config_hash_not_acknowledged")
            if self.config.execution.dry_run:
                blockers.append("config_execution_dry_run_true")
            if classifier_passes < 2:
                blockers.append("live_classifier_requires_two_passes")
            if getattr(self.config.classifier, "require_pass_agreement", False) is not True:
                blockers.append("live_classifier_requires_pass_agreement")
        telegram_missing = not _telegram_configured()
        anthropic_missing = not _anthropic_configured() and self.config.classifier.provider == "anthropic"
        if live_requested and self.config.safety.degraded_mode_alert and telegram_missing:
            blockers.append("telegram_not_configured")
        elif telegram_missing:
            warnings.append("telegram_not_configured")
        if live_requested and anthropic_missing:
            blockers.append("anthropic_not_configured")
        elif anthropic_missing:
            warnings.append("anthropic_not_configured")
        # Subscription-auth classification: the CLI binary must exist on this
        # host (the API key is NOT required -- and is deliberately stripped
        # from the classifier subprocess env).
        from polybot.core.claude_cli import claude_cli_available, is_claude_cli_provider

        if is_claude_cli_provider(self.config.classifier.provider) and not claude_cli_available(
            getattr(self.config.classifier, "cli_binary", "claude")
        ):
            if live_requested:
                blockers.append("claude_cli_not_installed")
            else:
                warnings.append("claude_cli_not_installed")
        from polybot.core.codex_cli import (
            codex_cli_available,
            is_codex_cli_provider,
        )

        if is_codex_cli_provider(
            self.config.classifier.provider
        ) and not codex_cli_available(
            getattr(self.config.classifier, "cli_binary", "codex")
        ):
            if live_requested:
                blockers.append("codex_cli_not_installed")
            else:
                warnings.append("codex_cli_not_installed")
        return OperatorStatus(
            position_id=self.position_id,
            operator_dir=str(self.operator_dir),
            config_hash=digest,
            config_acknowledged=ack_path.exists(),
            config_ack_path=str(ack_path),
            global_mode=global_mode,
            position_mode=position_mode,
            effective_mode=effective_mode,
            blockers=blockers,
            warnings=warnings,
        )

    def ack_path(self, digest: str | None = None) -> Path:
        digest = digest or config_hash(self.config_path)
        return self.operator_dir / "live_ack" / self.position_id / f"{digest}.json"

    def write_ack(self, *, note: str = "") -> dict[str, Any]:
        digest = config_hash(self.config_path)
        path = self.ack_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "position_id": self.position_id,
            "config_path": str(self.config_path),
            "config_hash": digest,
            "acked_at": datetime.now(timezone.utc).isoformat(),
            "note": note,
        }
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        log_event("operator_live_config_ack", **record)
        return {**record, "ack_path": str(path)}

    def set_position_mode(self, mode: str) -> dict[str, Any]:
        normalized = _normalize_mode(mode)
        path = self.operator_dir / "positions" / f"{self.position_id}.mode"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(normalized + "\n", encoding="utf-8")
        record = {
            "position_id": self.position_id,
            "mode": normalized,
            "path": str(path),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        log_event("operator_position_mode_set", **record)
        return record

    def current_mode(self) -> str:
        return self.status(live_requested=False).effective_mode

    def check(self, decision: Any, *, live_requested: bool) -> GateResult:
        if decision.action not in TRADE_ACTIONS:
            return GateResult(True, self.current_mode(), "non_trade_action")
        status = self.status(live_requested=live_requested)
        if status.effective_mode == "off":
            return GateResult(False, status.effective_mode, "operator_mode_off")
        if status.effective_mode == "dry_run" and not live_requested:
            return GateResult(True, status.effective_mode, "operator_allows_dry_run_execution")
        if status.effective_mode in {"alert_only", "dry_run"}:
            return GateResult(False, status.effective_mode, f"operator_mode_{status.effective_mode}")
        if live_requested and status.blockers:
            return GateResult(False, status.effective_mode, ",".join(status.blockers))
        return GateResult(True, status.effective_mode, "operator_allows_execution")

    def log_block_once(self, result: GateResult, decision: Any) -> bool:
        key = f"{result.mode}:{result.reason}:{decision.action}"
        if self._last_block_reason == key:
            return False
        self._last_block_reason = key
        log_event("operator_execution_blocked", mode=result.mode, reason=result.reason, action=decision.action)
        return True


def build_preflight(
    *,
    config_path: Path,
    config: Any,
    market: MarketMeta,
    adapter: TradingAdapter,
    live_requested: bool,
    gate: OperatorGate | None = None,
) -> dict[str, Any]:
    gate = gate or OperatorGate(config_path, config)
    status = gate.status(live_requested=live_requested)
    result: dict[str, Any] = {
        "status": "blocked" if status.blockers else "ok",
        "operator": status.as_dict(),
        "config": str(config_path),
        "market": {
            "slug": config.market.slug,
            "target_leg": config.market.target_leg,
            "held_side": config.market.held_side,
            "condition_id": market.condition_id,
            "question": market.question,
            "tradeable": market.tradeable(),
            "active": market.active,
            "closed": market.closed,
            "accepting_orders": market.accepting_orders,
        },
        "tokens": {
            "yes_token_id": market.yes_token_id,
            "no_token_id": market.no_token_id,
            "expected_yes_token_id": config.position.expected_yes_token_id,
            "expected_no_token_id": config.position.expected_no_token_id,
            "token_mapping_matches_config": (
                (not config.position.expected_yes_token_id or config.position.expected_yes_token_id == market.yes_token_id)
                and (not config.position.expected_no_token_id or config.position.expected_no_token_id == market.no_token_id)
            ),
        },
        "clob_settings": {
            "host": SETTINGS.clob_host,
            "chain_id": SETTINGS.chain_id,
            "signature_type": SETTINGS.signature_type,
            "funder_address": SETTINGS.funder_address,
            "has_private_key": bool(SETTINGS.private_key),
            "has_api_creds": bool(SETTINGS.clob_api_key and SETTINGS.clob_secret and SETTINGS.clob_passphrase),
        },
        "environment": {
            "telegram_configured": _telegram_configured(),
            "anthropic_configured": _anthropic_configured(),
        },
    }
    try:
        position = adapter.query_live_position(market.yes_token_id, market.no_token_id)
        result["live_position"] = _position_dict(position)
    except Exception as exc:
        result["live_position_error"] = str(exc)
        status.blockers.append("live_position_query_failed")
        result["status"] = "blocked"
    for token_id, label in ((market.yes_token_id, "yes"), (market.no_token_id, "no")):
        try:
            result[f"{label}_best_bid"] = adapter.yes_best_bid(token_id) if label == "yes" else None
        except Exception as exc:
            result[f"{label}_best_bid_error"] = str(exc)
        try:
            result[f"{label}_best_ask"] = adapter.yes_best_ask(token_id) if label == "yes" else adapter.no_best_ask(token_id)
        except Exception as exc:
            result[f"{label}_best_ask_error"] = str(exc)
    if live_requested and not market.tradeable():
        status.blockers.append("market_not_tradeable")
        result["status"] = "blocked"
    if not result["tokens"]["token_mapping_matches_config"]:
        status.blockers.append("token_mapping_mismatch")
        result["status"] = "blocked"
    result["operator"] = status.as_dict()
    result["status"] = "blocked" if status.blockers else "ok"
    return result


def _position_dict(position: LivePosition) -> dict[str, Any]:
    return {
        "yes_token_id": position.yes_token_id,
        "no_token_id": position.no_token_id,
        "yes_shares": position.yes_shares,
        "no_shares": position.no_shares,
    }


def _read_mode(path: Path, *, default: str) -> str:
    if not path.exists():
        return _normalize_mode(default)
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return _normalize_mode(default)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return _normalize_mode(text)
    if isinstance(value, str):
        return _normalize_mode(value)
    if isinstance(value, dict):
        return _normalize_mode(str(value.get("mode") or default))
    raise ValueError(f"{path} must contain a mode string or object")


def _normalize_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized not in VALID_MODES:
        raise ValueError(f"operator mode must be one of {sorted(VALID_MODES)}, got {mode!r}")
    return normalized


def _combine_modes(global_mode: str, position_mode: str) -> str:
    order = {"off": 0, "alert_only": 1, "dry_run": 2, "live": 3}
    return min(global_mode, position_mode, key=lambda mode: order[mode])


def _telegram_configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_ID")))


def _anthropic_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("LLM_API_KEY"))

__all__ = [
    "GateResult",
    "OperatorGate",
    "OperatorStatus",
    "build_preflight",
    "_anthropic_configured",
    "_telegram_configured",
]
