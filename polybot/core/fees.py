from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

FEE_SCHEDULE_SCHEMA_VERSION = 1
FEE_POLICY_VERSION = "polymarket-fee-schedule-v1"
FEE_SCHEDULE_SOURCE = "gamma_market_metadata"


@dataclass(frozen=True)
class FeeScheduleSnapshot:
    """Point-in-time Polymarket fee metadata bound to one market.

    The generic rules engine accepts only the current taker-only fee curve.
    A disabled schedule is an explicit zero-fee declaration, not a default.
    """

    schema_version: int
    fees_enabled: bool
    rate: float
    exponent: float
    taker_only: bool
    rebate_rate: float
    observed_at: str
    source: str = FEE_SCHEDULE_SOURCE

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def schedule_sha256(self) -> str:
        encoded = json.dumps(
            self.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FeeScheduleSnapshot":
        if not isinstance(raw, dict):
            raise ValueError("fee schedule must be an object")
        unknown = sorted(set(raw) - set(cls.__dataclass_fields__))
        if unknown:
            raise ValueError(
                "fee schedule contains unknown keys: " + ", ".join(unknown)
            )
        schema = _integer(raw.get("schema_version"), "schema_version")
        if schema != FEE_SCHEDULE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported fee schedule schema_version {schema}"
            )
        enabled = _boolean(raw.get("fees_enabled"), "fees_enabled")
        rate = _number(raw.get("rate"), "rate", minimum=0.0, maximum=1.0)
        exponent = _number(
            raw.get("exponent"),
            "exponent",
            minimum=0.0,
            maximum=10.0,
        )
        taker_only = _boolean(raw.get("taker_only"), "taker_only")
        rebate = _number(
            raw.get("rebate_rate"),
            "rebate_rate",
            minimum=0.0,
            maximum=1.0,
        )
        observed_at = _aware_iso(raw.get("observed_at"), "observed_at")
        source = str(raw.get("source") or "").strip()
        if source != FEE_SCHEDULE_SOURCE:
            raise ValueError(
                f"unsupported fee schedule source {source!r}"
            )
        if enabled:
            if rate <= 0:
                raise ValueError("enabled fee schedule requires a positive rate")
            if exponent <= 0:
                raise ValueError(
                    "enabled fee schedule requires a positive exponent"
                )
        elif any((rate != 0.0, rebate != 0.0)):
            raise ValueError(
                "disabled fee schedule must explicitly declare zero rate and rebate"
            )
        return cls(
            schema_version=schema,
            fees_enabled=enabled,
            rate=rate,
            exponent=exponent,
            taker_only=taker_only,
            rebate_rate=rebate,
            observed_at=observed_at,
            source=source,
        )

    @classmethod
    def from_gamma(
        cls,
        *,
        fees_enabled: Any,
        fee_schedule: Any,
        observed_at: str,
    ) -> "FeeScheduleSnapshot":
        if not isinstance(fees_enabled, bool):
            raise ValueError("feesEnabled is missing or is not a boolean")
        if not fees_enabled:
            if fee_schedule not in (None, {}):
                if not isinstance(fee_schedule, dict):
                    raise ValueError(
                        "disabled feeSchedule must be null or an object"
                    )
                rate = _gamma_number(fee_schedule.get("rate", 0), "rate")
                rebate = _gamma_number(
                    fee_schedule.get("rebateRate", 0),
                    "rebateRate",
                )
                if rate != 0 or rebate != 0:
                    raise ValueError(
                        "feesEnabled=false conflicts with a nonzero feeSchedule"
                    )
            return cls.from_dict(
                {
                    "schema_version": FEE_SCHEDULE_SCHEMA_VERSION,
                    "fees_enabled": False,
                    "rate": 0.0,
                    "exponent": 1.0,
                    "taker_only": True,
                    "rebate_rate": 0.0,
                    "observed_at": observed_at,
                    "source": FEE_SCHEDULE_SOURCE,
                }
            )
        if not isinstance(fee_schedule, dict):
            raise ValueError(
                "feesEnabled=true requires a feeSchedule object"
            )
        required = {"rate", "exponent", "takerOnly", "rebateRate"}
        missing = sorted(required - set(fee_schedule))
        if missing:
            raise ValueError(
                "feeSchedule is missing fields: " + ", ".join(missing)
            )
        return cls.from_dict(
            {
                "schema_version": FEE_SCHEDULE_SCHEMA_VERSION,
                "fees_enabled": True,
                "rate": _gamma_number(fee_schedule["rate"], "rate"),
                "exponent": _gamma_number(
                    fee_schedule["exponent"],
                    "exponent",
                ),
                "taker_only": fee_schedule["takerOnly"],
                "rebate_rate": _gamma_number(
                    fee_schedule["rebateRate"],
                    "rebateRate",
                ),
                "observed_at": observed_at,
                "source": FEE_SCHEDULE_SOURCE,
            }
        )

    def entry_blockers(
        self,
        *,
        as_of: datetime,
        max_age_hours: float,
    ) -> list[str]:
        now = _aware(as_of)
        observed = _parse_aware(self.observed_at)
        age_hours = (now - observed).total_seconds() / 3600.0
        blockers: list[str] = []
        if age_hours < -1e-9:
            blockers.append("fee_schedule_observed_in_future")
        if max_age_hours > 0 and age_hours > max_age_hours:
            blockers.append(
                f"fee_schedule_stale:{round(age_hours, 3)}h"
            )
        if self.fees_enabled and not self.taker_only:
            blockers.append("unsupported_non_taker_only_fee_schedule")
        return blockers

    def fee_per_share(self, price: float) -> float:
        parsed = _probability(price)
        if not self.fees_enabled:
            return 0.0
        price_component = parsed * (1.0 - parsed)
        return self.rate * (price_component ** self.exponent)

    def fee_for_fill(self, shares: float, price: float) -> float:
        quantity = _number(shares, "shares", minimum=0.0)
        raw = quantity * self.fee_per_share(price)
        # Polymarket fees are rounded to five decimal places; values below the
        # minimum precision become zero.
        rounded = round(raw + 1e-15, 5)
        return 0.0 if rounded < 0.00001 else rounded


def explicit_zero_fee_schedule(observed_at: str) -> FeeScheduleSnapshot:
    return FeeScheduleSnapshot.from_gamma(
        fees_enabled=False,
        fee_schedule=None,
        observed_at=observed_at,
    )


def fee_schedule_map(context: Any) -> dict[str, FeeScheduleSnapshot]:
    schedules: dict[str, FeeScheduleSnapshot] = {}
    for outcome in context.outcomes:
        schedule = outcome.fee_schedule
        if schedule is None:
            continue
        schedules[outcome.yes_token_id] = schedule
        schedules[outcome.no_token_id] = schedule
    return schedules


def fee_schedules_sha256(context: Any) -> str:
    payload = {
        outcome.name: (
            outcome.fee_schedule.as_dict()
            if outcome.fee_schedule is not None
            else {
                "status": "UNAVAILABLE",
                "error": outcome.fee_schedule_error,
            }
        )
        for outcome in context.outcomes
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _gamma_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"feeSchedule.{name} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"feeSchedule.{name} must be numeric"
        ) from exc
    if not math.isfinite(parsed):
        raise ValueError(f"feeSchedule.{name} must be finite")
    return parsed


def _number(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return parsed


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _probability(value: Any) -> float:
    return _number(value, "price", minimum=0.0, maximum=1.0)


def _aware_iso(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be an ISO date-time")
    parsed = _parse_aware(value)
    return parsed.isoformat()


def _parse_aware(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("date-time must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("date-time must include a timezone")
    return parsed.astimezone(timezone.utc)


def _aware(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


__all__ = [
    "FEE_POLICY_VERSION",
    "FEE_SCHEDULE_SCHEMA_VERSION",
    "FeeScheduleSnapshot",
    "explicit_zero_fee_schedule",
    "fee_schedule_map",
    "fee_schedules_sha256",
]
