"""Deterministic local paper execution. Never connects to a broker.

One explicit contract per account; fills require a subsequent fresh quote.
All limits and costs are engineering assumptions, not trading recommendations.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .clock import instant
from .market import atomic_json
from .schema import InstrumentDefinition, MarketEvent, digest
from .store import Journal, component_lock


@dataclass(frozen=True)
class PaperLimits:
    contracts: int = 1
    max_contracts: int = 1
    max_trades_per_day: int = 5
    daily_loss_usd: str = "100"
    fee_per_side: str = "1"
    slippage_ticks: int = 1
    max_spread_ticks: int = 5
    stop_ticks: int = 20
    take_profit_ticks: int = 40
    max_hold_seconds: int = 1800
    order_delay_seconds: int = 1
    order_ttl_seconds: int = 10
    max_quote_age_seconds: int = 2
    expiry_buffer_seconds: int = 3600

    def validate(self):
        for key, value in asdict(self).items():
            if key in {"daily_loss_usd", "fee_per_side"}:
                try:
                    amount = Decimal(value)
                except (InvalidOperation, TypeError) as exc:
                    raise ValueError("invalid paper limit: " + key) from exc
                if not amount.is_finite() or amount < 0 or (key == "daily_loss_usd" and amount == 0):
                    raise ValueError("invalid paper limit: " + key)
            elif type(value) is not int or value < (0 if key == "slippage_ticks" else 1):
                raise ValueError("invalid paper limit: " + key)
        if self.contracts > self.max_contracts:
            raise ValueError("contracts exceed position limit")
        if self.order_delay_seconds >= self.order_ttl_seconds:
            raise ValueError("order delay must be shorter than order TTL")


class PaperEngine:
    def __init__(self, store: Journal, definition: InstrumentDefinition, limits: PaperLimits):
        definition.validate()
        limits.validate()
        self.store, self.definition, self.limits = store, definition, limits
        self.identity = digest({"definition": asdict(definition), "limits": asdict(limits), "engine": "paper-v1"})

    def _state(self):
        state = self.store.cursor("paper:state")
        if state and state["identity"] != self.identity:
            raise ValueError("paper account configuration changed; use a new journal")
        return state or {"identity": self.identity, "at": None, "quote": None, "mark_quote": None,
                         "position": None, "pending": None, "cash_pnl": "0",
                         "day": None, "day_start_equity": "0", "trades_today": 0,
                         "loss_halt": False, "manual_halt": False, "close_reason": None}

    def _equity(self, state):
        value = Decimal(state["cash_pnl"])
        position, quote = state["position"], state["mark_quote"]
        if position and quote:
            price = quote["bid"] if position["side"] == 1 else quote["ask"]
            value += (Decimal(price) - Decimal(position["price"])) * position["side"] * position["quantity"] * Decimal(self.definition.multiplier)
        return value

    def _session(self, at):
        return any(instant(s["open"]) <= at < instant(s["close"]) for s in self.definition.sessions)

    def _entry_window(self, at):
        end = at + timedelta(seconds=self.limits.max_hold_seconds + self.limits.order_delay_seconds)
        return (instant(self.definition.available_at) <= at
                and end < instant(self.definition.last_trade_at) - timedelta(seconds=self.limits.expiry_buffer_seconds)
                and any(instant(s["open"]) <= at <= end < instant(s["close"]) for s in self.definition.sessions))

    def _fresh(self, quote, at):
        return (quote and quote["bid"] is not None and quote["ask"] is not None
                and quote["data_mode"] in {"fixture", "realtime"}
                and quote["event_type"] == "quote"
                and all(0 <= (at - instant(quote[key])).total_seconds() <= self.limits.max_quote_age_seconds
                        for key in ("available_at", "bid_at", "ask_at")))

    def process(self, kind: str, payload: dict, *, at: str, event_id: str):
        """Apply an input once, atomically with fills and account state.

        The caller must serialize event time. Identical delivery is a no-op;
        reusing an ID with different data and retrograde time are rejected.
        """
        when = instant(at)
        input_hash = digest([kind, payload, at])
        with self.store.transaction() as db:
            prior = self.store.cursor("paper:event:" + event_id)
            if prior:
                if prior != input_hash:
                    raise ValueError("paper event identity collision")
                return self._state()
            state = self._state()
            if state["at"] and when < instant(state["at"]):
                raise ValueError("paper events must be chronological")
            day = when.date().isoformat()
            if state["day"] != day:
                state.update(day=day, day_start_equity=str(self._equity(state)), trades_today=0, loss_halt=False)
            events = []

            def emit(event, **fields):
                events.append({"event": event, "source_event_id": event_id, **fields})

            def cancel(reason):
                if state["pending"]:
                    emit("ORDER_CANCELLED", reason=reason, signal_id=state["pending"]["signal_id"])
                    state["pending"] = None

            if kind == "quote":
                quote = MarketEvent(**payload)
                quote.validate(self.definition)
                if instant(quote.available_at) != when:
                    raise ValueError("quote time mismatch")
                old = state["quote"]
                if old and quote.sequence <= old["sequence"]:
                    raise ValueError("non-increasing quote sequence")
                if old and quote.sequence != old["sequence"] + 1:
                    state["manual_halt"] = True
                    cancel("QUOTE_SEQUENCE_GAP")
                    emit("HALTED", reason="QUOTE_SEQUENCE_GAP")
                state["quote"] = payload
            elif kind == "halt":
                state["manual_halt"] = True
                cancel("MANUAL_HALT")
                state["close_reason"] = "MANUAL_HALT"
                emit("HALTED", reason="MANUAL_HALT")
            elif kind not in {"signal", "clock"}:
                raise ValueError("unknown paper input")

            quote = state["quote"]
            fresh = self._fresh(quote, when)
            if fresh:
                state["mark_quote"] = quote
            if fresh and self._equity(state) - Decimal(state["day_start_equity"]) <= -Decimal(self.limits.daily_loss_usd):
                state["loss_halt"] = True
                cancel("DAILY_LOSS_LIMIT")
                state["close_reason"] = "DAILY_LOSS_LIMIT"

            position = state["position"]
            if position and fresh:
                mark = self.definition.ticks(quote["bid"] if position["side"] == 1 else quote["ask"])
                change = position["side"] * (mark - self.definition.ticks(position["price"]))
                held = (when - instant(position["opened_at"])).total_seconds()
                if state["manual_halt"]:
                    state["close_reason"] = state["close_reason"] or "MANUAL_HALT"
                elif change <= -self.limits.stop_ticks:
                    state["close_reason"] = state["close_reason"] or "STOP_LOSS"
                elif change >= self.limits.take_profit_ticks:
                    state["close_reason"] = state["close_reason"] or "TAKE_PROFIT"
                elif held >= self.limits.max_hold_seconds:
                    state["close_reason"] = state["close_reason"] or "MAX_HOLD"
                elif when >= instant(self.definition.last_trade_at) - timedelta(seconds=self.limits.expiry_buffer_seconds):
                    state["close_reason"] = state["close_reason"] or "EXPIRY_BUFFER"
                if state["close_reason"] and kind == "quote" and self._session(when) and when < instant(self.definition.last_trade_at):
                    quantity = min(position["quantity"], quote["bid_size"] if position["side"] == 1 else quote["ask_size"])
                    if quantity:
                        price = (mark - position["side"] * self.limits.slippage_ticks) * Decimal(self.definition.tick_size)
                        pnl = (price - Decimal(position["price"])) * position["side"] * quantity * Decimal(self.definition.multiplier)
                        pnl -= quantity * Decimal(self.limits.fee_per_side)
                        state["cash_pnl"] = str(Decimal(state["cash_pnl"]) + pnl)
                        position["quantity"] -= quantity
                        emit("EXIT_FILL", quantity=quantity, price=str(price), reason=state["close_reason"], cash_change=str(pnl))
                        if not position["quantity"]:
                            state["position"] = None
                            state["close_reason"] = None

            pending = state["pending"]
            if pending and when >= instant(pending["expires_at"]):
                emit("ORDER_CANCELLED", reason="TTL_EXPIRED", signal_id=pending["signal_id"])
                state["pending"] = pending = None
            if pending and kind == "quote" and when >= instant(pending["arrives_at"]) and fresh:
                spread = self.definition.ticks(quote["ask"]) - self.definition.ticks(quote["bid"])
                if self._entry_window(when) and spread <= self.limits.max_spread_ticks:
                    side = pending["side"]
                    ticks = self.definition.ticks(quote["ask"] if side == 1 else quote["bid"]) + side * self.limits.slippage_ticks
                    quantity = min(self.limits.contracts, quote["ask_size"] if side == 1 else quote["bid_size"])
                    if quantity and side * (ticks - pending["limit_ticks"]) <= 0:
                        price = ticks * Decimal(self.definition.tick_size)
                        state["position"] = {"side": side, "quantity": quantity, "price": str(price), "opened_at": at,
                                             "signal_id": pending["signal_id"]}
                        state["cash_pnl"] = str(Decimal(state["cash_pnl"]) - quantity * Decimal(self.limits.fee_per_side))
                        state["trades_today"] += 1
                        state["pending"] = None
                        emit("ENTRY_FILL", signal_id=pending["signal_id"], side=side, quantity=quantity, price=str(price),
                             unfilled_cancelled=self.limits.contracts - quantity)

            # Fees and exit slippage can cross the loss limit after a fill.
            if fresh and self._equity(state) - Decimal(state["day_start_equity"]) <= -Decimal(self.limits.daily_loss_usd):
                state["loss_halt"] = True
                cancel("DAILY_LOSS_LIMIT")
                if state["position"]:
                    state["close_reason"] = "DAILY_LOSS_LIMIT"

            if kind == "signal":
                side = payload.get("direction")
                reason = None
                if payload.get("action") != "RESEARCH_CANDIDATE" or type(side) is not int or side not in {-1, 1}:
                    reason = "INELIGIBLE_SIGNAL"
                elif state["manual_halt"] or state["loss_halt"]:
                    reason = "ACCOUNT_HALTED"
                elif state["position"] or state["pending"]:
                    reason = "POSITION_OR_ORDER_EXISTS"
                elif state["trades_today"] >= self.limits.max_trades_per_day:
                    reason = "DAILY_TRADE_LIMIT"
                elif not self._entry_window(when):
                    reason = "SESSION_OR_EXPIRY_RESTRICTED"
                elif not fresh:
                    reason = "QUOTE_UNAVAILABLE_OR_STALE"
                elif self.definition.ticks(quote["ask"]) - self.definition.ticks(quote["bid"]) > self.limits.max_spread_ticks:
                    reason = "SPREAD_LIMIT"
                if reason:
                    emit("SIGNAL_REJECTED", reason=reason)
                else:
                    ticks = self.definition.ticks(quote["ask"] if side == 1 else quote["bid"]) + side * self.limits.slippage_ticks
                    state["pending"] = {"signal_id": event_id, "side": side, "limit_ticks": ticks,
                                        "arrives_at": (when + timedelta(seconds=self.limits.order_delay_seconds)).isoformat(),
                                        "expires_at": (when + timedelta(seconds=self.limits.order_ttl_seconds)).isoformat()}
                    emit("ORDER_SUBMITTED", **state["pending"])

            state["at"] = at
            self.store.append("paper_input", {"event_id": event_id, "kind": kind, "payload": payload, "identity": self.identity}, available_at=at, db=db)
            for event in events:
                self.store.append("paper_event", event, available_at=at, db=db)
            self.store.set_cursor(db, "paper:state", state)
            self.store.set_cursor(db, "paper:event:" + event_id, input_hash)
            return state

    def summary(self):
        state = self._state()
        events = [row["payload"] for row in self.store.records("paper_event")]
        return {"mode": "local_paper", "broker_connected": False, "economic_evaluation": "unavailable",
                "instrument_id": self.definition.instrument_id, "limits": asdict(self.limits),
                "cash_pnl": state["cash_pnl"], "marked_equity": str(self._equity(state)),
                "mark_available_at": state["mark_quote"]["available_at"] if state["mark_quote"] else None,
                "position": state["position"], "pending_order": state["pending"],
                "halted": state["loss_halt"] or state["manual_halt"], "events": events,
                "limitations": "Hypothetical fills and fees; stops can slip. Open exposure remains unresolved when quotes end."}


def paper_replay(manifest_path: Path, destination: Path, instrument_id: str, limits: PaperLimits):
    from .replay import load_manifest
    manifest, reader, market = load_manifest(manifest_path)
    if market["gaps"] or any(row["kind"] == "gap" for row in market["records"]):
        raise ValueError("paper replay requires a market archive without recorded gaps")
    definitions = [InstrumentDefinition(**row["payload"]) for row in market["records"]
                   if row["kind"] == "instrument" and row["payload"]["instrument_id"] == instrument_id]
    if len(definitions) != 1:
        raise ValueError("one unambiguous explicit instrument definition required")
    limits.validate()
    destination = Path(destination).resolve()
    # Fresh output ensures results from different snapshots cannot mix.
    destination.mkdir(parents=True, exist_ok=False)
    with component_lock(destination, "paper"):
        engine = PaperEngine(Journal(destination / "paper.sqlite3"), definitions[0], limits)
        timeline = [(row["payload"]["available_at"], 0, row["id"], "quote", row["payload"])
                    for row in market["records"] if row["kind"] == "market" and row["payload"]["instrument_id"] == instrument_id]
        timeline += [(row["available_at"], 1, row["id"], "signal", row["payload"])
                     for row in reader.records if row["kind"] == "decision"]
        for at, _, event_id, kind, payload in sorted(timeline, key=lambda row: (instant(row[0]), row[1], row[4].get("sequence", 0), row[2])):
            engine.process(kind, payload, at=at, event_id=event_id)
        report = {**engine.summary(), "source_records_hash": manifest["records_hash"],
                  "source_manifest_hash": digest(manifest), "dataset_role": manifest["dataset_role"]}
        atomic_json(destination / "report.json", report)
        return {"report": str(destination / "report.json"), "journal": str(destination / "paper.sqlite3"),
                "mode": report["mode"], "cash_pnl": report["cash_pnl"], "open_position": bool(report["position"]),
                "economic_evaluation": "unavailable"}
