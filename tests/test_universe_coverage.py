from __future__ import annotations

from dataclasses import replace

from polybot.discovery.config import EnumerationView, UniverseConfig
from polybot.discovery.gamma_universe import enumerate_active_events
from polybot.discovery.store import DiscoveryStore


def _event(index: int, *, liquidity: float | None = None) -> dict:
    return {
        "id": str(index),
        "slug": f"event-{index}",
        "title": f"Geopolitical event {index}",
        "liquidity": float(index if liquidity is None else liquidity),
        "markets": [],
    }


def _universe(**changes) -> UniverseConfig:
    return replace(
        UniverseConfig(
            max_events=0,
            page_size=100,
            enumeration_views=[
                EnumerationView(order="liquidity", ascending=False)
            ],
        ),
        **changes,
    )


def test_keyset_enumerates_more_than_three_hundred_events() -> None:
    events = [_event(index) for index in range(350)]

    def fetch(_url, params):
        cursor = int(params.get("after_cursor") or 0)
        page = events[cursor : cursor + int(params["limit"])]
        next_cursor = (
            str(cursor + len(page))
            if cursor + len(page) < len(events)
            else None
        )
        return {"events": page, "next_cursor": next_cursor}

    result = enumerate_active_events(
        _universe(),
        fetch=fetch,
        scanned_at="2026-07-25T00:00:00+00:00",
    )
    assert len(result.events) == 350
    assert result.manifest["coverage_status"] == "COMPLETE"
    assert result.manifest["coverage_complete"] is True
    assert result.manifest["views"]["0:liquidity:desc"]["pages_fetched"] == 4


def test_thin_event_beyond_first_liquidity_page_is_not_truncated() -> None:
    events = [
        _event(index, liquidity=10_000 - index) for index in range(301)
    ]
    events[-1]["title"] = "Thin Iran peace talks market"

    def fetch(_url, params):
        cursor = int(params.get("after_cursor") or 0)
        page = events[cursor : cursor + int(params["limit"])]
        return {
            "events": page,
            "next_cursor": (
                str(cursor + len(page))
                if cursor + len(page) < len(events)
                else None
            ),
        }

    result = enumerate_active_events(_universe(), fetch=fetch)
    assert any(event["id"] == "300" for event in result.events)
    assert result.manifest["unique_events"] == 301


def test_multiple_views_deduplicate_stable_event_ids() -> None:
    universe = replace(
        _universe(),
        enumeration_views=[
            EnumerationView(order="liquidity", ascending=False),
            EnumerationView(order="volume", ascending=False),
        ],
    )

    def fetch(_url, params):
        return {"events": [_event(1), _event(2)], "next_cursor": None}

    result = enumerate_active_events(universe, fetch=fetch)
    assert len(result.events) == 2
    assert result.manifest["raw_events"] == 4
    assert result.manifest["duplicate_rows"] == 2


def test_conflicting_duplicate_payloads_are_recorded_deterministically() -> None:
    universe = replace(
        _universe(),
        enumeration_views=[
            EnumerationView(order="liquidity", ascending=False),
            EnumerationView(order="volume", ascending=False),
        ],
    )

    def fetch(_url, params):
        event = _event(1)
        if params["order"] == "volume":
            event["title"] = "Updated title"
        return {"events": [event], "next_cursor": None}

    first = enumerate_active_events(
        universe,
        fetch=fetch,
        scanned_at="2026-07-25T00:00:00+00:00",
    )
    second = enumerate_active_events(
        universe,
        fetch=fetch,
        scanned_at="2026-07-25T00:00:00+00:00",
    )
    assert len(first.manifest["payload_conflicts"]["id:1"]) == 2
    assert first.manifest["coverage_sha256"] == second.manifest["coverage_sha256"]
    assert first.events[0]["title"] == "Geopolitical event 1"


def test_empty_first_page_terminates_complete() -> None:
    result = enumerate_active_events(
        _universe(),
        fetch=lambda _url, _params: {"events": [], "next_cursor": None},
    )
    assert result.events == []
    assert result.manifest["coverage_status"] == "COMPLETE"


def test_short_legacy_fixture_page_terminates_complete() -> None:
    calls = []

    def fetch(_url, params):
        calls.append(dict(params))
        return [_event(1), _event(2)]

    result = enumerate_active_events(_universe(), fetch=fetch)
    assert len(calls) == 1
    assert len(result.events) == 2
    assert result.manifest["views"]["0:liquidity:desc"]["pagination"] == "offset"


def test_repeated_full_page_fails_closed() -> None:
    page = [_event(index) for index in range(100)]
    result = enumerate_active_events(
        _universe(),
        fetch=lambda _url, _params: list(page),
    )
    view = result.manifest["views"]["0:liquidity:desc"]
    assert result.manifest["coverage_status"] == "INCOMPLETE"
    assert view["failure"] == "repeated_page"


def test_repeated_cursor_fails_closed() -> None:
    def fetch(_url, params):
        return {
            "events": [_event(1)],
            "next_cursor": "same",
        }

    result = enumerate_active_events(
        replace(_universe(), page_size=1),
        fetch=fetch,
    )
    assert result.manifest["coverage_status"] == "INCOMPLETE"
    assert (
        result.manifest["views"]["0:liquidity:desc"]["failure"]
        == "repeated_page"
    )


def test_malformed_page_fails_closed() -> None:
    result = enumerate_active_events(
        _universe(),
        fetch=lambda _url, _params: {"events": "not-a-list"},
    )
    assert result.manifest["coverage_status"] == "INCOMPLETE"
    assert (
        result.manifest["views"]["0:liquidity:desc"]["failure"]
        == "malformed_keyset_response"
    )


def test_http_failure_fails_closed() -> None:
    def fetch(_url, _params):
        raise RuntimeError("boom")

    result = enumerate_active_events(_universe(), fetch=fetch)
    assert result.manifest["coverage_status"] == "INCOMPLETE"
    assert "fetch_error:RuntimeError" in result.manifest["views"]["0:liquidity:desc"]["failure"]


def test_positive_max_events_is_explicit_truncation() -> None:
    events = [_event(index) for index in range(20)]

    def fetch(_url, params):
        return {"events": events[: params["limit"]], "next_cursor": "more"}

    result = enumerate_active_events(
        replace(_universe(), max_events=10, page_size=10),
        fetch=fetch,
    )
    assert len(result.events) == 10
    assert result.manifest["coverage_status"] == "TRUNCATED"
    assert result.manifest["truncated"] is True


def test_zero_max_events_is_uncapped() -> None:
    events = [_event(index) for index in range(150)]

    def fetch(_url, params):
        cursor = int(params.get("after_cursor") or 0)
        page = events[cursor : cursor + params["limit"]]
        return {
            "events": page,
            "next_cursor": (
                str(cursor + len(page))
                if cursor + len(page) < len(events)
                else None
            ),
        }

    result = enumerate_active_events(_universe(max_events=0), fetch=fetch)
    assert len(result.events) == 150
    assert result.manifest["truncated"] is False


def test_slug_fallback_is_explicitly_recorded() -> None:
    event = _event(1)
    event.pop("id")
    result = enumerate_active_events(
        _universe(),
        fetch=lambda _url, _params: {"events": [event]},
    )
    assert result.manifest["slug_fallback_identities"] == ["slug:event-1"]


def test_identical_scan_inputs_have_deterministic_manifest_hash() -> None:
    fetch = lambda _url, _params: {"events": [_event(1)]}
    first = enumerate_active_events(
        _universe(),
        fetch=fetch,
        scanned_at="2026-07-25T00:00:00+00:00",
    )
    second = enumerate_active_events(
        _universe(),
        fetch=fetch,
        scanned_at="2026-07-25T00:00:00+00:00",
    )
    assert first.manifest == second.manifest


def test_current_coverage_manifest_fails_closed_after_tampering(
    tmp_path,
) -> None:
    result = enumerate_active_events(
        _universe(),
        fetch=lambda _url, _params: {"events": [_event(1)]},
        scanned_at="2026-07-25T00:00:00+00:00",
    )
    store = DiscoveryStore(tmp_path)
    store.save_coverage_manifest(result.manifest)
    assert store.load_coverage_manifest() == result.manifest

    current = tmp_path / "coverage_manifest.json"
    current.write_text(
        current.read_text(encoding="utf-8").replace(
            '"coverage_complete": true',
            '"coverage_complete": false',
        ),
        encoding="utf-8",
    )
    assert store.load_coverage_manifest() is None
