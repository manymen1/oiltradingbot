from __future__ import annotations

import math
from dataclasses import fields
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

from .config import ClassifierConfig, SafetyConfig, SourcesConfig
from .portfolio import PortfolioConfig


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys.

    PyYAML normally keeps the last value silently. In a trading config that
    turns a duplicated ``dry_run``, cap, or operator-mode key into an invisible
    policy override, so duplicates are configuration errors.
    """


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ValueError(
                f"unhashable YAML key at line {key_node.start_mark.line + 1}"
            ) from exc
        if duplicate:
            raise ValueError(
                f"duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_yaml_object(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return raw


def reject_unknown_keys(
    raw: dict[str, Any],
    allowed: Iterable[str],
    *,
    context: str,
) -> None:
    unknown = sorted(str(key) for key in set(raw) - set(allowed))
    if unknown:
        raise ValueError(f"{context} contains unknown keys: {', '.join(unknown)}")


def reject_unknown_dataclass_keys(
    raw: dict[str, Any],
    config_type: type[Any],
    *,
    context: str,
) -> None:
    reject_unknown_keys(
        raw,
        (item.name for item in fields(config_type)),
        context=context,
    )


def require_bool(value: Any, name: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")


def require_integer(
    value: Any,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")


def require_number(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    minimum_exclusive: bool = False,
    maximum_exclusive: bool = False,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None:
        invalid = numeric <= minimum if minimum_exclusive else numeric < minimum
        if invalid:
            operator = "greater than" if minimum_exclusive else "at least"
            raise ValueError(f"{name} must be {operator} {minimum:g}")
    if maximum is not None:
        invalid = numeric >= maximum if maximum_exclusive else numeric > maximum
        if invalid:
            operator = "less than" if maximum_exclusive else "at most"
            raise ValueError(f"{name} must be {operator} {maximum:g}")


def require_probability(
    value: Any,
    name: str,
    *,
    allow_zero: bool = True,
    allow_one: bool = True,
) -> None:
    require_number(
        value,
        name,
        minimum=0.0,
        maximum=1.0,
        minimum_exclusive=not allow_zero,
        maximum_exclusive=not allow_one,
    )


def require_text(value: Any, name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} must not be empty")


def require_string_list(
    value: Any,
    name: str,
    *,
    allow_empty_items: bool = False,
) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    for index, item in enumerate(value):
        require_text(item, f"{name}[{index}]", allow_empty=allow_empty_items)


def require_iso_date(value: Any, name: str, *, allow_empty: bool = False) -> date | None:
    require_text(value, name, allow_empty=allow_empty)
    if not str(value).strip():
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD)") from exc


def require_iso_datetime(
    value: Any,
    name: str,
    *,
    allow_empty: bool = False,
) -> datetime | None:
    require_text(value, name, allow_empty=allow_empty)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date-time") from exc


def validate_classifier_config(
    config: ClassifierConfig,
    *,
    allowed_providers: set[str],
    prefix: str = "classifier",
) -> None:
    require_text(config.provider, f"{prefix}.provider")
    provider = config.provider.strip().lower()
    normalized_allowed = {item.strip().lower() for item in allowed_providers}
    if provider not in normalized_allowed:
        raise ValueError(
            f"{prefix}.provider must be one of {sorted(normalized_allowed)!r}, "
            f"got {config.provider!r}"
        )
    require_text(config.model, f"{prefix}.model")
    require_number(config.temperature, f"{prefix}.temperature", minimum=0, maximum=2)
    require_integer(config.passes, f"{prefix}.passes", minimum=1)
    for field_name in (
        "require_pass_agreement",
        "require_verbatim_quote",
        "include_market_rule_text",
        "classify_feed_summaries",
    ):
        require_bool(getattr(config, field_name), f"{prefix}.{field_name}")
    if config.require_pass_agreement and config.passes < 2:
        raise ValueError(f"{prefix}.require_pass_agreement requires passes >= 2")
    for field_name in (
        "max_escalations_per_hour",
        "max_escalations_per_day",
        "max_classifier_errors_per_hour",
    ):
        require_integer(getattr(config, field_name), f"{prefix}.{field_name}", minimum=0)
    require_integer(config.cli_timeout_seconds, f"{prefix}.cli_timeout_seconds", minimum=1)
    require_text(config.cli_binary, f"{prefix}.cli_binary")
    require_text(config.if_api_down, f"{prefix}.if_api_down")
    require_text(config.screen_model, f"{prefix}.screen_model", allow_empty=True)
    require_text(config.budget_db_path, f"{prefix}.budget_db_path", allow_empty=True)


def validate_safety_config(config: SafetyConfig, *, prefix: str = "safety") -> None:
    for field_name in (
        "one_shot",
        "cancel_open_orders_first",
        "query_live_position",
        "verify_fills_before_final_lock",
        "quote_must_match_article_text",
        "token_mapping_must_match",
        "yes_cap_never_blocks_no_sell",
        "degraded_mode_alert",
    ):
        require_bool(getattr(config, field_name), f"{prefix}.{field_name}")
    require_integer(config.max_executions, f"{prefix}.max_executions", minimum=1)
    require_number(
        config.poll_seconds,
        f"{prefix}.poll_seconds",
        minimum=0,
        minimum_exclusive=True,
    )
    require_number(config.armed_poll_seconds, f"{prefix}.armed_poll_seconds", minimum=0)


def validate_sources_config(config: SourcesConfig, *, prefix: str = "sources") -> None:
    for field_name in (
        "auto_trade_domains",
        "alert_only_domains",
        "poll_urls",
        "feed_urls",
        "feed_include_terms",
        "feed_exclude_terms",
    ):
        require_string_list(getattr(config, field_name), f"{prefix}.{field_name}")
    auto_domains = {item.strip().lower() for item in config.auto_trade_domains}
    alert_domains = {item.strip().lower() for item in config.alert_only_domains}
    overlap = sorted(auto_domains & alert_domains)
    if overlap:
        raise ValueError(
            f"{prefix}.auto_trade_domains and alert_only_domains overlap: "
            f"{', '.join(overlap)}"
        )
    for field_name in (
        "allow_feed_auto_trade",
        "promote_feed_to_article",
        "allow_unknown_age_poll_auto_trade",
        "log_book_snapshots",
    ):
        require_bool(getattr(config, field_name), f"{prefix}.{field_name}")
    require_integer(
        config.max_feed_entries_per_cycle,
        f"{prefix}.max_feed_entries_per_cycle",
        minimum=1,
    )
    require_number(
        config.central_feed_stale_after_seconds,
        f"{prefix}.central_feed_stale_after_seconds",
        minimum=0,
        minimum_exclusive=True,
    )
    require_number(
        config.max_trade_article_age_hours,
        f"{prefix}.max_trade_article_age_hours",
        minimum=0,
    )
    for field_name in ("source_plan_sha256", "central_feed_db"):
        require_text(getattr(config, field_name), f"{prefix}.{field_name}", allow_empty=True)


def validate_portfolio_link_config(
    config: PortfolioConfig,
    *,
    prefix: str = "portfolio",
) -> None:
    for field_name in (
        "ledger_path",
        "market_id",
        "event_slug",
        "correlation_group",
        "region",
        "deadline_iso",
    ):
        require_text(getattr(config, field_name), f"{prefix}.{field_name}", allow_empty=True)
    if config.ledger_path.strip() and not config.market_id.strip():
        raise ValueError(f"{prefix}.ledger_path requires {prefix}.market_id")
    if config.deadline_iso.strip():
        require_iso_datetime(config.deadline_iso, f"{prefix}.deadline_iso")


def validate_time_decay_config(config: Any, *, prefix: str = "time_decay") -> None:
    require_bool(config.enabled, f"{prefix}.enabled")
    trim_date = require_iso_date(
        config.trim_after_date,
        f"{prefix}.trim_after_date",
        allow_empty=True,
    )
    exit_date = require_iso_date(
        config.exit_after_date,
        f"{prefix}.exit_after_date",
        allow_empty=True,
    )
    if config.enabled and trim_date is None and exit_date is None:
        raise ValueError(
            f"{prefix}.enabled requires trim_after_date or exit_after_date"
        )
    if trim_date is not None and exit_date is not None and trim_date > exit_date:
        raise ValueError(f"{prefix}.trim_after_date must not be after exit_after_date")
    require_probability(config.trim_fraction, f"{prefix}.trim_fraction")
    require_probability(config.min_trim_price, f"{prefix}.min_trim_price")
    require_probability(config.min_exit_price, f"{prefix}.min_exit_price")


__all__ = [
    "load_yaml_object",
    "reject_unknown_dataclass_keys",
    "reject_unknown_keys",
    "require_bool",
    "require_integer",
    "require_iso_date",
    "require_iso_datetime",
    "require_number",
    "require_probability",
    "require_string_list",
    "require_text",
    "validate_classifier_config",
    "validate_portfolio_link_config",
    "validate_safety_config",
    "validate_sources_config",
    "validate_time_decay_config",
]
