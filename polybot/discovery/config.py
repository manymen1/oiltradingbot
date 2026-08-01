from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from polybot.core.config import ClassifierConfig  # noqa: F401
from polybot.core.config_validation import (
    load_yaml_object,
    reject_unknown_dataclass_keys,
    require_bool,
    require_integer,
    require_iso_datetime,
    require_number,
    require_probability,
    require_string_list,
    require_text,
    validate_classifier_config,
)
from polybot.core.portfolio import AllocatorConfig  # noqa: F401


def _default_include_keywords() -> list[str]:
    return [
        # diplomacy / conflict / statecraft vocabulary that marks a market as
        # geopolitical; matched against question + description + tags.
        "ceasefire", "peace talks", "negotiation", "sanction", "sanctions",
        "treaty", "diplomatic", "diplomacy", "military", "invasion", "strike",
        "missile", "nuclear", "election", "president", "prime minister",
        "government", "parliament", "coup", "war", "truce", "hostage",
        "annex", "border", "summit", "nato", "united nations", "security council",
        "mediator", "ambassador", "minister", "regime",
    ]


def _default_exclude_keywords() -> list[str]:
    return [
        "bitcoin", "ethereum", "crypto", "nba", "nfl", "mlb", "premier league",
        "champions league", "oscars", "grammy", "box office", "album",
        "temperature", "rainfall", "stock", "s&p", "nasdaq", "fed rate",
        "airdrop", "token launch", "tiktok followers",
        # Sports and esports leak through the diplomacy vocabulary --
        # "strike" matches Counter-Strike, "world" cups abound -- and burned
        # trade-grade opus calls grading Valorant brackets. Excludes run
        # BEFORE includes, so these are safe against the include list.
        "world cup", "fifa", "uefa", "olympic", "super bowl", "playoff",
        "counter-strike", "cs2", "csgo", "valorant", "esports", "hltv",
        "league of legends", "dota", "overwatch", "fortnite",
        "ufc", "boxing", "tennis", "golf", "formula 1", "grand prix",
        "wrestling", "wnba", "nhl", "la liga", "serie a", "bundesliga",
    ]


def _default_include_tags() -> list[str]:
    return ["geopolitics", "politics", "world", "middle east", "war", "elections", "foreign policy"]


@dataclass(frozen=True)
class EnumerationView:
    """One documented Gamma keyset ordering used to audit universe coverage."""

    order: str
    ascending: bool

    @classmethod
    def from_dict(cls, raw: Any, *, index: int) -> "EnumerationView":
        if not isinstance(raw, dict):
            raise ValueError(
                f"universe.enumeration_views[{index}] must be an object"
            )
        unknown = sorted(set(raw) - {"order", "ascending"})
        if unknown:
            raise ValueError(
                f"universe.enumeration_views[{index}] contains unknown fields: "
                + ", ".join(unknown)
            )
        order = raw.get("order")
        ascending = raw.get("ascending")
        require_text(order, f"universe.enumeration_views[{index}].order")
        require_bool(
            ascending,
            f"universe.enumeration_views[{index}].ascending",
        )
        return cls(order=str(order), ascending=bool(ascending))


def _default_enumeration_views() -> list[EnumerationView]:
    # Gamma documents `order` as a comma-separated JSON-field list. Multiple
    # views make coverage robust to markets that are thin, old, or far from
    # the first liquidity-ranked page; stable event ids deduplicate them.
    return [
        EnumerationView(order="liquidity", ascending=False),
        EnumerationView(order="volume", ascending=False),
        EnumerationView(order="startDate", ascending=False),
        EnumerationView(order="endDate", ascending=True),
    ]


@dataclass(frozen=True)
class UniverseConfig:
    """What to enumerate and which events count as geopolitical candidates."""

    # 0 is intentionally uncapped. A positive value is an operator-declared
    # truncation and can never be represented as complete universe coverage.
    max_events: int = 0
    page_size: int = 100
    fail_on_truncation: bool = True
    enumeration_views: list[EnumerationView] = field(
        default_factory=_default_enumeration_views
    )
    pagination_safety_pages: int = 10_000
    # Liquidity/volume floors DEFAULT OFF: thin markets are the niche where a
    # confirmation bot out-waits bigger players, so liquidity must size orders,
    # never disqualify markets. Set floors only to skip literal dust.
    min_liquidity: float = 0.0
    min_volume: float = 0.0
    max_days_to_deadline: float = 365.0
    include_keywords: list[str] = field(default_factory=_default_include_keywords)
    exclude_keywords: list[str] = field(default_factory=_default_exclude_keywords)
    include_tags: list[str] = field(default_factory=_default_include_tags)


@dataclass(frozen=True)
class ScoringConfig:
    """Thresholds for the eligibility state machine."""

    min_rule_text_chars: int = 200
    # Discretionary rules can NEVER trade live. But excluding them from paper
    # too means the soak generates zero evidence about markets whose rules are
    # clear and observable yet contain one judgment word ("generally ceases",
    # "substantially complete") -- which describes the best-observability
    # announcement markets in the Iran theater (clarity 0.78, observability
    # 0.90). Paper cannot lose money, and the misfire corpus is the point.
    allow_discretionary_paper: bool = False
    min_clarity_live: float = 0.75
    min_clarity_paper: float = 0.55
    min_observability_live: float = 0.7
    min_observability_paper: float = 0.5
    min_automation_live: float = 0.7
    max_resolution_risk_live: float = 0.35
    # Liquidity gates DEFAULT OFF (0 = disabled): liquidity sizes orders via
    # the recommendation below, it never blocks eligibility. Thin markets are
    # where confirmation edge persists longest -- professionals do not compete
    # for $20 of edge. Set thresholds >0 only if you explicitly want floors.
    min_liquidity_live: float = 0.0
    min_liquidity_paper: float = 0.0
    max_spread_live: float = 0.10
    max_days_to_deadline_live: float = 180.0
    max_markets_per_correlation_group: int = 2
    # Book-aware order sizing: every graded market gets
    # recommended_max_order_usd = max(min_order, liquidity * fraction); the
    # opportunity scan, config emission, and fleet all size to it (capped by
    # the allocator per-order limit). Deep books hit the per-order cap; thin
    # books trade at what they can absorb, floored at a minimum viable order.
    small_live_enabled: bool = True
    small_live_liquidity_fraction: float = 0.02
    small_live_min_order_usd: float = 5.0
    # The offline fixture rule analyzer may never produce live-eligible
    # markets (tests opt in explicitly).
    allow_fixture_analysis_live: bool = False
    # Per-market resolution-risk scaling: effective buffer =
    # opportunity.resolution_risk_buffer + analyzer_resolution_risk * this.
    resolution_risk_scale: float = 0.05


@dataclass(frozen=True)
class OpportunityConfig:
    """Edge accounting: estimated probability must clear the executable price
    plus every buffer by min_edge before an outcome is an opportunity."""

    # Model-priced opportunities start in observation mode: estimates, market
    # mids, edges, and eventual resolutions are recorded, but the allocator
    # cannot size them. Promotion to "allocatable" is explicit and config
    # loading rejects a weight/buffer combination whose theoretical best edge
    # cannot clear min_edge. This does not disable independent confirmation
    # bots or grouped-market consistency arbitrage.
    model_pricing_mode: str = "calibration_only"
    min_edge: float = 0.05
    slippage_buffer: float = 0.01
    resolution_risk_buffer: float = 0.02
    model_uncertainty_buffer: float = 0.03
    max_entry_price: float = 0.90
    max_spread: float = 0.15
    # Operator-supplied probability estimates: market_id -> outcome -> p.
    # Forecast paper state, when present and fresh, overrides these.
    probability_estimates: dict[str, dict[str, float]] = field(default_factory=dict)
    # Where emitted executor configs keep their data dirs; the paper forecast
    # engine persists forecast_probability.json under
    # <root>/<market-slug>[/dry_run]/ and the scan reads it from there.
    forecast_data_root: str = "data/geopolitics"
    forecast_max_age_hours: float = 24.0
    # Effective resolution-risk buffer = resolution_risk_buffer +
    # analyzer_resolution_risk * resolution_risk_scale (per market).
    resolution_risk_scale: float = 0.05
    # Market-anchored blending: the market mid is itself a calibrated
    # probability estimator, usually better than an operator guess. The scan
    # prices edges with model_weight*model + (1-model_weight)*mid, so a
    # standing disagreement with the market must be LARGE to trade. Raise the
    # weight only after the calibration report proves the model beats the
    # market's own Brier score. 1.0 disables anchoring.
    model_weight: float = 0.35
    # Extra uncertainty buffer = |model - mid| * this scale: a big standing
    # disagreement with the crowd is itself evidence the model may be wrong,
    # so the edge bar rises exactly where miscalibration hurts most.
    disagreement_buffer_scale: float = 0.25
    # Forecast-state probabilities may only price allocatable opportunities
    # after the calibration report marks them calibrated (Brier beats the
    # market mid over min_resolved_for_calibration resolved outcomes).
    # Ungated they still appear in the scan/funnel with a blocker.
    require_calibrated_forecast: bool = True
    min_resolved_for_calibration: int = 20
    # Overpriced markets are edge too: price the NO side of every outcome
    # (executable NO ask = 1 - YES bid) with the same buffers and anchoring.
    scan_no_side: bool = True
    # Neg-risk internal consistency: when a grouped event's YES bids sum above
    # 1 (short every leg) or YES asks sum below 1 (buy every leg), the market
    # is arguing with itself -- no forecast needed. Minimum net edge after
    # per-leg slippage to report the arb.
    min_group_arb_edge: float = 0.02


def opportunity_reachability(config: OpportunityConfig) -> dict[str, Any]:
    """Return the best edge the model-pricing equation can ever produce.

    This assumes the most favorable possible binary book: zero spread, zero
    per-market resolution-risk surcharge, and a model probability at the
    opposite extreme from the market mid. It is therefore an upper bound, not
    an expected edge. If even this bound misses min_edge, production
    opportunity allocation is mathematically unreachable.
    """

    fixed_buffers = (
        config.slippage_buffer
        + config.resolution_risk_buffer
        + config.model_uncertainty_buffer
    )
    maximum_probability_lift = max(
        0.0,
        config.model_weight - config.disagreement_buffer_scale,
    )
    maximum_edge = maximum_probability_lift - fixed_buffers
    minimum_model_weight = (
        config.min_edge
        + config.disagreement_buffer_scale
        + fixed_buffers
    )
    return {
        "mode": config.model_pricing_mode,
        "reachable": maximum_edge + 1e-12 >= config.min_edge,
        "max_theoretical_edge": round(maximum_edge, 6),
        "min_edge": round(config.min_edge, 6),
        "edge_headroom": round(maximum_edge - config.min_edge, 6),
        "minimum_model_weight": round(minimum_model_weight, 6),
        "model_weight": round(config.model_weight, 6),
        "disagreement_buffer_scale": round(config.disagreement_buffer_scale, 6),
        "fixed_buffers": round(fixed_buffers, 6),
    }


def _validate_opportunity_config(config: OpportunityConfig) -> None:
    require_text(config.model_pricing_mode, "opportunity.model_pricing_mode")
    if config.model_pricing_mode not in {"calibration_only", "allocatable"}:
        raise ValueError(
            "opportunity.model_pricing_mode must be 'calibration_only' or 'allocatable'"
        )
    require_probability(config.model_weight, "opportunity.model_weight")
    for name, value in (
        ("min_edge", config.min_edge),
        ("slippage_buffer", config.slippage_buffer),
        ("resolution_risk_buffer", config.resolution_risk_buffer),
        ("model_uncertainty_buffer", config.model_uncertainty_buffer),
        ("disagreement_buffer_scale", config.disagreement_buffer_scale),
        ("resolution_risk_scale", config.resolution_risk_scale),
    ):
        require_number(value, f"opportunity.{name}", minimum=0)
    require_probability(config.min_edge, "opportunity.min_edge")
    reachability = opportunity_reachability(config)
    if config.model_pricing_mode == "allocatable" and not reachability["reachable"]:
        raise ValueError(
            "opportunity model pricing is unreachable in allocatable mode: "
            f"max_theoretical_edge={reachability['max_theoretical_edge']:.4f} "
            f"is below min_edge={config.min_edge:.4f}; use "
            "model_pricing_mode: calibration_only or raise model_weight to at "
            f"least {reachability['minimum_model_weight']:.4f} before "
            "market-specific risk"
        )


@dataclass(frozen=True)
class ScheduleConfig:
    """Pacing for the recurring run-discovery loop."""

    interval_minutes: float = 60.0


@dataclass(frozen=True)
class FleetConfig:
    """One supervisor process trading EVERY eligible geopolitical market.

    The fleet runs the discovery cycle, emits/refreshes an executor config per
    LIVE_CONFIRMATION_ELIGIBLE market, arms each market's operator gate with
    `position_mode`, and supervises one bot subprocess per market. Bots for
    markets that hold a position are never stopped (defense continues even
    after a market is demoted); bots for flat demoted/closed markets are
    stopped. The single master kill switch is the shared operator global mode
    file (`set-fleet-mode off`).
    """

    enabled: bool = False
    # Concurrent bot cap; <= 0 means UNCAPPED (cover every eligible market).
    # Screen-tier classification keeps per-bot cost low enough to run wide.
    max_bots: int = 0
    # Operator position mode written for every managed market: keep
    # "alert_only" for a monitoring soak; "live" arms autonomous trading.
    position_mode: str = "alert_only"
    # Automatically acknowledge generated config hashes when arming live.
    # Required for unattended trading across many markets -- the operator
    # reviews and arms THE FLEET once instead of each market. The generated
    # configs are deterministic renders of pipeline state the operator
    # configured, and the master kill switch still stops everything.
    auto_ack: bool = False
    generated_dir: str = "configs/geopolitics/generated"
    # A running bot whose heartbeat is older than this is considered hung and
    # gets terminated + restarted on the next cycle.
    heartbeat_stale_seconds: float = 300.0
    # Crash-loop guard: markets restarted more than this many times per hour
    # stop being respawned and raise a fleet alarm instead.
    max_restarts_per_hour: int = 3


@dataclass(frozen=True)
class CentralFeedConfig:
    """One feed fetcher shared by every fleet-managed market process."""

    enabled: bool = False
    # Empty resolves to <discovery data_dir>/central_feed.sqlite3.
    db_path: str = ""
    poll_seconds: float = 2.0
    max_workers: int = 12
    max_entries_per_feed: int = 50
    # Publisher feeds whose raw announcements are useful for measuring market
    # reaction even before discovery has produced a RuleSpec/SourcePlan. These
    # rows are capture evidence only; semantic readers still require their
    # own immutable source plans before they can classify or trade on them.
    impact_feed_urls: list[str] = field(default_factory=list)
    impact_poll_seconds: float = 30.0
    # Required named sources without a usable RSS/Atom endpoint are polled
    # through conditional-GET XML/JSON/HTML discovery adapters. They remain
    # centralized so adding markets cannot multiply publisher traffic.
    direct_sources_enabled: bool = True
    direct_poll_seconds: float = 2.0
    # An unchanged direct endpoint backs off exponentially to this interval.
    # Any newly inserted item immediately returns it to the fast cadence.
    direct_idle_max_seconds: float = 30.0
    max_entries_per_direct_source: int = 20
    aggregator_poll_seconds: float = 30.0
    max_urls_per_domain_per_cycle: int = 8
    retention_hours: float = 72.0
    stale_after_seconds: float = 60.0


@dataclass(frozen=True)
class ClassifierBudgetConfig:
    """Fleet-wide model-call circuit breaker shared by every market bot."""

    enabled: bool = True
    # Empty resolves to <discovery data_dir>/classifier_budget.sqlite3.
    db_path: str = ""
    # These count actual model calls, including screen and confirm passes.
    max_escalations_per_hour: int = 60
    max_escalations_per_day: int = 500
    max_classifier_errors_per_hour: int = 20
    exploitation_fraction: float = 0.70
    exploration_fraction: float = 0.20
    system_fraction: float = 0.10


@dataclass(frozen=True)
class ProfitPriorityConfig:
    """Conservative policy for monitoring/compute priority.

    This model never grants execution eligibility. Until the compatible
    forward campaign contains enough terminal labels, quote samples, stressed
    fills, and resolved trades, execution_priority remains exactly zero.
    """

    enabled: bool = True
    policy_version: str = "rules-first-profit-priority-v1"
    min_terminal_observations: int = 20
    min_human_labels: int = 100
    min_quote_samples: int = 20
    min_stressed_fill_samples: int = 20
    min_resolved_trades: int = 5
    lower_bound_z: float = 1.96
    missing_label_error_rate: float = 0.05
    processing_cost_reserve_usd: float = 0.01
    false_terminal_loss_usd: float = 50.0
    exploration_fraction: float = 0.20
    max_supported_latency_ms: int = 10_000


def _default_rule_families() -> list[str]:
    return [
        "OCCURRENCE_BEFORE_DEADLINE",
        "CATEGORICAL_EXCLUSIVE",
        "SOURCE_LOCKED_ANNOUNCEMENT",
        "STATUS_AT_DEADLINE",
        "NUMERIC_THRESHOLD",
        "DURATION_REQUIREMENT",
    ]


@dataclass(frozen=True)
class RuleCompilerConfig:
    """Migration gate for the versioned rules-first semantic layer."""

    # Off preserves legacy hand-written/generated configs. The production
    # discovery config enables it explicitly.
    enabled: bool = False
    max_per_cycle: int = 10
    # Markets explicitly selected by the operator receive one compilation
    # attempt before ordinary profit-priority ordering. This is scheduling
    # only: it does not bypass scope, semantic agreement, source, paper/live,
    # or execution gates.
    priority_market_ids: list[str] = field(default_factory=list)
    # Empty resolves to <data_dir>/rules.sqlite3.
    db_path: str = ""
    # Strict by default. The paper-only override is independently allowlisted
    # so scheduling priority can never imply semantic authority.
    deadline_authority_policy: str = "STRICT_GAMMA_MATCH_V1"
    deadline_authority_market_ids: list[str] = field(default_factory=list)
    # A reviewed RuleSpec can be imported only for markets named here. The
    # import command remains an explicit, hash-confirmed operator action and
    # never runs as part of an autonomous discovery cycle.
    reviewed_rule_market_ids: list[str] = field(default_factory=list)
    paper_families: list[str] = field(default_factory=_default_rule_families)
    # A family reaches live only after replay/calibration promotion.
    live_confirmation_families: list[str] = field(default_factory=list)


def _default_rule_runner_families() -> list[str]:
    return [
        "OCCURRENCE_BEFORE_DEADLINE",
        "CATEGORICAL_EXCLUSIVE",
        "SOURCE_LOCKED_ANNOUNCEMENT",
    ]


@dataclass(frozen=True)
class RuleRunnerConfig:
    """Opt-in generic rules-first runner.

    This surface is structurally paper-only. Enabling it changes only the
    paper fleet route; live continues through the hardened legacy executors
    until replay and forward evidence promote a family explicitly.
    """

    enabled: bool = False
    paper_execution_families: list[str] = field(
        default_factory=_default_rule_runner_families
    )
    extraction_passes: int = 2
    # A zero-model-call lane for source-specific normalizers. Only the strict
    # market/RuleSpec-bound official-claim envelope is supported; arbitrary
    # RSS/HTML/JSON never enters this path.
    deterministic_evidence_enabled: bool = True
    deterministic_evidence_policy_version: str = (
        "official-claim-envelope-v1"
    )
    deterministic_evidence_families: list[str] = field(
        default_factory=_default_rule_runner_families
    )
    poll_seconds: float = 2.0
    max_articles_per_feed: int = 50
    max_trade_article_age_hours: float = 24.0
    max_fee_schedule_age_hours: float = 24.0
    # Legacy flat fee support remains for non-rules paper adapters only. The
    # generic rules runner requires this to stay zero and reads each market's
    # explicit Gamma feeSchedule instead.
    paper_fee_bps: float = 0.0
    paper_slippage_bps: float = 25.0
    paper_max_book_age_seconds: float = 10.0
    confirmation_uncertainty_buffer: float = 0.03
    exit_price_buffer: float = 0.01


def _default_quote_survival_horizons_ms() -> list[int]:
    return [100, 250, 500, 1_000, 2_000, 5_000, 10_000]


@dataclass(frozen=True)
class ForwardRecorderConfig:
    """Credential-free forward evidence and public-book recorder."""

    enabled: bool = False
    # Empty resolves to <data_dir>/forward_recorder.sqlite3.
    db_path: str = ""
    websocket_url: str = (
        "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    )
    heartbeat_seconds: float = 10.0
    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    rest_seed: bool = True
    shared_book_service: bool = True
    max_tokens_per_connection: int = 200
    rest_seed_workers: int = 8
    max_book_levels: int = 20
    quote_survival_horizons_ms: list[int] = field(
        default_factory=_default_quote_survival_horizons_ms
    )
    max_sample_lag_ms: int = 250
    # Live soak-health policy. The service reports these checks in every
    # status snapshot and emits transition/cooldown alerts without granting
    # any execution permission.
    health_startup_grace_seconds: float = 30.0
    health_stale_after_seconds: float = 60.0
    health_growth_window_seconds: float = 60.0
    health_alert_cooldown_seconds: float = 300.0
    # Book recording normally follows the fleet's monitored set, which is
    # filtered to TRADEABLE_STATES. That couples the recorded universe to
    # grading thresholds that are themselves untested hypotheses, so a
    # MONITOR_ONLY or not-yet-graded market is never recorded and its book
    # history is unrecoverable afterwards. Setting this records every open
    # context instead. Recording grants no execution permission: the recorded
    # set is always a superset of the monitored set, never a substitute for it.
    record_all_contexts: bool = False
    # Safety valve for record_all_contexts against an unexpectedly large
    # universe: 0 is uncapped, otherwise the extra (non-monitored) contexts
    # are taken by descending volume until the cap. Monitored markets are
    # always recorded and are never displaced by the cap.
    max_recorded_markets: int = 0


@dataclass(frozen=True)
class DiscoveryConfig:
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    opportunity: OpportunityConfig = field(default_factory=OpportunityConfig)
    allocator: AllocatorConfig = field(default_factory=AllocatorConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    fleet: FleetConfig = field(default_factory=FleetConfig)
    central_feed: CentralFeedConfig = field(default_factory=CentralFeedConfig)
    classifier_budget: ClassifierBudgetConfig = field(default_factory=ClassifierBudgetConfig)
    profit_priority: ProfitPriorityConfig = field(
        default_factory=ProfitPriorityConfig
    )
    rule_compiler: RuleCompilerConfig = field(default_factory=RuleCompilerConfig)
    rule_runner: RuleRunnerConfig = field(default_factory=RuleRunnerConfig)
    forward_recorder: ForwardRecorderConfig = field(
        default_factory=ForwardRecorderConfig
    )
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    estimator: "EstimatorConfig" = field(default_factory=lambda: _estimator_config_default())
    data_dir: Path = Path("data/discovery")
    logs_dir: Path = Path("logs")


def _estimator_config_default():
    from .estimator import EstimatorConfig

    return EstimatorConfig()


def load_discovery_config(path: Path) -> DiscoveryConfig:
    raw = load_yaml_object(path)
    reject_unknown_dataclass_keys(raw, DiscoveryConfig, context=str(path))
    from .estimator import EstimatorConfig
    universe_raw = _section(raw, "universe")
    enumeration_raw = universe_raw.pop("enumeration_views", None)
    if enumeration_raw is None:
        enumeration_views = _default_enumeration_views()
    else:
        if not isinstance(enumeration_raw, list):
            raise ValueError("universe.enumeration_views must be a list")
        enumeration_views = [
            EnumerationView.from_dict(item, index=index)
            for index, item in enumerate(enumeration_raw)
        ]

    config = DiscoveryConfig(
        universe=UniverseConfig(
            **universe_raw,
            enumeration_views=enumeration_views,
        ),
        scoring=ScoringConfig(**_section(raw, "scoring")),
        opportunity=OpportunityConfig(**_section(raw, "opportunity")),
        allocator=AllocatorConfig(**_section(raw, "allocator")),
        schedule=ScheduleConfig(**_section(raw, "schedule")),
        fleet=FleetConfig(**_section(raw, "fleet")),
        central_feed=CentralFeedConfig(**_section(raw, "central_feed")),
        classifier_budget=ClassifierBudgetConfig(**_section(raw, "classifier_budget")),
        profit_priority=ProfitPriorityConfig(
            **_section(raw, "profit_priority")
        ),
        rule_compiler=RuleCompilerConfig(**_section(raw, "rule_compiler")),
        rule_runner=RuleRunnerConfig(**_section(raw, "rule_runner")),
        forward_recorder=ForwardRecorderConfig(
            **_section(raw, "forward_recorder")
        ),
        classifier=ClassifierConfig(**_section(raw, "classifier")),
        estimator=EstimatorConfig(**_section(raw, "estimator")),
        data_dir=Path(str(raw.get("data_dir", "data/discovery"))),
        logs_dir=Path(str(raw.get("logs_dir", "logs"))),
    )
    _validate_discovery_config(config)
    return config


def _validate_discovery_config(config: DiscoveryConfig) -> None:
    universe = config.universe
    require_integer(universe.max_events, "universe.max_events", minimum=0)
    require_integer(
        universe.page_size,
        "universe.page_size",
        minimum=1,
        maximum=500,
    )
    require_bool(universe.fail_on_truncation, "universe.fail_on_truncation")
    require_integer(
        universe.pagination_safety_pages,
        "universe.pagination_safety_pages",
        minimum=1,
    )
    if not universe.enumeration_views:
        raise ValueError("universe.enumeration_views must not be empty")
    seen_views: set[tuple[str, bool]] = set()
    for index, view in enumerate(universe.enumeration_views):
        if not isinstance(view, EnumerationView):
            raise ValueError(
                f"universe.enumeration_views[{index}] is invalid"
            )
        require_text(
            view.order,
            f"universe.enumeration_views[{index}].order",
        )
        require_bool(
            view.ascending,
            f"universe.enumeration_views[{index}].ascending",
        )
        identity = (view.order, view.ascending)
        if identity in seen_views:
            raise ValueError(
                "universe.enumeration_views must not contain duplicates"
            )
        seen_views.add(identity)
    require_number(universe.min_liquidity, "universe.min_liquidity", minimum=0)
    require_number(universe.min_volume, "universe.min_volume", minimum=0)
    require_number(
        universe.max_days_to_deadline,
        "universe.max_days_to_deadline",
        minimum=0,
        minimum_exclusive=True,
    )
    for field_name in ("include_keywords", "exclude_keywords", "include_tags"):
        require_string_list(getattr(universe, field_name), f"universe.{field_name}")

    scoring = config.scoring
    require_integer(scoring.min_rule_text_chars, "scoring.min_rule_text_chars", minimum=1)
    for field_name in (
        "min_clarity_live",
        "min_clarity_paper",
        "min_observability_live",
        "min_observability_paper",
        "min_automation_live",
        "max_resolution_risk_live",
        "max_spread_live",
        "small_live_liquidity_fraction",
    ):
        require_probability(getattr(scoring, field_name), f"scoring.{field_name}")
    for field_name in (
        "min_liquidity_live",
        "min_liquidity_paper",
        "small_live_min_order_usd",
        "resolution_risk_scale",
    ):
        require_number(getattr(scoring, field_name), f"scoring.{field_name}", minimum=0)
    require_number(
        scoring.max_days_to_deadline_live,
        "scoring.max_days_to_deadline_live",
        minimum=0,
        minimum_exclusive=True,
    )
    require_integer(
        scoring.max_markets_per_correlation_group,
        "scoring.max_markets_per_correlation_group",
        minimum=1,
    )
    for field_name in (
        "allow_discretionary_paper",
        "small_live_enabled",
        "allow_fixture_analysis_live",
    ):
        require_bool(getattr(scoring, field_name), f"scoring.{field_name}")
    if scoring.min_clarity_live < scoring.min_clarity_paper:
        raise ValueError("scoring.min_clarity_live must be at least min_clarity_paper")
    if scoring.min_observability_live < scoring.min_observability_paper:
        raise ValueError(
            "scoring.min_observability_live must be at least min_observability_paper"
        )

    _validate_opportunity_config(config.opportunity)
    opportunity = config.opportunity
    for field_name in (
        "max_entry_price",
        "max_spread",
        "min_group_arb_edge",
    ):
        require_probability(getattr(opportunity, field_name), f"opportunity.{field_name}")
    require_number(
        opportunity.forecast_max_age_hours,
        "opportunity.forecast_max_age_hours",
        minimum=0,
        minimum_exclusive=True,
    )
    require_integer(
        opportunity.min_resolved_for_calibration,
        "opportunity.min_resolved_for_calibration",
        minimum=1,
    )
    require_bool(
        opportunity.require_calibrated_forecast,
        "opportunity.require_calibrated_forecast",
    )
    require_bool(opportunity.scan_no_side, "opportunity.scan_no_side")
    require_text(opportunity.forecast_data_root, "opportunity.forecast_data_root")
    if not isinstance(opportunity.probability_estimates, dict):
        raise ValueError("opportunity.probability_estimates must be an object")
    for market_id, outcomes in opportunity.probability_estimates.items():
        require_text(market_id, "opportunity.probability_estimates market id")
        if not isinstance(outcomes, dict):
            raise ValueError(
                f"opportunity.probability_estimates[{market_id!r}] must be an object"
            )
        metadata_keys = {str(key) for key in outcomes if str(key).startswith("_")}
        unknown_metadata = sorted(metadata_keys - {"_decay", "_as_of"})
        if unknown_metadata:
            raise ValueError(
                f"opportunity.probability_estimates[{market_id!r}] contains "
                f"unknown metadata: {', '.join(unknown_metadata)}"
            )
        if "_decay" in outcomes:
            require_bool(
                outcomes["_decay"],
                f"opportunity.probability_estimates[{market_id!r}]['_decay']",
            )
        if "_as_of" in outcomes:
            require_iso_datetime(
                outcomes["_as_of"],
                f"opportunity.probability_estimates[{market_id!r}]['_as_of']",
            )
        if outcomes.get("_decay") and "_as_of" not in outcomes:
            raise ValueError(
                f"opportunity.probability_estimates[{market_id!r}] "
                "_decay requires _as_of"
            )
        for outcome, probability in outcomes.items():
            if str(outcome).startswith("_"):
                continue
            require_probability(
                probability,
                f"opportunity.probability_estimates[{market_id!r}][{outcome!r}]",
            )

    allocator = config.allocator
    for field_name in (
        "per_order_usd",
        "per_market_usd",
        "per_event_usd",
        "per_group_usd",
        "daily_usd",
        "total_usd",
        "max_per_deadline_week_usd",
        "per_region_usd",
        "max_drawdown_usd",
    ):
        require_number(
            getattr(allocator, field_name),
            f"allocator.{field_name}",
            minimum=0,
            minimum_exclusive=True,
        )
    require_integer(
        allocator.max_open_positions,
        "allocator.max_open_positions",
        minimum=1,
    )
    if allocator.per_order_usd > allocator.per_market_usd:
        raise ValueError("allocator.per_order_usd must not exceed per_market_usd")
    if allocator.per_market_usd > allocator.total_usd:
        raise ValueError("allocator.per_market_usd must not exceed total_usd")
    if allocator.per_event_usd > allocator.total_usd:
        raise ValueError("allocator.per_event_usd must not exceed total_usd")
    if allocator.per_group_usd > allocator.total_usd:
        raise ValueError("allocator.per_group_usd must not exceed total_usd")
    if allocator.per_region_usd > allocator.total_usd:
        raise ValueError("allocator.per_region_usd must not exceed total_usd")
    if allocator.daily_usd > allocator.total_usd:
        raise ValueError("allocator.daily_usd must not exceed total_usd")
    if allocator.max_per_deadline_week_usd > allocator.total_usd:
        raise ValueError(
            "allocator.max_per_deadline_week_usd must not exceed total_usd"
        )

    require_number(
        config.schedule.interval_minutes,
        "schedule.interval_minutes",
        minimum=0,
        minimum_exclusive=True,
    )
    fleet = config.fleet
    require_bool(fleet.enabled, "fleet.enabled")
    require_bool(fleet.auto_ack, "fleet.auto_ack")
    require_integer(fleet.max_bots, "fleet.max_bots", minimum=0)
    require_integer(
        fleet.max_restarts_per_hour,
        "fleet.max_restarts_per_hour",
        minimum=1,
    )
    require_number(
        fleet.heartbeat_stale_seconds,
        "fleet.heartbeat_stale_seconds",
        minimum=0,
        minimum_exclusive=True,
    )
    if fleet.position_mode not in {"off", "alert_only", "dry_run", "live"}:
        raise ValueError(
            "fleet.position_mode must be one of off, alert_only, dry_run, live"
        )
    if fleet.auto_ack and fleet.position_mode != "live":
        raise ValueError("fleet.auto_ack may only be true when position_mode is live")
    require_text(fleet.generated_dir, "fleet.generated_dir")

    require_bool(config.central_feed.enabled, "central_feed.enabled")
    for field_name in (
        "poll_seconds",
        "impact_poll_seconds",
        "direct_poll_seconds",
        "direct_idle_max_seconds",
        "aggregator_poll_seconds",
        "retention_hours",
        "stale_after_seconds",
    ):
        require_number(
            getattr(config.central_feed, field_name),
            f"central_feed.{field_name}",
            minimum=0,
            minimum_exclusive=True,
        )
    if (
        config.central_feed.direct_idle_max_seconds
        < config.central_feed.direct_poll_seconds
    ):
        raise ValueError(
            "central_feed.direct_idle_max_seconds must be at least "
            "direct_poll_seconds"
        )
    require_integer(config.central_feed.max_workers, "central_feed.max_workers", minimum=1)
    require_integer(
        config.central_feed.max_entries_per_feed,
        "central_feed.max_entries_per_feed",
        minimum=1,
    )
    require_string_list(
        config.central_feed.impact_feed_urls,
        "central_feed.impact_feed_urls",
    )
    impact_feed_urls = [url.strip() for url in config.central_feed.impact_feed_urls]
    if len(set(impact_feed_urls)) != len(impact_feed_urls):
        raise ValueError("central_feed.impact_feed_urls must not contain duplicates")
    for index, url in enumerate(impact_feed_urls):
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError(
                f"central_feed.impact_feed_urls[{index}] must be an https URL"
            )
    require_bool(
        config.central_feed.direct_sources_enabled,
        "central_feed.direct_sources_enabled",
    )
    require_integer(
        config.central_feed.max_entries_per_direct_source,
        "central_feed.max_entries_per_direct_source",
        minimum=1,
    )
    require_integer(
        config.central_feed.max_urls_per_domain_per_cycle,
        "central_feed.max_urls_per_domain_per_cycle",
        minimum=1,
    )
    require_text(config.central_feed.db_path, "central_feed.db_path", allow_empty=True)
    if config.central_feed.aggregator_poll_seconds < config.central_feed.poll_seconds:
        raise ValueError(
            "central_feed.aggregator_poll_seconds must be at least poll_seconds"
        )
    if config.central_feed.stale_after_seconds <= config.central_feed.poll_seconds:
        raise ValueError("central_feed.stale_after_seconds must exceed poll_seconds")

    require_bool(config.classifier_budget.enabled, "classifier_budget.enabled")
    for field_name in (
        "max_escalations_per_hour",
        "max_escalations_per_day",
        "max_classifier_errors_per_hour",
    ):
        require_integer(
            getattr(config.classifier_budget, field_name),
            f"classifier_budget.{field_name}",
            minimum=0,
        )
    require_text(
        config.classifier_budget.db_path,
        "classifier_budget.db_path",
        allow_empty=True,
    )
    if (
        config.classifier_budget.enabled
        and config.classifier_budget.max_escalations_per_hour <= 0
        and config.classifier_budget.max_escalations_per_day <= 0
    ):
        raise ValueError(
            "enabled classifier_budget requires an hourly or daily attempt cap"
        )
    quota_total = 0.0
    for field_name in (
        "exploitation_fraction",
        "exploration_fraction",
        "system_fraction",
    ):
        value = getattr(config.classifier_budget, field_name)
        require_probability(value, f"classifier_budget.{field_name}")
        quota_total += float(value)
    if abs(quota_total - 1.0) > 1e-9:
        raise ValueError(
            "classifier_budget exploitation/exploration/system fractions "
            "must sum to 1"
        )

    priority = config.profit_priority
    require_bool(priority.enabled, "profit_priority.enabled")
    require_text(
        priority.policy_version,
        "profit_priority.policy_version",
    )
    for field_name in (
        "min_terminal_observations",
        "min_human_labels",
        "min_quote_samples",
        "min_stressed_fill_samples",
        "min_resolved_trades",
    ):
        require_integer(
            getattr(priority, field_name),
            f"profit_priority.{field_name}",
            minimum=1,
        )
    require_integer(
        priority.max_supported_latency_ms,
        "profit_priority.max_supported_latency_ms",
        minimum=1,
    )
    for field_name in (
        "lower_bound_z",
        "processing_cost_reserve_usd",
        "false_terminal_loss_usd",
    ):
        require_number(
            getattr(priority, field_name),
            f"profit_priority.{field_name}",
            minimum=0,
        )
    require_probability(
        priority.missing_label_error_rate,
        "profit_priority.missing_label_error_rate",
    )
    require_probability(
        priority.exploration_fraction,
        "profit_priority.exploration_fraction",
    )
    if (
        priority.enabled
        and priority.false_terminal_loss_usd
        < config.allocator.per_order_usd
    ):
        raise ValueError(
            "profit_priority.false_terminal_loss_usd must be at least "
            "allocator.per_order_usd"
        )
    if (
        priority.enabled
        and abs(
            priority.exploration_fraction
            - config.classifier_budget.exploration_fraction
        )
        > 1e-9
    ):
        raise ValueError(
            "profit_priority.exploration_fraction must match "
            "classifier_budget.exploration_fraction"
        )

    from polybot.rules.contracts import RULE_FAMILIES

    require_bool(config.rule_compiler.enabled, "rule_compiler.enabled")
    require_integer(
        config.rule_compiler.max_per_cycle,
        "rule_compiler.max_per_cycle",
        minimum=1,
    )
    require_string_list(
        config.rule_compiler.priority_market_ids,
        "rule_compiler.priority_market_ids",
    )
    priority_market_ids = [
        item.strip() for item in config.rule_compiler.priority_market_ids
    ]
    if len(priority_market_ids) != len(set(priority_market_ids)):
        raise ValueError(
            "rule_compiler.priority_market_ids must not contain duplicates"
        )
    require_text(
        config.rule_compiler.db_path,
        "rule_compiler.db_path",
        allow_empty=True,
    )
    from polybot.rules.contracts import (
        DEADLINE_AUTHORITY_POLICIES,
        STRICT_DEADLINE_AUTHORITY,
        VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY,
    )

    require_text(
        config.rule_compiler.deadline_authority_policy,
        "rule_compiler.deadline_authority_policy",
    )
    deadline_policy = (
        config.rule_compiler.deadline_authority_policy.strip().upper()
    )
    if deadline_policy not in DEADLINE_AUTHORITY_POLICIES:
        raise ValueError(
            "rule_compiler.deadline_authority_policy must be one of: "
            + ", ".join(sorted(DEADLINE_AUTHORITY_POLICIES))
        )
    require_string_list(
        config.rule_compiler.deadline_authority_market_ids,
        "rule_compiler.deadline_authority_market_ids",
    )
    deadline_market_ids = [
        item.strip()
        for item in config.rule_compiler.deadline_authority_market_ids
    ]
    if len(deadline_market_ids) != len(set(deadline_market_ids)):
        raise ValueError(
            "rule_compiler.deadline_authority_market_ids must not contain duplicates"
        )
    if deadline_policy == STRICT_DEADLINE_AUTHORITY and deadline_market_ids:
        raise ValueError(
            "strict deadline authority cannot define market overrides"
        )
    if (
        deadline_policy == VERBATIM_RULES_PAPER_DEADLINE_AUTHORITY
        and not deadline_market_ids
    ):
        raise ValueError(
            "paper rule-deadline authority requires an explicit market allowlist"
        )
    unpinned_deadline_markets = sorted(
        set(deadline_market_ids) - set(priority_market_ids)
    )
    if unpinned_deadline_markets:
        raise ValueError(
            "deadline authority markets must also be explicit priority markets: "
            + ", ".join(unpinned_deadline_markets)
        )
    require_string_list(
        config.rule_compiler.reviewed_rule_market_ids,
        "rule_compiler.reviewed_rule_market_ids",
    )
    reviewed_market_ids = [
        item.strip()
        for item in config.rule_compiler.reviewed_rule_market_ids
    ]
    if len(reviewed_market_ids) != len(set(reviewed_market_ids)):
        raise ValueError(
            "rule_compiler.reviewed_rule_market_ids must not contain duplicates"
        )
    unpinned_reviewed_markets = sorted(
        set(reviewed_market_ids) - set(priority_market_ids)
    )
    if unpinned_reviewed_markets:
        raise ValueError(
            "reviewed rule markets must also be explicit priority markets: "
            + ", ".join(unpinned_reviewed_markets)
        )
    for field_name in ("paper_families", "live_confirmation_families"):
        values = getattr(config.rule_compiler, field_name)
        require_string_list(values, f"rule_compiler.{field_name}")
        normalized = [item.strip().upper() for item in values]
        unknown = sorted(set(normalized) - RULE_FAMILIES)
        if unknown:
            raise ValueError(
                f"rule_compiler.{field_name} contains unknown families: "
                + ", ".join(unknown)
            )
        if len(normalized) != len(set(normalized)):
            raise ValueError(
                f"rule_compiler.{field_name} must not contain duplicates"
            )
    paper = {item.strip().upper() for item in config.rule_compiler.paper_families}
    live = {
        item.strip().upper()
        for item in config.rule_compiler.live_confirmation_families
    }
    if not live.issubset(paper):
        raise ValueError(
            "rule_compiler.live_confirmation_families must be a subset of "
            "paper_families"
        )
    if "SUBJECTIVE_DISCRETIONARY" in live:
        raise ValueError(
            "SUBJECTIVE_DISCRETIONARY can never be live-confirmation eligible"
        )

    require_bool(config.rule_runner.enabled, "rule_runner.enabled")
    require_string_list(
        config.rule_runner.paper_execution_families,
        "rule_runner.paper_execution_families",
    )
    runner_families = [
        item.strip().upper()
        for item in config.rule_runner.paper_execution_families
    ]
    unknown_runner = sorted(set(runner_families) - RULE_FAMILIES)
    if unknown_runner:
        raise ValueError(
            "rule_runner.paper_execution_families contains unknown families: "
            + ", ".join(unknown_runner)
        )
    if len(runner_families) != len(set(runner_families)):
        raise ValueError(
            "rule_runner.paper_execution_families must not contain duplicates"
        )
    if "SUBJECTIVE_DISCRETIONARY" in runner_families:
        raise ValueError(
            "SUBJECTIVE_DISCRETIONARY cannot use the generic paper executor"
        )
    if config.rule_runner.enabled and not config.rule_compiler.enabled:
        raise ValueError(
            "rule_runner.enabled requires rule_compiler.enabled"
        )
    if not set(runner_families).issubset(paper):
        raise ValueError(
            "rule_runner.paper_execution_families must be a subset of "
            "rule_compiler.paper_families"
        )
    require_integer(
        config.rule_runner.extraction_passes,
        "rule_runner.extraction_passes",
        minimum=2,
        maximum=5,
    )
    require_bool(
        config.rule_runner.deterministic_evidence_enabled,
        "rule_runner.deterministic_evidence_enabled",
    )
    require_text(
        config.rule_runner.deterministic_evidence_policy_version,
        "rule_runner.deterministic_evidence_policy_version",
    )
    deterministic_families = {
        str(item).strip().upper()
        for item in config.rule_runner.deterministic_evidence_families
    }
    if (
        len(deterministic_families)
        != len(config.rule_runner.deterministic_evidence_families)
        or not deterministic_families
        or not deterministic_families.issubset(
            {
                "OCCURRENCE_BEFORE_DEADLINE",
                "CATEGORICAL_EXCLUSIVE",
                "SOURCE_LOCKED_ANNOUNCEMENT",
            }
        )
    ):
        raise ValueError(
            "rule_runner.deterministic_evidence_families must contain "
            "unique objective confirmation families"
        )
    require_number(
        config.rule_runner.poll_seconds,
        "rule_runner.poll_seconds",
        minimum=0,
        minimum_exclusive=True,
    )
    require_integer(
        config.rule_runner.max_articles_per_feed,
        "rule_runner.max_articles_per_feed",
        minimum=1,
    )
    for field_name in (
        "max_trade_article_age_hours",
        "max_fee_schedule_age_hours",
        "paper_fee_bps",
        "paper_slippage_bps",
        "confirmation_uncertainty_buffer",
        "exit_price_buffer",
    ):
        require_number(
            getattr(config.rule_runner, field_name),
            f"rule_runner.{field_name}",
            minimum=0,
        )
    if config.rule_runner.enabled and config.rule_runner.paper_fee_bps != 0:
        raise ValueError(
            "rule_runner.paper_fee_bps must be zero; generic execution uses "
            "market-specific feeSchedule metadata"
        )
    if (
        config.rule_runner.enabled
        and config.rule_runner.max_fee_schedule_age_hours <= 0
    ):
        raise ValueError(
            "rule_runner.max_fee_schedule_age_hours must be positive"
        )
    require_number(
        config.rule_runner.paper_max_book_age_seconds,
        "rule_runner.paper_max_book_age_seconds",
        minimum=0,
        minimum_exclusive=True,
    )
    require_probability(
        config.rule_runner.confirmation_uncertainty_buffer,
        "rule_runner.confirmation_uncertainty_buffer",
    )
    require_probability(
        config.rule_runner.exit_price_buffer,
        "rule_runner.exit_price_buffer",
    )

    recorder = config.forward_recorder
    require_bool(recorder.enabled, "forward_recorder.enabled")
    require_text(
        recorder.db_path,
        "forward_recorder.db_path",
        allow_empty=True,
    )
    require_text(
        recorder.websocket_url,
        "forward_recorder.websocket_url",
    )
    if not recorder.websocket_url.startswith(("wss://", "ws://")):
        raise ValueError(
            "forward_recorder.websocket_url must use ws:// or wss://"
        )
    for field_name in (
        "heartbeat_seconds",
        "reconnect_min_seconds",
        "reconnect_max_seconds",
    ):
        require_number(
            getattr(recorder, field_name),
            f"forward_recorder.{field_name}",
            minimum=0,
            minimum_exclusive=True,
        )
    if recorder.reconnect_max_seconds < recorder.reconnect_min_seconds:
        raise ValueError(
            "forward_recorder.reconnect_max_seconds must be at least "
            "reconnect_min_seconds"
        )
    require_bool(recorder.rest_seed, "forward_recorder.rest_seed")
    require_bool(
        recorder.shared_book_service,
        "forward_recorder.shared_book_service",
    )
    require_integer(
        recorder.max_tokens_per_connection,
        "forward_recorder.max_tokens_per_connection",
        minimum=1,
        maximum=2_000,
    )
    require_integer(
        recorder.rest_seed_workers,
        "forward_recorder.rest_seed_workers",
        minimum=1,
        maximum=64,
    )
    require_integer(
        recorder.max_book_levels,
        "forward_recorder.max_book_levels",
        minimum=1,
        maximum=500,
    )
    require_bool(
        recorder.record_all_contexts,
        "forward_recorder.record_all_contexts",
    )
    require_integer(
        recorder.max_recorded_markets,
        "forward_recorder.max_recorded_markets",
        minimum=0,
    )
    horizons = recorder.quote_survival_horizons_ms
    if not isinstance(horizons, list) or not horizons:
        raise ValueError(
            "forward_recorder.quote_survival_horizons_ms must be a non-empty list"
        )
    for index, horizon in enumerate(horizons):
        require_integer(
            horizon,
            f"forward_recorder.quote_survival_horizons_ms[{index}]",
            minimum=1,
            maximum=600_000,
        )
    if horizons != sorted(set(horizons)):
        raise ValueError(
            "forward_recorder.quote_survival_horizons_ms must be sorted and unique"
        )
    require_integer(
        recorder.max_sample_lag_ms,
        "forward_recorder.max_sample_lag_ms",
        minimum=1,
        maximum=60_000,
    )
    require_number(
        recorder.health_startup_grace_seconds,
        "forward_recorder.health_startup_grace_seconds",
        minimum=0,
    )
    for field_name in (
        "health_stale_after_seconds",
        "health_growth_window_seconds",
        "health_alert_cooldown_seconds",
    ):
        require_number(
            getattr(recorder, field_name),
            f"forward_recorder.{field_name}",
            minimum=0,
            minimum_exclusive=True,
        )
    if recorder.enabled and not config.rule_runner.enabled:
        raise ValueError(
            "forward_recorder.enabled requires rule_runner.enabled"
        )

    validate_classifier_config(
        config.classifier,
        allowed_providers={
            "rule_based",
            "anthropic",
            "claude_cli",
            "claude-cli",
            "claude_code_cli",
            "codex_cli",
            "codex-cli",
            "codex",
        },
    )
    estimator = config.estimator
    require_bool(estimator.enabled, "estimator.enabled")
    require_bool(estimator.use_portwatch, "estimator.use_portwatch")
    require_text(estimator.model, "estimator.model")
    require_number(
        estimator.refresh_hours,
        "estimator.refresh_hours",
        minimum=0,
        minimum_exclusive=True,
    )
    require_integer(estimator.max_per_cycle, "estimator.max_per_cycle", minimum=1)
    require_integer(
        estimator.cli_timeout_seconds,
        "estimator.cli_timeout_seconds",
        minimum=1,
    )
    require_integer(
        estimator.max_rule_text_chars,
        "estimator.max_rule_text_chars",
        minimum=1,
    )
    require_number(
        estimator.rate_limit_cooldown_minutes,
        "estimator.rate_limit_cooldown_minutes",
        minimum=0,
        minimum_exclusive=True,
    )


def central_feed_db_path(config: DiscoveryConfig) -> Path:
    configured = str(config.central_feed.db_path).strip()
    return Path(configured) if configured else config.data_dir / "central_feed.sqlite3"


def classifier_budget_db_path(config: DiscoveryConfig) -> Path:
    configured = str(config.classifier_budget.db_path).strip()
    return Path(configured) if configured else config.data_dir / "classifier_budget.sqlite3"


def rule_store_db_path(config: DiscoveryConfig) -> Path:
    configured = str(config.rule_compiler.db_path).strip()
    return Path(configured) if configured else config.data_dir / "rules.sqlite3"


def forward_recorder_db_path(config: DiscoveryConfig) -> Path:
    configured = str(config.forward_recorder.db_path).strip()
    return (
        Path(configured)
        if configured
        else config.data_dir / "forward_recorder.sqlite3"
    )


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value
