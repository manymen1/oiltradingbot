from __future__ import annotations

import json
from pathlib import Path

import pytest

from polybot.binary.config import load_binary_config
from polybot.discovery.allocator import AllocationRequest, PortfolioAllocator
from polybot.discovery.config import (
    AllocatorConfig,
    DiscoveryConfig,
    OpportunityConfig,
    ScoringConfig,
    UniverseConfig,
    load_discovery_config,
    opportunity_reachability,
)
from polybot.discovery.context import FixtureRuleAnalyzer
from polybot.discovery.emit import emit_bot_config
from polybot.discovery.gamma_universe import context_from_event, is_geopolitical_candidate, merge_refresh
from polybot.discovery.opportunity import (
    config_probability_lookup,
    scan_group_arbitrage,
    scan_opportunities,
    tradable_edge,
)
from polybot.discovery.runner import (
    discover_markets_command,
    emit_bot_config_command,
    funnel_report_command,
    grade_markets_command,
    plan_sources_command,
    scan_opportunities_command,
)
from polybot.discovery.scorer import grade_market
from polybot.discovery.sources import build_source_plan
from polybot.discovery.sources import source_plan_sha256
from polybot.discovery.store import DiscoveryStore
from polybot.discovery.types import MarketContext
from polybot.location.config import load_location_config


RULES = (
    "This market will resolve YES if senior representatives of the United States and Iran "
    "convene a formal round of peace talks negotiations before the deadline. "
    "Technical, staff-level, working-group, or preparatory meetings will not count. "
    "If no qualifying round begins by the deadline, this market resolves NO. "
    "Resolution source: a consensus of credible reporting including reuters.com. "
    "Brief greetings, chance encounters, or photo opportunities do not count and will not qualify."
) * 3


def _market(slug: str, question: str, *, group_title: str = "", liquidity: float = 8000.0, volume: float = 50000.0, description: str = RULES, closed: bool = False) -> dict:
    return {
        "slug": slug,
        "question": question,
        "groupItemTitle": group_title,
        "conditionId": f"0x{slug}",
        "clobTokenIds": json.dumps([f"{slug}-yes", f"{slug}-no"]),
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.4", "0.6"]),
        "description": description,
        "resolutionSource": "reuters.com",
        "negRisk": bool(group_title),
        "active": not closed,
        "closed": closed,
        "acceptingOrders": not closed,
        "feesEnabled": False,
        "feeSchedule": None,
        "volume": volume,
        "liquidity": liquidity,
        "endDate": "2026-09-30T23:59:00Z",
    }


def _grouped_event() -> dict:
    return {
        "slug": "us-iran-talks-location",
        "title": "Where will the next US-Iran peace talks be?",
        "description": RULES,
        "endDate": "2026-09-30T23:59:00Z",
        "negRisk": True,
        "tags": [{"label": "Geopolitics"}],
        "markets": [
            _market("talks-qatar", "Will the talks be in Qatar?", group_title="Qatar"),
            _market("talks-oman", "Will the talks be in Oman?", group_title="Oman"),
        ],
    }


def _binary_event(slug: str = "iran-ceasefire", *, liquidity: float = 9000.0, volume: float = 50000.0, description: str = RULES, closed: bool = False) -> dict:
    return {
        "slug": slug,
        "title": "Will the United States and Iran hold peace talks by September 30?",
        "endDate": "2026-09-30T23:59:00Z",
        "tags": ["politics"],
        "markets": [_market(f"{slug}-m", "Will the United States and Iran hold peace talks negotiations by September 30?", liquidity=liquidity, volume=volume, description=description, closed=closed)],
    }


def _sports_event() -> dict:
    return {
        "slug": "nba-finals",
        "title": "Who wins the NBA finals?",
        "tags": ["sports"],
        "markets": [_market("nba-m", "Who wins the NBA finals?")],
    }


def _universe() -> UniverseConfig:
    return UniverseConfig(min_liquidity=500.0, min_volume=1000.0)


# ---- universe filter + context ----


def test_geopolitical_filter_accepts_tags_and_keywords() -> None:
    ok, reason = is_geopolitical_candidate(_grouped_event(), _universe())
    assert ok and reason.startswith("tag_match")
    ok, reason = is_geopolitical_candidate(_binary_event(), _universe())
    assert ok
    ok, reason = is_geopolitical_candidate(_sports_event(), _universe())
    assert not ok
    assert reason == "excluded_keyword:nba"


def test_geopolitical_filter_does_not_match_exclusions_inside_words() -> None:
    event = _grouped_event()
    event["description"] = (
        "The conflict concerns a nuclear stockpile and diplomatic talks."
    )
    ok, reason = is_geopolitical_candidate(event, _universe())
    assert ok
    assert reason.startswith("tag_match")


def test_context_from_grouped_event() -> None:
    context = context_from_event(_grouped_event())
    assert context is not None
    assert context.kind == "grouped"
    assert context.market_id == "us-iran-talks-location"
    assert [o.name for o in context.outcomes] == ["qatar", "oman"]
    assert context.outcomes[0].yes_token_id == "talks-qatar-yes"
    assert context.outcome_topology == "EXCLUSIVE_ONE_OF_N"
    assert context.outcomes[0].deadline_iso == "2026-09-30T23:59:00Z"
    assert context.outcomes[0].deadline_consistency == "UNKNOWN"
    assert context.outcomes[0].rule_text == (
        RULES + "\n\nreuters.com"
    )
    assert context.outcomes[0].rule_text_sha256
    assert context.outcomes[0].resolution_source == "reuters.com"
    assert context.rule_text_sha256


def test_context_from_binary_event() -> None:
    context = context_from_event(_binary_event())
    assert context is not None
    assert context.kind == "binary"
    assert context.outcome_topology == "SINGLE_BINARY"
    assert context.market_id == "0xiran-ceasefire-m"
    assert len(context.outcomes) == 1
    assert context.outcomes[0].fee_schedule is not None
    assert context.outcomes[0].fee_schedule.fees_enabled is False
    assert context.outcomes[0].fee_schedule_error == ""


def test_grouped_topology_and_deadline_consistency_are_per_leg() -> None:
    event = _grouped_event()
    event["negRisk"] = False
    event["title"] = "Iran successfully targets shipping on...?"
    for index, market in enumerate(event["markets"], start=1):
        market["negRisk"] = False
        market["groupItemTitle"] = f"August {index}"
        market["question"] = (
            "Will Iran successfully target shipping "
            f"on August {index}?"
        )
        market["endDate"] = f"2026-08-0{index}T23:59:00Z"

    daily = context_from_event(event)
    assert daily is not None
    assert daily.outcome_topology == "INDEPENDENT_MULTI"
    assert {
        outcome.deadline_consistency for outcome in daily.outcomes
    } == {"MATCH"}

    event["title"] = "US-Iran final nuclear deal by...?"
    for market in event["markets"]:
        market["question"] = (
            "Will there be a final nuclear deal by "
            f"{market['groupItemTitle']}?"
        )
    event["markets"][1]["endDate"] = "2026-09-30T23:59:00Z"
    ladder = context_from_event(event)
    assert ladder is not None
    assert ladder.outcome_topology == "MONOTONE_DEADLINE_LADDER"
    assert [
        outcome.deadline_consistency for outcome in ladder.outcomes
    ] == ["MATCH", "MISMATCH"]


def test_malformed_fee_metadata_is_preserved_as_entry_blocker() -> None:
    event = _binary_event()
    market = event["markets"][0]
    market["feesEnabled"] = True
    market["feeSchedule"] = {"rate": 0.04}
    context = context_from_event(event)
    assert context is not None
    assert context.outcomes[0].fee_schedule is None
    assert "missing fields" in context.outcomes[0].fee_schedule_error


def test_rule_change_drops_analysis_and_demotes() -> None:
    original = context_from_event(_binary_event())
    assert original is not None
    analyzed = MarketContext.from_dict({**original.as_dict(), "rule_analysis": FixtureRuleAnalyzer().analyze(original).as_dict(), "state": "LIVE_CONFIRMATION_ELIGIBLE"})
    fresh = context_from_event(_binary_event(description=RULES + " AMENDED."))
    assert fresh is not None
    merged = merge_refresh(analyzed, fresh)
    assert merged.rule_version == 2
    assert merged.rule_analysis is None
    assert merged.state == "RULES_REVIEW_REQUIRED"
    assert merged.state_reasons == ["rule_text_changed"]


def test_refresh_updates_outcome_topology_and_bound_leg_metadata() -> None:
    original = context_from_event(_grouped_event())
    assert original is not None
    event = _grouped_event()
    event["negRisk"] = False
    event["resolutionSource"] = "https://example.com/settlement"
    event["title"] = "Iran successfully targets shipping on...?"
    for index, market in enumerate(event["markets"], start=1):
        market["negRisk"] = False
        market["groupItemTitle"] = f"August {index}"
        market["question"] = (
            "Will Iran successfully target shipping "
            f"on August {index}?"
        )
        market["endDate"] = f"2026-08-0{index}T23:59:00Z"
        market["resolutionSource"] = event["resolutionSource"]
    fresh = context_from_event(event)
    assert fresh is not None

    merged = merge_refresh(original, fresh)

    assert merged.outcome_topology == "INDEPENDENT_MULTI"
    assert merged.resolution_source == event["resolutionSource"]
    assert all(outcome.rule_text_sha256 for outcome in merged.outcomes)
    assert all(
        outcome.resolution_source == event["resolutionSource"]
        for outcome in merged.outcomes
    )


# ---- analyzer + scorer ----


def test_fixture_analyzer_detects_parties_and_families() -> None:
    context = context_from_event(_binary_event())
    assert context is not None
    analysis = FixtureRuleAnalyzer().analyze(context)
    assert "united_states" in analysis.parties
    assert "iran" in analysis.parties
    assert analysis.rule_clarity > 0.5
    assert not analysis.discretionary
    assert "talks" in " ".join(analysis.keywords)


def test_fixture_analyzer_flags_discretion() -> None:
    context = context_from_event(_binary_event(description=RULES + " The committee may resolve at its sole discretion."))
    assert context is not None
    analysis = FixtureRuleAnalyzer().analyze(context)
    assert analysis.discretionary
    assert analysis.automation_suitability == 0.0


def _analyzed_context(event: dict) -> MarketContext:
    context = context_from_event(event)
    assert context is not None
    analysis = FixtureRuleAnalyzer().analyze(context)
    return MarketContext.from_dict({**context.as_dict(), "rule_analysis": analysis.as_dict()})


def test_scorer_liquidity_sizes_orders_never_disqualifies() -> None:
    scoring = ScoringConfig(allow_fixture_analysis_live=True)
    live = grade_market(_analyzed_context(_binary_event()), scoring)
    assert live.state == "LIVE_CONFIRMATION_ELIGIBLE"
    assert live.correlation_group == "iran|united_states"
    assert live.scores["recommended_max_order_usd"] == 180.0  # 9000 * 0.02

    # Thin book: fully live, sized to what the book can absorb.
    thin = grade_market(_analyzed_context(_binary_event(liquidity=800.0)), scoring)
    assert thin.state == "LIVE_CONFIRMATION_ELIGIBLE"
    assert thin.scores["recommended_max_order_usd"] == 16.0

    # Ultra-thin: still live, floored at the minimum viable order.
    tiny = grade_market(_analyzed_context(_binary_event(liquidity=200.0)), scoring)
    assert tiny.state == "LIVE_CONFIRMATION_ELIGIBLE"
    assert tiny.scores["recommended_max_order_usd"] == 5.0

    # Sizing disabled: still live (liquidity never gates by default), just no
    # book-aware recommendation.
    no_sizing = grade_market(_analyzed_context(_binary_event(liquidity=800.0)), ScoringConfig(allow_fixture_analysis_live=True, small_live_enabled=False))
    assert no_sizing.state == "LIVE_CONFIRMATION_ELIGIBLE"
    assert "recommended_max_order_usd" not in no_sizing.scores

    # Operators who explicitly configure floors keep them: hard floor demotes
    # to paper without the sizing bypass...
    floored = grade_market(
        _analyzed_context(_binary_event(liquidity=800.0)),
        ScoringConfig(allow_fixture_analysis_live=True, min_liquidity_live=5000.0, small_live_enabled=False),
    )
    assert floored.state == "PAPER_ELIGIBLE"
    assert any(reason.startswith("liquidity_below_live_threshold") for reason in floored.state_reasons)
    # ...and with sizing enabled the floor is bypassed at book-absorbable size.
    bypassed = grade_market(
        _analyzed_context(_binary_event(liquidity=800.0)),
        ScoringConfig(allow_fixture_analysis_live=True, min_liquidity_live=5000.0, small_live_enabled=True),
    )
    assert bypassed.state == "LIVE_CONFIRMATION_ELIGIBLE"
    assert any(reason.startswith("small_size_live") for reason in bypassed.state_reasons)


def test_scorer_hard_states() -> None:
    scoring = ScoringConfig(allow_fixture_analysis_live=True)
    closed = grade_market(_analyzed_context(_binary_event(closed=True)), scoring)
    assert closed.state == "CLOSED"

    context = context_from_event(_binary_event(description="short"))
    assert context is not None
    short_rules = grade_market(context, scoring)
    assert short_rules.state == "RULES_REVIEW_REQUIRED"
    assert short_rules.state_reasons == ["missing_or_short_resolution_rules"]

    unanalyzed = grade_market(context_from_event(_binary_event()), scoring)  # type: ignore[arg-type]
    assert unanalyzed.state == "RULES_REVIEW_REQUIRED"
    assert unanalyzed.state_reasons == ["rule_analysis_missing"]

    discretionary = grade_market(_analyzed_context(_binary_event(description=RULES + " sole discretion.")), scoring)
    assert discretionary.state == "MONITOR_ONLY"


def test_grouped_market_requires_verified_mapping_for_every_outcome() -> None:
    context = _analyzed_context(_grouped_event())
    raw = context.as_dict()
    raw["outcomes"][1]["no_token_id"] = ""
    broken = MarketContext.from_dict(raw)

    graded = grade_market(broken, ScoringConfig(allow_fixture_analysis_live=True))

    assert graded.state == "RULES_REVIEW_REQUIRED"
    assert graded.state_reasons == ["unverified_token_mapping"]


def test_pentagon_rename_domain_is_auto_trade_eligible() -> None:
    # defense.gov 301-redirects to war.gov, and domain_allowed() matches on
    # exact host / suffix -- so a Pentagon announcement (the decisive source
    # for the Iran halt-in-offensive ladders) would silently demote to
    # alert-only unless war.gov is an official domain too.
    from polybot.binary.runner import domain_allowed
    from polybot.discovery.registry import official_domains

    domains = official_domains(["united_states"])
    assert "war.gov" in domains
    assert "defense.gov" in domains
    assert domain_allowed("www.war.gov", domains)
    assert domain_allowed("media.war.gov", domains)
    assert not domain_allowed("warmongering-blog.com", domains)


def test_credible_reporting_sources_can_authorize_a_trade() -> None:
    # These markets resolve on "a consensus of credible reporting", and the
    # rule analyses name major broadsheets as decisive sources. Official
    # government feeds measured 2.7-5.6 DAYS stale, so if broadsheets cannot
    # authorize a trade the strategy can never react in time.
    from polybot.binary.runner import domain_allowed
    from polybot.core.config import DEFAULT_AUTO_TRADE_DOMAINS

    for host in ("nytimes.com", "www.theguardian.com", "www.bbc.co.uk", "wsj.com"):
        assert domain_allowed(host, DEFAULT_AUTO_TRADE_DOMAINS), host
    # Speed sources that are state-affiliated or lower-tier stay alert-only:
    # we poll them for reaction time, they must not authorize a trade.
    for host in ("aa.com.tr", "middleeasteye.net", "timesofisrael.com", "x.com"):
        assert not domain_allowed(host, DEFAULT_AUTO_TRADE_DOMAINS), host


def test_fast_feeds_lead_with_measured_fastest_sources() -> None:
    # Ordering here is by measured freshness, not reputation: BBC Middle East
    # probed at 212 min while the Guardian probed at 0 min.
    from polybot.discovery.registry import GENERAL_FAST_FEEDS

    assert "theguardian.com" in GENERAL_FAST_FEEDS[0]
    assert any("nytimes.com" in u for u in GENERAL_FAST_FEEDS)
    assert any("aljazeera.com" in u for u in GENERAL_FAST_FEEDS)


def test_direct_actor_feeds_are_not_silently_empty() -> None:
    # The previous entries returned HTTP 200 with zero <item>s, so the system
    # believed it had fast official sources while receiving nothing. Keep the
    # registry honest: entries must at least be well-formed feed URLs.
    from polybot.discovery.registry import DIRECT_ACTOR_FEEDS

    urls = [u for feeds in DIRECT_ACTOR_FEEDS.values() for u in feeds]
    assert urls, "direct feeds are the latency edge; an empty registry is a regression"
    assert all(u.startswith("https://") for u in urls)
    # These two were verified dead on 2026-07-20; do not reintroduce silently.
    assert not any("state.gov/rss-feed/press-releases" in u for u in urls)
    assert not any("news.un.org/feed/subscribe" in u for u in urls)


def test_discretionary_reaches_paper_but_never_live_when_allowed() -> None:
    # The Iran announcement ladders are clear and observable but contain one
    # judgment word ("generally ceases"). Excluding them from paper generated
    # zero evidence about the classifier; allowing paper must NOT leak live.
    event = _binary_event(description=RULES + " sole discretion.")
    blocked = grade_market(_analyzed_context(event), ScoringConfig(allow_fixture_analysis_live=True))
    assert blocked.state == "MONITOR_ONLY"

    allowed = grade_market(
        _analyzed_context(event),
        ScoringConfig(allow_fixture_analysis_live=True, allow_discretionary_paper=True),
    )
    assert allowed.state == "PAPER_ELIGIBLE"
    assert "discretionary_rules_paper_only" in allowed.state_reasons


def test_scorer_correlation_group_limit_downgrades_live() -> None:
    scoring = ScoringConfig(max_markets_per_correlation_group=1)
    context = _analyzed_context(_binary_event())
    graded = grade_market(context, scoring, group_counts={"iran|united_states": 1})
    assert graded.state == "PAPER_ELIGIBLE"
    assert any(reason.startswith("correlation_group_limit") for reason in graded.state_reasons)


def test_grading_keeps_best_markets_up_to_correlation_limit(tmp_path, capsys) -> None:
    config_path = _pipeline_config(tmp_path)
    events = [
        _binary_event(slug=f"same-group-{index}", liquidity=1000.0 + index)
        for index in range(3)
    ]

    def fetch(url: str, params: dict) -> list[dict]:
        return events if params.get("offset", 0) == 0 else []

    assert discover_markets_command(config_path, events_fetch=fetch) == 0
    capsys.readouterr()
    assert grade_markets_command(config_path) == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary["states"]["LIVE_CONFIRMATION_ELIGIBLE"] == 2
    assert summary["states"]["PAPER_ELIGIBLE"] == 1


# ---- source plan ----


def test_source_plan_requires_analysis_and_derives_sources() -> None:
    unanalyzed = context_from_event(_binary_event())
    assert unanalyzed is not None
    with pytest.raises(ValueError, match="no rule analysis"):
        build_source_plan(unanalyzed)

    plan = build_source_plan(_analyzed_context(_binary_event()))
    assert "reuters.com" in plan.auto_trade_domains
    assert "state.gov" in plan.auto_trade_domains  # united_states official domain
    assert any("news.google.com/rss" in url for url in plan.feed_urls)
    # Both aggregators must be present: one degraded route (observed: ISP
    # peering fault toward Google) must not blind the discovery layer.
    assert any("bing.com/news" in url for url in plan.feed_urls)
    assert plan.escalate_terms
    assert plan.rationale


# ---- allocator ----


def _allocator(tmp_path: Path, **overrides) -> PortfolioAllocator:
    config = AllocatorConfig(**overrides) if overrides else AllocatorConfig()
    return PortfolioAllocator(tmp_path / "allocations.json", config)


def _request(usd: float = 50.0, market: str = "m1", group: str = "g1") -> AllocationRequest:
    return AllocationRequest(market_id=market, event_slug="e1", correlation_group=group, deadline_iso="2026-09-30T23:59:00Z", usd=usd)


def test_allocator_grants_within_caps_and_commits(tmp_path) -> None:
    allocator = _allocator(tmp_path)
    granted, blockers = allocator.preview(_request())
    assert granted == 50.0 and not blockers
    allocator.commit(_request())
    granted, blockers = allocator.preview(_request())
    assert granted == 50.0 and not blockers  # per-market 100 cap leaves 50
    allocator.commit(_request())
    granted, blockers = allocator.preview(_request())
    assert granted == 0.0
    assert "per_market_limit" in blockers


def test_allocator_max_positions_and_release(tmp_path) -> None:
    allocator = _allocator(tmp_path, max_open_positions=1, per_event_usd=1000.0, per_group_usd=1000.0, daily_usd=1000.0, total_usd=1000.0, per_market_usd=1000.0)
    allocator.commit(_request(market="m1"))
    _, blockers = allocator.preview(_request(market="m2", group="g2"))
    assert "max_open_positions" in blockers
    allocator.release_position("m1")
    granted, blockers = allocator.preview(_request(market="m2", group="g2"))
    assert granted == 50.0 and not blockers


def test_allocator_group_concentration(tmp_path) -> None:
    allocator = _allocator(tmp_path, per_group_usd=60.0, per_market_usd=1000.0, per_event_usd=1000.0)
    allocator.commit(_request(usd=50.0, market="m1", group="iran|united_states"))
    granted, blockers = allocator.preview(_request(usd=50.0, market="m2", group="iran|united_states"))
    assert granted == 10.0 and not blockers


# ---- opportunity engine ----


class _FakeQuotes:
    def __init__(self, ask: float | None = 0.40, bid: float | None = 0.38):
        self.ask, self.bid = ask, bid

    def yes_best_ask(self, token_id: str) -> float | None:
        return self.ask

    def yes_best_bid(self, token_id: str) -> float | None:
        return self.bid


def test_tradable_edge_accounting() -> None:
    config = OpportunityConfig()
    assert tradable_edge(0.60, 0.40, config) == pytest.approx(0.14)


def test_default_model_pricing_is_explicitly_calibration_only(tmp_path) -> None:
    context = _graded(_binary_event())
    config = OpportunityConfig(
        probability_estimates={context.market_id: {"yes": 0.99}},
        model_weight=1.0,
        disagreement_buffer_scale=0.0,
    )
    results = scan_opportunities(
        [context],
        config,
        _FakeQuotes(),
        _allocator(tmp_path),
        probability_lookup=config_probability_lookup(config),
    )
    yes = next(result for result in results if result.side == "YES")
    assert "model_pricing_calibration_only" in yes.blockers
    assert yes.tradable_edge is not None and yes.tradable_edge > config.min_edge
    assert yes.allocation_usd == 0.0


def test_opportunity_reachability_matches_pricing_equation() -> None:
    status = opportunity_reachability(OpportunityConfig())
    # 0.35 model lift - 0.25 disagreement penalty - 0.06 fixed buffers.
    assert status["max_theoretical_edge"] == pytest.approx(0.04)
    assert status["min_edge"] == pytest.approx(0.05)
    assert status["minimum_model_weight"] == pytest.approx(0.36)
    assert status["reachable"] is False


def test_allocatable_mode_rejects_unreachable_model_pricing(tmp_path) -> None:
    path = tmp_path / "discovery.yaml"
    path.write_text(
        """
opportunity:
  model_pricing_mode: allocatable
  model_weight: 0.35
  disagreement_buffer_scale: 0.25
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_theoretical_edge=0.0400"):
        load_discovery_config(path)


def test_allocatable_mode_accepts_reachable_model_pricing(tmp_path) -> None:
    path = tmp_path / "discovery.yaml"
    path.write_text(
        """
opportunity:
  model_pricing_mode: allocatable
  model_weight: 0.40
  disagreement_buffer_scale: 0.25
""",
        encoding="utf-8",
    )
    config = load_discovery_config(path)
    assert opportunity_reachability(config.opportunity)["reachable"] is True


def test_model_pricing_mode_rejects_unknown_value(tmp_path) -> None:
    path = tmp_path / "discovery.yaml"
    path.write_text(
        """
opportunity:
  model_pricing_mode: automatic
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="calibration_only.*allocatable"):
        load_discovery_config(path)


def _graded(event: dict) -> MarketContext:
    return grade_market(_analyzed_context(event), ScoringConfig(allow_fixture_analysis_live=True))


def test_scan_finds_executable_opportunity(tmp_path) -> None:
    context = _graded(_binary_event())
    # model_weight=1.0 opts out of market anchoring to isolate the base
    # edge accounting; anchoring itself is covered in test_calibration.py.
    config = OpportunityConfig(
        model_pricing_mode="allocatable",
        probability_estimates={context.market_id: {"yes": 0.60}},
        model_weight=1.0,
        disagreement_buffer_scale=0.0,
    )
    results = scan_opportunities(
        [context],
        config,
        _FakeQuotes(),
        _allocator(tmp_path),
        probability_lookup=config_probability_lookup(config),
    )
    # One YES row and one NO row per estimated outcome; here YES carries the edge.
    assert [r.side for r in sorted(results, key=lambda r: r.side)] == ["NO", "YES"]
    opp = next(r for r in results if r.side == "YES")
    assert not opp.blockers
    # Base edge 0.14 minus the per-market resolution-risk scaling
    # (analyzer risk * resolution_risk_scale).
    expected = round(0.14 - round(context.rule_analysis.resolution_risk * 0.05, 4), 4)
    assert opp.tradable_edge == pytest.approx(expected)
    assert opp.allocation_usd == 50.0


def test_scan_blockers(tmp_path) -> None:
    context = _graded(_binary_event())
    allocator = _allocator(tmp_path)
    empty_config = OpportunityConfig()
    no_estimate = scan_opportunities(
        [context],
        empty_config,
        _FakeQuotes(),
        allocator,
        probability_lookup=config_probability_lookup(empty_config),
    )
    assert no_estimate[0].blockers == ["no_probability_estimate"]

    config = OpportunityConfig(probability_estimates={context.market_id: {"yes": 0.60}})
    thin_edge = scan_opportunities(
        [context],
        config,
        _FakeQuotes(ask=0.58, bid=0.56),
        allocator,
        probability_lookup=config_probability_lookup(config),
    )
    assert any(b.startswith("edge_below_minimum") for b in thin_edge[0].blockers)

    wide = scan_opportunities(
        [context],
        config,
        _FakeQuotes(ask=0.40, bid=0.10),
        allocator,
        probability_lookup=config_probability_lookup(config),
    )
    assert any(b.startswith("spread_above_limit") for b in wide[0].blockers)

    pricey_config = OpportunityConfig(
        probability_estimates={context.market_id: {"yes": 0.99}}
    )
    pricey = scan_opportunities(
        [context],
        pricey_config,
        _FakeQuotes(ask=0.95, bid=0.94),
        allocator,
        probability_lookup=config_probability_lookup(pricey_config),
    )
    assert any(b.startswith("price_above_cap") for b in pricey[0].blockers)


# ---- config emission round-trips through the real executor loaders ----


def test_emit_binary_config_loads(tmp_path) -> None:
    context = _graded(_binary_event())
    plan = build_source_plan(context)
    out = emit_bot_config(context, plan, entry_usd=50.0, out_path=tmp_path / "binary.yaml")
    config = load_binary_config(out)
    assert config.entry.enabled and config.entry.side == "YES"
    assert config.market.expected_rule_text_sha256 == context.rule_text_sha256
    assert config.execution.dry_run is True
    assert config.execution.paper_slippage_bps == 25.0
    assert config.execution.paper_max_book_age_seconds == 10.0
    assert config.sources.feed_urls == plan.feed_urls
    assert config.sources.feed_include_terms == plan.escalate_terms
    assert config.sources.source_plan_sha256 == source_plan_sha256(plan)


def test_emit_fleet_config_routes_feeds_through_central_store(tmp_path) -> None:
    context = _graded(_binary_event())
    plan = build_source_plan(context)
    db_path = tmp_path / "central.sqlite3"
    out = emit_bot_config(
        context,
        plan,
        entry_usd=50.0,
        out_path=tmp_path / "binary-central.yaml",
        central_feed_db=str(db_path),
        central_feed_stale_after_seconds=45.0,
    )
    config = load_binary_config(out)
    assert config.sources.central_feed_db == str(db_path)
    assert config.sources.central_feed_stale_after_seconds == 45.0


def test_emit_fleet_config_routes_shared_classifier_budget(tmp_path) -> None:
    context = _graded(_binary_event())
    plan = build_source_plan(context)
    db_path = tmp_path / "classifier.sqlite3"
    out = emit_bot_config(
        context,
        plan,
        entry_usd=50.0,
        out_path=tmp_path / "binary-budget.yaml",
        classifier_budget_db=str(db_path),
        classifier_max_escalations_per_hour=12,
        classifier_max_escalations_per_day=80,
        classifier_max_errors_per_hour=5,
    )
    config = load_binary_config(out)
    assert config.classifier.budget_db_path == str(db_path)
    assert config.classifier.max_escalations_per_hour == 12
    assert config.classifier.max_escalations_per_day == 80
    assert config.classifier.max_classifier_errors_per_hour == 5


def test_emit_location_config_loads(tmp_path) -> None:
    context = _graded(_grouped_event())
    plan = build_source_plan(context)
    out = emit_bot_config(context, plan, entry_usd=50.0, out_path=tmp_path / "location.yaml")
    config = load_location_config(out)
    assert config.entry.enabled
    assert config.entry_target_names() == {"qatar", "oman"}
    assert config.event.expected_rule_text_sha256 == context.rule_text_sha256
    assert config.execution.dry_run is True
    assert config.execution.paper_slippage_bps == 25.0
    assert config.execution.paper_max_book_age_seconds == 10.0


@pytest.mark.parametrize(
    ("event", "loader"),
    [
        (_binary_event(), load_binary_config),
        (_grouped_event(), load_location_config),
    ],
)
def test_emit_live_config_requires_two_agreeing_classifier_passes(tmp_path, event, loader) -> None:
    context = _graded(event)
    plan = build_source_plan(context)
    out = emit_bot_config(
        context,
        plan,
        entry_usd=50.0,
        out_path=tmp_path / f"{context.kind}.yaml",
        dry_run=False,
    )
    config = loader(out)
    assert config.execution.dry_run is False
    assert config.classifier.passes >= 2
    assert config.classifier.require_pass_agreement is True


def test_emit_rejects_stale_source_plan(tmp_path) -> None:
    context = _graded(_binary_event())
    plan = build_source_plan(context)
    stale = plan.from_dict({**plan.as_dict(), "rule_text_sha256": "different"})
    with pytest.raises(ValueError, match="different rule-text version"):
        emit_bot_config(context, stale, entry_usd=50.0, out_path=tmp_path / "x.yaml")


# ---- full pipeline through the CLI commands ----


def _pipeline_config(tmp_path: Path) -> Path:
    path = tmp_path / "discovery.yaml"
    path.write_text(
        f"""
classifier:
  provider: rule_based
estimator:
  enabled: false
scoring:
  allow_fixture_analysis_live: true
data_dir: {tmp_path / 'data'}
logs_dir: {tmp_path / 'logs'}
""",
        encoding="utf-8",
    )
    return path


def test_full_pipeline(tmp_path, capsys) -> None:
    config_path = _pipeline_config(tmp_path)
    # The thin market is about different parties so the correlation-group cap
    # (2 per group) doesn't demote the trio -- and being thin no longer
    # disqualifies it.
    events = [
        _grouped_event(),
        _binary_event(),
        _sports_event(),
        _binary_event(slug="thin", liquidity=100.0, volume=200.0, description=RULES.replace("United States and Iran", "Russia and Ukraine")),
    ]

    def fetch(url: str, params: dict) -> list[dict]:
        return events if params.get("offset", 0) == 0 else []

    assert discover_markets_command(config_path, events_fetch=fetch) == 0
    summary = json.loads(capsys.readouterr().out)
    # Sports excluded; the thin market is KEPT -- liquidity never
    # disqualifies, it only sizes orders.
    assert summary["new_contexts"] == 3
    assert summary["rejected"]["excluded_keyword"] == 1

    assert grade_markets_command(config_path) == 0
    graded = json.loads(capsys.readouterr().out)
    assert graded["states"]["LIVE_CONFIRMATION_ELIGIBLE"] == 3

    assert plan_sources_command(config_path) == 0
    planned = json.loads(capsys.readouterr().out)
    assert len(planned["planned"]) == 3

    store = DiscoveryStore(tmp_path / "data")
    binary_id = "0xiran-ceasefire-m"
    # Give the binary market a probability estimate so the scan can price it.
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + f"""
opportunity:
  model_pricing_mode: allocatable
  model_weight: 1.0
  disagreement_buffer_scale: 0.0
  forecast_data_root: {tmp_path / 'forecast'}
  probability_estimates:
    "{binary_id}":
      "yes": 0.60
""",
        encoding="utf-8",
    )
    assert scan_opportunities_command(config_path, quotes=_FakeQuotes()) == 0
    scan = json.loads(capsys.readouterr().out)
    # 3 outcomes without estimates + YES and NO rows for the estimated binary.
    assert scan["scanned_outcomes"] == 5
    assert len(scan["executable"]) == 1
    assert scan["executable"][0]["market_id"] == binary_id
    assert scan["executable"][0]["side"] == "YES"
    assert scan["model_pricing"]["mode"] == "allocatable"
    assert scan["model_pricing"]["reachable"] is True

    out_path = tmp_path / "generated" / "binary.yaml"
    assert emit_bot_config_command(config_path, binary_id, out=out_path) == 0
    capsys.readouterr()
    assert load_binary_config(out_path).market.expected_rule_text_sha256 == store.load_context(binary_id).rule_text_sha256

    assert funnel_report_command(config_path) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["funnel"]["all_markets"] == 3
    assert report["funnel"]["understandable_markets"] == 3
    assert report["funnel"]["live_confirmation_eligible"] == 3
    assert report["funnel"]["executable_opportunities"] == 1
    assert report["model_pricing"]["mode"] == "allocatable"


# ---- portfolio ledger caps + executor link ----


def test_allocator_caps_roundtrip_from_ledger(tmp_path) -> None:
    allocator = _allocator(tmp_path, per_order_usd=25.0, total_usd=500.0)
    allocator.write_caps()
    attached = PortfolioAllocator.from_ledger(tmp_path / "allocations.json")
    assert attached.config.per_order_usd == 25.0
    assert attached.config.total_usd == 500.0

    with pytest.raises(ValueError, match="does not exist"):
        PortfolioAllocator.from_ledger(tmp_path / "missing.json")


def test_binary_executor_debits_and_releases_ledger(tmp_path) -> None:
    from polybot.binary.config import BinaryBotConfig as _BinaryBotConfig  # local alias to avoid confusion
    from polybot.binary.config import EntryConfig as BinaryEntryConfig
    from polybot.binary.config import MarketConfig as BinaryMarketConfig
    from polybot.binary.decision import BinaryDecision
    from polybot.binary.executor import BinaryExecutor
    from polybot.binary.market_verifier import BinaryMarketVerification
    from polybot.core.execution import DryRunTradingAdapter
    from polybot.core.portfolio import PortfolioConfig
    from polybot.core.storage import StateStore

    ledger = tmp_path / "allocations.json"
    _allocator(tmp_path, per_order_usd=40.0).write_caps()

    config = _BinaryBotConfig(
        market=BinaryMarketConfig(slug="s", deadline_date="2026-09-30", held_side="", resolution_rules="rules"),
        entry=BinaryEntryConfig(enabled=True, side="YES", usd_budget=100.0, max_price=0.90),
        portfolio=PortfolioConfig(
            ledger_path=str(ledger),
            market_id="mkt-1",
            event_slug="evt-1",
            correlation_group="iran|united_states",
            deadline_iso="2026-09-30T23:59:00Z",
        ),
    )
    verification = BinaryMarketVerification(
        event_slug="s", market_question="q", rule_text="rules", rule_text_sha256="d",
        condition_id="0xc", yes_token_id="yt", no_token_id="nt", tradeable=True, tick_size="0.01", neg_risk=False,
    )

    class _Notifier:
        def notify(self, message, **fields):
            pass

    executor = BinaryExecutor(config, verification, StateStore(tmp_path / "state"), _Notifier(), DryRunTradingAdapter(yes_shares=250.0, yes_ask=0.40))
    decision = BinaryDecision("ENTER_YES", "4B", "qualifying_event_confirmed:scheduled")
    assert executor.execute(decision, _sig_article("The round will be held next week.")) == "ENTERED"

    # Entry budget (100) was clamped to the ledger's per-order cap (40) and debited.
    snapshot = PortfolioAllocator.from_ledger(ledger).snapshot()
    assert snapshot["per_market"]["mkt-1"] == 40.0
    assert snapshot["open_positions"] == ["mkt-1"]
    entered = executor.store.current()
    assert entered is not None and entered.payload["usd_budget"] == 40.0

    exit_decision = BinaryDecision("EXIT_HELD", "4B", "yes_foreclosure_confirmed")
    assert executor.execute(exit_decision, _sig_article("Talks cancelled, will not happen.")) == "EXITED"
    snapshot = PortfolioAllocator.from_ledger(ledger).snapshot()
    assert snapshot["open_positions"] == []  # slot freed; spend history retained
    assert snapshot["per_market"]["mkt-1"] == 40.0


def test_binary_executor_blocked_when_ledger_exhausted(tmp_path) -> None:
    from polybot.binary.config import BinaryBotConfig as _BinaryBotConfig
    from polybot.binary.config import EntryConfig as BinaryEntryConfig
    from polybot.binary.config import MarketConfig as BinaryMarketConfig
    from polybot.binary.decision import BinaryDecision
    from polybot.binary.executor import BinaryExecutor
    from polybot.binary.market_verifier import BinaryMarketVerification
    from polybot.core.execution import DryRunTradingAdapter
    from polybot.core.portfolio import AllocationRequest, PortfolioConfig
    from polybot.core.storage import StateStore

    ledger = tmp_path / "allocations.json"
    allocator = _allocator(tmp_path, per_market_usd=50.0)
    allocator.write_caps()
    allocator.commit(AllocationRequest(market_id="mkt-1", event_slug="evt-1", correlation_group="g", deadline_iso="2026-09-30T23:59:00Z", usd=50.0))

    config = _BinaryBotConfig(
        market=BinaryMarketConfig(slug="s", deadline_date="2026-09-30", held_side="", resolution_rules="rules"),
        entry=BinaryEntryConfig(enabled=True, side="YES", usd_budget=100.0, max_price=0.90),
        portfolio=PortfolioConfig(ledger_path=str(ledger), market_id="mkt-1", event_slug="evt-1", correlation_group="g", deadline_iso="2026-09-30T23:59:00Z"),
    )
    verification = BinaryMarketVerification(
        event_slug="s", market_question="q", rule_text="rules", rule_text_sha256="d",
        condition_id="0xc", yes_token_id="yt", no_token_id="nt", tradeable=True, tick_size="0.01", neg_risk=False,
    )

    class _Notifier:
        def notify(self, message, **fields):
            pass

    executor = BinaryExecutor(config, verification, StateStore(tmp_path / "state"), _Notifier(), DryRunTradingAdapter(yes_ask=0.40))
    decision = BinaryDecision("ENTER_YES", "4B", "qualifying_event_confirmed:scheduled")
    assert executor.execute(decision, _sig_article("The round will be held next week.")) == "ENTRY_PORTFOLIO_BLOCKED"
    assert executor.holdings.held_location() is None


def _sig_article(text: str):
    from polybot.core.types import Article

    return Article(
        url="https://reuters.com/story",
        domain="reuters.com",
        title=text,
        published_at=None,
        fetched_at="2026-07-10T00:00:00Z",
        raw_text=text,
        hash=str(abs(hash(text))),
    )


# ---- forecast probability source ----


def _write_forecast_state(root: Path, market_id: str, probabilities: dict, updated_at: str) -> None:
    from polybot.discovery.types import market_dir_slug

    path = root / market_dir_slug(market_id) / "dry_run" / "forecast_probability.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updated_at": updated_at, "probabilities": probabilities}), encoding="utf-8")


def test_scan_prefers_fresh_forecast_state(tmp_path) -> None:
    from datetime import datetime, timezone

    context = _graded(_binary_event())
    root = tmp_path / "geo"
    _write_forecast_state(root, context.market_id, {"yes": 0.70}, datetime.now(timezone.utc).isoformat())
    config = OpportunityConfig(
        probability_estimates={context.market_id: {"yes": 0.55}},
        forecast_data_root=str(root),
    )
    results = scan_opportunities([context], config, _FakeQuotes(), _allocator(tmp_path))
    assert results[0].probability_source == "forecast_state"
    assert results[0].estimated_probability == 0.70


def test_scan_ignores_stale_forecast_state(tmp_path) -> None:
    context = _graded(_binary_event())
    root = tmp_path / "geo"
    _write_forecast_state(root, context.market_id, {"yes": 0.70}, "2020-01-01T00:00:00+00:00")
    config = OpportunityConfig(
        probability_estimates={context.market_id: {"yes": 0.55}},
        forecast_data_root=str(root),
    )
    results = scan_opportunities([context], config, _FakeQuotes(), _allocator(tmp_path))
    assert results[0].probability_source == "config_estimate"
    assert results[0].estimated_probability == 0.55


# ---- scheduled loop ----


def test_run_discovery_once_alerts_on_new_eligible(tmp_path) -> None:
    from polybot.discovery.runner import run_discovery_command

    config_path = _pipeline_config(tmp_path)
    binary_id = "0xiran-ceasefire-m"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + f"""
opportunity:
  model_pricing_mode: allocatable
  model_weight: 1.0
  disagreement_buffer_scale: 0.0
  forecast_data_root: {tmp_path / 'forecast'}
  probability_estimates:
    "{binary_id}":
      "yes": 0.60
""",
        encoding="utf-8",
    )
    events = [_grouped_event(), _binary_event()]

    def fetch(url: str, params: dict) -> list[dict]:
        return events if params.get("offset", 0) == 0 else []

    notified: list[tuple[str, dict]] = []

    class _Notifier:
        def notify(self, message, **fields):
            notified.append((message, fields))

    assert run_discovery_command(config_path, once=True, events_fetch=fetch, quotes=_FakeQuotes(), notifier=_Notifier()) == 0
    new_live = [n for n in notified if "newly LIVE_CONFIRMATION_ELIGIBLE" in n[0]]
    new_exec = [n for n in notified if "newly executable" in n[0]]
    assert len(new_live) == 2
    assert len(new_exec) == 1
    assert (tmp_path / "data" / "pipeline_state.json").exists()

    notified.clear()
    assert run_discovery_command(config_path, once=True, events_fetch=fetch, quotes=_FakeQuotes(), notifier=_Notifier()) == 0
    assert not [n for n in notified if "newly" in n[0]]  # no repeats on an unchanged universe


# ---- profit levers: small-live sizing, direct feeds ----


def test_scan_sizes_small_live_market_to_its_book(tmp_path) -> None:
    context = grade_market(_analyzed_context(_binary_event(liquidity=800.0)), ScoringConfig(allow_fixture_analysis_live=True))
    assert context.scores["recommended_max_order_usd"] == 16.0
    config = OpportunityConfig(
        model_pricing_mode="allocatable",
        probability_estimates={context.market_id: {"yes": 0.60}},
        model_weight=1.0,
        disagreement_buffer_scale=0.0,
    )
    results = scan_opportunities(
        [context],
        config,
        _FakeQuotes(),
        _allocator(tmp_path),
        probability_lookup=config_probability_lookup(config),
    )
    assert not results[0].blockers
    assert results[0].allocation_usd == 16.0  # book-absorbable size, not the 50 per-order cap


def test_scan_prices_no_side_of_overpriced_market(tmp_path) -> None:
    context = _graded(_binary_event())
    # Model says 20%, market asks 80c: the edge is on the NO side
    # (executable NO ask = 1 - yes_bid = 0.22 against a 0.80 NO probability).
    config = OpportunityConfig(
        model_pricing_mode="allocatable",
        probability_estimates={context.market_id: {"yes": 0.20}},
        model_weight=1.0,
        disagreement_buffer_scale=0.0,
    )
    results = scan_opportunities(
        [context],
        config,
        _FakeQuotes(ask=0.80, bid=0.78),
        _allocator(tmp_path),
        probability_lookup=config_probability_lookup(config),
    )
    no_row = next(r for r in results if r.side == "NO")
    assert not no_row.blockers
    assert no_row.estimated_probability == pytest.approx(0.80)
    assert no_row.executable_price == pytest.approx(0.22)
    yes_row = next(r for r in results if r.side == "YES")
    assert any(b.startswith("edge_below_minimum") for b in yes_row.blockers)


def test_scan_no_side_can_be_disabled(tmp_path) -> None:
    context = _graded(_binary_event())
    config = OpportunityConfig(probability_estimates={context.market_id: {"yes": 0.60}}, scan_no_side=False)
    results = scan_opportunities(
        [context],
        config,
        _FakeQuotes(),
        _allocator(tmp_path),
        probability_lookup=config_probability_lookup(config),
    )
    assert [r.side for r in results] == ["YES"]


class _MapQuotes:
    """Per-token quotes for grouped-market tests."""

    def __init__(self, books: dict[str, tuple[float | None, float | None]]):
        self.books = books  # token_id -> (ask, bid)

    def yes_best_ask(self, token_id: str) -> float | None:
        return self.books.get(token_id, (None, None))[0]

    def yes_best_bid(self, token_id: str) -> float | None:
        return self.books.get(token_id, (None, None))[1]


# ---- autonomous estimator ----


def _fake_estimator_runner(p_yes: float):
    def runner(prompt: str) -> str:
        assert "Resolution rules" in prompt  # the estimate must see the rules
        assert "0." not in prompt.split("Question:")[0]  # no market price leaked
        return json.dumps({
            "type": "result",
            "result": json.dumps({
                "p_yes": p_yes,
                "confidence": 0.6,
                "base_rate_reasoning": "short-window discrete events rarely occur",
                "key_factors": ["no scheduled meeting"],
            }),
        })

    return runner


def test_estimator_roundtrips_into_forecast_lookup(tmp_path) -> None:
    from polybot.discovery.estimator import EstimatorConfig, refresh_estimates
    from polybot.discovery.opportunity import forecast_probability_lookup

    context = _graded(_binary_event())
    summary = refresh_estimates(
        [context],
        forecast_data_root=str(tmp_path),
        config=EstimatorConfig(),
        runner=_fake_estimator_runner(0.12),
    )
    assert summary["estimated"] == [context.market_id]
    lookup = forecast_probability_lookup(OpportunityConfig(forecast_data_root=str(tmp_path)))
    outcome = context.outcomes[0].name
    estimate = lookup(context.market_id, outcome)
    assert estimate is not None
    probability, source = estimate
    assert probability == pytest.approx(0.12)
    assert source == "forecast_state"


def test_estimator_skips_fresh_and_respects_budget(tmp_path) -> None:
    from polybot.discovery.estimator import EstimatorConfig, refresh_estimates

    contexts = [_graded(_binary_event(f"iran-ceasefire-{i}")) for i in range(4)]
    config = EstimatorConfig(max_per_cycle=2)
    first = refresh_estimates([contexts[0]], forecast_data_root=str(tmp_path), config=config, runner=_fake_estimator_runner(0.3))
    assert len(first["estimated"]) == 1
    # Fresh estimate is not re-run; budget caps the rest of the sweep.
    second = refresh_estimates(contexts, forecast_data_root=str(tmp_path), config=config, runner=_fake_estimator_runner(0.3))
    assert first["estimated"][0] not in second["estimated"]
    assert second["skipped_fresh"] == 1
    assert len(second["estimated"]) == 2
    assert second.get("budget_exhausted") is True


def test_estimator_aborts_and_cools_down_on_rate_limit(tmp_path) -> None:
    from polybot.discovery.estimator import EstimatorConfig, refresh_estimates

    contexts = [_graded(_binary_event(f"iran-ceasefire-{i}")) for i in range(5)]
    calls = {"n": 0}

    def limited(prompt: str) -> str:
        calls["n"] += 1
        raise RuntimeError('claude CLI exited 1: {"is_error":true,"api_error_status":429,'
                           '"result":"You\'ve hit your session limit"}')

    config = EstimatorConfig()
    first = refresh_estimates(contexts, forecast_data_root=str(tmp_path), config=config, runner=limited)
    # Aborts on the FIRST 429 instead of burning all 5 (or 25) calls.
    assert calls["n"] == 1
    assert first["rate_limited"] is True
    assert first["estimated"] == []

    # A follow-up cycle is skipped entirely while the cooldown holds -- no
    # further calls into the limited subscription.
    second = refresh_estimates(contexts, forecast_data_root=str(tmp_path), config=config, runner=limited)
    assert calls["n"] == 1
    assert second["rate_limited"] is True

    # Once the cooldown expires, estimation resumes.
    from datetime import datetime, timedelta, timezone
    later = datetime.now(timezone.utc) + timedelta(minutes=config.rate_limit_cooldown_minutes + 1)
    resumed = refresh_estimates(
        [contexts[0]], forecast_data_root=str(tmp_path), config=config,
        runner=_fake_estimator_runner(0.2), now=later,
    )
    assert resumed["estimated"] == [contexts[0].market_id]


def test_estimator_skips_grouped_and_survives_errors(tmp_path) -> None:
    from polybot.discovery.estimator import EstimatorConfig, refresh_estimates

    grouped = _graded(_grouped_event())
    binary = _graded(_binary_event())

    def broken(prompt: str) -> str:
        raise RuntimeError("cli down")

    summary = refresh_estimates(
        [grouped, binary],
        forecast_data_root=str(tmp_path),
        config=EstimatorConfig(),
        runner=broken,
    )
    assert summary["skipped_grouped"] == 1
    assert summary["estimated"] == []
    assert summary["errors"] == [binary.market_id]


def test_group_arbitrage_detects_overround(tmp_path) -> None:
    context = _graded(_grouped_event())
    books = {o.yes_token_id: (0.62, 0.60) for o in context.outcomes}
    arbs = scan_group_arbitrage([context], OpportunityConfig(), _MapQuotes(books))
    # Bids sum to 1.20: buying NO on every leg locks in 0.20 minus per-leg slippage.
    assert len(arbs) == 1
    arb = arbs[0]
    assert arb["type"] == "short_all_overround"
    assert arb["sum_yes_bids"] == pytest.approx(1.20)
    assert arb["edge_after_slippage"] == pytest.approx(0.18)


def test_quote_adapter_backs_off_dead_books() -> None:
    from polybot.location.quotes import PublicClobQuoteAdapter

    adapter = PublicClobQuoteAdapter(["t1"], refresh_seconds=0.0)
    calls = {"n": 0}

    def broken(token_id: str) -> None:
        calls["n"] += 1
        raise RuntimeError("404 Client Error: Not Found for url: .../book")

    adapter.book.rest_snapshot = broken  # type: ignore[method-assign]
    adapter.quote_snapshot("t1")
    adapter.quote_snapshot("t1")
    adapter.quote_snapshot("t1")
    # A gone book is fetched once, then negative-cached: a universe sweep
    # must not re-poll ~2,000 dead books every cycle.
    assert calls["n"] == 1
    assert adapter.yes_best_ask("t1") is None


def test_group_arbitrage_skips_non_neg_risk_groups(tmp_path) -> None:
    # Multi-winner groups (ballot access, pardon lists) share an event but
    # are not mutually exclusive: YES prices legitimately sum past 1.0, so
    # the exactly-one-YES arithmetic must not flag them as arbitrage.
    event = _grouped_event()
    event["negRisk"] = False
    for market in event["markets"]:
        market["negRisk"] = False  # _market() infers negRisk from group_title
    context = _graded(event)
    assert not context.neg_risk
    books = {o.yes_token_id: (0.62, 0.60) for o in context.outcomes}
    arbs = scan_group_arbitrage([context], OpportunityConfig(), _MapQuotes(books))
    assert arbs == []


def test_group_arbitrage_detects_underround(tmp_path) -> None:
    context = _graded(_grouped_event())
    books = {o.yes_token_id: (0.40, 0.35) for o in context.outcomes}
    arbs = scan_group_arbitrage([context], OpportunityConfig(), _MapQuotes(books))
    # Asks sum to 0.80: buying YES on every leg locks in 0.20 minus slippage.
    assert len(arbs) == 1
    assert arbs[0]["type"] == "long_all_underround"
    assert arbs[0]["edge_after_slippage"] == pytest.approx(0.18)


def test_group_arbitrage_requires_full_quotes_and_consistent_books(tmp_path) -> None:
    context = _graded(_grouped_event())
    # Missing quote on one leg: no arb call.
    partial = {context.outcomes[0].yes_token_id: (0.62, 0.60), context.outcomes[1].yes_token_id: (None, None)}
    assert scan_group_arbitrage([context], OpportunityConfig(), _MapQuotes(partial)) == []
    # Consistent books (sum ~ 1): nothing to report.
    fair = {o.yes_token_id: (0.51, 0.49) for o in context.outcomes}
    assert scan_group_arbitrage([context], OpportunityConfig(), _MapQuotes(fair)) == []


def test_source_plan_puts_direct_feeds_first() -> None:
    plan = build_source_plan(_analyzed_context(_binary_event()))
    # united_states is a deciding party -> its direct press feed leads, ahead
    # of aggregator queries whose indexing lag dominates reaction time.
    # Ordering follows MEASURED latency: credible-reporting feeds (0-30 min)
    # lead, official government feeds (measured 2.7-5.6 days stale) follow,
    # aggregators trail. Was state.gov until 2026-07-20, when probing found
    # that feed serving 200-with-zero-items.
    assert "theguardian.com" in plan.feed_urls[0]
    war_gov = next(i for i, u in enumerate(plan.feed_urls) if "war.gov" in u)
    guardian = plan.feed_urls.index(plan.feed_urls[0])
    assert guardian < war_gov, "fast credible feeds must be polled before slow official ones"
    assert "https://www.aljazeera.com/xml/rss/all.xml" in plan.feed_urls
    google = [u for u in plan.feed_urls if "news.google.com" in u]
    assert google and plan.feed_urls.index(google[0]) > plan.feed_urls.index("https://www.aljazeera.com/xml/rss/all.xml")
