from __future__ import annotations

from polybot.discovery.portwatch import (
    ChokepointReading,
    build_query_url,
    chokepoint_reading,
    evidence_line,
    match_chokepoint,
    moving_average_series,
    parse_features,
)


def _payload(rows: list[tuple[str, int]]) -> dict:
    return {"features": [{"attributes": {"date": d, "n_total": v, "portname": "Strait of Hormuz"}} for d, v in rows]}


def test_build_query_url_filters_portname_and_orders_desc() -> None:
    url = build_query_url("Strait of Hormuz")
    assert "portname+%3D+%27Strait+of+Hormuz%27" in url or "portname = 'Strait of Hormuz'" in url.replace("+", " ").replace("%3D", "=").replace("%27", "'")
    assert "date+DESC" in url or "date DESC" in url.replace("+", " ")


def test_parse_features_handles_string_and_epoch_dates() -> None:
    payload = {
        "features": [
            {"attributes": {"date": "2026-07-12", "n_total": 10}},
            {"attributes": {"date": 1751500800000, "n_total": 22}},  # epoch millis
            {"attributes": {"date": None, "n_total": 5}},  # dropped
            {"attributes": {"date": "2026-07-11", "n_total": None}},  # dropped
        ]
    }
    rows = parse_features(payload)
    assert all(len(d) == 10 for d, _ in rows)  # normalized to YYYY-MM-DD
    assert rows[0][0] >= rows[-1][0]  # most-recent first


def test_chokepoint_reading_computes_trailing_7day_average() -> None:
    # 10 days; only the most recent 7 count toward the average.
    rows = [
        ("2026-07-12", 10), ("2026-07-11", 14), ("2026-07-10", 9),
        ("2026-07-09", 11), ("2026-07-08", 15), ("2026-07-07", 28),
        ("2026-07-06", 28), ("2026-07-05", 100), ("2026-07-04", 100),
        ("2026-07-03", 100),
    ]
    reading = chokepoint_reading("Strait of Hormuz", fetcher=lambda _url: _payload(rows))
    assert reading is not None
    assert reading.latest_date == "2026-07-12"
    assert reading.latest_value == 10
    # (10+14+9+11+15+28+28)/7 = 16.43 -- the older 100s must not leak in.
    assert reading.ma7 == 16.43
    assert reading.days_available == 10


def test_chokepoint_reading_short_series_averages_available_days() -> None:
    rows = [("2026-07-12", 12), ("2026-07-11", 18)]
    reading = chokepoint_reading("Strait of Hormuz", fetcher=lambda _url: _payload(rows))
    assert reading is not None and reading.ma7 == 15.0


def test_moving_average_series_requires_consecutive_calendar_days() -> None:
    complete = [
        (f"2026-07-{day:02d}", day)
        for day in range(1, 9)
    ]
    assert moving_average_series(complete) == [
        ("2026-07-07", 4.0),
        ("2026-07-08", 5.0),
    ]
    missing = [item for item in complete if item[0] != "2026-07-04"]
    assert moving_average_series(missing) == []


def test_chokepoint_reading_empty_returns_none() -> None:
    assert chokepoint_reading("Nowhere", fetcher=lambda _url: {"features": []}) is None


def test_match_chokepoint_maps_market_text() -> None:
    assert match_chokepoint("Strait of Hormuz traffic returns to normal?") == "Strait of Hormuz"
    assert match_chokepoint("Will Suez Canal transits recover?") == "Suez Canal"
    assert match_chokepoint("Will the US invade Iran?") is None


def test_staleness_is_disclosed_in_evidence() -> None:
    from datetime import date

    from polybot.discovery.portwatch import staleness_days

    reading = ChokepointReading(
        portname="Strait of Hormuz", latest_date="2026-07-12", latest_value=10,
        ma7=16.43, days_available=7, series=[],
    )
    assert staleness_days(reading, today=date(2026, 7, 20)) == 8
    line = evidence_line(reading, threshold=60, today=date(2026, 7, 20))
    # A stale series must never be presented as current state: the estimator
    # would otherwise read the market's disagreement as free edge.
    assert "8 days stale" in line
    assert "automatic edge" in line


def test_fresh_data_carries_no_staleness_warning() -> None:
    from datetime import date

    reading = ChokepointReading(
        portname="Suez Canal", latest_date="2026-07-20", latest_value=41,
        ma7=41.14, days_available=7, series=[],
    )
    line = evidence_line(reading, threshold=60, today=date(2026, 7, 20))
    assert "stale" not in line


def test_evidence_line_reports_gap_to_threshold() -> None:
    reading = ChokepointReading(
        portname="Strait of Hormuz", latest_date="2026-07-12", latest_value=10,
        ma7=16.43, days_available=7, series=[],
    )
    line = evidence_line(reading, threshold=60)
    assert "16.43" in line
    assert "BELOW" in line
    assert "43.57" in line  # 60 - 16.43  # 60 - 16.43
