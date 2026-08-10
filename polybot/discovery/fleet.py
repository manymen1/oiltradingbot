from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from polybot.core.holdings import _atomic_json_write
from polybot.core.operator import OperatorGate
from polybot.log import log_event

from .config import (
    DiscoveryConfig,
    central_feed_db_path,
    classifier_budget_db_path,
    load_discovery_config,
    rule_store_db_path,
)
from .emit import GEO_DATA_ROOT, emit_bot_config
from .store import DiscoveryStore
from .sources import source_plan_sha256, validate_source_plan_freshness
from .types import TRADEABLE_STATES, MarketContext, market_dir_slug


def fleet_operator_dir() -> Path:
    # Every fleet-managed executor keeps data under GEO_DATA_ROOT/<slug>, so
    # the shared operator dir (data_dir.parent / "operator") is the same for
    # all of them: one global mode file kills or arms the entire fleet.
    return Path(GEO_DATA_ROOT) / "operator"


def set_fleet_mode_command(mode: str) -> int:
    """Master switch for every fleet-managed bot: writes the shared operator
    global mode ('off' halts all execution mid-cycle; 'alert_only' keeps bots
    watching but never trading; 'live' defers to each market's position mode)."""
    if mode not in {"off", "alert_only", "dry_run", "live"}:
        raise SystemExit(f"invalid mode {mode!r}")
    path = fleet_operator_dir() / "global_mode.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json_write(path, {"mode": mode, "updated_at": datetime.now(timezone.utc).isoformat()})
    print(json.dumps({"path": str(path), "mode": mode}, indent=2))
    return 0


def recorded_book_contexts(
    config: DiscoveryConfig,
    all_contexts: list[MarketContext],
    desired_contexts: list[MarketContext],
) -> list[MarketContext]:
    """Choose which markets the shared book service records.

    Monitoring is a scarce-resource decision; recording is not. An order book
    cannot be reconstructed after the fact, so a market excluded from
    recording today is permanently missing from the research record -- and
    ``desired_markets`` excludes everything outside TRADEABLE_STATES, i.e.
    exactly the MONITOR_ONLY and ungraded markets whose gradings we most want
    to test against data later.

    ``desired_contexts`` is always returned in full and first: recording must
    never regress for a market a paper worker depends on, so the cap can only
    trim the extra non-monitored contexts, never displace a monitored one.
    """
    if not config.forward_recorder.record_all_contexts:
        return desired_contexts
    desired_ids = {context.market_id for context in desired_contexts}
    contexts_by_id = {
        context.market_id: context
        for context in all_contexts
    }
    priority_contexts = [
        context
        for market_id in config.rule_compiler.priority_market_ids
        if (
            (context := contexts_by_id.get(market_id)) is not None
            and context.market_id not in desired_ids
            and not context.closed
            and context.state not in {"CLOSED", "REJECTED"}
        )
    ]
    cap = config.forward_recorder.max_recorded_markets
    if cap > 0:
        priority_contexts = priority_contexts[
            : max(0, cap - len(desired_contexts))
        ]
    reserved_ids = {
        *desired_ids,
        *(context.market_id for context in priority_contexts),
    }
    from .scope import market_scope_decision

    extra = []
    for context in all_contexts:
        if context.market_id in reserved_ids:
            continue
        decision = market_scope_decision(context, config.universe)
        if decision.status != "IN_SCOPE":
            continue
        extra.append(context)
    # Volume first: attention is where both stale quotes and crowd reaction
    # live, and it is observable now without any forward data.
    extra.sort(
        key=lambda context: (
            -(context.volume or 0.0),
            -(context.liquidity or 0.0),
            context.market_id,
        )
    )
    if cap > 0:
        extra = extra[
            : max(
                0,
                cap - len(desired_contexts) - len(priority_contexts),
            )
        ]
    return [*desired_contexts, *priority_contexts, *extra]


def _forward_books_disabled_status(
    config: DiscoveryConfig,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    if reason is None:
        reason = (
            "forward_recorder.enabled=false"
            if not config.forward_recorder.enabled
            else "forward_recorder.shared_book_service=false"
            if not config.forward_recorder.shared_book_service
            else "status_unavailable"
        )
    return {
        "enabled": False,
        "paper_only": True,
        "shared": config.forward_recorder.shared_book_service,
        "reason": reason,
    }


def _publish_forward_books_status(
    config: DiscoveryConfig,
    status: dict[str, Any],
) -> None:
    """Publish recorder startup state without claiming a fresh fleet sync."""
    _publish_startup_service_status(config, "forward_books", status)


def _publish_startup_service_status(
    config: DiscoveryConfig,
    key: str,
    status: dict[str, Any],
) -> None:
    """Publish one startup service block without claiming a fleet sync."""
    path = config.data_dir / "fleet_state.json"
    current: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                current = raw
        except (OSError, json.JSONDecodeError):
            current = {}
    _atomic_json_write(path, {**current, key: status})


def _publish_discovery_cycle_status(
    config: DiscoveryConfig,
    *,
    state: str,
    started_at: str,
    completed_at: str | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Publish cycle progress without pretending fleet reconciliation ran."""
    path = config.data_dir / "fleet_state.json"
    current: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                current = raw
        except (OSError, json.JSONDecodeError):
            current = {}
    previous = current.get("discovery_cycle")
    previous = previous if isinstance(previous, dict) else {}
    status = {
        "state": state,
        "started_at": started_at,
        "completed_at": completed_at,
        "last_completed_at": (
            completed_at
            if state == "COMPLETE" and completed_at
            else previous.get("last_completed_at")
        ),
        "error": error[:500],
    }
    _publish_startup_service_status(config, "discovery_cycle", status)
    return status


def _central_feed_disabled_status() -> dict[str, Any]:
    return {
        "enabled": False,
        "reason": "central_feed.enabled=false",
    }


class FleetManager:
    """Supervises one executor subprocess per eligible market.

    Desired set = LIVE_CONFIRMATION_ELIGIBLE markets (liquidity-ranked, capped
    at fleet.max_bots) UNION any market currently holding a position: a bot
    defending a position is never stopped by a grading change. Flat bots whose
    markets left the desired set are terminated.
    """

    def __init__(
        self,
        config: DiscoveryConfig,
        store: DiscoveryStore,
        *,
        live: bool,
        per_order_usd: float,
        ledger_path: str,
        config_path: Path | None = None,
        spawner: Callable[[list[str], Path], Any] | None = None,
        notifier: Any = None,
    ):
        self.config = config
        self.store = store
        self.live = live
        self.per_order_usd = per_order_usd
        self.ledger_path = ledger_path
        self.config_path = config_path
        self.spawner = spawner or _default_spawner
        self.processes: dict[str, Any] = {}
        self.notifier = notifier
        self._restarts: dict[str, list[float]] = {}
        self._drawdown_halted = False

    # -- planning --

    def desired_markets(self, contexts: list[MarketContext]) -> list[MarketContext]:
        # Monitoring priority controls scarce worker slots; it never changes
        # execution eligibility. Cold-start markets rotate through an explicit
        # exploration quota so a self-reinforcing incumbent ranking cannot
        # starve new objective rules.
        from .profit_priority import load_priority_snapshot

        priorities = load_priority_snapshot(self.config.data_dir)
        eligible_states = {"LIVE_CONFIRMATION_ELIGIBLE"} if self.live else TRADEABLE_STATES
        from .scope import market_scope_decision

        candidates = [
            context
            for context in contexts
            if context.state in eligible_states
            and market_scope_decision(
                context,
                self.config.universe,
            ).status
            == "IN_SCOPE"
        ]

        def exploitation_key(context: MarketContext):
            priority = priorities.get(context.market_id)
            return (
                -(
                    priority.monitor_priority
                    if priority is not None
                    else 0.0
                ),
                context.market_id,
            )

        exploitation = sorted(
            (
                context
                for context in candidates
                if (
                    priorities.get(context.market_id) is not None
                    and not priorities[context.market_id].cold_start
                )
            ),
            key=exploitation_key,
        )
        exploration = sorted(
            (
                context
                for context in candidates
                if context.market_id
                not in {item.market_id for item in exploitation}
                and (
                    priorities.get(context.market_id) is None
                    or priorities[context.market_id].exploration_eligible
                )
            ),
            key=lambda context: (
                context.discovered_at or "",
                context.market_id,
            ),
        )
        unsupported = sorted(
            (
                context
                for context in candidates
                if context.market_id
                not in {
                    item.market_id
                    for item in [*exploitation, *exploration]
                }
            ),
            key=exploitation_key,
        )
        if self.config.fleet.max_bots <= 0:
            eligible = [*exploitation, *exploration, *unsupported]
        else:
            cap = self.config.fleet.max_bots
            exploration_slots = min(
                len(exploration),
                int(
                    math.floor(
                        cap
                        * self.config.profit_priority.exploration_fraction
                    )
                ),
            )
            exploitation_slots = max(0, cap - exploration_slots)
            eligible = exploitation[:exploitation_slots]
            eligible.extend(exploration[:exploration_slots])
            remaining = cap - len(eligible)
            if remaining > 0:
                eligible.extend(exploitation[exploitation_slots:][:remaining])
                remaining = cap - len(eligible)
            if remaining > 0:
                eligible.extend(exploration[exploration_slots:][:remaining])
                remaining = cap - len(eligible)
            if remaining > 0:
                eligible.extend(unsupported[:remaining])
        desired = {c.market_id: c for c in eligible}
        for context in contexts:
            if context.market_id not in desired and self.is_holding(context.market_id):
                desired[context.market_id] = context
        return list(desired.values())

    def _last_scan_edges(self) -> dict[str, float]:
        """Max executable (blocker-free) edge per market from the last
        opportunity scan; markets without one rank below edge-bearing ones."""
        return {market_id: edge for market_id, (edge, _side) in self._last_scan_best().items()}

    def best_entry_side(self, market_id: str) -> str:
        """Side of the best executable edge for this market ('YES' when the
        scan has no opinion): an overpriced market gets a NO-entry bot."""
        best = self._last_scan_best().get(market_id)
        return best[1] if best else "YES"

    def _last_scan_best(self) -> dict[str, tuple[float, str]]:
        path = self.config.data_dir / "opportunities.json"
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        best: dict[str, tuple[float, str]] = {}
        items = raw.get("opportunities") if isinstance(raw, dict) else None
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("blockers") or item.get("tradable_edge") is None:
                continue
            market_id = str(item.get("market_id") or "")
            edge = float(item["tradable_edge"])
            side = str(item.get("side") or "YES")
            if market_id and edge > best.get(market_id, (float("-inf"), ""))[0]:
                best[market_id] = (edge, side)
        return best

    def is_holding(self, market_id: str) -> bool:
        if self.config.rule_runner.enabled and not self.live:
            generic = (
                self.config.data_dir
                / "rule_runner"
                / market_dir_slug(market_id)
                / "paper_broker.json"
            )
            if generic.exists():
                try:
                    raw = json.loads(generic.read_text(encoding="utf-8"))
                    balances = (
                        raw.get("balances")
                        if isinstance(raw, dict)
                        else None
                    )
                    if isinstance(balances, dict) and any(
                        float(value) > 0 for value in balances.values()
                    ):
                        return True
                except (
                    OSError,
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
                ):
                    # Corrupt paper state is not evidence that the position is
                    # flat. Keep the worker desired so it can fail closed and
                    # surface the corruption instead of silently freeing its
                    # portfolio slot.
                    return True
        base = Path(GEO_DATA_ROOT) / market_dir_slug(market_id)
        candidate = base / "holdings.json" if self.live else base / "dry_run" / "holdings.json"
        if not candidate.exists():
            return False
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return isinstance(raw, dict) and bool(raw.get("held_location"))

    # -- reconciliation --

    def sync(self, contexts: list[MarketContext]) -> dict[str, Any]:
        self._reconcile_ledger()
        self._drawdown_check()
        self._terminate_hung_bots()
        desired = self.desired_markets(contexts)
        desired_ids = {c.market_id for c in desired}
        summary: dict[str, Any] = {"desired": sorted(desired_ids), "started": [], "stopped": [], "armed": [], "errors": {}}

        # Stop flat bots whose markets left the desired set.
        for market_id in sorted(set(self.processes) - desired_ids):
            process = self.processes.pop(market_id)
            if process.poll() is None:
                process.terminate()
            summary["stopped"].append(market_id)
            log_event("fleet_bot_stopped", market_id=market_id)

        for context in desired:
            try:
                config_path = self._ensure_config(context)
                if self._arm(config_path):
                    summary["armed"].append(context.market_id)
                if self._ensure_running(context, config_path):
                    summary["started"].append(context.market_id)
            except Exception as exc:
                summary["errors"][context.market_id] = str(exc)
                log_event("fleet_market_error", market_id=context.market_id, error=str(exc))
        summary["running"] = sorted(m for m, p in self.processes.items() if p.poll() is None)
        return summary

    def _reconcile_ledger(self) -> None:
        try:
            from polybot.core.portfolio import PortfolioAllocator

            allocator = PortfolioAllocator.from_ledger(Path(self.ledger_path))
            allocator.reconcile(is_open=self.is_holding)
        except (ValueError, OSError) as exc:
            log_event("fleet_ledger_reconcile_skipped", error=str(exc))

    def _drawdown_check(self) -> None:
        """Portfolio kill switch: cumulative realized trading losses beyond
        max_drawdown_usd flip the shared operator mode to alert_only -- every
        bot stops trading mid-cycle while continuing to watch and alert."""
        if self._drawdown_halted:
            return
        try:
            from polybot.core.portfolio import PortfolioAllocator

            allocator = PortfolioAllocator.from_ledger(Path(self.ledger_path))
            realized = allocator.realized_net()
            limit = allocator.config.max_drawdown_usd
        except (ValueError, OSError):
            return
        if limit > 0 and realized <= -limit:
            self._drawdown_halted = True
            path = Path(GEO_DATA_ROOT) / "operator" / "global_mode.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json_write(path, {"mode": "alert_only", "reason": "max_drawdown", "realized_net": realized, "updated_at": datetime.now(timezone.utc).isoformat()})
            log_event("fleet_drawdown_halt", realized_net=realized, limit=limit)
            if self.notifier is not None:
                self.notifier.notify("FLEET HALTED: realized drawdown limit reached", realized_net=realized, limit=limit)

    def _terminate_hung_bots(self) -> None:
        stale_after = self.config.fleet.heartbeat_stale_seconds
        if stale_after <= 0:
            return
        for market_id, process in list(self.processes.items()):
            if process.poll() is not None:
                continue
            age = self._heartbeat_age_seconds(market_id)
            if age is not None and age > stale_after:
                process.terminate()
                log_event("fleet_bot_hung_terminated", market_id=market_id, heartbeat_age_seconds=round(age, 1))
                if self.notifier is not None:
                    self.notifier.notify("Fleet: hung bot terminated for restart", market_id=market_id, heartbeat_age_seconds=round(age, 1))

    def _heartbeat_age_seconds(self, market_id: str) -> float | None:
        base = Path(GEO_DATA_ROOT) / market_dir_slug(market_id)
        candidates = [
            base / "dry_run" / "heartbeat.json",
            base / "heartbeat.json",
        ]
        if self.config.rule_runner.enabled and not self.live:
            candidates.insert(
                0,
                self.config.data_dir
                / "rule_runner"
                / market_dir_slug(market_id)
                / "heartbeat.json",
            )
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
                stamp = datetime.fromisoformat(str(raw.get("at")).replace("Z", "+00:00"))
            except (OSError, json.JSONDecodeError, ValueError, TypeError):
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - stamp).total_seconds()
        return None

    def _restart_allowed(self, market_id: str) -> bool:
        import time as _time

        window_start = _time.monotonic() - 3600.0
        history = [t for t in self._restarts.get(market_id, []) if t >= window_start]
        self._restarts[market_id] = history
        return len(history) < self.config.fleet.max_restarts_per_hour

    def _record_restart(self, market_id: str) -> None:
        import time as _time

        self._restarts.setdefault(market_id, []).append(_time.monotonic())

    def _pre_spawn_check(self, config_path: Path, loaded: Any) -> None:
        """Offline gate check before spawning with --live: a generated config
        whose mode/ack/dry-run state cannot pass the operator gate should be a
        visible fleet error, not a bot crash-loop."""
        if not self.live:
            return
        gate = OperatorGate(config_path, loaded)
        status = gate.status(live_requested=True)
        hard = [b for b in status.blockers if b != "operator_mode_alert_only"]
        if hard:
            raise ValueError(f"pre-spawn gate check failed: {','.join(hard)}")

    def _ensure_config(self, context: MarketContext) -> Path:
        plan = self.store.load_source_plan(context.market_id)
        if plan is None:
            raise ValueError("no source plan; plan-sources stage has not covered this market yet")
        if self.config.rule_compiler.enabled:
            from polybot.rules.store import RuleStore

            spec = RuleStore(rule_store_db_path(self.config)).load_spec(
                context.market_id,
                context.rule_text_sha256,
            )
            if spec is None:
                raise ValueError(
                    "no current RuleSpec; compile-rules has not covered this market"
                )
            validate_source_plan_freshness(context, plan, spec)
        out = Path(self.config.fleet.generated_dir) / f"{market_dir_slug(context.market_id)}.yaml"
        entry_side = self.best_entry_side(context.market_id) if context.kind != "grouped" else "YES"
        # Re-emit only when missing, the pinned rule hash changed, the entry
        # side flipped, or the dry-run mode no longer matches the fleet's:
        # rewriting an unchanged config would needlessly churn the ack hash.
        expected_mode = f"dry_run: {'false' if self.live else 'true'}"
        provider = self.config.classifier.provider if self.config.classifier.provider != "rule_based" else "anthropic"
        if out.exists():
            text = out.read_text(encoding="utf-8")
            # yaml quotes 'NO' (bare NO is a YAML boolean), so match both forms.
            side_ok = context.kind == "grouped" or f"side: {entry_side}" in text or f"side: '{entry_side}'" in text
            provider_ok = f"provider: {provider}" in text
            try:
                existing = self._load_executor_config(out)
                live_classifier_ok = (
                    not self.live
                    or (
                        int(existing.classifier.passes) >= 2
                        and existing.classifier.require_pass_agreement is True
                    )
                )
                expected_central_db = (
                    str(central_feed_db_path(self.config))
                    if self.config.central_feed.enabled
                    else ""
                )
                central_feed_ok = (
                    str(existing.sources.central_feed_db) == expected_central_db
                    and (
                        not expected_central_db
                        or float(existing.sources.central_feed_stale_after_seconds)
                        == float(self.config.central_feed.stale_after_seconds)
                    )
                )
                expected_budget_db = (
                    str(classifier_budget_db_path(self.config))
                    if self.config.classifier_budget.enabled
                    else ""
                )
                classifier_budget_ok = (
                    str(existing.classifier.budget_db_path) == expected_budget_db
                    and (
                        not expected_budget_db
                        or (
                            int(existing.classifier.max_escalations_per_hour)
                            == int(self.config.classifier_budget.max_escalations_per_hour)
                            and int(existing.classifier.max_escalations_per_day)
                            == int(self.config.classifier_budget.max_escalations_per_day)
                            and int(existing.classifier.max_classifier_errors_per_hour)
                            == int(self.config.classifier_budget.max_classifier_errors_per_hour)
                        )
                    )
                )
                source_plan_ok = (
                    str(existing.sources.source_plan_sha256)
                    == source_plan_sha256(plan)
                )
                classifier_transport_ok = (
                    str(existing.classifier.provider) == provider
                    and str(existing.classifier.model)
                    == str(self.config.classifier.model)
                    and str(existing.classifier.cli_binary)
                    == str(self.config.classifier.cli_binary)
                    and int(existing.classifier.cli_timeout_seconds)
                    == int(self.config.classifier.cli_timeout_seconds)
                )
            except (OSError, TypeError, ValueError):
                live_classifier_ok = False
                central_feed_ok = False
                classifier_budget_ok = False
                source_plan_ok = False
                classifier_transport_ok = False
            if (
                context.rule_text_sha256 in text
                and expected_mode in text
                and side_ok
                and provider_ok
                and classifier_transport_ok
                and live_classifier_ok
                and central_feed_ok
                and classifier_budget_ok
                and source_plan_ok
            ):
                return out
        return emit_bot_config(
            context,
            plan,
            entry_usd=self._entry_usd(context),
            out_path=out,
            ledger_path=self.ledger_path,
            dry_run=not self.live,
            entry_side=entry_side,
            classifier_provider=self.config.classifier.provider if self.config.classifier.provider != "rule_based" else "anthropic",
            classifier_model=self.config.classifier.model,
            classifier_cli_binary=self.config.classifier.cli_binary,
            classifier_cli_timeout_seconds=self.config.classifier.cli_timeout_seconds,
            classifier_budget_db=(
                str(classifier_budget_db_path(self.config))
                if self.config.classifier_budget.enabled
                else ""
            ),
            classifier_max_escalations_per_hour=self.config.classifier_budget.max_escalations_per_hour,
            classifier_max_escalations_per_day=self.config.classifier_budget.max_escalations_per_day,
            classifier_max_errors_per_hour=self.config.classifier_budget.max_classifier_errors_per_hour,
            central_feed_db=(
                str(central_feed_db_path(self.config))
                if self.config.central_feed.enabled
                else ""
            ),
            central_feed_stale_after_seconds=self.config.central_feed.stale_after_seconds,
        )

    def _entry_usd(self, context: MarketContext) -> float:
        recommended = context.scores.get("recommended_max_order_usd")
        if recommended:
            return min(self.per_order_usd, float(recommended))
        return self.per_order_usd

    def _arm(self, config_path: Path) -> bool:
        """Write the fleet position mode (and, when configured, the live ack)
        for one managed market's operator gate."""
        loaded = self._load_executor_config(config_path)
        gate = OperatorGate(config_path, loaded)
        gate.set_position_mode(self.config.fleet.position_mode)
        if self.live and self.config.fleet.position_mode == "live" and self.config.fleet.auto_ack:
            gate.write_ack(note="fleet auto-ack: generated config, fleet-level review")
            return True
        return False

    def _ensure_running(self, context: MarketContext, config_path: Path) -> bool:
        process = self.processes.get(context.market_id)
        if process is not None and process.poll() is None:
            return False
        if process is not None:
            log_event("fleet_bot_exited", market_id=context.market_id, returncode=process.poll())
            if not self._restart_allowed(context.market_id):
                raise ValueError("restart limit reached; investigate crash loop before respawning")
            self._record_restart(context.market_id)
        self._pre_spawn_check(config_path, self._load_executor_config(config_path))
        command = self._command(context, config_path)
        log_path = self.config.logs_dir / f"fleet_{market_dir_slug(context.market_id)}.out"
        self.processes[context.market_id] = self.spawner(command, log_path)
        log_event("fleet_bot_started", market_id=context.market_id, command=" ".join(command))
        return True

    def _command(self, context: MarketContext, config_path: Path) -> list[str]:
        if self.config.rule_runner.enabled and not self.live:
            if self.config_path is None:
                raise ValueError(
                    "generic paper runner requires the discovery config path"
                )
            return [
                sys.executable,
                "-m",
                "polybot.geopolitics",
                "run-rule-market",
                "--config",
                str(self.config_path),
                "--market",
                context.market_id,
            ]
        runner = "run-location-protection" if context.kind == "grouped" else "run-binary"
        command = [sys.executable, "-m", "polybot.geopolitics", runner, "--config", str(config_path)]
        if self.live:
            command.append("--live")
        return command

    def _load_executor_config(self, config_path: Path) -> Any:
        text = config_path.read_text(encoding="utf-8")
        if "\nevent:" in text or text.startswith("event:"):
            from polybot.location.config import load_location_config

            return load_location_config(config_path)
        from polybot.binary.config import load_binary_config

        return load_binary_config(config_path)

    def shutdown(self) -> None:
        for market_id, process in self.processes.items():
            if process.poll() is None:
                process.terminate()
                log_event("fleet_bot_stopped", market_id=market_id, reason="fleet_shutdown")
        self.processes.clear()


def run_fleet_command(
    config_path: Path,
    *,
    live: bool = False,
    once: bool = False,
    events_fetch=None,
    quotes=None,
    analyzer=None,
    notifier=None,
    spawner=None,
    markets_fetch=None,
) -> int:
    """One supervisor for the whole geopolitical universe: run the discovery
    cycle, then reconcile the bot fleet against its output, forever.

    Master controls: `set-fleet-mode off` halts all execution mid-cycle via
    the shared operator dir; stopping this process leaves running bots
    unmanaged (they keep their own safety machinery) until it restarts.
    """
    from polybot.core.notifier import TelegramNotifier

    from .runner import _classifier_budget_status, _load, _run_discovery_cycle

    config, store, allocator = _load(config_path)
    if not config.fleet.enabled:
        raise SystemExit("fleet.enabled is false; enable it in the discovery config to run the fleet")
    notifier = notifier or TelegramNotifier()
    manager = FleetManager(
        config,
        store,
        live=live,
        per_order_usd=allocator.config.per_order_usd,
        ledger_path=str(allocator.state_path),
        config_path=config_path,
        spawner=spawner,
        notifier=notifier,
    )
    central_service = None
    if config.central_feed.enabled:
        from polybot.core.central_feed import CentralFeedService, CentralFeedStore

        def active_feed_urls() -> list[str]:
            urls: set[str] = set()
            for context in manager.desired_markets(store.all_contexts()):
                plan = store.load_source_plan(context.market_id)
                if plan is not None:
                    urls.update(plan.feed_urls)
            return sorted(urls)

        def active_direct_urls() -> list[str]:
            if not config.central_feed.direct_sources_enabled:
                return []
            urls: set[str] = set()
            for context in manager.desired_markets(store.all_contexts()):
                plan = store.load_source_plan(context.market_id)
                if plan is not None:
                    urls.update(plan.poll_urls)
            return sorted(urls)

        def active_direct_priorities() -> dict[str, float]:
            from .profit_priority import load_priority_snapshot

            priorities = load_priority_snapshot(config.data_dir)
            by_url: dict[str, float] = {}
            for context in manager.desired_markets(store.all_contexts()):
                plan = store.load_source_plan(context.market_id)
                if plan is None:
                    continue
                record = priorities.get(context.market_id)
                score = (
                    float(record.monitor_priority)
                    if record is not None
                    else 0.0
                )
                for url in plan.poll_urls:
                    by_url[url] = max(by_url.get(url, 0.0), score)
            return by_url

        central_service = CentralFeedService(
            store=CentralFeedStore(central_feed_db_path(config)),
            feed_urls_provider=active_feed_urls,
            impact_feed_urls_provider=(
                lambda: config.central_feed.impact_feed_urls
            ),
            direct_urls_provider=active_direct_urls,
            direct_priorities_provider=active_direct_priorities,
            poll_seconds=config.central_feed.poll_seconds,
            impact_poll_seconds=config.central_feed.impact_poll_seconds,
            max_workers=config.central_feed.max_workers,
            max_entries_per_feed=config.central_feed.max_entries_per_feed,
            direct_poll_seconds=config.central_feed.direct_poll_seconds,
            direct_idle_max_seconds=(
                config.central_feed.direct_idle_max_seconds
            ),
            max_entries_per_direct_source=(
                config.central_feed.max_entries_per_direct_source
            ),
            retention_hours=config.central_feed.retention_hours,
            aggregator_poll_seconds=config.central_feed.aggregator_poll_seconds,
            max_urls_per_domain_per_cycle=config.central_feed.max_urls_per_domain_per_cycle,
        )
    forward_book_service = None
    if (
        config.forward_recorder.enabled
        and config.forward_recorder.shared_book_service
    ):
        from polybot.rules.forward import ForwardBookService

        forward_book_service = ForwardBookService(
            config,
            notifier=notifier,
        )
    try:
        if forward_book_service is None:
            forward_books_status = _forward_books_disabled_status(config)
        else:
            startup_contexts = store.all_contexts()
            startup_desired = manager.desired_markets(startup_contexts)
            startup_recorded = recorded_book_contexts(
                config,
                startup_contexts,
                startup_desired,
            )
            try:
                forward_books_status = forward_book_service.sync(
                    startup_recorded,
                    start_websocket=not once,
                )
            except Exception as exc:
                log_event(
                    "forward_book_service_startup_error",
                    error=str(exc),
                )
                forward_books_status = {
                    **forward_book_service.status(),
                    "enabled": False,
                    "reason": f"startup_error:{exc}",
                }
        log_event(
            "forward_book_service_startup",
            **forward_books_status,
        )
        _publish_forward_books_status(config, forward_books_status)

        if central_service is None:
            central_feed_status = _central_feed_disabled_status()
        else:
            try:
                if once:
                    central_service.poll_once(force=True)
                else:
                    central_service.start()
                central_feed_status = {
                    "enabled": True,
                    **central_service.status(),
                }
            except Exception as exc:
                log_event("central_feed_startup_error", error=str(exc))
                central_feed_status = {
                    "enabled": False,
                    "reason": f"startup_error:{exc}",
                    **central_service.status(),
                }
        log_event("central_feed_service_startup", **central_feed_status)
        _publish_startup_service_status(
            config,
            "central_feed",
            central_feed_status,
        )

        while True:
            cycle_started_at = datetime.now(timezone.utc).isoformat()
            discovery_cycle_status = _publish_discovery_cycle_status(
                config,
                state="RUNNING",
                started_at=cycle_started_at,
            )
            try:
                _run_discovery_cycle(config_path, config, events_fetch=events_fetch, quotes=quotes, analyzer=analyzer, notifier=notifier, markets_fetch=markets_fetch)
            except Exception as exc:
                log_event("fleet_discovery_cycle_error", error=str(exc))
                discovery_cycle_status = _publish_discovery_cycle_status(
                    config,
                    state="FAILED",
                    started_at=cycle_started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    error=str(exc),
                )
            else:
                discovery_cycle_status = _publish_discovery_cycle_status(
                    config,
                    state="COMPLETE",
                    started_at=cycle_started_at,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
            cycle_contexts = store.all_contexts()
            desired_contexts = manager.desired_markets(cycle_contexts)
            if forward_book_service is not None:
                recorded_contexts = recorded_book_contexts(
                    config,
                    cycle_contexts,
                    desired_contexts,
                )
                try:
                    if once:
                        forward_book_service.poll_once(recorded_contexts)
                    else:
                        forward_book_service.sync(recorded_contexts)
                except Exception as exc:
                    log_event(
                        "forward_book_service_cycle_error",
                        error=str(exc),
                    )
            if central_service is not None:
                if once:
                    try:
                        central_service.poll_once(force=True)
                    except Exception as exc:
                        log_event("central_feed_cycle_error", error=str(exc))
                else:
                    central_service.start()
            try:
                summary = manager.sync(store.all_contexts())
                if central_service is not None:
                    summary["central_feed"] = {
                        "enabled": True,
                        **central_service.status(),
                    }
                else:
                    summary["central_feed"] = _central_feed_disabled_status()
                if forward_book_service is not None:
                    summary["forward_books"] = (
                        forward_book_service.status()
                    )
                else:
                    summary["forward_books"] = (
                        _forward_books_disabled_status(config)
                    )
                summary["classifier_budget"] = _classifier_budget_status(config)
                summary["discovery_cycle"] = discovery_cycle_status
                _atomic_json_write(config.data_dir / "fleet_state.json", {**summary, "live": live, "updated_at": datetime.now(timezone.utc).isoformat()})
                print(json.dumps(summary, indent=2, sort_keys=True))
                for market_id in summary["started"]:
                    notifier.notify("Fleet: bot started", market_id=market_id, live=live)
                for market_id in summary["stopped"]:
                    notifier.notify("Fleet: bot stopped", market_id=market_id)
            except Exception as exc:
                log_event("fleet_sync_error", error=str(exc))
                try:
                    notifier.notify("Fleet sync failed; continuing", error=str(exc))
                except Exception as notify_exc:
                    log_event("fleet_notify_failed", error=str(notify_exc))
            if once:
                return 0
            time.sleep(max(60.0, config.schedule.interval_minutes * 60.0))
    finally:
        if central_service is not None:
            central_service.stop()
        if forward_book_service is not None:
            forward_book_service.stop()
        if once:
            # A one-shot invocation must not leave orphan children behind in
            # tests/CI; the long-running mode keeps bots alive across cycles.
            manager.shutdown()


def _default_spawner(command: list[str], log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "a", encoding="utf-8")
    return subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT)
