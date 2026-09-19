from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Protocol, Iterable

from .clock import instant


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class NewsItem:
    native_id: str
    url: str
    title: str
    text: str
    published_at: str | None = None
    status: str = "update"
    origin: str | None = None
    links: tuple[str, ...] = ()


class NewsSourceAdapter(Protocol):
    def parse(self, payload: bytes, url: str, content_type: str) -> list[NewsItem]: ...


@dataclass(frozen=True)
class InstrumentDefinition:
    instrument_id: str
    product: str
    month: str
    exchange: str
    currency: str
    multiplier: str
    tick_size: str
    last_trade_at: str
    sessions: list[dict]
    available_at: str
    vendor_id: str
    data_mode: str = "fixture"
    broker_id: str | None = None

    def validate(self) -> None:
        if self.product not in {"CL", "MCL"} or self.exchange != "NYMEX" or self.currency != "USD":
            raise ValueError("unsupported instrument")
        if Decimal(self.multiplier) != {"CL": 1000, "MCL": 100}[self.product]:
            raise ValueError("incorrect multiplier")
        if Decimal(self.tick_size) != Decimal("0.01"):
            raise ValueError("incorrect tick size")
        import re
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", self.month):
            raise ValueError("explicit YYYY-MM contract month required")
        instant(self.available_at)
        instant(self.last_trade_at)
        if not self.instrument_id or not self.vendor_id or not self.sessions:
            raise ValueError("contract identifiers and sessions required")
        for session in self.sessions:
            if instant(session["open"]) >= instant(session["close"]):
                raise ValueError("invalid session")

    def ticks(self, price: str) -> int:
        value = Decimal(price) / Decimal(self.tick_size)
        if not value.is_finite() or value != value.to_integral_value():
            raise ValueError("price is not a finite tick multiple")
        return int(value)


@dataclass(frozen=True)
class MarketEvent:
    instrument_id: str
    available_at: str
    bid: str | None
    ask: str | None
    bid_size: int
    ask_size: int
    bid_at: str
    ask_at: str
    sequence: int
    data_mode: str
    subscription: str
    source_at: str | None = None
    event_type: str = "quote"

    def validate(self, definition: InstrumentDefinition) -> None:
        definition.validate()
        if self.instrument_id != definition.instrument_id:
            raise ValueError("instrument mismatch")
        for at in (self.available_at, self.bid_at, self.ask_at):
            instant(at)
        if instant(definition.available_at) > instant(self.available_at):
            raise ValueError("future instrument definition")
        if max(instant(self.bid_at), instant(self.ask_at)) > instant(self.available_at):
            raise ValueError("future quote component")
        if self.bid is not None:
            definition.ticks(self.bid)
        if self.ask is not None:
            definition.ticks(self.ask)
        if self.bid is not None and self.ask is not None and Decimal(self.bid) > Decimal(self.ask):
            raise ValueError("crossed book")
        if any(type(v) is not int or v < 0 for v in (self.bid_size, self.ask_size, self.sequence)):
            raise ValueError("invalid size/sequence")
        if self.data_mode not in {"fixture", "realtime", "delayed", "snapshot", "aggregated"}:
            raise ValueError("unknown market data mode")


class MarketDataAdapter(Protocol):
    def definitions(self) -> Iterable[InstrumentDefinition]: ...
    def events(self) -> Iterable[MarketEvent]: ...


FACT_FIELDS = {
    "actor", "asset", "action", "location", "event_time", "operational_status",
    "reported_quantity", "duration", "attribution", "restoration_window",
    "restoration_extent", "flow_evidence",
}
ASSERTIONS = {"asserted", "denied", "conditional", "historical", "unknown"}
OPERATIONS = {"unknown", "operating", "impaired", "suspended", "partly_restored", "restored"}


@dataclass(frozen=True)
class Fact:
    field: str
    value: str
    start: int
    end: int
    quote: str
    assertion: str
    unit: str | None = None
    quantity_kind: str | None = None

    def validate(self, text: str) -> None:
        if self.field not in FACT_FIELDS or self.assertion not in ASSERTIONS:
            raise ValueError("invalid fact category")
        if type(self.start) is not int or type(self.end) is not int or not 0 <= self.start < self.end <= len(text):
            raise ValueError("invalid evidence offsets")
        if text[self.start:self.end] != self.quote or not self.quote.strip():
            raise ValueError("unsupported evidence span")
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("missing fact value")
        if self.field == "operational_status" and self.value not in OPERATIONS:
            raise ValueError("invalid operational status")
        if self.field == "operational_status" and self.assertion == "asserted":
            import re
            if re.search(r"\b(?:not\s+(?:suspended|restored|impaired|operating)|if|unless|might|could|would|denies|denied)\b", self.quote, re.I):
                raise ValueError("operational assertion conflicts with explicit conditional/denial language")
        if self.field == "reported_quantity":
            if self.quantity_kind not in {"gross_capacity", "production_loss", "delivery_disruption", "replacement_supply", "inventory_withdrawal", "demand_reduction", "unknown"}:
                raise ValueError("quantity accounting boundary required")
            if not self.unit or not Decimal(self.value).is_finite() or Decimal(self.value) < 0:
                raise ValueError("invalid reported quantity")
            import re
            numbers = re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", self.quote)
            if Decimal(self.value) not in [Decimal(number.replace(",", "")) for number in numbers]:
                raise ValueError("quantity not literally supported; do not infer or multiply capacity")
            if self.unit.casefold() not in self.quote.casefold():
                raise ValueError("original reported unit must appear in quantity evidence")


def to_dict(record) -> dict:
    return asdict(record)
