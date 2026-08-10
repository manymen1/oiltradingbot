"""IMF PortWatch chokepoint data client.

A family of Polymarket geopolitics markets resolves on a hard, public number:
the 7-day moving average of transit calls ("Arrivals of Ships") through a
maritime chokepoint (Strait of Hormuz, Bab-el-Mandeb, Suez, ...), published
daily by IMF PortWatch. Example rule:

    Resolves YES if IMF Portwatch publishes a 7-day moving average of transit
    calls for the Strait of Hormuz equal to or above 60 for any date between
    market creation and the deadline.

That is exactly the shape the valuation/NPM strategy already exploits: a
scheduled official dataset resolving a thin market. This module fetches the
daily series and computes the trailing 7-day average, so the estimator prices
these markets against the real number instead of a base-rate guess.

Data source: the PortWatch "Daily_Chokepoints_Data" ArcGIS feature service.
`n_total` is the day's transit-call count (container + dry bulk + general
cargo + roro + tanker), matching the "Arrivals of Ships" definition in the
market rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable
from urllib.parse import urlencode

FEATURE_SERVICE = (
    "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
    "Daily_Chokepoints_Data/FeatureServer/0/query"
)

# PortWatch chokepoint portnames, keyed by the terms most likely to appear in
# a market question. Extend as new chokepoint markets are discovered.
CHOKEPOINT_NAMES = {
    "hormuz": "Strait of Hormuz",
    "bab-el-mandeb": "Bab el-Mandeb Strait",
    "bab el-mandeb": "Bab el-Mandeb Strait",
    "suez": "Suez Canal",
    "panama": "Panama Canal",
    "malacca": "Strait of Malacca",
    "bosphorus": "Bosphorus Strait",
    "gibraltar": "Strait of Gibraltar",
}

Fetcher = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class ChokepointReading:
    portname: str
    latest_date: str
    latest_value: int
    ma7: float                 # trailing 7-day moving average of n_total
    days_available: int
    series: list[tuple[str, int]]  # (date, n_total), most-recent first


def _http_fetch(url: str) -> dict[str, Any]:
    import requests

    response = requests.get(url, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("PortWatch returned a non-object payload")
    if "error" in payload:
        raise RuntimeError(f"PortWatch query error: {payload['error']}")
    return payload


def build_query_url(portname: str, *, limit: int = 30) -> str:
    params = {
        "where": f"portname = '{portname}'",
        "outFields": "date,n_total,portname",
        "orderByFields": "date DESC",
        "resultRecordCount": limit,
        "f": "json",
    }
    return f"{FEATURE_SERVICE}?{urlencode(params)}"


def parse_features(payload: dict[str, Any]) -> list[tuple[str, int]]:
    """Return (iso_date, n_total) rows, most-recent first. ArcGIS dates come
    back either as 'YYYY-MM-DD' strings (DateOnly) or epoch-millis ints."""
    rows: list[tuple[str, int]] = []
    for feature in payload.get("features", []):
        attrs = feature.get("attributes") if isinstance(feature, dict) else None
        if not isinstance(attrs, dict):
            continue
        raw_date = attrs.get("date")
        value = attrs.get("n_total")
        if raw_date is None or value is None:
            continue
        rows.append((_normalize_date(raw_date), int(value)))
    rows.sort(key=lambda row: row[0], reverse=True)
    return rows


def _normalize_date(raw: Any) -> str:
    if isinstance(raw, str):
        return raw[:10]
    # epoch millis -> date
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc).date().isoformat()


def chokepoint_reading(portname: str, *, fetcher: Fetcher | None = None, limit: int = 30) -> ChokepointReading | None:
    fetcher = fetcher or _http_fetch
    payload = fetcher(build_query_url(portname, limit=limit))
    rows = parse_features(payload)
    if not rows:
        return None
    window = [value for _date, value in rows[:7]]
    ma7 = round(sum(window) / len(window), 2)
    return ChokepointReading(
        portname=portname,
        latest_date=rows[0][0],
        latest_value=rows[0][1],
        ma7=ma7,
        days_available=len(rows),
        series=rows,
    )


def chokepoint_series(
    portname: str,
    *,
    fetcher: Fetcher | None = None,
    limit: int = 500,
) -> list[tuple[str, int]]:
    """Fetch the published daily series, most-recent first.

    Rule evidence needs substantially more history than the estimator's
    current-reading helper.  Keeping this fetch path here also ensures both
    consumers parse the official ArcGIS dataset identically.
    """

    fetcher = fetcher or _http_fetch
    return parse_features(fetcher(build_query_url(portname, limit=limit)))


def moving_average_series(
    rows: list[tuple[str, int]],
    *,
    window_days: int = 7,
) -> list[tuple[str, float]]:
    """Return exact trailing calendar-day averages, oldest first.

    A missing calendar date does not silently turn a seven-day moving average
    into an average over a longer interval.  PortWatch normally publishes one
    observation per day; gaps therefore remain unavailable evidence.
    """

    from datetime import date as _date, timedelta

    values = {stamp: int(value) for stamp, value in rows}
    output: list[tuple[str, float]] = []
    for stamp in sorted(values):
        end = _date.fromisoformat(stamp)
        dates = [
            (end - timedelta(days=offset)).isoformat()
            for offset in range(window_days - 1, -1, -1)
        ]
        if not all(item in values for item in dates):
            continue
        average = sum(values[item] for item in dates) / window_days
        output.append((stamp, round(average, 6)))
    return output


def match_chokepoint(text: str) -> str | None:
    """Map a market question/rule text to a PortWatch portname, or None."""
    lowered = text.lower()
    for term, portname in CHOKEPOINT_NAMES.items():
        if term in lowered:
            return portname
    return None


def staleness_days(reading: ChokepointReading, today: "date | None" = None) -> int:
    """How far behind real time the published series is. PortWatch publishes
    with a multi-day lag (observed: 8 days), so the estimator must not treat
    the latest value as current-state evidence."""
    from datetime import date as _date

    today = today or _date.today()
    try:
        return max(0, (today - _date.fromisoformat(reading.latest_date)).days)
    except ValueError:
        return 0


def evidence_line(reading: ChokepointReading, threshold: float | None = None, *, today: "date | None" = None) -> str:
    """One-line factual summary for injection into an estimator prompt.

    ALWAYS discloses the publication lag: the fix that made the estimator
    data-aware would otherwise make it stale-data-overconfident, which is the
    same error in the opposite direction. The market prices today's news; our
    series may be a week behind it.
    """
    lag = staleness_days(reading, today)
    base = (
        f"IMF PortWatch data for {reading.portname}: latest daily transit calls "
        f"{reading.latest_value} on {reading.latest_date}; trailing 7-day moving "
        f"average {reading.ma7} (over {min(7, reading.days_available)} days)."
    )
    if threshold is not None:
        gap = round(threshold - reading.ma7, 2)
        base += (
            f" Market threshold is {threshold:g}; the 7-day average is currently "
            f"{'ABOVE' if gap <= 0 else 'BELOW'} it by {abs(gap):g}."
        )
    if lag > 1:
        base += (
            f" CAUTION: this data is {lag} days stale (published with a lag); it says nothing "
            f"about the most recent {lag} days, during which the market has been pricing live "
            f"news. Do not treat the market's disagreement with this figure as automatic edge."
        )
    return base
