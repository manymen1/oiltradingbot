from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from .fees import FeeScheduleSnapshot
from .holdings import _atomic_json_write


@dataclass(frozen=True)
class LivePosition:
    yes_token_id: str
    no_token_id: str
    no_shares: float
    yes_shares: float = 0.0


@dataclass(frozen=True)
class Fill:
    filled_shares: float
    raw: Any = None


class TradingAdapter(Protocol):
    def query_live_position(self, yes_token_id: str, no_token_id: str) -> LivePosition:
        ...

    def cancel_open_orders_for_market(self, condition_id: str) -> Any:
        ...

    def open_orders_for_market(self, condition_id: str) -> Any:
        ...

    def sell_no_fak(self, no_token_id: str, shares: float, min_price: float) -> Any:
        ...

    def sell_yes_fak(self, yes_token_id: str, shares: float, min_price: float) -> Any:
        ...

    def buy_yes_fak(self, yes_token_id: str, usd: float, max_price: float) -> Any:
        ...

    def buy_no_fak(self, no_token_id: str, usd: float, max_price: float) -> Any:
        ...

    def verify_fill(self, result: Any, token_id: str) -> Fill:
        ...

    def yes_best_ask(self, yes_token_id: str) -> float | None:
        ...

    def no_best_ask(self, no_token_id: str) -> float | None:
        ...

    def no_best_bid(self, no_token_id: str) -> float | None:
        ...

    def yes_best_bid(self, yes_token_id: str) -> float | None:
        ...


class PaperQuoteProvider(Protocol):
    def quote_snapshot(self, token_id: str) -> dict[str, Any]:
        ...

    def yes_best_ask(self, yes_token_id: str) -> float | None:
        ...

    def yes_best_bid(self, yes_token_id: str) -> float | None:
        ...


class DryRunTradingAdapter:
    def __init__(
        self,
        no_shares: float = 1.0,
        yes_shares: float = 1.0,
        yes_ask: float = 0.50,
        no_ask: float = 0.50,
        yes_bid: float = 0.50,
    ):
        self.no_shares = no_shares
        self.yes_shares = yes_shares
        self.yes_ask_value = yes_ask
        self.no_ask_value = no_ask
        self.yes_bid_value = yes_bid

    def query_live_position(self, yes_token_id: str, no_token_id: str) -> LivePosition:
        return LivePosition(yes_token_id=yes_token_id, no_token_id=no_token_id, no_shares=self.no_shares, yes_shares=self.yes_shares)

    def cancel_open_orders_for_market(self, condition_id: str) -> dict[str, Any]:
        return {"dry_run": True, "condition_id": condition_id}

    def open_orders_for_market(self, condition_id: str) -> list[Any]:
        return []

    def sell_no_fak(self, no_token_id: str, shares: float, min_price: float) -> dict[str, Any]:
        return {"dry_run": True, "side": "SELL", "token_id": no_token_id, "shares": shares, "min_price": min_price}

    def sell_yes_fak(self, yes_token_id: str, shares: float, min_price: float) -> dict[str, Any]:
        return {"dry_run": True, "side": "SELL", "token_id": yes_token_id, "shares": shares, "min_price": min_price}

    def buy_yes_fak(self, yes_token_id: str, usd: float, max_price: float) -> dict[str, Any]:
        return {"dry_run": True, "side": "BUY", "token_id": yes_token_id, "usd": usd, "max_price": max_price}

    def buy_no_fak(self, no_token_id: str, usd: float, max_price: float) -> dict[str, Any]:
        return {"dry_run": True, "side": "BUY", "token_id": no_token_id, "usd": usd, "max_price": max_price}

    def verify_fill(self, result: Any, token_id: str) -> Fill:
        if isinstance(result, dict) and result.get("side") == "SELL":
            return Fill(filled_shares=float(result["shares"]), raw=result)
        if isinstance(result, dict) and result.get("side") == "BUY":
            return Fill(filled_shares=float(result["usd"]) / float(result["max_price"]), raw=result)
        return Fill(filled_shares=0.0, raw=result)

    def yes_best_ask(self, yes_token_id: str) -> float | None:
        return self.yes_ask_value

    def no_best_ask(self, no_token_id: str) -> float | None:
        return self.no_ask_value

    def no_best_bid(self, no_token_id: str) -> float | None:
        return max(0.0, min(1.0, 1.0 - self.yes_ask_value))

    def yes_best_bid(self, yes_token_id: str) -> float | None:
        return self.yes_bid_value


class PaperTradingAdapter:
    """Persistent simulated broker filled from public executable quotes.

    Unlike ``DryRunTradingAdapter`` (a deterministic unit-test fixture), this
    adapter mutates token balances, survives restarts, honors price limits, and
    records cash/fee/slippage economics. It deliberately exposes no live order
    client or credentials.
    """

    def __init__(
        self,
        *,
        state_path: Path,
        quote_provider: PaperQuoteProvider,
        token_pairs: list[tuple[str, str]],
        fee_bps: float = 0.0,
        fee_schedules: dict[str, FeeScheduleSnapshot] | None = None,
        slippage_bps: float = 0.0,
        max_book_age_seconds: float = 10.0,
    ) -> None:
        if fee_bps < 0 or slippage_bps < 0 or max_book_age_seconds <= 0:
            raise ValueError("paper fee/slippage bps must be non-negative and max book age must be positive")
        self.state_path = state_path
        self.quote_provider = quote_provider
        self.fee_rate = fee_bps / 10_000.0
        self.fee_schedules = dict(fee_schedules or {})
        if self.fee_schedules and fee_bps != 0:
            raise ValueError(
                "paper adapter cannot combine flat fees with fee schedules"
            )
        self.slippage_rate = slippage_bps / 10_000.0
        self.max_book_age_seconds = max_book_age_seconds
        self._no_to_yes: dict[str, str] = {}
        self._yes_to_no: dict[str, str] = {}
        self._consumed_depth: dict[tuple[str, str, str, int], float] = {}
        self._active_revision: dict[str, str] = {}
        for yes_token_id, no_token_id in token_pairs:
            if not yes_token_id or not no_token_id or yes_token_id == no_token_id:
                raise ValueError("paper token pairs require distinct non-empty YES/NO token ids")
            if no_token_id in self._no_to_yes and self._no_to_yes[no_token_id] != yes_token_id:
                raise ValueError(f"paper NO token {no_token_id} maps to multiple YES tokens")
            if yes_token_id in self._yes_to_no and self._yes_to_no[yes_token_id] != no_token_id:
                raise ValueError(f"paper YES token {yes_token_id} maps to multiple NO tokens")
            self._no_to_yes[no_token_id] = yes_token_id
            self._yes_to_no[yes_token_id] = no_token_id
        self._state = self._load()

    def query_live_position(self, yes_token_id: str, no_token_id: str) -> LivePosition:
        return LivePosition(
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            yes_shares=self._balance(yes_token_id),
            no_shares=self._balance(no_token_id),
        )

    def cancel_open_orders_for_market(self, condition_id: str) -> dict[str, Any]:
        return {"paper": True, "condition_id": condition_id, "cancelled": 0}

    def open_orders_for_market(self, condition_id: str) -> list[Any]:
        return []

    def sell_no_fak(self, no_token_id: str, shares: float, min_price: float) -> dict[str, Any]:
        return self._sell(no_token_id, shares, min_price)

    def sell_yes_fak(self, yes_token_id: str, shares: float, min_price: float) -> dict[str, Any]:
        return self._sell(yes_token_id, shares, min_price)

    def buy_yes_fak(self, yes_token_id: str, usd: float, max_price: float) -> dict[str, Any]:
        return self._buy(yes_token_id, usd, max_price)

    def buy_no_fak(self, no_token_id: str, usd: float, max_price: float) -> dict[str, Any]:
        return self._buy(no_token_id, usd, max_price)

    def verify_fill(self, result: Any, token_id: str) -> Fill:
        if isinstance(result, dict) and result.get("token_id") == token_id:
            return Fill(filled_shares=float(result.get("filled_shares") or 0.0), raw=result)
        return Fill(filled_shares=0.0, raw=result)

    def yes_best_ask(self, yes_token_id: str) -> float | None:
        return self._best_price(yes_token_id, "asks")

    def no_best_ask(self, no_token_id: str) -> float | None:
        return self._best_price(no_token_id, "asks")

    def no_best_bid(self, no_token_id: str) -> float | None:
        return self._best_price(no_token_id, "bids")

    def yes_best_bid(self, yes_token_id: str) -> float | None:
        return self._best_price(yes_token_id, "bids")

    def snapshot(self) -> dict[str, Any]:
        return {
            "balances": dict(self._state["balances"]),
            "net_cash_usd": float(self._state["net_cash_usd"]),
            "fees_usd": float(self._state["fees_usd"]),
            "updated_at": self._state.get("updated_at"),
        }

    def _yes_for_no(self, no_token_id: str) -> str:
        try:
            return self._no_to_yes[no_token_id]
        except KeyError as exc:
            raise ValueError(f"paper NO token {no_token_id} has no YES-token mapping") from exc

    def _buy(self, token_id: str, usd: float, max_price: float) -> dict[str, Any]:
        requested_usd = max(0.0, float(usd))
        if requested_usd <= 0:
            return self._zero_fill(
                "buy_budget_zero",
                token_id,
                requested_usd=requested_usd,
                limit_price=max_price,
            )
        try:
            revision, levels = self._available_levels(token_id, "asks")
        except ValueError as exc:
            return self._zero_fill(
                str(exc),
                token_id,
                requested_usd=requested_usd,
                limit_price=max_price,
            )

        remaining_usd = requested_usd
        gross = 0.0
        fee = 0.0
        filled_shares = 0.0
        fills: list[dict[str, float]] = []
        for index, raw_price, available_shares in levels:
            execution_price = self._execution_price(raw_price, side="BUY")
            if execution_price is None or execution_price > max_price:
                break
            cash_per_share = (
                execution_price
                + self._fee_per_share(token_id, execution_price)
            )
            if cash_per_share <= 0:
                continue
            level_fill = min(available_shares, remaining_usd / cash_per_share)
            if level_fill <= 1e-12:
                break
            level_gross = level_fill * execution_price
            level_fee = self._fee_for_fill(
                token_id,
                level_fill,
                execution_price,
            )
            level_cost = level_gross + level_fee
            if level_cost > remaining_usd:
                # Exchange fees are rounded per fill.  The continuous
                # fee-per-share estimate above can therefore overshoot the
                # cash reservation by a few millionths of a dollar.  Solve
                # against the rounded fee so a paper fill never spends more
                # than the reserved budget.
                low = 0.0
                high = level_fill
                for _ in range(64):
                    candidate = (low + high) / 2.0
                    candidate_cost = (
                        candidate * execution_price
                        + self._fee_for_fill(
                            token_id,
                            candidate,
                            execution_price,
                        )
                    )
                    if candidate_cost <= remaining_usd:
                        low = candidate
                    else:
                        high = candidate
                level_fill = low
                if level_fill <= 1e-12:
                    break
                level_gross = level_fill * execution_price
                level_fee = self._fee_for_fill(
                    token_id,
                    level_fill,
                    execution_price,
                )
                level_cost = level_gross + level_fee
            gross += level_gross
            fee += level_fee
            filled_shares += level_fill
            remaining_usd = max(0.0, remaining_usd - level_cost)
            self._consume_level(token_id, revision, "asks", index, level_fill)
            fills.append(
                {
                    "book_price": raw_price,
                    "execution_price": execution_price,
                    "filled_shares": level_fill,
                }
            )
            if remaining_usd <= 1e-9:
                break

        if filled_shares <= 0:
            return self._zero_fill(
                "buy_book_empty_or_above_cap",
                token_id,
                requested_usd=requested_usd,
                limit_price=max_price,
                book_revision=revision,
            )
        total_cost = gross + fee
        execution_price = gross / filled_shares
        self._set_balance(token_id, self._balance(token_id) + filled_shares)
        self._state["net_cash_usd"] = round(float(self._state["net_cash_usd"]) - total_cost, 8)
        self._state["fees_usd"] = round(float(self._state["fees_usd"]) + fee, 8)
        self._save()
        unfilled_usd = max(0.0, requested_usd - total_cost)
        return {
            "paper": True,
            "side": "BUY",
            "token_id": token_id,
            "requested_usd": requested_usd,
            "execution_price": execution_price,
            "gross_cost_usd": gross,
            "fee_usd": fee,
            "total_cost_usd": total_cost,
            "filled_shares": filled_shares,
            "unfilled_usd": unfilled_usd,
            "fill_fraction": min(1.0, total_cost / requested_usd),
            "partial": unfilled_usd > 1e-6,
            "book_revision": revision,
            "level_fills": fills,
            "balance_after": self._balance(token_id),
        }

    def _sell(self, token_id: str, shares: float, min_price: float) -> dict[str, Any]:
        requested_shares = max(0.0, float(shares))
        available = self._balance(token_id)
        target_shares = min(requested_shares, available)
        if target_shares <= 0:
            return self._zero_fill(
                "sell_balance_empty",
                token_id,
                requested_shares=requested_shares,
                available_shares=available,
                limit_price=min_price,
            )
        try:
            revision, levels = self._available_levels(token_id, "bids")
        except ValueError as exc:
            return self._zero_fill(
                str(exc),
                token_id,
                requested_shares=requested_shares,
                available_shares=available,
                limit_price=min_price,
            )

        remaining_shares = target_shares
        gross = 0.0
        fee = 0.0
        filled_shares = 0.0
        fills: list[dict[str, float]] = []
        for index, raw_price, available_shares in levels:
            execution_price = self._execution_price(raw_price, side="SELL")
            if execution_price is None or execution_price < min_price:
                break
            level_fill = min(available_shares, remaining_shares)
            if level_fill <= 1e-12:
                continue
            gross += level_fill * execution_price
            fee += self._fee_for_fill(
                token_id,
                level_fill,
                execution_price,
            )
            filled_shares += level_fill
            remaining_shares = max(0.0, remaining_shares - level_fill)
            self._consume_level(token_id, revision, "bids", index, level_fill)
            fills.append(
                {
                    "book_price": raw_price,
                    "execution_price": execution_price,
                    "filled_shares": level_fill,
                }
            )
            if remaining_shares <= 1e-9:
                break

        if filled_shares <= 0:
            return self._zero_fill(
                "sell_book_empty_or_below_floor",
                token_id,
                requested_shares=requested_shares,
                available_shares=available,
                limit_price=min_price,
                book_revision=revision,
            )
        proceeds = gross - fee
        execution_price = gross / filled_shares
        self._set_balance(token_id, available - filled_shares)
        self._state["net_cash_usd"] = round(float(self._state["net_cash_usd"]) + proceeds, 8)
        self._state["fees_usd"] = round(float(self._state["fees_usd"]) + fee, 8)
        self._save()
        return {
            "paper": True,
            "side": "SELL",
            "token_id": token_id,
            "requested_shares": requested_shares,
            "execution_price": execution_price,
            "net_execution_price": proceeds / filled_shares,
            "gross_proceeds_usd": gross,
            "fee_usd": fee,
            "proceeds_usd": proceeds,
            "filled_shares": filled_shares,
            "unfilled_shares": max(0.0, requested_shares - filled_shares),
            "fill_fraction": min(1.0, filled_shares / requested_shares) if requested_shares > 0 else 0.0,
            "partial": filled_shares + 1e-9 < requested_shares,
            "book_revision": revision,
            "level_fills": fills,
            "balance_after": self._balance(token_id),
        }

    def _best_price(self, token_id: str, side: str) -> float | None:
        try:
            _revision, levels = self._available_levels(token_id, side)
        except ValueError:
            return None
        return levels[0][1] if levels else None

    def _available_levels(
        self,
        token_id: str,
        side: str,
    ) -> tuple[str, list[tuple[int, float, float]]]:
        snapshot = self._book_snapshot(token_id)
        staleness = _non_negative_float(snapshot.get("staleness"))
        if staleness is None:
            raise ValueError("paper_book_age_unavailable")
        if staleness > self.max_book_age_seconds:
            raise ValueError("paper_book_stale")
        raw_levels = snapshot.get(side)
        if not isinstance(raw_levels, list):
            raise ValueError("paper_book_depth_unavailable")
        parsed_levels: list[tuple[float, float]] = []
        for level in raw_levels:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                price = _valid_probability(level[0])
                size = _positive_float(level[1])
            elif isinstance(level, dict):
                price = _valid_probability(level.get("price"))
                size = _positive_float(level.get("size"))
            else:
                continue
            if price is not None and size is not None:
                parsed_levels.append((price, size))
        parsed_levels.sort(key=lambda item: item[0], reverse=side == "bids")
        if not parsed_levels:
            raise ValueError("paper_book_depth_empty")

        revision = self._snapshot_revision(token_id, snapshot, parsed_levels)
        previous = self._active_revision.get(token_id)
        if previous != revision:
            self._consumed_depth = {key: value for key, value in self._consumed_depth.items() if key[0] != token_id}
            self._active_revision[token_id] = revision
        available: list[tuple[int, float, float]] = []
        for index, (price, size) in enumerate(parsed_levels):
            consumed = self._consumed_depth.get((token_id, revision, side, index), 0.0)
            remaining = max(0.0, size - consumed)
            if remaining > 1e-12:
                available.append((index, price, remaining))
        if not available:
            raise ValueError("paper_book_depth_consumed")
        return revision, available

    def _book_snapshot(self, token_id: str) -> dict[str, Any]:
        try:
            snapshot = self.quote_provider.quote_snapshot(token_id)
        except (KeyError, ValueError):
            snapshot = {}
        if isinstance(snapshot, dict) and (snapshot.get("asks") or snapshot.get("bids")):
            return snapshot
        if token_id not in self._no_to_yes:
            raise ValueError("paper_book_depth_unavailable")
        yes_token_id = self._yes_for_no(token_id)
        try:
            yes_snapshot = self.quote_provider.quote_snapshot(yes_token_id)
        except (KeyError, ValueError) as exc:
            raise ValueError("paper_book_depth_unavailable") from exc
        if not isinstance(yes_snapshot, dict):
            raise ValueError("paper_book_depth_unavailable")
        return {
            "token_id": token_id,
            "asks": _complement_levels(yes_snapshot.get("bids")),
            "bids": _complement_levels(yes_snapshot.get("asks")),
            "staleness": yes_snapshot.get("staleness"),
            "revision": f"derived:{yes_snapshot.get('revision', '')}",
        }

    def _snapshot_revision(
        self,
        token_id: str,
        snapshot: dict[str, Any],
        levels: list[tuple[float, float]],
    ) -> str:
        revision = snapshot.get("revision")
        if revision is not None and str(revision):
            return str(revision)
        encoded = json.dumps([token_id, levels], separators=(",", ":"), sort_keys=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _consume_level(self, token_id: str, revision: str, side: str, index: int, shares: float) -> None:
        key = (token_id, revision, side, index)
        self._consumed_depth[key] = self._consumed_depth.get(key, 0.0) + shares

    def _execution_price(self, quote: float | None, *, side: str) -> float | None:
        parsed = _valid_probability(quote)
        if parsed is None:
            return None
        multiplier = 1.0 + self.slippage_rate if side == "BUY" else 1.0 - self.slippage_rate
        return round(min(1.0, max(0.0, parsed * multiplier)), 6)

    def _fee_per_share(self, token_id: str, price: float) -> float:
        schedule = self.fee_schedules.get(token_id)
        if schedule is not None:
            return schedule.fee_per_share(price)
        return price * self.fee_rate

    def _fee_for_fill(
        self,
        token_id: str,
        shares: float,
        price: float,
    ) -> float:
        schedule = self.fee_schedules.get(token_id)
        if schedule is not None:
            return schedule.fee_for_fill(shares, price)
        return shares * price * self.fee_rate

    def _zero_fill(self, reason: str, token_id: str, **fields: Any) -> dict[str, Any]:
        return {
            "paper": True,
            "token_id": token_id,
            "filled_shares": 0.0,
            "reason": reason,
            **fields,
        }

    def _balance(self, token_id: str) -> float:
        return float(self._state["balances"].get(token_id, 0.0))

    def _set_balance(self, token_id: str, shares: float) -> None:
        self._state["balances"][token_id] = round(max(0.0, shares), 10)

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"balances": {}, "net_cash_usd": 0.0, "fees_usd": 0.0, "updated_at": None}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt paper broker state {self.state_path}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("balances"), dict):
            raise ValueError(f"invalid paper broker state {self.state_path}")
        try:
            balances = {str(token): max(0.0, float(shares)) for token, shares in raw["balances"].items()}
            net_cash = float(raw.get("net_cash_usd", 0.0))
            fees = float(raw.get("fees_usd", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid paper broker balances {self.state_path}") from exc
        return {
            "balances": balances,
            "net_cash_usd": net_cash,
            "fees_usd": fees,
            "updated_at": raw.get("updated_at"),
        }

    def _save(self) -> None:
        self._state["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_json_write(self.state_path, self._state)


class LiveClobTradingAdapter:
    CONDITIONAL_TOKEN_DECIMALS = Decimal("1000000")
    BALANCE_POLL_ATTEMPTS = 20
    BALANCE_POLL_INTERVAL_SECONDS = 0.5

    def __init__(self, client: Any | None = None):
        if client is None:
            from polybot.exec_engine import build_clob_client

            client = build_clob_client()
        self.client = client

    def query_live_position(self, yes_token_id: str, no_token_id: str) -> LivePosition:
        return LivePosition(
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            yes_shares=self._conditional_balance(yes_token_id),
            no_shares=self._conditional_balance(no_token_id),
        )

    def cancel_open_orders_for_market(self, condition_id: str) -> Any:
        return self.client.cancel_market_orders(market=condition_id)

    def open_orders_for_market(self, condition_id: str) -> Any:
        from py_clob_client.clob_types import OpenOrderParams

        return self.client.get_orders(OpenOrderParams(market=condition_id))

    def sell_no_fak(self, no_token_id: str, shares: float, min_price: float) -> dict[str, Any]:
        return self._post_fak(token_id=no_token_id, amount=shares, side="SELL", price=min_price)

    def sell_yes_fak(self, yes_token_id: str, shares: float, min_price: float) -> dict[str, Any]:
        return self._post_fak(token_id=yes_token_id, amount=shares, side="SELL", price=min_price)

    def buy_yes_fak(self, yes_token_id: str, usd: float, max_price: float) -> dict[str, Any]:
        return self._post_fak(token_id=yes_token_id, amount=usd, side="BUY", price=max_price)

    def buy_no_fak(self, no_token_id: str, usd: float, max_price: float) -> dict[str, Any]:
        return self._post_fak(token_id=no_token_id, amount=usd, side="BUY", price=max_price)

    def verify_fill(self, result: Any, token_id: str) -> Fill:
        if isinstance(result, dict) and result.get("token_id") == token_id:
            return Fill(filled_shares=float(result.get("filled_shares") or 0.0), raw=result)
        return Fill(filled_shares=0.0, raw=result)

    def yes_best_ask(self, yes_token_id: str) -> float | None:
        return self._best_ask(yes_token_id)

    def no_best_ask(self, no_token_id: str) -> float | None:
        return self._best_ask(no_token_id)

    def no_best_bid(self, no_token_id: str) -> float | None:
        return self._best_bid(no_token_id)

    def yes_best_bid(self, yes_token_id: str) -> float | None:
        return self._best_bid(yes_token_id)

    def _post_fak(self, *, token_id: str, amount: float, side: str, price: float) -> dict[str, Any]:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY, SELL

        before = self._conditional_balance(token_id)
        args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=BUY if side == "BUY" else SELL,
            price=price,
            order_type=OrderType.FAK,
        )
        # The SDK can infer tick size and neg-risk details from the live market book.
        order = self.client.create_market_order(args, PartialCreateOrderOptions())
        response = self.client.post_order(order, OrderType.FAK)
        response_fill = _extract_fill_from_response(response)
        after = self._poll_balance_change(token_id, before, side)
        balance_fill = max(0.0, after - before) if side == "BUY" else max(0.0, before - after)
        filled = response_fill if response_fill is not None else balance_fill
        return {
            "live": True,
            "side": side,
            "token_id": token_id,
            "amount": amount,
            "price": price,
            "balance_before": before,
            "balance_after": after,
            "filled_shares": filled,
            "response": response,
        }

    def _poll_balance_change(self, token_id: str, before: float, side: str) -> float:
        latest = before
        for _ in range(self.BALANCE_POLL_ATTEMPTS):
            latest = self._conditional_balance(token_id)
            if side == "BUY" and latest > before:
                return latest
            if side == "SELL" and latest < before:
                return latest
            time.sleep(self.BALANCE_POLL_INTERVAL_SECONDS)
        return latest

    def _conditional_balance(self, token_id: str) -> float:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        raw = self.client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        return _raw_conditional_balance_to_shares(_extract_first(raw, ("balance", "available", "available_balance")))

    def _best_ask(self, token_id: str) -> float | None:
        book = self.client.get_order_book(token_id)
        asks = _extract_levels(book, "asks")
        prices = [_as_float(_extract_first(level, ("price",))) for level in asks]
        prices = [price for price in prices if price is not None]
        return min(prices) if prices else None

    def _best_bid(self, token_id: str) -> float | None:
        book = self.client.get_order_book(token_id)
        bids = _extract_levels(book, "bids")
        prices = [_as_float(_extract_first(level, ("price",))) for level in bids]
        prices = [price for price in prices if price is not None]
        return max(prices) if prices else None


class TsClobV2TradingAdapter:
    bridge_script = "tools/polymarket-ts/clobV2Bridge.ts"
    bridge_name = "clob-v2 bridge"

    def __init__(self, *, tick_size: str = "0.01", neg_risk: bool = False) -> None:
        self.tick_size = tick_size
        self.neg_risk = neg_risk

    def query_live_position(self, yes_token_id: str, no_token_id: str) -> LivePosition:
        raw = self._run_bridge(
            "balance",
            {
                "yes-token-id": yes_token_id,
                "no-token-id": no_token_id,
            },
        )
        position = raw.get("live_position") if isinstance(raw, dict) else None
        if not isinstance(position, dict):
            raise RuntimeError("clob-v2 bridge balance response missing live_position")
        return LivePosition(
            yes_token_id=str(position.get("yes_token_id") or yes_token_id),
            no_token_id=str(position.get("no_token_id") or no_token_id),
            yes_shares=float(position.get("yes_shares") or 0.0),
            no_shares=float(position.get("no_shares") or 0.0),
        )

    def cancel_open_orders_for_market(self, condition_id: str) -> Any:
        return self._run_bridge("cancel-market-orders", {"condition-id": condition_id})

    def open_orders_for_market(self, condition_id: str) -> Any:
        raw = self._run_bridge("open-orders", {"condition-id": condition_id})
        return raw.get("open_orders", []) if isinstance(raw, dict) else []

    def sell_no_fak(self, no_token_id: str, shares: float, min_price: float) -> Any:
        return self._post_fak(token_id=no_token_id, amount=shares, side="SELL", price=min_price)

    def sell_yes_fak(self, yes_token_id: str, shares: float, min_price: float) -> Any:
        return self._post_fak(token_id=yes_token_id, amount=shares, side="SELL", price=min_price)

    def buy_yes_fak(self, yes_token_id: str, usd: float, max_price: float) -> Any:
        return self._post_fak(token_id=yes_token_id, amount=usd, side="BUY", price=max_price)

    def buy_no_fak(self, no_token_id: str, usd: float, max_price: float) -> Any:
        return self._post_fak(token_id=no_token_id, amount=usd, side="BUY", price=max_price)

    def verify_fill(self, result: Any, token_id: str) -> Fill:
        if isinstance(result, dict) and result.get("token_id") == token_id:
            return Fill(filled_shares=float(result.get("filled_shares") or 0.0), raw=result)
        return Fill(filled_shares=0.0, raw=result)

    def yes_best_ask(self, yes_token_id: str) -> float | None:
        return self._best(yes_token_id, "best_ask")

    def no_best_ask(self, no_token_id: str) -> float | None:
        return self._best(no_token_id, "best_ask")

    def no_best_bid(self, no_token_id: str) -> float | None:
        return self._best(no_token_id, "best_bid")

    def yes_best_bid(self, yes_token_id: str) -> float | None:
        return self._best(yes_token_id, "best_bid")

    def _best(self, token_id: str, field: str) -> float | None:
        raw = self._run_bridge("book", {"token-id": token_id})
        book = raw.get("book") if isinstance(raw, dict) else None
        if not isinstance(book, dict):
            return None
        return _as_float(book.get(field))

    def _post_fak(self, *, token_id: str, amount: float, side: str, price: float) -> dict[str, Any]:
        raw = self._run_bridge(
            "fak",
            {
                "token-id": token_id,
                "side": side,
                "amount": str(amount),
                "price": str(price),
                "tick-size": self.tick_size,
                "neg-risk": "true" if self.neg_risk else "false",
            },
        )
        if not isinstance(raw, dict):
            raise RuntimeError("clob-v2 bridge FAK response was not an object")
        return raw

    def _run_bridge(self, action: str, args: dict[str, str]) -> Any:
        env = dict(os.environ)
        env.setdefault("TMPDIR", "/tmp")
        node24_bin = "/home/tstuv/.nvm/versions/node/v24.16.0/bin"
        if Path(node24_bin).exists():
            env["PATH"] = f"{node24_bin}:{env.get('PATH', '')}"
        if action in {"fak", "cancel-market-orders"}:
            env["POLYBOT_TS_BRIDGE_ALLOW_POST"] = "1"
        command = ["./node_modules/.bin/tsx", self.bridge_script, "--action", action]
        for key, value in args.items():
            command.extend([f"--{key}", str(value)])
        completed = subprocess.run(command, env=env, check=False, text=True, capture_output=True)
        if completed.returncode != 0:
            raise RuntimeError(f"{self.bridge_name} {action} failed: {completed.stderr.strip() or completed.stdout.strip()}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{self.bridge_name} {action} returned non-JSON output: {completed.stdout[:500]}") from exc


class TsPolymarketBetaTradingAdapter(TsClobV2TradingAdapter):
    bridge_script = "tools/polymarket-ts/polymarketBetaBridge.ts"
    bridge_name = "polymarket beta bridge"


def live_adapter_from_env(*, tick_size: str = "0.01", neg_risk: bool = False) -> TradingAdapter:
    backend = os.getenv("POLYBOT_EXECUTION_BACKEND", "py_clob").strip().lower()
    if backend in {"polymarket_beta", "beta", "ts_beta"}:
        return TsPolymarketBetaTradingAdapter(tick_size=tick_size, neg_risk=neg_risk)
    if backend in {"clob_v2", "ts_clob_v2", "typescript"}:
        return TsClobV2TradingAdapter(tick_size=tick_size, neg_risk=neg_risk)
    if backend in {"py_clob", "python", ""}:
        return LiveClobTradingAdapter()
    raise SystemExit(f"unsupported POLYBOT_EXECUTION_BACKEND={backend!r}; expected py_clob, clob_v2, or polymarket_beta")


def live_backend_name() -> str:
    backend = os.getenv("POLYBOT_EXECUTION_BACKEND", "py_clob").strip().lower()
    if backend in {"polymarket_beta", "beta", "ts_beta"}:
        return "polymarket_beta"
    if backend in {"clob_v2", "ts_clob_v2", "typescript"}:
        return "clob_v2"
    if backend in {"py_clob", "python", ""}:
        return "py_clob"
    return backend


def _extract_first(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return value[key]
    for key in keys:
        if hasattr(value, key):
            return getattr(value, key)
    return None


def _extract_levels(value: Any, key: str) -> list[Any]:
    levels = _extract_first(value, (key,))
    if isinstance(levels, list):
        return levels
    return []


def _extract_fill_from_response(value: Any) -> float | None:
    direct = _as_float(
        _extract_first(
            value,
            (
                "filled_shares",
                "filledShares",
                "filled_size",
                "filledSize",
                "matched_size",
                "matchedSize",
                "size_matched",
                "sizeMatched",
            ),
        )
    )
    if direct is not None:
        return direct
    if isinstance(value, dict):
        for key in ("order", "data", "result"):
            nested = value.get(key)
            if nested is not None and nested is not value:
                parsed = _extract_fill_from_response(nested)
                if parsed is not None:
                    return parsed
    return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid_probability(value: Any) -> float | None:
    parsed = _as_float(value)
    if parsed is None or parsed < 0.0 or parsed > 1.0:
        return None
    return parsed


def _positive_float(value: Any) -> float | None:
    parsed = _as_float(value)
    if parsed is None or parsed <= 0.0:
        return None
    return parsed


def _non_negative_float(value: Any) -> float | None:
    parsed = _as_float(value)
    if parsed is None or parsed < 0.0:
        return None
    return parsed


def _complement_levels(raw: Any) -> list[tuple[float, float]]:
    if not isinstance(raw, list):
        return []
    levels: list[tuple[float, float]] = []
    for level in raw:
        if isinstance(level, (list, tuple)) and len(level) >= 2:
            price = _valid_probability(level[0])
            size = _positive_float(level[1])
        elif isinstance(level, dict):
            price = _valid_probability(level.get("price"))
            size = _positive_float(level.get("size"))
        else:
            continue
        if price is not None and size is not None:
            levels.append((round(1.0 - price, 6), size))
    return levels


def _raw_conditional_balance_to_shares(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return 0.0
    return float(parsed / LiveClobTradingAdapter.CONDITIONAL_TOKEN_DECIMALS)


__all__ = [
    "DryRunTradingAdapter",
    "Fill",
    "LiveClobTradingAdapter",
    "LivePosition",
    "PaperTradingAdapter",
    "TradingAdapter",
    "TsClobV2TradingAdapter",
    "TsPolymarketBetaTradingAdapter",
    "live_adapter_from_env",
    "live_backend_name",
]
