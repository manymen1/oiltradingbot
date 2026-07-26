from __future__ import annotations

from dataclasses import dataclass, field


DEFAULT_AUTO_TRADE_DOMAINS = [
    "reuters.com",
    "apnews.com",
    "afp.com",
    "state.gov",
    "whitehouse.gov",
    "war.gov",  # defense.gov redirects here; exact-host matching would miss it
    "mfa.gov.ir",
    "mofa.gov.qa",
    "fm.gov.om",
    "mofa.gov.pk",
    # Tier-one broadsheets. These markets resolve on "a consensus of credible
    # reporting", and the rule analyses name major broadsheets (NYT, WaPo,
    # WSJ) as decisive sources -- so they ARE the resolution standard, not
    # colour commentary. They also publish minutes after an event while the
    # official feeds measured DAYS behind. Single-source risk is carried by
    # the classifier's two-pass agreement + verbatim-quote requirements, and
    # every one of these markets is paper-only today.
    "nytimes.com",
    "washingtonpost.com",
    "wsj.com",
    "theguardian.com",
    "bbc.com",
    "bbc.co.uk",
]

DEFAULT_ALERT_ONLY_DOMAINS = ["x.com", "twitter.com", "t.me", "irna.ir"]


@dataclass(frozen=True)
class ClassifierConfig:
    provider: str = "rule_based"
    model: str = "claude-sonnet-4-6"
    # Optional cheap/fast screen tier: when set, every escalated article is
    # first classified once with this model, and the expensive trade-grade
    # model (`model`, `passes` times) only runs when the screen pass produces
    # anything other than NO_ACTION. Cuts both cost (most escalated articles
    # are noise) and latency (the screen model answers faster on noise).
    screen_model: str = ""
    temperature: float = 0.0
    passes: int = 1
    require_pass_agreement: bool = False
    require_verbatim_quote: bool = True
    include_market_rule_text: bool = True
    if_api_down: str = "urgent_alert_no_trade"
    max_escalations_per_hour: int = 4
    max_escalations_per_day: int = 20
    max_classifier_errors_per_hour: int = 3
    # Fleet-generated bots point this at one shared WAL SQLite database.
    # Empty preserves the standalone bot's local JSON counter.
    budget_db_path: str = ""
    classify_feed_summaries: bool = False
    cli_binary: str = "claude"
    cli_timeout_seconds: int = 180


@dataclass(frozen=True)
class SafetyConfig:
    one_shot: bool = True
    cancel_open_orders_first: bool = True
    query_live_position: bool = True
    verify_fills_before_final_lock: bool = True
    quote_must_match_article_text: bool = True
    token_mapping_must_match: bool = True
    yes_cap_never_blocks_no_sell: bool = True
    degraded_mode_alert: bool = True
    max_executions: int = 1
    poll_seconds: float = 30.0
    # When live-armed, poll this fast instead (0 = use poll_seconds). Latency
    # is the confirmed-entry strategy's dominant cost: the news race is lost
    # in the gap between publication and the next polling cycle.
    armed_poll_seconds: float = 0.0


@dataclass(frozen=True)
class SourcesConfig:
    auto_trade_domains: list[str] = field(default_factory=lambda: list(DEFAULT_AUTO_TRADE_DOMAINS))
    alert_only_domains: list[str] = field(default_factory=lambda: list(DEFAULT_ALERT_ONLY_DOMAINS))
    poll_urls: list[str] = field(default_factory=list)
    feed_urls: list[str] = field(default_factory=list)
    # Fingerprint of the generated SourcePlan. Fleet config reuse requires an
    # exact match so registry/feed changes cannot leave a running bot
    # subscribed to stale URLs under an unchanged market-rule hash.
    source_plan_sha256: str = ""
    # Fleet-generated bots consume RSS/Atom items from one shared SQLite
    # fan-out service. Empty keeps hand-authored standalone bots on direct
    # fetching for backwards compatibility. There is deliberately no direct
    # fallback when this is set: a central outage must not turn N market
    # processes into a publisher request storm.
    central_feed_db: str = ""
    central_feed_stale_after_seconds: float = 60.0
    feed_include_terms: list[str] = field(
        default_factory=lambda: [
            "iran",
            "u.s.",
            "us ",
            "united states",
            "witkoff",
            "araghchi",
            "doha",
            "oman",
            "qatar",
            "talks",
            "negotiations",
        ]
    )
    feed_exclude_terms: list[str] = field(
        default_factory=lambda: [
            "visa",
            "visas",
            "reciprocity",
            "civil documents",
            "travel advisory",
            "travel.state.gov",
            "aoprals.state.gov",
            "2001-2009.state.gov",
            "2009-2017.state.gov",
            "2021-2025.state.gov",
            "consular",
            "passport",
            "allowances",
        ]
    )
    max_feed_entries_per_cycle: int = 20
    allow_feed_auto_trade: bool = False
    promote_feed_to_article: bool = True
    max_trade_article_age_hours: float = 24.0
    allow_unknown_age_poll_auto_trade: bool = False
    # Event-anchored order-book capture (book_snapshots.jsonl): full depth is
    # persisted when an article clears the keyword gate, right before an
    # order, and right after a fill. Off by default (tests/offline); emitted
    # fleet configs enable it -- this is the slippage/repricing dataset.
    log_book_snapshots: bool = False

__all__ = [
    "ClassifierConfig",
    "DEFAULT_ALERT_ONLY_DOMAINS",
    "DEFAULT_AUTO_TRADE_DOMAINS",
    "SafetyConfig",
    "SourcesConfig",
]
