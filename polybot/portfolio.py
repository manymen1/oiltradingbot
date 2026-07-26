from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from polybot.gamma import MarketMeta, select_market
from polybot.core.config_validation import (
    load_yaml_object,
    reject_unknown_dataclass_keys,
    reject_unknown_keys,
    require_number,
    require_text,
)
from polybot.core.execution import LiveClobTradingAdapter, TradingAdapter
from polybot.core.operator import OperatorGate
from polybot.iran.config import ExecutionConfig, IranBotConfig, MarketConfig, PositionConfig


@dataclass(frozen=True)
class PortfolioPosition:
    id: str
    event_slug: str
    held_side: str
    strategy: str = "manual_watch"
    mode: str = "alert_only"
    target_leg: str = ""
    expected_question_contains: str = ""
    expected_yes_token_id: str = ""
    expected_no_token_id: str = ""
    max_yes_shares_to_sell: float = 0.0
    max_no_shares_to_sell: float = 0.0
    max_yes_usd_to_buy: float = 0.0
    max_no_usd_to_buy: float = 0.0
    data_dir: str = ""


@dataclass(frozen=True)
class PortfolioConfig:
    positions: list[PortfolioPosition] = field(default_factory=list)


def load_portfolio_config(path: Path) -> PortfolioConfig:
    raw = load_yaml_object(path)
    reject_unknown_dataclass_keys(raw, PortfolioConfig, context=str(path))
    positions_raw = raw.get("positions", [])
    if not isinstance(positions_raw, list):
        raise ValueError("positions must be a list")
    positions = [_position_from_raw(item) for item in positions_raw]
    ids = [position.id for position in positions]
    duplicate_ids = sorted({position_id for position_id in ids if ids.count(position_id) > 1})
    if duplicate_ids:
        raise ValueError(f"duplicate position ids: {duplicate_ids}")
    return PortfolioConfig(positions=positions)


def snapshot_portfolio(config_path: Path, *, adapter: TradingAdapter | None = None, position_id: str | None = None) -> dict[str, Any]:
    config = load_portfolio_config(config_path)
    selected = [position for position in config.positions if position_id is None or position.id == position_id]
    if position_id is not None and not selected:
        raise ValueError(f"position id not found: {position_id}")
    live_adapter = adapter or LiveClobTradingAdapter()
    snapshots = [snapshot_position(config_path, position, live_adapter) for position in selected]
    return {
        "config": str(config_path),
        "positions": snapshots,
        "summary": {
            "count": len(snapshots),
            "errors": sum(1 for item in snapshots if item.get("errors")),
            "live_yes_shares": sum(_safe_float(item.get("live_position", {}).get("yes_shares")) for item in snapshots),
            "live_no_shares": sum(_safe_float(item.get("live_position", {}).get("no_shares")) for item in snapshots),
        },
    }


def snapshot_position(config_path: Path, position: PortfolioPosition, adapter: TradingAdapter) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    snapshot: dict[str, Any] = {
        "id": position.id,
        "held_side": position.held_side,
        "strategy": position.strategy,
        "configured_mode": position.mode,
        "limits": {
            "max_yes_shares_to_sell": position.max_yes_shares_to_sell,
            "max_no_shares_to_sell": position.max_no_shares_to_sell,
            "max_yes_usd_to_buy": position.max_yes_usd_to_buy,
            "max_no_usd_to_buy": position.max_no_usd_to_buy,
        },
        "errors": errors,
    }
    try:
        market = _resolve_market(position)
        snapshot["market"] = _market_dict(market)
        snapshot["tokens"] = {
            "yes_token_id": market.yes_token_id,
            "no_token_id": market.no_token_id,
            "expected_yes_token_id": position.expected_yes_token_id,
            "expected_no_token_id": position.expected_no_token_id,
            "token_mapping_matches_config": _token_mapping_matches(position, market),
        }
    except Exception as exc:
        errors.append({"phase": "market", "error": str(exc)})
        snapshot["status"] = _position_status(snapshot)
        return snapshot

    gate_config = _iran_config_from_position(position)
    gate = OperatorGate(config_path, gate_config, position_id=position.id)
    snapshot["operator"] = gate.status(live_requested=True).as_dict()
    try:
        live_position = adapter.query_live_position(market.yes_token_id, market.no_token_id)
        snapshot["live_position"] = asdict(live_position)
    except Exception as exc:
        errors.append({"phase": "live_position", "error": str(exc)})
    try:
        snapshot["open_orders"] = _normalize_open_orders(adapter.open_orders_for_market(market.condition_id))
    except Exception as exc:
        errors.append({"phase": "open_orders", "error": str(exc)})
        snapshot["open_orders"] = []
    snapshot["book"] = {}
    for label, token_id in (("yes", market.yes_token_id), ("no", market.no_token_id)):
        snapshot["book"][label] = _book_snapshot(adapter, label, token_id)
    snapshot["status"] = _position_status(snapshot)
    return snapshot


def print_portfolio_snapshot(config_path: Path, *, position_id: str | None = None) -> int:
    print(json.dumps(snapshot_portfolio(config_path, position_id=position_id), indent=2, sort_keys=True))
    return 0


def _position_from_raw(raw: Any) -> PortfolioPosition:
    if not isinstance(raw, dict):
        raise ValueError("each position must be an object")
    reject_unknown_keys(
        raw,
        {*PortfolioPosition.__dataclass_fields__, "market_slug"},
        context="portfolio position",
    )
    event_slug = raw.get("event_slug") or raw.get("market_slug")
    if not event_slug:
        raise ValueError("position requires event_slug")
    position_id = raw.get("id")
    if not position_id:
        raise ValueError("position requires id")
    held_side = _held_side(raw.get("held_side"))
    if held_side not in {"YES", "NO"}:
        raise ValueError(f"position {position_id} held_side must be YES or NO")
    position = PortfolioPosition(
        id=str(position_id),
        event_slug=str(event_slug),
        held_side=held_side,
        strategy=str(raw.get("strategy") or "manual_watch"),
        mode=str(raw.get("mode") or "alert_only"),
        target_leg=str(raw.get("target_leg") or ""),
        expected_question_contains=str(raw.get("expected_question_contains") or raw.get("target_leg") or ""),
        expected_yes_token_id=_string_field(raw.get("expected_yes_token_id")),
        expected_no_token_id=_string_field(raw.get("expected_no_token_id")),
        max_yes_shares_to_sell=float(raw.get("max_yes_shares_to_sell") or 0.0),
        max_no_shares_to_sell=float(raw.get("max_no_shares_to_sell") or 0.0),
        max_yes_usd_to_buy=float(raw.get("max_yes_usd_to_buy") or 0.0),
        max_no_usd_to_buy=float(raw.get("max_no_usd_to_buy") or 0.0),
        data_dir=str(raw.get("data_dir") or ""),
    )
    _validate_portfolio_position(position)
    return position


def _validate_portfolio_position(position: PortfolioPosition) -> None:
    require_text(position.id, "position.id")
    require_text(position.event_slug, f"position {position.id}.event_slug")
    require_text(position.held_side, f"position {position.id}.held_side")
    require_text(position.strategy, f"position {position.id}.strategy")
    if position.mode not in {"off", "alert_only", "dry_run", "live"}:
        raise ValueError(
            f"position {position.id}.mode must be off, alert_only, dry_run, or live"
        )
    for field_name in (
        "target_leg",
        "expected_question_contains",
        "expected_yes_token_id",
        "expected_no_token_id",
        "data_dir",
    ):
        require_text(
            getattr(position, field_name),
            f"position {position.id}.{field_name}",
            allow_empty=True,
        )
    if (
        position.expected_yes_token_id
        and position.expected_yes_token_id == position.expected_no_token_id
    ):
        raise ValueError(f"position {position.id} YES and NO token ids must be distinct")
    for field_name in (
        "max_yes_shares_to_sell",
        "max_no_shares_to_sell",
        "max_yes_usd_to_buy",
        "max_no_usd_to_buy",
    ):
        require_number(
            getattr(position, field_name),
            f"position {position.id}.{field_name}",
            minimum=0,
        )


def _held_side(value: Any) -> str:
    if value is True:
        return "YES"
    if value is False:
        return "NO"
    return str(value or "").upper()


def _string_field(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return str(value or "")


def _resolve_market(position: PortfolioPosition) -> MarketMeta:
    market = select_market(position.event_slug, position.expected_question_contains or None)
    if not _token_mapping_matches(position, market):
        raise ValueError("configured token ids do not match Gamma")
    return market


def _iran_config_from_position(position: PortfolioPosition) -> IranBotConfig:
    target_leg = position.target_leg or position.expected_question_contains
    return IranBotConfig(
        market=MarketConfig(
            slug=position.event_slug,
            target_leg=target_leg,
            held_side=position.held_side,
            expected_question_contains=position.expected_question_contains,
        ),
        position=PositionConfig(
            expected_yes_token_id=position.expected_yes_token_id,
            expected_no_token_id=position.expected_no_token_id,
            max_yes_shares_to_sell=position.max_yes_shares_to_sell,
            max_no_shares_to_sell=position.max_no_shares_to_sell,
            max_yes_usd_to_buy=position.max_yes_usd_to_buy,
            max_no_usd_to_buy=position.max_no_usd_to_buy,
        ),
        execution=ExecutionConfig(dry_run=False),
        data_dir=Path(position.data_dir or f"data/{position.id}"),
    )


def _token_mapping_matches(position: PortfolioPosition, market: MarketMeta) -> bool:
    return (
        (not position.expected_yes_token_id or position.expected_yes_token_id == market.yes_token_id)
        and (not position.expected_no_token_id or position.expected_no_token_id == market.no_token_id)
    )


def _market_dict(market: MarketMeta) -> dict[str, Any]:
    return {
        "event_slug": market.event_slug,
        "market_slug": market.market_slug,
        "condition_id": market.condition_id,
        "question": market.question,
        "tradeable": market.tradeable(),
        "active": market.active,
        "closed": market.closed,
        "accepting_orders": market.accepting_orders,
    }


def _normalize_open_orders(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        data = value.get("data") or value.get("orders") or value.get("results")
        if isinstance(data, list):
            return data
        return [value]
    return []


def _book_snapshot(adapter: TradingAdapter, label: str, token_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        result["best_ask"] = adapter.yes_best_ask(token_id) if label == "yes" else adapter.no_best_ask(token_id)
    except Exception as exc:
        result["best_ask_error"] = str(exc)
    try:
        result["best_bid"] = adapter.yes_best_bid(token_id) if label == "yes" else None
    except Exception as exc:
        result["best_bid_error"] = str(exc)
    return result


def _position_status(snapshot: dict[str, Any]) -> str:
    if snapshot.get("errors"):
        return "needs_attention"
    operator = snapshot.get("operator", {})
    blockers = operator.get("blockers") if isinstance(operator, dict) else []
    if blockers:
        return "blocked"
    return "ok"


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
