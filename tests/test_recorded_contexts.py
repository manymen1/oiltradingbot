"""Which markets the shared book service records.

Recording is not a scarce-resource decision the way monitoring is: an order
book cannot be rebuilt afterwards, so anything not recorded today is missing
from the research record forever. These tests pin the two properties that
make widening safe -- monitored markets are never displaced, and the default
stays the old monitored-only behaviour.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from polybot.discovery.config import (
    DiscoveryConfig,
    ForwardRecorderConfig,
    RuleRunnerConfig,
    _validate_discovery_config,
    load_discovery_config,
)
from polybot.discovery.fleet import recorded_book_contexts
from polybot.discovery.types import MarketContext


def _context(
    market_id: str,
    *,
    state: str = "MONITOR_ONLY",
    volume: float = 0.0,
    liquidity: float = 0.0,
    closed: bool = False,
) -> MarketContext:
    return MarketContext(
        market_id=market_id,
        kind="binary",
        event_slug=f"slug-{market_id}",
        event_title=f"title {market_id}",
        question=f"question {market_id}?",
        deadline_iso="2026-12-31T00:00:00+00:00",
        outcomes=[],
        rule_text="rule text",
        rule_text_sha256="0" * 64,
        rule_version=1,
        state=state,
        volume=volume,
        liquidity=liquidity,
        closed=closed,
    )


def _config(**recorder: object) -> DiscoveryConfig:
    # The recorder refuses to validate without the rules-first runner, so a
    # config used for validation has to enable both.
    return DiscoveryConfig(
        rule_runner=RuleRunnerConfig(enabled=True),
        forward_recorder=ForwardRecorderConfig(enabled=True, **recorder),
    )


def test_default_records_only_the_monitored_set() -> None:
    monitored = [_context("a", state="PAPER_ELIGIBLE")]
    everything = [*monitored, _context("b"), _context("c")]

    recorded = recorded_book_contexts(_config(), everything, monitored)

    assert [c.market_id for c in recorded] == ["a"]


def test_record_all_contexts_adds_unmonitored_markets() -> None:
    monitored = [_context("a", state="PAPER_ELIGIBLE")]
    everything = [
        *monitored,
        _context("b", state="MONITOR_ONLY"),
        _context("c", state="DISCOVERED"),
    ]

    recorded = recorded_book_contexts(
        _config(record_all_contexts=True), everything, monitored
    )

    # MONITOR_ONLY and ungraded markets are exactly the ones whose gradings
    # the recorded data is supposed to test, so they must be included.
    assert sorted(c.market_id for c in recorded) == ["a", "b", "c"]


def test_closed_markets_are_not_recorded() -> None:
    monitored: list[MarketContext] = []
    everything = [_context("open"), _context("done", closed=True)]

    recorded = recorded_book_contexts(
        _config(record_all_contexts=True), everything, monitored
    )

    assert [c.market_id for c in recorded] == ["open"]


def test_extra_contexts_are_ordered_by_volume() -> None:
    everything = [
        _context("low", volume=10.0),
        _context("high", volume=900.0),
        _context("mid", volume=100.0),
    ]

    recorded = recorded_book_contexts(
        _config(record_all_contexts=True), everything, []
    )

    assert [c.market_id for c in recorded] == ["high", "mid", "low"]


def test_cap_trims_extras_but_never_displaces_monitored_markets() -> None:
    monitored = [
        _context("m1", state="PAPER_ELIGIBLE"),
        _context("m2", state="PAPER_ELIGIBLE"),
    ]
    everything = [
        *monitored,
        _context("x", volume=50.0),
        _context("y", volume=500.0),
    ]

    recorded = recorded_book_contexts(
        _config(record_all_contexts=True, max_recorded_markets=3),
        everything,
        monitored,
    )

    # Cap of 3 leaves room for exactly one extra, and it is the higher-volume
    # one; both monitored markets survive.
    assert [c.market_id for c in recorded] == ["m1", "m2", "y"]


def test_cap_below_monitored_count_still_keeps_every_monitored_market() -> None:
    monitored = [
        _context("m1", state="PAPER_ELIGIBLE"),
        _context("m2", state="PAPER_ELIGIBLE"),
    ]
    everything = [*monitored, _context("x", volume=50.0)]

    recorded = recorded_book_contexts(
        _config(record_all_contexts=True, max_recorded_markets=1),
        everything,
        monitored,
    )

    # A paper worker must never lose book recording to the cap: the recorded
    # set stays a superset of the monitored set even when the cap is smaller.
    assert [c.market_id for c in recorded] == ["m1", "m2"]


def test_config_validation_accepts_and_rejects_the_new_fields() -> None:
    # Validate against the shipped config: the recorder sits behind a
    # dependency chain (recorder -> rule_runner -> rule_compiler), so a
    # hand-built fragment cannot exercise validation honestly.
    shipped = load_discovery_config(
        Path("configs/geopolitics/discovery.yaml")
    )
    assert shipped.forward_recorder.record_all_contexts is True

    def _with(**recorder: object) -> DiscoveryConfig:
        return replace(
            shipped,
            forward_recorder=replace(shipped.forward_recorder, **recorder),
        )

    _validate_discovery_config(
        _with(record_all_contexts=True, max_recorded_markets=25)
    )
    _validate_discovery_config(_with(record_all_contexts=False))

    for bad in (
        {"record_all_contexts": "yes"},
        {"max_recorded_markets": -1},
        {"max_recorded_markets": True},
    ):
        try:
            _validate_discovery_config(_with(**bad))
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")
