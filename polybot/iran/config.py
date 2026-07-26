from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polybot.core.config import (
    DEFAULT_ALERT_ONLY_DOMAINS,
    DEFAULT_AUTO_TRADE_DOMAINS,
    ClassifierConfig,
    SafetyConfig,
    SourcesConfig,
)
from polybot.core.config_validation import (
    load_yaml_object,
    reject_unknown_dataclass_keys,
    require_bool,
    require_integer,
    require_number,
    require_probability,
    require_text,
    validate_classifier_config,
    validate_safety_config,
    validate_sources_config,
    validate_time_decay_config,
)


@dataclass(frozen=True)
class MarketConfig:
    slug: str
    target_leg: str = "July 17"
    held_side: str = "YES"
    expected_question_contains: str = "July 17"
    expected_rule_text_sha256: str | None = None


@dataclass(frozen=True)
class PositionConfig:
    source: str = "onchain"
    expected_yes_token_id: str = ""
    expected_no_token_id: str = ""
    max_no_shares_to_sell: float = 100000.0
    max_yes_shares_to_sell: float = 100000.0
    max_yes_usd_to_buy: float = 100.0
    max_no_usd_to_buy: float = 100.0


@dataclass(frozen=True)
class TriggerConfig:
    auto_execute_level: int = 4
    require_two_sources: bool = False
    trusted_single_source_execution: bool = True


@dataclass(frozen=True)
class SellNoConfig:
    enabled: bool = True
    min_price: float = 0.03
    retry_partial_once: bool = True
    retry_delay_seconds: float = 2.0


@dataclass(frozen=True)
class BuyYesConfig:
    enabled: bool = True
    max_price_level4a: float = 0.90
    max_price_level4b: float = 0.95
    usd_budget: float = 100.0
    skip_if_above_cap: bool = True


@dataclass(frozen=True)
class SellYesConfig:
    enabled: bool = True
    min_price: float = 0.03
    retry_partial_once: bool = True
    retry_delay_seconds: float = 2.0
    trim_fraction: float = 0.25


@dataclass(frozen=True)
class BuyNoConfig:
    enabled: bool = True
    max_price_exit: float = 0.90
    usd_budget: float = 100.0
    skip_if_above_cap: bool = True


@dataclass(frozen=True)
class ExecutionConfig:
    dry_run: bool = True
    order_type: str = "FAK"
    sell_no: SellNoConfig = field(default_factory=SellNoConfig)
    buy_yes: BuyYesConfig = field(default_factory=BuyYesConfig)
    sell_yes: SellYesConfig = field(default_factory=SellYesConfig)
    buy_no: BuyNoConfig = field(default_factory=BuyNoConfig)


@dataclass(frozen=True)
class TimeDecayConfig:
    enabled: bool = False
    trim_after_date: str = ""
    exit_after_date: str = ""
    trim_fraction: float = 0.25
    suspend_exit_on_scheduled_signal: bool = True
    scheduled_signal_suspension_days: int = 3
    min_trim_price: float = 0.0
    min_exit_price: float = 0.0


@dataclass(frozen=True)
class IranBotConfig:
    market: MarketConfig
    position: PositionConfig = field(default_factory=PositionConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    time_decay: TimeDecayConfig = field(default_factory=TimeDecayConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    data_dir: Path = Path("data/iran-protection-bot")
    logs_dir: Path = Path("logs")


def load_iran_config(path: Path) -> IranBotConfig:
    raw = load_yaml_object(path)
    reject_unknown_dataclass_keys(raw, IranBotConfig, context=str(path))
    market_raw = dict(_section(raw, "market"))
    market_raw["held_side"] = str(
        market_raw.get("held_side", MarketConfig.held_side) or ""
    ).strip().upper()
    config = IranBotConfig(
        market=MarketConfig(**market_raw),
        position=PositionConfig(**_section(raw, "position")),
        trigger=TriggerConfig(**_section(raw, "trigger")),
        classifier=ClassifierConfig(**_section(raw, "classifier")),
        execution=ExecutionConfig(
            **{
                key: value
                for key, value in _section(raw, "execution").items()
                if key not in {"sell_no", "buy_yes", "sell_yes", "buy_no"}
            },
            sell_no=SellNoConfig(**_section(_section(raw, "execution"), "sell_no")),
            buy_yes=BuyYesConfig(**_section(_section(raw, "execution"), "buy_yes")),
            sell_yes=SellYesConfig(**_section(_section(raw, "execution"), "sell_yes")),
            buy_no=BuyNoConfig(**_section(_section(raw, "execution"), "buy_no")),
        ),
        time_decay=TimeDecayConfig(**_section(raw, "time_decay")),
        safety=SafetyConfig(**_section(raw, "safety")),
        sources=SourcesConfig(**_section(raw, "sources")),
        data_dir=Path(str(raw.get("data_dir", "data/iran-protection-bot"))),
        logs_dir=Path(str(raw.get("logs_dir", "logs"))),
    )
    validate_iran_config(config)
    return config


def validate_iran_config(config: IranBotConfig) -> None:
    require_text(config.market.slug, "market.slug")
    require_text(config.market.target_leg, "market.target_leg")
    if config.market.held_side not in {"YES", "NO"}:
        raise ValueError("market.held_side must be YES or NO")
    require_text(
        config.market.expected_question_contains,
        "market.expected_question_contains",
    )
    if config.market.expected_rule_text_sha256 is not None:
        require_text(
            config.market.expected_rule_text_sha256,
            "market.expected_rule_text_sha256",
            allow_empty=True,
        )

    position = config.position
    require_text(position.source, "position.source")
    require_text(
        position.expected_yes_token_id,
        "position.expected_yes_token_id",
        allow_empty=True,
    )
    require_text(
        position.expected_no_token_id,
        "position.expected_no_token_id",
        allow_empty=True,
    )
    if (
        position.expected_yes_token_id
        and position.expected_yes_token_id == position.expected_no_token_id
    ):
        raise ValueError("position YES and NO token ids must be distinct")
    for field_name in (
        "max_no_shares_to_sell",
        "max_yes_shares_to_sell",
        "max_yes_usd_to_buy",
        "max_no_usd_to_buy",
    ):
        require_number(
            getattr(position, field_name),
            f"position.{field_name}",
            minimum=0,
        )

    trigger = config.trigger
    require_integer(
        trigger.auto_execute_level,
        "trigger.auto_execute_level",
        minimum=1,
        maximum=4,
    )
    require_bool(trigger.require_two_sources, "trigger.require_two_sources")
    require_bool(
        trigger.trusted_single_source_execution,
        "trigger.trusted_single_source_execution",
    )
    if trigger.require_two_sources and trigger.trusted_single_source_execution:
        raise ValueError(
            "trigger.require_two_sources conflicts with trusted_single_source_execution"
        )

    execution = config.execution
    require_bool(execution.dry_run, "execution.dry_run")
    if execution.order_type != "FAK":
        raise ValueError("execution.order_type must be FAK")
    sell_no = execution.sell_no
    require_bool(sell_no.enabled, "execution.sell_no.enabled")
    require_probability(sell_no.min_price, "execution.sell_no.min_price")
    require_bool(
        sell_no.retry_partial_once,
        "execution.sell_no.retry_partial_once",
    )
    require_number(
        sell_no.retry_delay_seconds,
        "execution.sell_no.retry_delay_seconds",
        minimum=0,
    )
    buy_yes = execution.buy_yes
    require_bool(buy_yes.enabled, "execution.buy_yes.enabled")
    require_probability(
        buy_yes.max_price_level4a,
        "execution.buy_yes.max_price_level4a",
        allow_zero=False,
    )
    require_probability(
        buy_yes.max_price_level4b,
        "execution.buy_yes.max_price_level4b",
        allow_zero=False,
    )
    if buy_yes.max_price_level4a > buy_yes.max_price_level4b:
        raise ValueError(
            "execution.buy_yes.max_price_level4a must not exceed max_price_level4b"
        )
    require_number(
        buy_yes.usd_budget,
        "execution.buy_yes.usd_budget",
        minimum=0,
        minimum_exclusive=buy_yes.enabled,
    )
    require_bool(
        buy_yes.skip_if_above_cap,
        "execution.buy_yes.skip_if_above_cap",
    )
    sell_yes = execution.sell_yes
    require_bool(sell_yes.enabled, "execution.sell_yes.enabled")
    require_probability(sell_yes.min_price, "execution.sell_yes.min_price")
    require_bool(
        sell_yes.retry_partial_once,
        "execution.sell_yes.retry_partial_once",
    )
    require_number(
        sell_yes.retry_delay_seconds,
        "execution.sell_yes.retry_delay_seconds",
        minimum=0,
    )
    require_probability(sell_yes.trim_fraction, "execution.sell_yes.trim_fraction")
    buy_no = execution.buy_no
    require_bool(buy_no.enabled, "execution.buy_no.enabled")
    require_probability(
        buy_no.max_price_exit,
        "execution.buy_no.max_price_exit",
        allow_zero=False,
    )
    require_number(
        buy_no.usd_budget,
        "execution.buy_no.usd_budget",
        minimum=0,
        minimum_exclusive=buy_no.enabled,
    )
    require_bool(buy_no.skip_if_above_cap, "execution.buy_no.skip_if_above_cap")

    validate_time_decay_config(config.time_decay)
    require_bool(
        config.time_decay.suspend_exit_on_scheduled_signal,
        "time_decay.suspend_exit_on_scheduled_signal",
    )
    require_integer(
        config.time_decay.scheduled_signal_suspension_days,
        "time_decay.scheduled_signal_suspension_days",
        minimum=0,
    )
    validate_classifier_config(
        config.classifier,
        allowed_providers={
            "rule_based",
            "openai",
            "anthropic",
            "claude_cli",
            "claude-cli",
            "claude_code_cli",
        },
    )
    validate_safety_config(config.safety)
    validate_sources_config(config.sources)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value
