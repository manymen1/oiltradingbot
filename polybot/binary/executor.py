from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.config import SETTINGS
from polybot.log import log_event

from polybot.core.execution import Fill, LivePosition, TradingAdapter
from polybot.core.holdings import HoldingsStore
from polybot.core.notifier import Notifier
from polybot.core.portfolio import PortfolioLink
from polybot.core.runtime import ExecutionJournal, ReconciliationError
from polybot.core.storage import StateStore
from polybot.core.types import Article

from .config import BinaryBotConfig
from .decision import BinaryDecision
from .market_verifier import BinaryMarketVerification

TERMINAL_STATES = {"EXITED", "FLIPPED", "FLIP_INCOMPLETE", "EXIT_INCOMPLETE", "STOPPED"}

PROTECTION_ACTIONS = {"TRIM_HELD", "EXIT_HELD"}
ENTRY_ACTIONS = {"ENTER_YES", "ENTER_NO"}


class BinaryExecutor:
    def __init__(
        self,
        config: BinaryBotConfig,
        market: BinaryMarketVerification,
        store: StateStore,
        notifier: Notifier,
        adapter: TradingAdapter,
    ):
        self.config = config
        self.market = market
        self.store = _effective_store(config, store)
        self.notifier = notifier
        self.adapter = adapter
        self.holdings = HoldingsStore(self.store.data_dir, default_held=config.market.held_side.lower() or None)
        self.journal = ExecutionJournal(self.store.data_dir)
        self.portfolio = PortfolioLink.from_config(config.portfolio)
        from polybot.core.book_snapshots import build_book_snapshot_logger

        self.book_snapshots = build_book_snapshot_logger(self.store.data_dir, config.sources.log_book_snapshots)

    def held_side(self) -> str | None:
        return self.holdings.held_location()

    def reconcile_live_holding(self) -> dict[str, Any]:
        """Make the wallet authoritative (mirror of the location executor).

        Adopts exactly one meaningful balance side; ambiguous states (both
        sides funded, resting orders) fail closed instead of guessing which
        side the strategy should defend.
        """
        threshold = self.config.entry.reconcile_min_shares
        position: LivePosition = self.adapter.query_live_position(self.market.yes_token_id, self.market.no_token_id)
        orders = self.adapter.open_orders_for_market(self.market.condition_id)
        if orders:
            raise ReconciliationError(
                "unexpected resting orders exist on the market; cancel or adopt them before autonomous execution"
            )
        yes_held = position.yes_shares > threshold
        no_held = position.no_shares > threshold
        if yes_held and no_held:
            raise ReconciliationError(
                f"both sides funded (yes={position.yes_shares:g}, no={position.no_shares:g}); manual reconciliation required"
            )
        local = self.holdings.held_location()
        wallet_side = "yes" if yes_held else ("no" if no_held else None)
        changed = local != wallet_side
        if changed:
            if wallet_side is None:
                self.holdings.clear_held(source="wallet_reconciliation", previous_local=local)
            else:
                self.holdings.set_held(
                    wallet_side,
                    source="wallet_reconciliation",
                    previous_local=local,
                    yes_shares=position.yes_shares,
                    no_shares=position.no_shares,
                )
        return {
            "held_side": wallet_side,
            "yes_shares": position.yes_shares,
            "no_shares": position.no_shares,
            "changed": changed,
        }

    def _portfolio_allowed(self, requested: float) -> tuple[float, list[str]]:
        """Clamp by the shared cross-market ledger (discovery pipeline
        allocator). No-op for standalone configs without a ledger."""
        if self.portfolio is None:
            return requested, []
        granted, blockers = self.portfolio.allowed(requested)
        return min(requested, granted), blockers

    def _portfolio_reserve(self, usd: float) -> None:
        if self.portfolio is not None and usd > 0:
            self.portfolio.reserve(usd)

    def _portfolio_release(self) -> None:
        if self.portfolio is not None:
            self.portfolio.release()

    def _portfolio_settle(self, proceeds_usd: float | None) -> None:
        if self.portfolio is not None:
            self.portfolio.settle(proceeds_usd)

    def _portfolio_reconcile_entry_basis(self, reserved_usd: float, actual_cost_usd: float | None) -> None:
        if self.portfolio is not None and actual_cost_usd is not None:
            self.portfolio.reconcile_entry_basis(reserved_usd, actual_cost_usd)

    def _portfolio_reduce_basis(self, proceeds_usd: float) -> None:
        if self.portfolio is not None and proceeds_usd > 0:
            self.portfolio.reduce_basis(proceeds_usd)

    def entry_count(self) -> int:
        path = self._entry_count_path()
        if not path.exists():
            return 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return 0
        try:
            return int(raw.get("count", 0)) if isinstance(raw, dict) else 0
        except (TypeError, ValueError):
            return 0

    def _record_entry_execution(self) -> int:
        count = self.entry_count() + 1
        path = self._entry_count_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"count": count, "updated_at": datetime.now(timezone.utc).isoformat()}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return count

    def _entry_count_path(self) -> Path:
        return self.store.data_dir / "entry_count.json"

    def execute(self, decision: BinaryDecision, article: Article) -> str:
        try:
            return self._execute(decision, article)
        except Exception as exc:
            current = self.store.current()
            self.store.write(
                "EXECUTION_ERROR",
                reason=str(exc),
                previous_state=current.state if current else None,
                decision=_decision_dict(decision),
                article=article.__dict__,
            )
            log_event("binary_execution_error", action=decision.action, error=str(exc))
            self.notifier.notify("Binary rule bot execution failed; bot will continue polling", error=str(exc))
            return "EXECUTION_ERROR"

    def _execute(self, decision: BinaryDecision, article: Article) -> str:
        if decision.action in ENTRY_ACTIONS:
            return self._execute_entry(decision, article)
        if decision.action not in PROTECTION_ACTIONS:
            log_event("binary_execution_skip", action=decision.action, reason=decision.reason)
            return "SKIPPED"

        if decision.action == "TRIM_HELD":
            prior = self.store.marker("TRIMMED")
            current = self.store.current()
            if (current and current.state in {"TRIMMED", "EXITED", "FLIPPED"}) or prior is not None:
                self.notifier.notify("Held trim already recorded; no duplicate trim", state=(current.state if current else prior.state))
                return current.state if current else prior.state

        current_for_terminal = self.store.current()
        if current_for_terminal is not None and current_for_terminal.state in TERMINAL_STATES and self.config.safety.one_shot:
            self.notifier.notify("Terminal state exists; no trade", state=current_for_terminal.state)
            return current_for_terminal.state

        held = self.held_side()
        if held is None:
            log_event("binary_execution_skip", action=decision.action, reason="no_held_position")
            return "SKIPPED"

        if not self.config.execution.sell.enabled:
            self.store.write("STOPPED", reason="sell_disabled", article=article.__dict__)
            self.notifier.notify("Sell disabled; no trade")
            return "STOPPED"

        floor_state = self._time_decay_floor_block(decision, held)
        if floor_state is not None:
            return floor_state

        held_token = self.market.yes_token_id if held == "yes" else self.market.no_token_id
        journal = self.journal.start(decision.action, decision=_decision_dict(decision), article=article.__dict__)
        self.store.write("TRIGGER_DETECTED", execution_id=journal.execution_id, decision=_decision_dict(decision), article=article.__dict__)
        self.book_snapshots.snapshot([held_token], moment="pre_order", execution_id=journal.execution_id, action=decision.action)
        cancel_result = None
        if self.config.safety.cancel_open_orders_first:
            self.store.write("CANCELING_ORDERS", side=held)
            cancel_result = self.adapter.cancel_open_orders_for_market(self.market.condition_id)

        position = self.adapter.query_live_position(self.market.yes_token_id, self.market.no_token_id)
        held_shares = position.yes_shares if held == "yes" else position.no_shares
        if held_shares <= 0:
            self.store.write("HELD_POSITION_UNCONFIRMED", reason="no_live_balance", side=held, cancel_result=cancel_result, article=article.__dict__)
            self.notifier.notify("No live balance found on held side; no trade attempted", side=held)
            return "HELD_POSITION_UNCONFIRMED"
        if self.config.safety.token_mapping_must_match and (
            position.yes_token_id != self.market.yes_token_id or position.no_token_id != self.market.no_token_id
        ):
            self.store.write("STOPPED", reason="token_mapping_mismatch")
            self.notifier.notify("Token mapping mismatch; no trade")
            return "STOPPED"

        target_shares = min(held_shares, self.config.position.max_shares_to_sell)
        if decision.action == "TRIM_HELD":
            fraction = self.config.time_decay.trim_fraction or self.config.execution.sell.trim_fraction
            target_shares = max(0.0, min(target_shares, held_shares * fraction))
        if target_shares <= 0:
            self.store.write("STOPPED", reason="zero_sell_size", article=article.__dict__)
            self.notifier.notify("Sell size is zero; no trade")
            return "STOPPED"

        pre_trade_yes_bid = self.adapter.yes_best_bid(self.market.yes_token_id)
        pre_trade_yes_ask = self.adapter.yes_best_ask(self.market.yes_token_id)
        self.store.write(
            "SELLING_HELD",
            side=held,
            shares_to_sell=target_shares,
            live_yes_shares=position.yes_shares,
            live_no_shares=position.no_shares,
            pre_trade_yes_best_bid=pre_trade_yes_bid,
            pre_trade_yes_best_ask=pre_trade_yes_ask,
            sell_min_price=self.config.execution.sell.min_price,
            cancel_result=cancel_result,
        )
        # Staged exit (see location executor): fraction first, remainder
        # after a requote delay.
        stage_fraction = min(1.0, max(0.1, self.config.execution.sell.max_fraction_per_order))
        first_order_shares = target_shares if stage_fraction >= 1.0 else max(0.0, target_shares * stage_fraction)
        sell_result = self._sell_side(held, first_order_shares)
        sell_fill = self.adapter.verify_fill(sell_result, held_token)
        total_sold = sell_fill.filled_shares

        if total_sold < target_shares and (self.config.execution.sell.retry_partial_once or stage_fraction < 1.0):
            self.store.write(
                "HELD_PARTIAL",
                side=held,
                filled_shares=total_sold,
                remaining=target_shares - total_sold,
                target_shares=target_shares,
            )
            time.sleep(self.config.execution.sell.retry_delay_seconds)
            retry_result = self._sell_side(held, target_shares - total_sold)
            total_sold += self.adapter.verify_fill(retry_result, held_token).filled_shares

        self.book_snapshots.snapshot(
            [held_token], moment="post_execution", execution_id=journal.execution_id, action=decision.action, total_sold=total_sold
        )
        if total_sold < target_shares:
            # Some shares remain live, so the holding is NOT cleared.
            self.store.write(
                "EXIT_INCOMPLETE",
                reason="held_partially_sold",
                side=held,
                total_sold=total_sold,
                target_shares=target_shares,
                article=article.__dict__,
            )
            self.notifier.notify("Held side partially sold. Manual intervention required.", side=held, total_sold=total_sold)
            self.journal.update(journal, "failed", result="EXIT_INCOMPLETE", total_sold=total_sold)
            return "EXIT_INCOMPLETE"

        sale_price = self._estimate_sale_price(sell_fill, held, pre_trade_yes_bid, pre_trade_yes_ask)
        confirmed_proceeds = total_sold * sale_price

        if decision.action == "TRIM_HELD":
            self.store.write(
                "TRIMMED",
                side=held,
                total_sold=total_sold,
                target_shares=target_shares,
                sale_price_used=sale_price,
                confirmed_proceeds=confirmed_proceeds,
                article=article.__dict__,
            )
            self._portfolio_reduce_basis(confirmed_proceeds)
            self.notifier.notify("Trimmed held exposure", side=held, total_sold=total_sold)
            self.journal.update(journal, "completed", result="TRIMMED", total_sold=total_sold)
            return "TRIMMED"

        # News-triggered exits may optionally flip into the opposite side;
        # time-decay exits (level "TIME") never do.
        if self.config.execution.flip_buy.enabled and decision.level != "TIME":
            return self._flip_buy_leg(decision, article, held, total_sold, sale_price, confirmed_proceeds)

        self.store.write(
            "EXITED",
            side=held,
            total_sold=total_sold,
            target_shares=target_shares,
            sale_price_used=sale_price,
            confirmed_proceeds=confirmed_proceeds,
            article=article.__dict__,
        )
        self.holdings.clear_held(source="exit", from_side=held, total_sold=total_sold)
        self._portfolio_settle(confirmed_proceeds)
        self._portfolio_release()
        self.notifier.notify("Exited held exposure (sell-only)", side=held, total_sold=total_sold)
        self.journal.update(journal, "completed", result="EXITED", total_sold=total_sold, confirmed_proceeds=confirmed_proceeds)
        return "EXITED"

    def _execute_entry(self, decision: BinaryDecision, article: Article) -> str:
        entry = self.config.entry
        side = "yes" if decision.action == "ENTER_YES" else "no"
        if not self.config.execution.dry_run:
            self.reconcile_live_holding()
        held = self.held_side()
        if held is not None:
            log_event("binary_entry_skip", reason="already_holding", held=held, side=side)
            return "SKIPPED"

        current = self.store.current()
        if current is not None and current.state in TERMINAL_STATES and self.config.safety.one_shot:
            self.notifier.notify("Terminal state exists; no entry", state=current.state)
            return current.state

        if not entry.enabled:
            log_event("binary_entry_skip", reason="entry_disabled", side=side)
            return "SKIPPED"
        if side != entry.side.lower():
            log_event("binary_entry_skip", reason="entry_side_mismatch", side=side, configured_side=entry.side)
            return "SKIPPED"
        entry_count = self.entry_count()
        if entry_count >= entry.max_entries:
            log_event("binary_entry_skip", reason="max_entries_reached", entry_count=entry_count, max_entries=entry.max_entries)
            return "SKIPPED"

        cap = min(entry.max_price, SETTINGS.guardrails.max_entry_price)
        usd_budget = entry.usd_budget
        if usd_budget < 1.0:
            log_event("binary_entry_skip", reason="entry_budget_below_minimum", usd_budget=usd_budget)
            return "SKIPPED"
        usd_budget, portfolio_blockers = self._portfolio_allowed(usd_budget)
        if portfolio_blockers or usd_budget < 1.0:
            reason = ",".join(portfolio_blockers) or "portfolio_allowance_exhausted"
            self.store.write("ENTRY_PORTFOLIO_BLOCKED", reason=reason, side=side, usd_budget=usd_budget)
            log_event("binary_entry_skip", reason=f"portfolio:{reason}", usd_budget=usd_budget)
            return "ENTRY_PORTFOLIO_BLOCKED"

        # Large entries need a SECOND independent domain inside the freshness
        # window before any capital moves: one wrong wire story costs an
        # alert, not the stake.
        if entry.second_source_above_usd > 0 and usd_budget >= entry.second_source_above_usd:
            from polybot.core.confirmations import SecondSourceGate

            gate = SecondSourceGate(self.store.data_dir, entry.second_source_window_minutes)
            if not gate.confirm(
                side,
                article.domain,
                origin_organization=article.origin_organization,
                byline=article.byline,
            ):
                self.store.write(
                    "ENTRY_AWAITING_SECOND_SOURCE",
                    side=side,
                    first_domain=article.domain,
                    usd_budget=usd_budget,
                    window_minutes=entry.second_source_window_minutes,
                    article=article.__dict__,
                )
                self.notifier.notify(
                    "Large entry deferred; awaiting second independent source",
                    side=side,
                    first_domain=article.domain,
                    usd_budget=usd_budget,
                    window_minutes=entry.second_source_window_minutes,
                )
                log_event("binary_entry_awaiting_second_source", side=side, domain=article.domain, usd_budget=usd_budget)
                return "ENTRY_AWAITING_SECOND_SOURCE"

        token = self.market.yes_token_id if side == "yes" else self.market.no_token_id
        journal = self.journal.start(decision.action, decision=_decision_dict(decision), article=article.__dict__)
        self.store.write("TRIGGER_DETECTED", execution_id=journal.execution_id, decision=_decision_dict(decision), article=article.__dict__)
        self.book_snapshots.snapshot([token], moment="pre_order", execution_id=journal.execution_id, action=decision.action)
        cancel_result = None
        if self.config.safety.cancel_open_orders_first:
            self.store.write("CANCELING_ORDERS", side=side)
            cancel_result = self.adapter.cancel_open_orders_for_market(self.market.condition_id)

        ask = self.adapter.yes_best_ask(token) if side == "yes" else self.adapter.no_best_ask(token)
        if ask is None or ask > cap:
            previous = current.state if current is not None else None
            self.store.write(
                "ENTRY_PRICE_ABOVE_CAP",
                side=side,
                best_ask=ask,
                entry_max_price=cap,
                configured_entry_max_price=entry.max_price,
                usd_budget=usd_budget,
                cancel_result=cancel_result,
                article=article.__dict__,
            )
            if previous != "ENTRY_PRICE_ABOVE_CAP":
                self.notifier.notify("Entry skipped; ask above price cap or unavailable", side=side, best_ask=ask, cap=cap)
            self.journal.update(journal, "blocked", reason="entry_price_above_cap", best_ask=ask)
            return "ENTRY_PRICE_ABOVE_CAP"

        self.store.write(
            "BUYING_ENTRY",
            side=side,
            usd_budget=usd_budget,
            entry_max_price=cap,
            best_ask=ask,
            cancel_result=cancel_result,
        )
        self._portfolio_reserve(usd_budget)
        buy_result = self.adapter.buy_yes_fak(token, usd_budget, cap) if side == "yes" else self.adapter.buy_no_fak(token, usd_budget, cap)
        buy_fill = self.adapter.verify_fill(buy_result, token)
        self.book_snapshots.snapshot(
            [token], moment="post_execution", execution_id=journal.execution_id, action=decision.action, filled_shares=buy_fill.filled_shares
        )
        if buy_fill.filled_shares <= 0:
            self._portfolio_settle(None)
            self._portfolio_release()
            self.store.write(
                "ENTRY_UNFILLED",
                side=side,
                best_ask=ask,
                entry_max_price=cap,
                usd_budget=usd_budget,
                article=article.__dict__,
            )
            self.notifier.notify("Entry buy did not fill; still flat", side=side, usd_budget=usd_budget)
            self.journal.update(journal, "unfilled", side=side)
            return "ENTRY_UNFILLED"

        actual_cost_usd = _paper_fill_cost(buy_result)
        self._portfolio_reconcile_entry_basis(usd_budget, actual_cost_usd)
        estimated_fill_usd = actual_cost_usd if actual_cost_usd is not None else buy_fill.filled_shares * ask
        fill_fraction = min(1.0, estimated_fill_usd / usd_budget) if usd_budget > 0 else 0.0
        partial = estimated_fill_usd < entry.min_fill_usd or fill_fraction < entry.min_fill_fraction
        resulting_state = "PARTIALLY_ENTERED" if partial else "ENTERED"
        total_entries = self._record_entry_execution()
        self.store.write(
            resulting_state,
            side=side,
            filled_shares=buy_fill.filled_shares,
            usd_budget=usd_budget,
            estimated_fill_usd=estimated_fill_usd,
            fill_fraction=fill_fraction,
            entry_max_price=cap,
            best_ask=ask,
            entry_count=total_entries,
            article=article.__dict__,
        )
        self.holdings.set_held(
            side,
            source="entry",
            filled_shares=buy_fill.filled_shares,
            article_url=article.url,
            reason=decision.reason,
        )
        self.notifier.notify(
            f"{'Partially entered' if partial else 'Entered'} {side.upper()}; now defending it",
            filled_shares=buy_fill.filled_shares,
            usd_budget=usd_budget,
            best_ask=ask,
        )
        self.journal.update(
            journal,
            "completed",
            result=resulting_state,
            held_side=side,
            filled_shares=buy_fill.filled_shares,
            estimated_fill_usd=round(estimated_fill_usd, 4),
            fill_fraction=fill_fraction,
            usd_budget=usd_budget,
        )
        return resulting_state

    def _flip_buy_leg(
        self,
        decision: BinaryDecision,
        article: Article,
        held: str,
        total_sold: float,
        sale_price: float,
        confirmed_proceeds: float,
    ) -> str:
        opposite = "no" if held == "yes" else "yes"
        opposite_token = self.market.no_token_id if opposite == "no" else self.market.yes_token_id
        cap = self.config.execution.flip_buy.max_price
        ask = self.adapter.no_best_ask(opposite_token) if opposite == "no" else self.adapter.yes_best_ask(opposite_token)
        if ask is None or ask > cap:
            self.store.write(
                "EXITED",
                side=held,
                total_sold=total_sold,
                reason="flip_target_above_cap_or_unavailable",
                flip_side=opposite,
                flip_best_ask=ask,
                flip_max_price=cap,
                sale_price_used=sale_price,
                confirmed_proceeds=confirmed_proceeds,
                article=article.__dict__,
            )
            self.holdings.clear_held(source="exit", from_side=held, total_sold=total_sold, reason="flip_target_above_cap_or_unavailable")
            self._portfolio_settle(confirmed_proceeds)
            self._portfolio_release()
            self.notifier.notify("Sold held side. Flip buy skipped (price above cap or unavailable).", flip_side=opposite, flip_best_ask=ask, cap=cap)
            return "EXITED"

        # Same rule as the location rotation buy: the flip leg is capped by
        # actual confirmed sale proceeds, never blindly by the configured
        # budget.
        usd_budget = min(
            self.config.execution.flip_buy.usd_budget,
            self.config.position.max_flip_usd_to_buy,
            confirmed_proceeds,
        )
        usd_budget, portfolio_blockers = self._portfolio_allowed(usd_budget)
        if portfolio_blockers:
            self.store.write(
                "EXITED",
                side=held,
                total_sold=total_sold,
                reason=f"flip_portfolio_blocked:{','.join(portfolio_blockers)}",
                flip_side=opposite,
                sale_price_used=sale_price,
                confirmed_proceeds=confirmed_proceeds,
                article=article.__dict__,
            )
            self.holdings.clear_held(source="exit", from_side=held, total_sold=total_sold, reason="flip_portfolio_blocked")
            self._portfolio_settle(confirmed_proceeds)
            self._portfolio_release()
            self.notifier.notify("Sold held side. Flip buy skipped (portfolio limits).", flip_side=opposite, blockers=",".join(portfolio_blockers))
            return "EXITED"
        if usd_budget < 1.0:
            self.store.write(
                "EXITED",
                side=held,
                total_sold=total_sold,
                reason="insufficient_sale_proceeds_for_flip_buy",
                flip_side=opposite,
                confirmed_proceeds=confirmed_proceeds,
                sale_price_used=sale_price,
                article=article.__dict__,
            )
            self.holdings.clear_held(source="exit", from_side=held, total_sold=total_sold, reason="insufficient_sale_proceeds_for_flip_buy")
            self._portfolio_settle(confirmed_proceeds)
            self._portfolio_release()
            self.notifier.notify("Sold held side. Flip buy skipped (proceeds too small).", flip_side=opposite, confirmed_proceeds=confirmed_proceeds)
            return "EXITED"

        self.store.write(
            "BUYING_FLIP",
            flip_side=opposite,
            usd_budget=usd_budget,
            cap=cap,
            flip_best_ask=ask,
            confirmed_proceeds=confirmed_proceeds,
            sale_price_used=sale_price,
        )
        self._portfolio_reserve(usd_budget)
        buy_result = self.adapter.buy_no_fak(opposite_token, usd_budget, cap) if opposite == "no" else self.adapter.buy_yes_fak(opposite_token, usd_budget, cap)
        buy_fill = self.adapter.verify_fill(buy_result, opposite_token)
        if buy_fill.filled_shares <= 0:
            # The held side was fully sold before the buy failed: flat.
            self.store.write(
                "FLIP_INCOMPLETE",
                reason="flip_buy_unfilled",
                side=held,
                total_sold=total_sold,
                flip_side=opposite,
                flip_best_ask=ask,
                usd_budget=usd_budget,
                article=article.__dict__,
            )
            self.holdings.clear_held(source="flip_incomplete", from_side=held, total_sold=total_sold, intended_side=opposite)
            self._portfolio_settle(confirmed_proceeds)
            self._portfolio_release()
            self.notifier.notify("Sold held side but flip buy did not fill. Manual intervention required.", total_sold=total_sold, flip_side=opposite)
            return "FLIP_INCOMPLETE"

        self._portfolio_settle(confirmed_proceeds)
        self.store.write(
            "FLIPPED",
            from_side=held,
            to_side=opposite,
            total_sold=total_sold,
            sale_price_used=sale_price,
            confirmed_proceeds=confirmed_proceeds,
            flip_best_ask=ask,
            flip_max_price=cap,
            flip_usd_budget=usd_budget,
            flip_filled_shares=buy_fill.filled_shares,
            article=article.__dict__,
        )
        self.holdings.set_held(
            opposite,
            source="flip",
            from_side=held,
            filled_shares=buy_fill.filled_shares,
            article_url=article.url,
        )
        self.notifier.notify(
            f"Flipped: sold {held.upper()}, bought {opposite.upper()}",
            total_sold=total_sold,
            flip_filled_shares=buy_fill.filled_shares,
        )
        return "FLIPPED"

    def _sell_side(self, side: str, shares: float) -> Any:
        min_price = self.config.execution.sell.min_price
        if side == "yes":
            return self.adapter.sell_yes_fak(self.market.yes_token_id, shares, min_price)
        return self.adapter.sell_no_fak(self.market.no_token_id, shares, min_price)

    def _time_decay_floor_block(self, decision: BinaryDecision, held: str) -> str | None:
        """Same guard as the iran/location executors: calendar-decay sales
        must not dump the held YES below the configured best-bid floor. Only
        applies to time-decay decisions (level "TIME") on a held YES."""
        if decision.level != "TIME" or held != "yes":
            return None
        floor = (
            self.config.time_decay.min_trim_price
            if decision.action == "TRIM_HELD"
            else self.config.time_decay.min_exit_price
        )
        if floor <= 0:
            return None
        yes_bid = self.adapter.yes_best_bid(self.market.yes_token_id)
        if yes_bid is not None and yes_bid >= floor:
            return None
        current = self.store.current()
        self.store.write(
            "TIME_DECAY_PRICE_FLOOR",
            reason="yes_bid_below_time_decay_floor",
            action=decision.action,
            yes_best_bid=yes_bid,
            price_floor=floor,
        )
        if current is None or current.state != "TIME_DECAY_PRICE_FLOOR":
            self.notifier.notify(
                "Time-decay sale skipped; best bid below floor",
                action=decision.action,
                yes_best_bid=yes_bid,
                price_floor=floor,
            )
        return "TIME_DECAY_PRICE_FLOOR"

    def _estimate_sale_price(self, sell_fill: Fill, held: str, yes_bid: float | None, yes_ask: float | None) -> float:
        """Best available estimate of per-share proceeds from the held-side
        sell, used to cap the flip buy. Tries the fill's raw order response,
        then the relevant pre-trade quote (for NO, derived as 1 - yes_ask),
        and only falls back to the configured min_price floor."""
        raw = sell_fill.raw
        if isinstance(raw, dict):
            for key in ("net_execution_price", "execution_price", "avg_price", "average_price", "price", "avgPrice"):
                value = raw.get(key)
                if value is None:
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        if held == "yes" and yes_bid is not None and yes_bid > 0:
            return yes_bid
        if held == "no" and yes_ask is not None and 0 < yes_ask < 1:
            return 1.0 - yes_ask
        return self.config.execution.sell.min_price


def _effective_store(config: BinaryBotConfig, store: StateStore) -> StateStore:
    if config.execution.dry_run and store.data_dir.name != "dry_run":
        return StateStore(store.data_dir / "dry_run")
    return store


def _paper_fill_cost(result: Any) -> float | None:
    if not isinstance(result, dict) or result.get("paper") is not True:
        return None
    try:
        value = float(result.get("total_cost_usd"))
    except (TypeError, ValueError):
        return None
    return max(0.0, value)


def _decision_dict(decision: BinaryDecision) -> dict[str, Any]:
    return {
        "action": decision.action,
        "level": decision.level,
        "reason": decision.reason,
        "factors": decision.factors.__dict__ if decision.factors else None,
    }
