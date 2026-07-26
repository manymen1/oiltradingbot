from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Reused as-is: classifier/sources/safety config shapes are provider-agnostic
# and OperatorGate duck-types on ClassifierConfig's and SafetyConfig's
# attributes, so sharing the dataclasses keeps that gate reusable without
# modification.
from polybot.core.config import ClassifierConfig, SafetyConfig, SourcesConfig  # noqa: F401
from polybot.core.config_validation import (
    load_yaml_object,
    reject_unknown_dataclass_keys,
    require_bool,
    require_integer,
    require_iso_date,
    require_number,
    require_probability,
    require_string_list,
    require_text,
    validate_classifier_config,
    validate_portfolio_link_config,
    validate_safety_config,
    validate_sources_config,
    validate_time_decay_config,
)
from polybot.core.portfolio import PortfolioConfig  # noqa: F401


@dataclass(frozen=True)
class OutcomeMarket:
    """One leg of the grouped categorical market (one location's Yes/No pair)."""

    name: str  # e.g. "qatar", "pakistan" -- must match LocationSignal.confirmed_location values
    label: str  # display label, e.g. "Qatar"
    condition_id: str
    yes_token_id: str
    no_token_id: str
    # Only rotation targets get an automatic buy-YES leg when confirmed; other
    # tracked outcomes (informational only) get sell-only treatment.
    rotation_target: bool = False


@dataclass(frozen=True)
class EventConfig:
    slug: str
    question: str
    deadline_date: str  # ISO date the grouped market resolves by
    # Key into outcomes, e.g. "qatar" -- the INITIAL/default location held YES.
    # Empty string means the bot starts flat; that is only valid with
    # entry.enabled, and once the bot has entered/rotated/exited, the live
    # holding in HoldingsStore overrides this value.
    held_location: str = ""
    resolution_rules: str = ""  # full market resolution-criteria text, fed to the classifier as context
    analyst_context: str = ""  # user's own thesis/background reasoning, fed to the classifier as context
    # Opt-in pin, mirroring polybot.iran.market_verifier's pattern: left blank
    # until an operator runs inspect-location, reviews the live rule text, and
    # pins its digest here. Blank means "not yet reviewed" -- not "verified".
    expected_rule_text_sha256: str = ""


@dataclass(frozen=True)
class PositionConfig:
    source: str = "onchain"
    held_yes_shares: float = 0.0
    max_yes_shares_to_sell: float = 100000.0
    max_rotation_usd_to_buy: float = 1000.0
    # Sell the whole position when the held outcome's YES bid reaches this
    # price: once the thesis is priced in, the last few cents are not worth
    # carrying full resolution risk. 0 disables.
    take_profit_price: float = 0.0


@dataclass(frozen=True)
class TriggerConfig:
    auto_execute_level: int = 4
    trusted_single_source_execution: bool = True


@dataclass(frozen=True)
class SellConfig:
    enabled: bool = True
    min_price: float = 0.03
    retry_partial_once: bool = True
    retry_delay_seconds: float = 2.0
    trim_fraction: float = 0.25
    # Staged defense exits: < 1.0 sells this fraction first, requotes after
    # retry_delay_seconds, then sells the remainder -- softer on thin books.
    max_fraction_per_order: float = 1.0


@dataclass(frozen=True)
class BuyRotationConfig:
    enabled: bool = True
    max_price: float = 0.95
    usd_budget: float = 500.0
    skip_if_above_cap: bool = True
    max_spread: float = 0.50


@dataclass(frozen=True)
class EntryConfig:
    """Automated flat-to-position entry.

    Disabled by default: the bot stays a pure protection bot unless entry is
    explicitly configured. Entry uses the same evidence bar as a rotation buy
    (trusted tier-one source, confirmed senior round at a configured venue),
    and the same operator gate / live ack applies before any real order.
    """

    enabled: bool = False
    # Outcome keys eligible for automatic entry; must be a subset of outcomes.
    targets: list[str] = field(default_factory=list)
    usd_budget: float = 100.0
    max_price: float = 0.90
    max_spread: float = 0.50
    # Deterministic confirmation valuation.  The classifier extracts facts;
    # code converts the strongest accepted confirmation into this probability.
    confirmed_probability: float = 0.97
    min_edge: float = 0.05
    slippage_buffer: float = 0.01
    resolution_risk_buffer: float = 0.02
    # Any positive live fill creates exposure and is recorded, but fills below
    # these thresholds enter PARTIALLY_ENTERED and require reconciliation rather
    # than being silently treated as a complete entry.
    min_fill_usd: float = 5.0
    min_fill_fraction: float = 0.25
    reconcile_min_shares: float = 0.01
    # Entries above this notional require a second independent source to have
    # confirmed the same thesis within the window before buying (0 = off).
    second_source_above_usd: float = 0.0
    second_source_window_minutes: float = 60.0
    # Post-entry corroboration: if no second independent source confirms the
    # thesis within this many minutes of entry, alert (or trim). 0 = off.
    corroboration_minutes: float = 0.0
    corroboration_action: str = "alert"  # alert | trim
    # Lifetime cap on entry executions for this position config.
    max_entries: int = 1


def _default_source_likelihoods() -> dict[str, float]:
    return {
        "official_government": 2.5,
        "mediator_government": 2.5,
        "wire": 2.0,
        "state_media": 1.35,
        "other": 1.15,
    }


def _default_evidence_likelihoods() -> dict[str, float]:
    return {
        "confirmed_started": 8.0,
        "confirmed_scheduled": 5.0,
        "reported_indirect": 2.0,
        "speculative": 1.25,
        "denied": 4.0,
    }


@dataclass(frozen=True)
class ForecastConfig:
    """Anticipatory probability research. Always paper-only in this release."""

    enabled: bool = False
    paper_only: bool = True
    prior_probabilities: dict[str, float] = field(default_factory=dict)
    source_likelihoods: dict[str, float] = field(default_factory=_default_source_likelihoods)
    evidence_likelihoods: dict[str, float] = field(default_factory=_default_evidence_likelihoods)
    min_paper_edge: float = 0.12
    max_paper_price: float = 0.70
    paper_order_usd: float = 10.0
    slippage_buffer: float = 0.02
    resolution_risk_buffer: float = 0.03
    exit_remaining_edge: float = 0.03
    max_processed_articles: int = 2000
    model_version: str = "location-forecast-v2"
    # Paper fills use live quote snapshots when the runner is dry-run.  Tests
    # may still inject a deterministic quote adapter.
    live_quotes_in_dry_run: bool = True
    quote_refresh_seconds: float = 2.0
    max_quote_age_seconds: float = 10.0
    max_spread: float = 0.20
    fee_rate: float = 0.0
    simulated_slippage: float = 0.005


@dataclass(frozen=True)
class ExecutionConfig:
    dry_run: bool = True
    paper_fee_bps: float = 0.0
    paper_slippage_bps: float = 25.0
    paper_max_book_age_seconds: float = 10.0
    sell: SellConfig = field(default_factory=SellConfig)
    buy_rotation: BuyRotationConfig = field(default_factory=BuyRotationConfig)


@dataclass(frozen=True)
class TimeDecayConfig:
    enabled: bool = False
    trim_after_date: str = ""
    exit_after_date: str = ""
    trim_fraction: float = 0.25
    min_trim_price: float = 0.0
    min_exit_price: float = 0.0


@dataclass(frozen=True)
class PriceAlertConfig:
    enabled: bool = False
    outcome: str = ""
    thresholds: list[float] = field(default_factory=list)
    # When execution is dry-run, use public live CLOB books for monitoring
    # alerts instead of the synthetic DryRunTradingAdapter quote.
    live_quotes_in_dry_run: bool = False


@dataclass(frozen=True)
class HeartbeatConfig:
    enabled: bool = False
    interval_hours: float = 24.0


@dataclass(frozen=True)
class MarketVerificationMonitorConfig:
    enabled: bool = False
    interval_minutes: float = 30.0


@dataclass(frozen=True)
class MonitoringConfig:
    price_alerts: PriceAlertConfig = field(default_factory=PriceAlertConfig)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    market_verification: MarketVerificationMonitorConfig = field(default_factory=MarketVerificationMonitorConfig)


@dataclass(frozen=True)
class LocationBotConfig:
    event: EventConfig
    outcomes: list[OutcomeMarket] = field(default_factory=list)
    position: PositionConfig = field(default_factory=PositionConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    forecast: ForecastConfig = field(default_factory=ForecastConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    time_decay: TimeDecayConfig = field(default_factory=TimeDecayConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    data_dir: Path = Path("data/location-protection-bot")
    logs_dir: Path = Path("logs")

    def outcome(self, name: str) -> OutcomeMarket | None:
        normalized = name.strip().lower().replace(" ", "_")
        for outcome in self.outcomes:
            if outcome.name == normalized:
                return outcome
        return None

    def held_outcome(self) -> OutcomeMarket:
        outcome = self.outcome(self.event.held_location)
        if outcome is None:
            raise ValueError(f"held_location {self.event.held_location!r} not found in outcomes")
        return outcome

    def rotation_targets(self, held_location: str | None = None) -> list[OutcomeMarket]:
        held = self.event.held_location if held_location is None else held_location
        return [o for o in self.outcomes if o.rotation_target and o.name != held]

    def entry_target_names(self) -> set[str]:
        return {name.strip().lower().replace(" ", "_") for name in self.entry.targets}

    def entry_targets(self) -> list[OutcomeMarket]:
        names = self.entry_target_names()
        return [o for o in self.outcomes if o.name in names]


def load_location_config(path: Path) -> LocationBotConfig:
    raw = load_yaml_object(path)
    reject_unknown_dataclass_keys(raw, LocationBotConfig, context=str(path))
    outcomes_raw = raw.get("outcomes", [])
    if not isinstance(outcomes_raw, list):
        raise ValueError("outcomes must be a list")
    outcomes = [OutcomeMarket(**_normalize_outcome(item)) for item in outcomes_raw]
    event_raw = dict(_section(raw, "event"))
    if "held_location" in event_raw:
        event_raw["held_location"] = (
            str(event_raw["held_location"] or "").strip().lower().replace(" ", "_")
        )
    execution_raw = _section(raw, "execution")
    monitoring_raw = _section(raw, "monitoring")
    reject_unknown_dataclass_keys(
        execution_raw,
        ExecutionConfig,
        context="execution",
    )
    reject_unknown_dataclass_keys(
        monitoring_raw,
        MonitoringConfig,
        context="monitoring",
    )
    config = LocationBotConfig(
        event=EventConfig(**event_raw),
        outcomes=outcomes,
        position=PositionConfig(**_section(raw, "position")),
        trigger=TriggerConfig(**_section(raw, "trigger")),
        classifier=ClassifierConfig(**_section(raw, "classifier")),
        entry=EntryConfig(**_section(raw, "entry")),
        forecast=ForecastConfig(**_section(raw, "forecast")),
        portfolio=PortfolioConfig(**_section(raw, "portfolio")),
        execution=ExecutionConfig(
            dry_run=execution_raw.get("dry_run", True),
            paper_fee_bps=float(execution_raw.get("paper_fee_bps", 0.0)),
            paper_slippage_bps=float(execution_raw.get("paper_slippage_bps", 25.0)),
            paper_max_book_age_seconds=float(execution_raw.get("paper_max_book_age_seconds", 10.0)),
            sell=SellConfig(**_section(execution_raw, "sell")),
            buy_rotation=BuyRotationConfig(**_section(execution_raw, "buy_rotation")),
        ),
        time_decay=TimeDecayConfig(**_section(raw, "time_decay")),
        monitoring=MonitoringConfig(
            price_alerts=PriceAlertConfig(**_section(monitoring_raw, "price_alerts")),
            heartbeat=HeartbeatConfig(**_section(monitoring_raw, "heartbeat")),
            market_verification=MarketVerificationMonitorConfig(**_section(monitoring_raw, "market_verification")),
        ),
        safety=SafetyConfig(**_section(raw, "safety")),
        sources=SourcesConfig(**_section(raw, "sources")),
        data_dir=Path(str(raw.get("data_dir", "data/location-protection-bot"))),
        logs_dir=Path(str(raw.get("logs_dir", "logs"))),
    )
    _validate_location_config(config)
    _validate_entry(config)
    _validate_forecast(config)
    return config


def _validate_location_config(config: LocationBotConfig) -> None:
    require_text(config.event.slug, "event.slug")
    require_text(config.event.question, "event.question")
    require_iso_date(config.event.deadline_date, "event.deadline_date")
    require_text(config.event.held_location, "event.held_location", allow_empty=True)
    require_text(config.event.resolution_rules, "event.resolution_rules")
    require_text(config.event.analyst_context, "event.analyst_context", allow_empty=True)
    require_text(
        config.event.expected_rule_text_sha256,
        "event.expected_rule_text_sha256",
        allow_empty=True,
    )
    if not config.outcomes:
        raise ValueError("outcomes must contain at least one outcome")
    names = [outcome.name for outcome in config.outcomes]
    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    if duplicate_names:
        raise ValueError(f"outcome names must be unique: {', '.join(duplicate_names)}")
    token_owners: dict[str, str] = {}
    condition_ids: set[str] = set()
    for outcome in config.outcomes:
        prefix = f"outcomes[{outcome.name!r}]"
        require_text(outcome.name, f"{prefix}.name")
        require_text(outcome.label, f"{prefix}.label")
        require_text(outcome.condition_id, f"{prefix}.condition_id")
        require_text(outcome.yes_token_id, f"{prefix}.yes_token_id")
        require_text(outcome.no_token_id, f"{prefix}.no_token_id")
        require_bool(outcome.rotation_target, f"{prefix}.rotation_target")
        if outcome.yes_token_id == outcome.no_token_id:
            raise ValueError(f"{prefix} YES and NO token ids must be distinct")
        if outcome.condition_id in condition_ids:
            raise ValueError(
                f"outcome condition ids must be unique: {outcome.condition_id}"
            )
        condition_ids.add(outcome.condition_id)
        for token_id in (outcome.yes_token_id, outcome.no_token_id):
            previous = token_owners.get(token_id)
            if previous is not None:
                raise ValueError(
                    f"outcome token ids must be globally unique: {token_id} "
                    f"is used by {previous} and {outcome.name}"
                )
            token_owners[token_id] = outcome.name

    position = config.position
    require_text(position.source, "position.source")
    for field_name in (
        "held_yes_shares",
        "max_yes_shares_to_sell",
        "max_rotation_usd_to_buy",
    ):
        require_number(getattr(position, field_name), f"position.{field_name}", minimum=0)
    require_probability(position.take_profit_price, "position.take_profit_price")

    require_integer(
        config.trigger.auto_execute_level,
        "trigger.auto_execute_level",
        minimum=1,
        maximum=4,
    )
    require_bool(
        config.trigger.trusted_single_source_execution,
        "trigger.trusted_single_source_execution",
    )

    execution = config.execution
    require_bool(execution.dry_run, "execution.dry_run")
    require_number(execution.paper_fee_bps, "execution.paper_fee_bps", minimum=0)
    require_number(
        execution.paper_slippage_bps,
        "execution.paper_slippage_bps",
        minimum=0,
    )
    require_number(
        execution.paper_max_book_age_seconds,
        "execution.paper_max_book_age_seconds",
        minimum=0,
        minimum_exclusive=True,
    )
    sell = execution.sell
    require_bool(sell.enabled, "execution.sell.enabled")
    require_probability(sell.min_price, "execution.sell.min_price")
    require_bool(sell.retry_partial_once, "execution.sell.retry_partial_once")
    require_number(
        sell.retry_delay_seconds,
        "execution.sell.retry_delay_seconds",
        minimum=0,
    )
    require_probability(sell.trim_fraction, "execution.sell.trim_fraction")
    require_probability(
        sell.max_fraction_per_order,
        "execution.sell.max_fraction_per_order",
        allow_zero=False,
    )
    rotation = execution.buy_rotation
    require_bool(rotation.enabled, "execution.buy_rotation.enabled")
    require_probability(
        rotation.max_price,
        "execution.buy_rotation.max_price",
        allow_zero=False,
    )
    require_number(
        rotation.usd_budget,
        "execution.buy_rotation.usd_budget",
        minimum=0,
        minimum_exclusive=rotation.enabled,
    )
    require_bool(
        rotation.skip_if_above_cap,
        "execution.buy_rotation.skip_if_above_cap",
    )
    require_probability(
        rotation.max_spread,
        "execution.buy_rotation.max_spread",
        allow_zero=False,
    )

    validate_time_decay_config(config.time_decay)
    alerts = config.monitoring.price_alerts
    require_bool(alerts.enabled, "monitoring.price_alerts.enabled")
    require_text(alerts.outcome, "monitoring.price_alerts.outcome", allow_empty=True)
    if not isinstance(alerts.thresholds, list):
        raise ValueError("monitoring.price_alerts.thresholds must be a list")
    for index, threshold in enumerate(alerts.thresholds):
        require_probability(
            threshold,
            f"monitoring.price_alerts.thresholds[{index}]",
        )
    if alerts.enabled and config.outcome(alerts.outcome) is None:
        raise ValueError("monitoring.price_alerts.outcome must name a configured outcome")
    require_bool(
        alerts.live_quotes_in_dry_run,
        "monitoring.price_alerts.live_quotes_in_dry_run",
    )
    require_bool(config.monitoring.heartbeat.enabled, "monitoring.heartbeat.enabled")
    require_number(
        config.monitoring.heartbeat.interval_hours,
        "monitoring.heartbeat.interval_hours",
        minimum=0,
        minimum_exclusive=True,
    )
    require_bool(
        config.monitoring.market_verification.enabled,
        "monitoring.market_verification.enabled",
    )
    require_number(
        config.monitoring.market_verification.interval_minutes,
        "monitoring.market_verification.interval_minutes",
        minimum=0,
        minimum_exclusive=True,
    )

    validate_classifier_config(
        config.classifier,
        allowed_providers={
            "rule_based",
            "openai",
            "anthropic",
            "codex_cli",
            "codex-cli",
            "codex",
            "claude_cli",
            "claude-cli",
            "claude_code_cli",
        },
    )
    validate_portfolio_link_config(config.portfolio)
    validate_safety_config(config.safety)
    validate_sources_config(config.sources)


def _validate_entry(config: LocationBotConfig) -> None:
    require_bool(config.entry.enabled, "entry.enabled")
    require_string_list(config.entry.targets, "entry.targets")
    outcome_names = {o.name for o in config.outcomes}
    unknown = sorted(config.entry_target_names() - outcome_names)
    if unknown:
        raise ValueError(f"entry.targets not found in outcomes: {', '.join(unknown)}")
    if not config.event.held_location and not config.entry.enabled:
        raise ValueError("event.held_location is empty and entry is disabled: nothing to protect or enter")
    if config.entry.enabled and not config.entry.targets:
        raise ValueError("entry.enabled requires at least one entry.targets outcome key")
    if config.entry.enabled and config.event.held_location and config.event.held_location in config.entry_target_names():
        raise ValueError("event.held_location must not be listed in entry.targets (it is already held)")
    if config.event.held_location and config.outcome(config.event.held_location) is None:
        raise ValueError(f"event.held_location {config.event.held_location!r} not found in outcomes")
    require_number(
        config.entry.usd_budget,
        "entry.usd_budget",
        minimum=0,
    )
    if config.entry.usd_budget <= 0:
        raise ValueError("entry.usd_budget must be positive")
    for name, value in {
        "entry.max_price": config.entry.max_price,
        "entry.confirmed_probability": config.entry.confirmed_probability,
        "entry.max_spread": config.entry.max_spread,
    }.items():
        require_number(value, name)
        if value <= 0 or value > 1:
            raise ValueError(f"{name} must be in (0, 1]")
    for name, value in {
        "entry.min_edge": config.entry.min_edge,
        "entry.slippage_buffer": config.entry.slippage_buffer,
        "entry.resolution_risk_buffer": config.entry.resolution_risk_buffer,
        "entry.min_fill_usd": config.entry.min_fill_usd,
        "entry.reconcile_min_shares": config.entry.reconcile_min_shares,
    }.items():
        require_number(value, name, minimum=0)
    require_number(config.entry.min_fill_fraction, "entry.min_fill_fraction")
    if config.entry.min_fill_fraction < 0 or config.entry.min_fill_fraction > 1:
        raise ValueError("entry.min_fill_fraction must be in [0, 1]")
    for field_name in (
        "second_source_above_usd",
        "second_source_window_minutes",
        "corroboration_minutes",
    ):
        require_number(
            getattr(config.entry, field_name),
            f"entry.{field_name}",
            minimum=0,
        )
    if (
        config.entry.second_source_above_usd > 0
        and config.entry.second_source_window_minutes <= 0
    ):
        raise ValueError(
            "entry.second_source_above_usd requires a positive second_source_window_minutes"
        )
    if config.entry.corroboration_action not in {"alert", "trim"}:
        raise ValueError("entry.corroboration_action must be alert or trim")
    require_integer(config.entry.max_entries, "entry.max_entries", minimum=1)
    if config.entry.confirmed_probability <= (
        config.entry.slippage_buffer + config.entry.resolution_risk_buffer
    ):
        raise ValueError("entry confirmation probability must exceed execution/rule-risk buffers")


def _validate_forecast(config: LocationBotConfig) -> None:
    forecast = config.forecast
    require_bool(forecast.enabled, "forecast.enabled")
    require_bool(forecast.paper_only, "forecast.paper_only")
    require_bool(
        forecast.live_quotes_in_dry_run,
        "forecast.live_quotes_in_dry_run",
    )
    if not forecast.paper_only:
        raise ValueError("forecast.paper_only must remain true; anticipatory live execution is not implemented")
    for name, value in {
        "min_paper_edge": forecast.min_paper_edge,
        "slippage_buffer": forecast.slippage_buffer,
        "resolution_risk_buffer": forecast.resolution_risk_buffer,
        "exit_remaining_edge": forecast.exit_remaining_edge,
        "max_spread": forecast.max_spread,
        "fee_rate": forecast.fee_rate,
        "simulated_slippage": forecast.simulated_slippage,
    }.items():
        require_probability(value, f"forecast.{name}")
    require_probability(
        forecast.max_paper_price,
        "forecast.max_paper_price",
        allow_zero=False,
    )
    require_number(
        forecast.paper_order_usd,
        "forecast.paper_order_usd",
        minimum=0,
        minimum_exclusive=True,
    )
    require_text(forecast.model_version, "forecast.model_version")
    require_number(
        forecast.quote_refresh_seconds,
        "forecast.quote_refresh_seconds",
        minimum=0,
    )
    require_number(
        forecast.max_quote_age_seconds,
        "forecast.max_quote_age_seconds",
        minimum=0,
        minimum_exclusive=True,
    )
    require_integer(
        forecast.max_processed_articles,
        "forecast.max_processed_articles",
        minimum=1,
    )
    if not isinstance(forecast.prior_probabilities, dict):
        raise ValueError("forecast.prior_probabilities must be an object")
    for mapping_name, mapping in {
        "source_likelihoods": forecast.source_likelihoods,
        "evidence_likelihoods": forecast.evidence_likelihoods,
    }.items():
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError(f"forecast.{mapping_name} must be a non-empty object")
        for key, value in mapping.items():
            require_text(key, f"forecast.{mapping_name} key")
            require_number(
                value,
                f"forecast.{mapping_name}[{key!r}]",
                minimum=0,
                minimum_exclusive=True,
            )
    if not forecast.enabled:
        return
    outcome_names = {outcome.name for outcome in config.outcomes}
    prior_names = {str(name).strip().lower().replace(" ", "_") for name in forecast.prior_probabilities}
    if prior_names != outcome_names:
        missing = sorted(outcome_names - prior_names)
        extra = sorted(prior_names - outcome_names)
        raise ValueError(f"forecast priors must cover every outcome exactly; missing={missing}, extra={extra}")
    for name, value in forecast.prior_probabilities.items():
        require_probability(value, f"forecast.prior_probabilities[{name!r}]")
    priors = [float(value) for value in forecast.prior_probabilities.values()]
    if sum(priors) <= 0:
        raise ValueError("forecast prior probabilities must be non-negative with positive total")
    if abs(sum(priors) - 1.0) > 1e-6:
        raise ValueError("forecast prior probabilities must sum to 1")


def _normalize_outcome(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("each outcome must be an object")
    if "name" not in item:
        raise ValueError("each outcome requires name")
    normalized = dict(item)
    normalized["name"] = str(item["name"]).strip().lower().replace(" ", "_")
    return normalized


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value
