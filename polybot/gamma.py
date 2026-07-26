from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from .config import SETTINGS
from .core.fees import FeeScheduleSnapshot


def _decode_json_list(value: Any, field: str) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} is not valid JSON: {value!r}") from exc
        if isinstance(parsed, list):
            return parsed
    raise ValueError(f"{field} is not a list: {value!r}")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MarketMeta:
    event_slug: str
    market_slug: str
    condition_id: str
    question: str
    description: str
    resolution_source: str
    outcomes: list[str]
    outcome_prices: list[float]
    yes_token_id: str
    no_token_id: str
    tick_size: str
    neg_risk: bool
    active: bool
    closed: bool
    accepting_orders: bool
    volume: float
    liquidity: float
    fee_schedule: FeeScheduleSnapshot | None = None
    fee_schedule_error: str = ""
    revisable_rule: bool = False

    @property
    def token_ids(self) -> list[str]:
        return [self.yes_token_id, self.no_token_id]

    def tradeable(self) -> bool:
        return self.active and not self.closed and self.accepting_orders


def fetch_event_by_slug(slug: str, gamma_host: str = SETTINGS.gamma_host) -> dict[str, Any]:
    url = f"{gamma_host.rstrip('/')}/events/slug/{slug}"
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"unexpected Gamma event response for {slug}")
    return data


def market_from_gamma(
    event: dict[str, Any],
    market: dict[str, Any],
    *,
    observed_at: str | None = None,
) -> MarketMeta:
    token_ids = [str(item) for item in _decode_json_list(market.get("clobTokenIds"), "clobTokenIds")]
    if len(token_ids) != 2:
        raise ValueError(f"expected two clobTokenIds, got {token_ids!r}")
    outcomes = [str(item) for item in _decode_json_list(market.get("outcomes"), "outcomes")]
    prices = [_as_float(item) for item in _decode_json_list(market.get("outcomePrices", "[]"), "outcomePrices")]
    tick = str(market.get("orderPriceMinTickSize") or market.get("tickSize") or "0.01")
    description = str(market.get("description") or "")
    source = str(market.get("resolutionSource") or event.get("resolutionSource") or "")
    fee_schedule: FeeScheduleSnapshot | None = None
    fee_schedule_error = ""
    try:
        fee_schedule = FeeScheduleSnapshot.from_gamma(
            fees_enabled=market.get("feesEnabled"),
            fee_schedule=market.get("feeSchedule"),
            observed_at=(
                observed_at
                or datetime.now(timezone.utc).isoformat()
            ),
        )
    except ValueError as exc:
        fee_schedule_error = str(exc)
    return MarketMeta(
        event_slug=str(event.get("slug") or ""),
        market_slug=str(market.get("slug") or ""),
        condition_id=str(market.get("conditionId") or ""),
        question=str(market.get("question") or ""),
        description=description,
        resolution_source=source,
        outcomes=outcomes,
        outcome_prices=prices,
        yes_token_id=token_ids[0],
        no_token_id=token_ids[1],
        tick_size=tick,
        neg_risk=bool(market.get("negRisk") or event.get("negRisk")),
        active=bool(market.get("active")),
        closed=bool(market.get("closed")),
        accepting_orders=bool(market.get("acceptingOrders", False)),
        volume=_as_float(market.get("volume") or market.get("volumeNum")),
        liquidity=_as_float(market.get("liquidity")),
        fee_schedule=fee_schedule,
        fee_schedule_error=fee_schedule_error,
        revisable_rule=("revision" in description.lower() or "revisions" in description.lower()),
    )


def markets_for_event_slug(slug: str) -> list[MarketMeta]:
    event = fetch_event_by_slug(slug)
    markets = event.get("markets")
    if not isinstance(markets, list):
        raise ValueError(f"event {slug} has no markets list")
    return [market_from_gamma(event, market) for market in markets if isinstance(market, dict)]


def select_market(slug: str, question_contains: str | None = None) -> MarketMeta:
    markets = markets_for_event_slug(slug)
    if question_contains:
        lowered = question_contains.lower()
        markets = [
            market
            for market in markets
            if lowered in market.question.lower() or lowered in market.market_slug.lower()
        ]
    if len(markets) != 1:
        raise ValueError(f"expected one market for {slug!r}, matched {len(markets)}")
    return markets[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect Gamma metadata for a Polymarket event slug.")
    parser.add_argument("slug")
    args = parser.parse_args()
    markets = markets_for_event_slug(args.slug)
    print(json.dumps([market.__dict__ for market in markets], indent=2))


if __name__ == "__main__":
    main()
