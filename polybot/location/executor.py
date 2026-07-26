from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polybot.config import SETTINGS
from polybot.log import log_event
from polybot.risk import RiskState

from polybot.core.execution import DryRunTradingAdapter, Fill, LivePosition, LiveClobTradingAdapter, TradingAdapter  # noqa: F401
from polybot.core.notifier import Notifier
from polybot.core.portfolio import PortfolioLink
from polybot.core.storage import StateStore
from polybot.core.types import Article

from .config import LocationBotConfig, OutcomeMarket
from .decision import LocationDecision
from .holdings import HoldingsStore
from .runtime import ExecutionJournal, JournalRecord, ReconciliationError

# Local terminal-state set. Location has "ROTATED", which is not part of the
# shared StateStore contract and should stay strategy-specific.
# ROTATED is deliberately non-terminal: the newly acquired leg must remain
# protected and may later be rotated again or exited.
TERMINAL_STATES = {"EXITED", "FLIP_INCOMPLETE", "STOPPED"}

PROTECTION_ACTIONS = {"TRIM_YES", "EXIT_YES_ONLY", "ROTATE_YES"}


class LocationExecutor:
    def __init__(
        self,
        config: LocationBotConfig,
        store: StateStore,
        notifier: Notifier,
        adapter: TradingAdapter,
        risk: RiskState | None = None,
    ):
        self.config = config
        self.store = _effective_store(config, store)
        self.notifier = notifier
        self.adapter = adapter
        self.holdings = HoldingsStore(self.store.data_dir, default_held=config.event.held_location or None)
        self.journal = ExecutionJournal(self.store.data_dir)
        from polybot.core.book_snapshots import build_book_snapshot_logger

        self.book_snapshots = build_book_snapshot_logger(self.store.data_dir, config.sources.log_book_snapshots)
        risk_path = SETTINGS.risk_state_path if not config.execution.dry_run else self.store.data_dir / "risk_state.json"
        self.risk = risk or RiskState.load(path=risk_path)
        self.portfolio = PortfolioLink.from_config(config.portfolio)

    def held_outcome(self) -> OutcomeMarket | None:
        name = self.holdings.held_location()
        if name is None:
            return None
        return self.config.outcome(name)

    def entry_count(self) -> int:
        path = self._entry_count_path()
        if not path.exists():
            return 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt entry counter {path}: {exc}") from exc
        try:
            return int(raw.get("count", 0)) if isinstance(raw, dict) else 0
        except (TypeError, ValueError):
            raise ValueError(f"invalid entry counter {path}")

    def _record_entry_execution(self) -> int:
        count = self.entry_count() + 1
        path = self._entry_count_path()
        from .holdings import _atomic_json_write

        _atomic_json_write(path, {"count": count, "updated_at": datetime.now(timezone.utc).isoformat()})
        return count

    def _entry_count_path(self) -> Path:
        return self.store.data_dir / "entry_count.json"

    def protection_execution_count(self) -> int:
        path = self.store.data_dir / "protection_execution_count.json"
        if not path.exists():
            # Backward-compatible migration from state markers written before
            # the durable per-execution ledger existed.
            states = ("TRIMMED", "EXITED", "ROTATED", "FLIP_INCOMPLETE")
            current = self.store.current()
            return sum(
                1
                for state in states
                if self.store.marker(state) is not None or (current is not None and current.state == state)
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt protection execution counter {path}: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("execution_ids"), list):
            raise ValueError(f"invalid protection execution counter {path}")
        return len({str(item) for item in raw["execution_ids"] if str(item)})

    def _record_protection_execution(self, execution_id: str) -> int:
        from .holdings import _atomic_json_write

        path = self.store.data_dir / "protection_execution_count.json"
        execution_ids: list[str] = []
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("execution_ids"), list):
                raise ValueError(f"invalid protection execution counter {path}")
            execution_ids = [str(item) for item in raw["execution_ids"] if str(item)]
        if execution_id not in execution_ids:
            execution_ids.append(execution_id)
        _atomic_json_write(
            path,
            {
                "count": len(set(execution_ids)),
                "execution_ids": execution_ids,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return len(set(execution_ids))

    def reconcile_live_holding(self) -> dict[str, Any]:
        """Make the wallet authoritative for the single-outcome strategy.

        A local file can disappear or a process can die after an order fills.
        Exactly one meaningful YES balance is adopted. Multiple outcome
        balances are ambiguous and fail closed instead of guessing which leg
        the strategy should defend.
        """
        found: list[tuple[OutcomeMarket, LivePosition]] = []
        balances: dict[str, float] = {}
        no_balances: dict[str, float] = {}
        open_orders: dict[str, Any] = {}
        threshold = self.config.entry.reconcile_min_shares
        for outcome in self.config.outcomes:
            position = self.adapter.query_live_position(outcome.yes_token_id, outcome.no_token_id)
            balances[outcome.name] = position.yes_shares
            no_balances[outcome.name] = position.no_shares
            orders = self.adapter.open_orders_for_market(outcome.condition_id)
            if orders:
                open_orders[outcome.name] = orders
            if position.yes_shares > threshold:
                found.append((outcome, position))
        if open_orders:
            raise ReconciliationError(
                "unexpected resting orders exist on location markets; cancel or adopt them before autonomous execution: "
                + ", ".join(sorted(open_orders))
            )
        unexpected_no = {name: shares for name, shares in no_balances.items() if shares > threshold}
        if unexpected_no:
            details = ", ".join(f"{name}={shares:g}" for name, shares in sorted(unexpected_no.items()))
            raise ReconciliationError(f"unexpected location NO balances require manual reconciliation: {details}")
        if len(found) > 1:
            names = ", ".join(f"{outcome.name}={position.yes_shares:g}" for outcome, position in found)
            raise ReconciliationError(f"multiple live location YES balances require manual reconciliation: {names}")
        local = self.holdings.held_location()
        if not found:
            if local is not None:
                self.holdings.clear_held(source="wallet_reconciliation", previous_local=local, balances=balances)
                self._portfolio_settle(None)
                self._portfolio_release()
            return {"held_location": None, "balances": balances, "no_balances": no_balances, "open_orders": open_orders, "changed": local is not None}
        outcome, position = found[0]
        changed = local != outcome.name
        if changed:
            self.holdings.set_held(
                outcome.name,
                source="wallet_reconciliation",
                previous_local=local,
                yes_shares=position.yes_shares,
                balances=balances,
            )
        return {"held_location": outcome.name, "balances": balances, "no_balances": no_balances, "open_orders": open_orders, "changed": changed}

    def execute(self, decision: LocationDecision, article: Article) -> str:
        try:
            result = self._execute(decision, article)
            if result in {"ENTERED", "PARTIALLY_ENTERED", "TRIMMED", "EXITED", "ROTATED"}:
                self.risk.record_settlement_success(decision.target_outcome)
            return result
        except Exception as exc:
            self.risk.record_settlement_failure(decision.target_outcome, str(exc))
            current = self.store.current()
            self.store.write(
                "EXECUTION_ERROR",
                reason=str(exc),
                previous_state=current.state if current else None,
                decision=_decision_dict(decision),
                article=article.__dict__,
            )
            log_event("location_execution_error", action=decision.action, error=str(exc))
            self.notifier.notify("Location protection execution failed; bot will continue polling", error=str(exc))
            return "EXECUTION_ERROR"

    def _execute(self, decision: LocationDecision, article: Article) -> str:
        if decision.action == "ENTER_YES":
            return self._execute_entry(decision, article)
        if decision.action not in PROTECTION_ACTIONS:
            log_event("location_execution_skip", action=decision.action, reason=decision.reason)
            return "SKIPPED"

        if decision.action == "TRIM_YES":
            prior = self.store.marker("TRIMMED")
            current = self.store.current()
            if (current and current.state in {"TRIMMED", "EXITED", "ROTATED"}) or prior is not None:
                self.notifier.notify("YES trim already recorded; no duplicate trim", state=(current.state if current else prior.state))
                return current.state if current else prior.state

        current_for_terminal = self.store.current()
        if current_for_terminal is not None and current_for_terminal.state in TERMINAL_STATES and self.config.safety.one_shot:
            self.notifier.notify("Terminal state exists; no trade", state=current_for_terminal.state)
            return current_for_terminal.state

        held = self.held_outcome()
        if held is None:
            # Flat with no terminal state: a protection decision has nothing to
            # sell. Runner routing should prevent this; treat it as a skip
            # rather than an error so a race between an exit and an in-flight
            # article stays harmless.
            log_event("location_execution_skip", action=decision.action, reason="no_held_position")
            return "SKIPPED"

        if not self.config.execution.sell.enabled:
            self.store.write("STOPPED", reason="sell_disabled", article=article.__dict__)
            self.notifier.notify("Sell disabled; no trade")
            return "STOPPED"

        floor_state = self._time_decay_floor_block(decision, held)
        if floor_state is not None:
            return floor_state

        journal = self.journal.start(decision.action, decision=_decision_dict(decision), article=article.__dict__)
        self.store.write("TRIGGER_DETECTED", execution_id=journal.execution_id, decision=_decision_dict(decision), article=article.__dict__)
        self.book_snapshots.snapshot([held.yes_token_id], moment="pre_order", execution_id=journal.execution_id, action=decision.action)
        cancel_result = None
        if self.config.safety.cancel_open_orders_first:
            self.store.write("CANCELING_ORDERS", outcome=held.name)
            cancel_result = self.adapter.cancel_open_orders_for_market(held.condition_id)

        position = self.adapter.query_live_position(held.yes_token_id, held.no_token_id)
        if position.yes_shares <= 0:
            self.store.write("YES_POSITION_UNCONFIRMED", reason="no_live_yes_balance", cancel_result=cancel_result, article=article.__dict__)
            self.notifier.notify("No live YES balance found on held outcome; no trade attempted")
            return "YES_POSITION_UNCONFIRMED"
        if self.config.safety.token_mapping_must_match and (
            position.yes_token_id != held.yes_token_id or position.no_token_id != held.no_token_id
        ):
            self.store.write("STOPPED", reason="token_mapping_mismatch", position=asdict(position))
            self.notifier.notify("Token mapping mismatch on held outcome; no trade")
            return "STOPPED"

        target_shares = min(position.yes_shares, self.config.position.max_yes_shares_to_sell)
        if decision.action == "TRIM_YES":
            fraction = self.config.time_decay.trim_fraction or self.config.execution.sell.trim_fraction
            target_shares = max(0.0, min(target_shares, position.yes_shares * fraction))
        if target_shares <= 0:
            self.store.write("STOPPED", reason="zero_sell_size", article=article.__dict__)
            self.notifier.notify("Sell size is zero; no trade")
            return "STOPPED"

        pre_trade_bid = self.adapter.yes_best_bid(held.yes_token_id)
        pre_trade_ask = self.adapter.yes_best_ask(held.yes_token_id)
        self.store.write(
            "SELLING_YES",
            outcome=held.name,
            shares_to_sell=target_shares,
            live_yes_shares=position.yes_shares,
            live_no_shares=position.no_shares,
            pre_trade_yes_best_bid=pre_trade_bid,
            pre_trade_yes_best_ask=pre_trade_ask,
            sell_min_price=self.config.execution.sell.min_price,
            cancel_result=cancel_result,
        )
        # Consume the protection execution allowance before posting. If the
        # process dies after the exchange accepts the order, a restart must not
        # forget that the mutation was attempted.
        self._record_protection_execution(journal.execution_id)
        # Staged exit: sell only a fraction first when configured (< 1.0);
        # the remainder goes out after retry_delay against a fresh book --
        # softer on thin books than one full-size FAK sweep.
        stage_fraction = min(1.0, max(0.1, self.config.execution.sell.max_fraction_per_order))
        first_order_shares = target_shares if stage_fraction >= 1.0 else max(0.0, target_shares * stage_fraction)
        sell_result = self.adapter.sell_yes_fak(held.yes_token_id, first_order_shares, self.config.execution.sell.min_price)
        sell_fill = self.adapter.verify_fill(sell_result, held.yes_token_id)
        journal = self.journal.update(journal, "sell_reconciled", sell_result=sell_result, filled_shares=sell_fill.filled_shares)
        total_sold = sell_fill.filled_shares

        if total_sold < target_shares and (self.config.execution.sell.retry_partial_once or stage_fraction < 1.0):
            self.store.write(
                "YES_PARTIAL",
                outcome=held.name,
                filled_shares=total_sold,
                remaining=target_shares - total_sold,
                target_shares=target_shares,
                pre_trade_yes_best_bid=pre_trade_bid,
                pre_trade_yes_best_ask=pre_trade_ask,
                sell_min_price=self.config.execution.sell.min_price,
            )
            time.sleep(self.config.execution.sell.retry_delay_seconds)
            retry_result = self.adapter.sell_yes_fak(held.yes_token_id, target_shares - total_sold, self.config.execution.sell.min_price)
            total_sold += self.adapter.verify_fill(retry_result, held.yes_token_id).filled_shares

        self.book_snapshots.snapshot(
            [held.yes_token_id], moment="post_execution", execution_id=journal.execution_id, action=decision.action, total_sold=total_sold
        )
        if total_sold < target_shares:
            self.store.write(
                "FLIP_INCOMPLETE",
                reason="yes_partially_sold",
                outcome=held.name,
                total_sold=total_sold,
                target_shares=target_shares,
                pre_trade_yes_best_bid=pre_trade_bid,
                pre_trade_yes_best_ask=pre_trade_ask,
                sell_min_price=self.config.execution.sell.min_price,
                article=article.__dict__,
            )
            self.notifier.notify("YES partially sold on held outcome. Manual intervention required.", total_sold=total_sold)
            self.journal.update(journal, "failed", reason="yes_partially_sold", total_sold=total_sold)
            return "FLIP_INCOMPLETE"

        if decision.action == "TRIM_YES":
            sale_price = self._estimate_sale_price(sell_fill, pre_trade_bid)
            self.store.write(
                "TRIMMED",
                outcome=held.name,
                total_sold=total_sold,
                target_shares=target_shares,
                pre_trade_yes_best_bid=pre_trade_bid,
                pre_trade_yes_best_ask=pre_trade_ask,
                sell_min_price=self.config.execution.sell.min_price,
                sale_price_used=sale_price,
                confirmed_proceeds=total_sold * sale_price,
                article=article.__dict__,
            )
            self.notifier.notify("Trimmed held-outcome YES exposure", outcome=held.label, total_sold=total_sold)
            self._portfolio_reduce_basis(total_sold * sale_price)
            self.journal.update(journal, "completed", result="TRIMMED", total_sold=total_sold)
            return "TRIMMED"

        if decision.action == "EXIT_YES_ONLY" or not self.config.execution.buy_rotation.enabled:
            sale_price = self._estimate_sale_price(sell_fill, pre_trade_bid)
            self.store.write(
                "EXITED",
                outcome=held.name,
                total_sold=total_sold,
                target_shares=target_shares,
                pre_trade_yes_best_bid=pre_trade_bid,
                pre_trade_yes_best_ask=pre_trade_ask,
                sell_min_price=self.config.execution.sell.min_price,
                sale_price_used=sale_price,
                confirmed_proceeds=total_sold * sale_price,
                article=article.__dict__,
            )
            self.holdings.clear_held(source="exit", from_outcome=held.name, total_sold=total_sold)
            self._portfolio_settle(total_sold * sale_price)
            self._portfolio_release()
            self.notifier.notify("Exited held-outcome YES exposure (sell-only)", outcome=held.label, total_sold=total_sold)
            self.journal.update(journal, "completed", result="EXITED", total_sold=total_sold)
            return "EXITED"

        target = self.config.outcome(decision.target_outcome or "")
        if target is None:
            sale_price = self._estimate_sale_price(sell_fill, pre_trade_bid)
            self.store.write(
                "EXITED",
                outcome=held.name,
                total_sold=total_sold,
                target_shares=target_shares,
                reason="rotation_target_missing",
                pre_trade_yes_best_bid=pre_trade_bid,
                pre_trade_yes_best_ask=pre_trade_ask,
                sell_min_price=self.config.execution.sell.min_price,
                sale_price_used=sale_price,
                confirmed_proceeds=total_sold * sale_price,
                article=article.__dict__,
            )
            self.holdings.clear_held(source="exit", from_outcome=held.name, total_sold=total_sold, reason="rotation_target_missing")
            self._portfolio_settle(total_sold * sale_price)
            self._portfolio_release()
            self.notifier.notify("Rotation target missing from config; exited without buying", total_sold=total_sold)
            self.journal.update(journal, "completed", result="EXITED", reason="rotation_target_missing")
            return "EXITED"

        return self._buy_rotation_leg(decision, article, held, target, total_sold, target_shares, sell_fill, pre_trade_bid, pre_trade_ask, journal)

    def _execute_entry(self, decision: LocationDecision, article: Article) -> str:
        entry = self.config.entry
        if not self.config.execution.dry_run:
            self.reconcile_live_holding()
        held = self.held_outcome()
        if held is not None:
            log_event("location_entry_skip", reason="already_holding", held=held.name, target=decision.target_outcome)
            return "SKIPPED"

        current = self.store.current()
        if current is not None and current.state in TERMINAL_STATES and self.config.safety.one_shot:
            self.notifier.notify("Terminal state exists; no entry", state=current.state)
            return current.state

        if not entry.enabled:
            log_event("location_entry_skip", reason="entry_disabled", target=decision.target_outcome)
            return "SKIPPED"
        entry_count = self.entry_count()
        if entry_count >= entry.max_entries:
            log_event("location_entry_skip", reason="max_entries_reached", entry_count=entry_count, max_entries=entry.max_entries)
            return "SKIPPED"
        target = self.config.outcome(decision.target_outcome or "")
        if target is None or target.name not in self.config.entry_target_names():
            log_event("location_entry_skip", reason="entry_target_not_configured", target=decision.target_outcome)
            return "SKIPPED"

        # The global guardrail can only lower the config's price cap, mirroring
        # exec_engine's entry-price policy for the legacy engine.
        cap = min(entry.max_price, SETTINGS.guardrails.max_entry_price)
        usd_budget = self._allowed_buy_notional(target.condition_id, entry.usd_budget)
        min_viable_order_usd = 1.0
        if usd_budget < min_viable_order_usd:
            reason = "risk_halted" if self.risk.halted else "risk_budget_exhausted"
            self.store.write("ENTRY_RISK_BLOCKED", reason=reason, target_outcome=target.name, usd_budget=usd_budget)
            log_event("location_entry_skip", reason=reason, usd_budget=usd_budget)
            return "ENTRY_RISK_BLOCKED"
        usd_budget, portfolio_blockers = self._portfolio_allowed(usd_budget)
        if portfolio_blockers or usd_budget < min_viable_order_usd:
            reason = ",".join(portfolio_blockers) or "portfolio_allowance_exhausted"
            self.store.write("ENTRY_PORTFOLIO_BLOCKED", reason=reason, target_outcome=target.name, usd_budget=usd_budget)
            log_event("location_entry_skip", reason=f"portfolio:{reason}", usd_budget=usd_budget)
            return "ENTRY_PORTFOLIO_BLOCKED"

        # Large entries need a SECOND independent domain inside the freshness
        # window before any capital moves: one wrong wire story costs an
        # alert, not the stake.
        if entry.second_source_above_usd > 0 and usd_budget >= entry.second_source_above_usd:
            from polybot.core.confirmations import SecondSourceGate

            gate = SecondSourceGate(self.store.data_dir, entry.second_source_window_minutes)
            if not gate.confirm(
                target.name,
                article.domain,
                origin_organization=article.origin_organization,
                byline=article.byline,
            ):
                self.store.write(
                    "ENTRY_AWAITING_SECOND_SOURCE",
                    target_outcome=target.name,
                    first_domain=article.domain,
                    usd_budget=usd_budget,
                    window_minutes=entry.second_source_window_minutes,
                    article=article.__dict__,
                )
                self.notifier.notify(
                    "Large entry deferred; awaiting second independent source",
                    target=target.label,
                    first_domain=article.domain,
                    usd_budget=usd_budget,
                    window_minutes=entry.second_source_window_minutes,
                )
                log_event("location_entry_awaiting_second_source", target=target.name, domain=article.domain, usd_budget=usd_budget)
                return "ENTRY_AWAITING_SECOND_SOURCE"

        journal = self.journal.start("ENTER_YES", decision=_decision_dict(decision), article=article.__dict__)
        self.store.write("TRIGGER_DETECTED", execution_id=journal.execution_id, decision=_decision_dict(decision), article=article.__dict__)
        self.book_snapshots.snapshot([target.yes_token_id], moment="pre_order", execution_id=journal.execution_id, action="ENTER_YES")
        cancel_result = None
        if self.config.safety.cancel_open_orders_first:
            self.store.write("CANCELING_ORDERS", outcome=target.name)
            cancel_result = self.adapter.cancel_open_orders_for_market(target.condition_id)

        target_ask = self.adapter.yes_best_ask(target.yes_token_id)
        target_bid = self.adapter.yes_best_bid(target.yes_token_id)
        if target_ask is None or target_ask > cap:
            previous = current.state if current is not None else None
            self.store.write(
                "ENTRY_PRICE_ABOVE_CAP",
                target_outcome=target.name,
                target_best_ask=target_ask,
                entry_max_price=cap,
                configured_entry_max_price=entry.max_price,
                usd_budget=usd_budget,
                cancel_result=cancel_result,
                article=article.__dict__,
            )
            if previous != "ENTRY_PRICE_ABOVE_CAP":
                self.notifier.notify(
                    "Entry skipped; target ask above price cap or unavailable",
                    target=target.label,
                    target_best_ask=target_ask,
                    cap=cap,
                )
            self.journal.update(journal, "blocked", reason="entry_price_above_cap", target_best_ask=target_ask)
            return "ENTRY_PRICE_ABOVE_CAP"
        spread = target_ask - target_bid if target_bid is not None else None
        if spread is None or spread > entry.max_spread:
            self.store.write(
                "ENTRY_SPREAD_TOO_WIDE",
                target_outcome=target.name,
                target_best_bid=target_bid,
                target_best_ask=target_ask,
                spread=spread,
                max_spread=entry.max_spread,
                article=article.__dict__,
            )
            self.journal.update(journal, "blocked", reason="entry_spread_too_wide", spread=spread)
            return "ENTRY_SPREAD_TOO_WIDE"

        fair_probability = entry.confirmed_probability
        execution_edge = fair_probability - target_ask - entry.slippage_buffer - entry.resolution_risk_buffer
        if execution_edge < entry.min_edge:
            self.store.write(
                "ENTRY_NO_EDGE",
                target_outcome=target.name,
                target_best_ask=target_ask,
                fair_probability=fair_probability,
                execution_edge=execution_edge,
                minimum_edge=entry.min_edge,
                article=article.__dict__,
            )
            self.journal.update(journal, "blocked", reason="insufficient_entry_edge", execution_edge=execution_edge)
            return "ENTRY_NO_EDGE"

        self.store.write(
            "BUYING_ENTRY",
            target_outcome=target.name,
            usd_budget=usd_budget,
            entry_max_price=cap,
            target_best_ask=target_ask,
            cancel_result=cancel_result,
        )
        self.risk.reserve_order_attempt(target.condition_id, usd_budget)
        self._portfolio_reserve(usd_budget)
        buy_result = self.adapter.buy_yes_fak(target.yes_token_id, usd_budget, cap)
        buy_fill = self.adapter.verify_fill(buy_result, target.yes_token_id)
        journal = self.journal.update(journal, "buy_reconciled", buy_result=buy_result, filled_shares=buy_fill.filled_shares)
        self.book_snapshots.snapshot(
            [target.yes_token_id], moment="post_execution", execution_id=journal.execution_id, action="ENTER_YES", filled_shares=buy_fill.filled_shares
        )
        if buy_fill.filled_shares <= 0:
            self._portfolio_settle(None)
            self._portfolio_release()
            self.store.write(
                "ENTRY_UNFILLED",
                target_outcome=target.name,
                target_best_ask=target_ask,
                entry_max_price=cap,
                usd_budget=usd_budget,
                article=article.__dict__,
            )
            self.notifier.notify("Entry buy did not fill; still flat", target=target.label, usd_budget=usd_budget)
            self.journal.update(journal, "unfilled", target_outcome=target.name)
            return "ENTRY_UNFILLED"

        actual_cost_usd = _paper_fill_cost(buy_result)
        self._portfolio_reconcile_entry_basis(usd_budget, actual_cost_usd)
        total_entries = self._record_entry_execution()
        estimated_fill_usd = actual_cost_usd if actual_cost_usd is not None else buy_fill.filled_shares * target_ask
        fill_fraction = min(1.0, estimated_fill_usd / usd_budget) if usd_budget > 0 else 0.0
        partial = estimated_fill_usd < entry.min_fill_usd or fill_fraction < entry.min_fill_fraction
        resulting_state = "PARTIALLY_ENTERED" if partial else "ENTERED"
        self.store.write(
            resulting_state,
            target_outcome=target.name,
            filled_shares=buy_fill.filled_shares,
            usd_budget=usd_budget,
            entry_max_price=cap,
            target_best_ask=target_ask,
            entry_count=total_entries,
            estimated_fill_usd=estimated_fill_usd,
            fill_fraction=fill_fraction,
            article=article.__dict__,
        )
        self.holdings.set_held(
            target.name,
            source="entry",
            filled_shares=buy_fill.filled_shares,
            article_url=article.url,
            reason=decision.reason,
        )
        self.notifier.notify(
            f"{'Partially entered' if partial else 'Entered'} {target.label}-YES; now defending it",
            filled_shares=buy_fill.filled_shares,
            usd_budget=usd_budget,
            target_best_ask=target_ask,
        )
        self.journal.update(
            journal,
            "completed",
            result=resulting_state,
            held_location=target.name,
            filled_shares=buy_fill.filled_shares,
            estimated_fill_usd=estimated_fill_usd,
        )
        return resulting_state

    def _time_decay_floor_block(self, decision: LocationDecision, held: OutcomeMarket) -> str | None:
        """Mirror of the iran executor's TIME_DECAY_PRICE_FLOOR guard.

        Calendar-decay sales must not dump the position below the configured
        best-bid floor. Returns the nonterminal state name when blocked,
        None when the sale may proceed. Only applies to time-decay decisions
        (level "TIME"); news-triggered sells are deliberately not floored.
        """
        if decision.level != "TIME":
            return None
        floor = (
            self.config.time_decay.min_trim_price
            if decision.action == "TRIM_YES"
            else self.config.time_decay.min_exit_price
        )
        if floor <= 0:
            return None
        yes_bid = self.adapter.yes_best_bid(held.yes_token_id)
        if yes_bid is not None and yes_bid >= floor:
            return None
        current = self.store.current()
        self.store.write(
            "TIME_DECAY_PRICE_FLOOR",
            reason="yes_bid_below_time_decay_floor",
            action=decision.action,
            outcome=held.name,
            yes_best_bid=yes_bid,
            price_floor=floor,
        )
        if current is None or current.state != "TIME_DECAY_PRICE_FLOOR":
            self.notifier.notify(
                "Time-decay YES sale skipped; best bid below floor",
                action=decision.action,
                outcome=held.label,
                yes_best_bid=yes_bid,
                price_floor=floor,
            )
        return "TIME_DECAY_PRICE_FLOOR"

    def _estimate_sale_price(self, sell_fill: Fill, pre_trade_bid: float | None) -> float:
        """Best available estimate of the actual per-share proceeds from the
        held-outcome sell, used to cap the rotation buy (see _buy_rotation_leg).

        Tries the fill's raw order response first (adapters that surface a
        fill price there give the most accurate number), falls back to the
        best bid observed immediately before the sell was placed, and only
        falls back to the configured min_price floor -- a worst-case, never
        an average-case -- if neither is available.
        """
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
        if pre_trade_bid is not None and pre_trade_bid > 0:
            return pre_trade_bid
        return self.config.execution.sell.min_price

    def _allowed_buy_notional(self, market_key: str, requested: float) -> float:
        if self.risk.halted:
            return 0.0
        return max(
            0.0,
            min(
                requested,
                SETTINGS.guardrails.per_order_notional,
                self.risk.remaining_for_market(market_key),
                self.risk.remaining_for_day(),
            ),
        )

    def _portfolio_allowed(self, requested: float) -> tuple[float, list[str]]:
        """Additional clamp by the shared cross-market ledger (discovery
        pipeline allocator). No-op for standalone configs without a ledger."""
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

    def _buy_rotation_leg(
        self,
        decision: LocationDecision,
        article: Article,
        held: OutcomeMarket,
        target: OutcomeMarket,
        total_sold: float,
        target_shares: float,
        sell_fill: Fill,
        pre_trade_bid: float | None,
        pre_trade_ask: float | None,
        journal: JournalRecord,
    ) -> str:
        cap = self.config.execution.buy_rotation.max_price
        sale_price = self._estimate_sale_price(sell_fill, pre_trade_bid)
        confirmed_proceeds = total_sold * sale_price
        target_ask = self.adapter.yes_best_ask(target.yes_token_id)
        target_bid = self.adapter.yes_best_bid(target.yes_token_id)
        if target_ask is None or target_ask > cap:
            self.store.write(
                "EXITED",
                outcome=held.name,
                total_sold=total_sold,
                reason="rotation_target_above_cap_or_unavailable",
                target_outcome=target.name,
                target_best_ask=target_ask,
                target_max_price=cap,
                target_shares=target_shares,
                pre_trade_yes_best_bid=pre_trade_bid,
                pre_trade_yes_best_ask=pre_trade_ask,
                sell_min_price=self.config.execution.sell.min_price,
                sale_price_used=sale_price,
                confirmed_proceeds=confirmed_proceeds,
                configured_rotation_usd_budget=self.config.execution.buy_rotation.usd_budget,
                max_rotation_usd_to_buy=self.config.position.max_rotation_usd_to_buy,
                article=article.__dict__,
            )
            self.holdings.clear_held(source="exit", from_outcome=held.name, total_sold=total_sold, reason="rotation_target_above_cap_or_unavailable")
            self._portfolio_settle(confirmed_proceeds)
            self._portfolio_release()
            self.notifier.notify(
                "Sold held YES. Rotation buy skipped (target price above cap or unavailable).",
                target=target.label,
                target_best_ask=target_ask,
                cap=cap,
            )
            self.journal.update(journal, "completed", result="EXITED", reason="rotation_target_above_cap_or_unavailable")
            return "EXITED"
        target_spread = target_ask - target_bid if target_bid is not None else None
        if target_spread is None or target_spread > self.config.execution.buy_rotation.max_spread:
            self.store.write(
                "EXITED",
                outcome=held.name,
                total_sold=total_sold,
                reason="rotation_target_spread_too_wide",
                target_outcome=target.name,
                target_best_bid=target_bid,
                target_best_ask=target_ask,
                target_spread=target_spread,
                max_spread=self.config.execution.buy_rotation.max_spread,
                confirmed_proceeds=confirmed_proceeds,
                article=article.__dict__,
            )
            self.holdings.clear_held(
                source="exit",
                from_outcome=held.name,
                total_sold=total_sold,
                reason="rotation_target_spread_too_wide",
            )
            self._portfolio_settle(confirmed_proceeds)
            self._portfolio_release()
            self.journal.update(journal, "completed", result="EXITED", reason="rotation_target_spread_too_wide")
            return "EXITED"

        # Rotation buy is capped by actual confirmed sale proceeds, not blindly
        # by the configured budget -- a partial fill, thin book, or bad price
        # on the sell leg must not let the buy leg overspend relative to what
        # was actually raised (found during 2026-07-06 hardening review).
        requested_budget = min(
            self.config.execution.buy_rotation.usd_budget,
            self.config.position.max_rotation_usd_to_buy,
            confirmed_proceeds,
        )
        usd_budget = self._allowed_buy_notional(target.condition_id, requested_budget)
        usd_budget, portfolio_blockers = self._portfolio_allowed(usd_budget)
        min_viable_order_usd = 1.0
        if usd_budget < min_viable_order_usd:
            block_reason = (
                "insufficient_sale_proceeds_for_rotation_buy"
                if requested_budget < min_viable_order_usd
                else (
                    f"rotation_portfolio_blocked:{','.join(portfolio_blockers)}"
                    if portfolio_blockers
                    else ("risk_halted" if self.risk.halted else "rotation_risk_budget_exhausted")
                )
            )
            self.store.write(
                "EXITED",
                outcome=held.name,
                total_sold=total_sold,
                reason=block_reason,
                target_outcome=target.name,
                target_best_ask=target_ask,
                target_max_price=cap,
                target_shares=target_shares,
                pre_trade_yes_best_bid=pre_trade_bid,
                pre_trade_yes_best_ask=pre_trade_ask,
                sell_min_price=self.config.execution.sell.min_price,
                confirmed_proceeds=confirmed_proceeds,
                sale_price_used=sale_price,
                configured_rotation_usd_budget=self.config.execution.buy_rotation.usd_budget,
                max_rotation_usd_to_buy=self.config.position.max_rotation_usd_to_buy,
                article=article.__dict__,
            )
            self.holdings.clear_held(source="exit", from_outcome=held.name, total_sold=total_sold, reason=block_reason)
            self._portfolio_settle(confirmed_proceeds)
            self._portfolio_release()
            self.notifier.notify(
                "Sold held YES. Rotation buy skipped (confirmed sale proceeds too small to fund a real order).",
                target=target.label,
                confirmed_proceeds=confirmed_proceeds,
            )
            self.journal.update(journal, "completed", result="EXITED", reason="insufficient_sale_proceeds_for_rotation_buy")
            return "EXITED"

        if self.config.safety.cancel_open_orders_first:
            self.adapter.cancel_open_orders_for_market(target.condition_id)
        self.store.write(
            "BUYING_ROTATION",
            target_outcome=target.name,
            usd_budget=usd_budget,
            cap=cap,
            target_best_ask=target_ask,
            confirmed_proceeds=confirmed_proceeds,
            sale_price_used=sale_price,
            configured_rotation_usd_budget=self.config.execution.buy_rotation.usd_budget,
            max_rotation_usd_to_buy=self.config.position.max_rotation_usd_to_buy,
        )
        self.risk.reserve_order_attempt(target.condition_id, usd_budget)
        self._portfolio_reserve(usd_budget)
        buy_result = self.adapter.buy_yes_fak(target.yes_token_id, usd_budget, cap)
        buy_fill = self.adapter.verify_fill(buy_result, target.yes_token_id)
        journal = self.journal.update(journal, "rotation_buy_reconciled", buy_result=buy_result, filled_shares=buy_fill.filled_shares)
        if buy_fill.filled_shares <= 0:
            self.store.write(
                "FLIP_INCOMPLETE",
                reason="rotation_buy_unfilled",
                outcome=held.name,
                total_sold=total_sold,
                target_outcome=target.name,
                target_best_ask=target_ask,
                target_max_price=cap,
                usd_budget=usd_budget,
                confirmed_proceeds=confirmed_proceeds,
                sale_price_used=sale_price,
                article=article.__dict__,
            )
            # The held YES was fully sold before the buy failed, so the live
            # book position is flat even though the state is FLIP_INCOMPLETE.
            self.holdings.clear_held(source="rotation_incomplete", from_outcome=held.name, total_sold=total_sold, intended_target=target.name)
            self._portfolio_settle(confirmed_proceeds)
            self._portfolio_release()
            self.notifier.notify("Sold held YES but rotation buy did not fill. Manual intervention required.", total_sold=total_sold, target=target.label)
            self.journal.update(journal, "failed", result="FLIP_INCOMPLETE", reason="rotation_buy_unfilled")
            return "FLIP_INCOMPLETE"

        self._portfolio_settle(confirmed_proceeds)
        self.store.write(
            "ROTATED",
            from_outcome=held.name,
            to_outcome=target.name,
            total_sold=total_sold,
            target_shares=target_shares,
            pre_trade_yes_best_bid=pre_trade_bid,
            pre_trade_yes_best_ask=pre_trade_ask,
            sell_min_price=self.config.execution.sell.min_price,
            sale_price_used=sale_price,
            confirmed_proceeds=confirmed_proceeds,
            target_best_ask=target_ask,
            target_max_price=cap,
            rotation_usd_budget=usd_budget,
            configured_rotation_usd_budget=self.config.execution.buy_rotation.usd_budget,
            max_rotation_usd_to_buy=self.config.position.max_rotation_usd_to_buy,
            rotation_filled_shares=buy_fill.filled_shares,
            article=article.__dict__,
        )
        self.holdings.set_held(
            target.name,
            source="rotation",
            from_outcome=held.name,
            filled_shares=buy_fill.filled_shares,
            article_url=article.url,
        )
        self.journal.update(journal, "completed", result="ROTATED", held_location=target.name, filled_shares=buy_fill.filled_shares)
        self.notifier.notify(
            f"Rotated: sold {held.label}-YES, bought {target.label}-YES",
            total_sold=total_sold,
            rotation_filled_shares=buy_fill.filled_shares,
        )
        return "ROTATED"


def _effective_store(config: LocationBotConfig, store: StateStore) -> StateStore:
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


def _decision_dict(decision: LocationDecision) -> dict[str, Any]:
    return {
        "action": decision.action,
        "level": decision.level,
        "reason": decision.reason,
        "target_outcome": decision.target_outcome,
        "factors": decision.factors.__dict__ if decision.factors else None,
    }
