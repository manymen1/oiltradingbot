from __future__ import annotations

import json

import pytest

from polybot.core.execution import PaperTradingAdapter


class _Quotes:
    def __init__(self, books: dict[str, dict]) -> None:
        self.books = books

    def yes_best_ask(self, yes_token_id: str) -> float | None:
        asks = self.books.get(yes_token_id, {}).get("asks", [])
        return min((float(level[0]) for level in asks), default=None)

    def yes_best_bid(self, yes_token_id: str) -> float | None:
        bids = self.books.get(yes_token_id, {}).get("bids", [])
        return max((float(level[0]) for level in bids), default=None)

    def quote_snapshot(self, token_id: str) -> dict:
        if token_id not in self.books:
            raise KeyError(token_id)
        return {"token_id": token_id, **self.books[token_id]}


def _book(
    *,
    asks: list[tuple[float, float]],
    bids: list[tuple[float, float]],
    revision: int = 1,
    staleness: float = 0.0,
) -> dict:
    return {"asks": asks, "bids": bids, "revision": revision, "staleness": staleness}


def _adapter(
    tmp_path,
    *,
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
    quotes: _Quotes | None = None,
    max_book_age_seconds: float = 10.0,
) -> PaperTradingAdapter:
    return PaperTradingAdapter(
        state_path=tmp_path / "paper_broker.json",
        quote_provider=quotes
        or _Quotes(
            {
                "yes": _book(asks=[(0.40, 1_000.0)], bids=[(0.35, 1_000.0)]),
                "no": _book(asks=[(0.65, 1_000.0)], bids=[(0.60, 1_000.0)]),
            }
        ),
        token_pairs=[("yes", "no")],
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        max_book_age_seconds=max_book_age_seconds,
    )


def test_paper_broker_mutates_balances_and_survives_restart(tmp_path) -> None:
    adapter = _adapter(tmp_path)

    buy = adapter.buy_yes_fak("yes", usd=40.0, max_price=0.40)
    assert adapter.verify_fill(buy, "yes").filled_shares == pytest.approx(100.0)
    assert adapter.query_live_position("yes", "no").yes_shares == pytest.approx(100.0)

    restarted = _adapter(tmp_path)
    assert restarted.query_live_position("yes", "no").yes_shares == pytest.approx(100.0)
    sell = restarted.sell_yes_fak("yes", shares=25.0, min_price=0.30)
    assert restarted.verify_fill(sell, "yes").filled_shares == pytest.approx(25.0)
    assert restarted.query_live_position("yes", "no").yes_shares == pytest.approx(75.0)
    assert restarted.snapshot()["net_cash_usd"] == pytest.approx(-31.25)


def test_paper_broker_derives_no_book_from_yes_book(tmp_path) -> None:
    adapter = PaperTradingAdapter(
        state_path=tmp_path / "paper_broker.json",
        quote_provider=_Quotes(
            {
                "yes": _book(
                    asks=[(0.40, 500.0)],
                    bids=[(0.55, 500.0)],
                )
            }
        ),
        token_pairs=[("yes", "no")],
    )

    assert adapter.no_best_ask("no") == pytest.approx(0.45)
    buy = adapter.buy_no_fak("no", usd=45.0, max_price=0.45)
    assert adapter.verify_fill(buy, "no").filled_shares == pytest.approx(100.0)
    # Executable NO bid is 1 - YES ask = 0.60.
    sell = adapter.sell_no_fak("no", shares=20.0, min_price=0.55)
    assert sell["execution_price"] == pytest.approx(0.60)
    assert adapter.query_live_position("yes", "no").no_shares == pytest.approx(80.0)


def test_paper_broker_prefers_direct_no_depth_when_available(tmp_path) -> None:
    quotes = _Quotes(
        {
            "yes": _book(asks=[(0.40, 100.0)], bids=[(0.35, 100.0)]),
            "no": _book(asks=[(0.30, 20.0)], bids=[(0.25, 100.0)]),
        }
    )
    adapter = _adapter(tmp_path, quotes=quotes)

    buy = adapter.buy_no_fak("no", usd=30.0, max_price=0.35)

    assert adapter.no_best_ask("no") is None  # the only displayed ask was consumed
    assert buy["filled_shares"] == pytest.approx(20.0)
    assert buy["total_cost_usd"] == pytest.approx(6.0)
    assert buy["partial"] is True


def test_paper_broker_honors_limits_and_never_invents_a_fill(tmp_path) -> None:
    adapter = _adapter(tmp_path)

    blocked_buy = adapter.buy_yes_fak("yes", usd=40.0, max_price=0.39)
    blocked_sell = adapter.sell_yes_fak("yes", shares=10.0, min_price=0.30)

    assert blocked_buy["filled_shares"] == 0.0
    assert blocked_sell["filled_shares"] == 0.0
    assert adapter.query_live_position("yes", "no").yes_shares == 0.0
    assert not (tmp_path / "paper_broker.json").exists()


def test_paper_broker_records_configured_fee_and_slippage(tmp_path) -> None:
    adapter = _adapter(tmp_path, fee_bps=100.0, slippage_bps=100.0)

    buy = adapter.buy_yes_fak("yes", usd=100.0, max_price=0.50)

    assert buy["execution_price"] == pytest.approx(0.404)
    assert buy["filled_shares"] == pytest.approx(100.0 / (0.404 * 1.01))
    assert buy["gross_cost_usd"] + buy["fee_usd"] == pytest.approx(100.0)
    assert adapter.snapshot()["fees_usd"] == pytest.approx(buy["fee_usd"])
    sell = adapter.sell_yes_fak("yes", shares=10.0, min_price=0.30)
    assert sell["execution_price"] == pytest.approx(0.3465)
    assert sell["net_execution_price"] == pytest.approx(0.3465 * 0.99)


def test_paper_broker_walks_depth_and_returns_partial_buy(tmp_path) -> None:
    quotes = _Quotes(
        {
            "yes": _book(
                asks=[(0.40, 10.0), (0.45, 20.0), (0.60, 100.0)],
                bids=[(0.35, 100.0)],
            ),
            "no": _book(asks=[(0.65, 100.0)], bids=[(0.60, 100.0)]),
        }
    )
    adapter = _adapter(tmp_path, quotes=quotes)

    buy = adapter.buy_yes_fak("yes", usd=100.0, max_price=0.50)

    assert buy["filled_shares"] == pytest.approx(30.0)
    assert buy["gross_cost_usd"] == pytest.approx(13.0)
    assert buy["execution_price"] == pytest.approx(13.0 / 30.0)
    assert buy["unfilled_usd"] == pytest.approx(87.0)
    assert buy["partial"] is True
    assert len(buy["level_fills"]) == 2


def test_paper_broker_does_not_reuse_same_snapshot_depth(tmp_path) -> None:
    quotes = _Quotes(
        {
            "yes": _book(asks=[(0.40, 10.0)], bids=[(0.35, 100.0)], revision=7),
            "no": _book(asks=[(0.65, 100.0)], bids=[(0.60, 100.0)], revision=7),
        }
    )
    adapter = _adapter(tmp_path, quotes=quotes)

    first = adapter.buy_yes_fak("yes", usd=10.0, max_price=0.50)
    second = adapter.buy_yes_fak("yes", usd=10.0, max_price=0.50)
    quotes.books["yes"]["revision"] = 8
    third = adapter.buy_yes_fak("yes", usd=10.0, max_price=0.50)

    assert first["filled_shares"] == pytest.approx(10.0)
    assert second["filled_shares"] == 0.0
    assert second["reason"] == "paper_book_depth_consumed"
    assert third["filled_shares"] == pytest.approx(10.0)


def test_paper_broker_walks_bid_depth_and_returns_partial_sell(tmp_path) -> None:
    quotes = _Quotes(
        {
            "yes": _book(
                asks=[(0.40, 100.0)],
                bids=[(0.35, 20.0), (0.30, 20.0), (0.20, 100.0)],
            ),
            "no": _book(asks=[(0.65, 100.0)], bids=[(0.60, 100.0)]),
        }
    )
    adapter = _adapter(tmp_path, quotes=quotes)
    adapter.buy_yes_fak("yes", usd=40.0, max_price=0.40)

    sell = adapter.sell_yes_fak("yes", shares=100.0, min_price=0.25)

    assert sell["filled_shares"] == pytest.approx(40.0)
    assert sell["execution_price"] == pytest.approx(0.325)
    assert sell["unfilled_shares"] == pytest.approx(60.0)
    assert sell["partial"] is True
    assert adapter.query_live_position("yes", "no").yes_shares == pytest.approx(60.0)


def test_paper_broker_rejects_stale_depth(tmp_path) -> None:
    quotes = _Quotes(
        {
            "yes": _book(asks=[(0.40, 100.0)], bids=[(0.35, 100.0)], staleness=10.1),
            "no": _book(asks=[(0.65, 100.0)], bids=[(0.60, 100.0)], staleness=10.1),
        }
    )
    adapter = _adapter(tmp_path, quotes=quotes, max_book_age_seconds=10.0)

    buy = adapter.buy_yes_fak("yes", usd=40.0, max_price=0.50)

    assert adapter.yes_best_ask("yes") is None
    assert buy["filled_shares"] == 0.0
    assert buy["reason"] == "paper_book_stale"
    assert not (tmp_path / "paper_broker.json").exists()


def test_paper_broker_fails_closed_on_corrupt_state(tmp_path) -> None:
    state = tmp_path / "paper_broker.json"
    state.write_text(json.dumps({"balances": "not-an-object"}), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid paper broker state"):
        _adapter(tmp_path)
