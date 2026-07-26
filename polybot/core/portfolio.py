from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from polybot.log import log_event

from .holdings import _atomic_json_write

try:
    import fcntl
except ImportError:  # pragma: no cover - production deployment is Linux
    fcntl = None  # type: ignore[assignment]


_LEDGER_THREAD_LOCKS: dict[str, threading.Lock] = {}
_LEDGER_THREAD_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class AllocatorConfig:
    """Portfolio-level exposure caps across every discovered market."""

    per_order_usd: float = 50.0
    per_market_usd: float = 100.0
    per_event_usd: float = 150.0
    per_group_usd: float = 200.0  # correlation-group concentration
    daily_usd: float = 300.0
    total_usd: float = 1000.0
    max_open_positions: int = 5
    max_per_deadline_week_usd: float = 400.0
    # Second correlation dimension: regional contagion moves different party
    # sets together, so exposure is also capped per theater.
    per_region_usd: float = 300.0
    # Fleet-level kill switch: when cumulative realized trading losses reach
    # this, the fleet writes the shared operator mode to alert_only and pages.
    max_drawdown_usd: float = 150.0


@dataclass(frozen=True)
class AllocationRequest:
    market_id: str
    event_slug: str
    correlation_group: str
    deadline_iso: str
    usd: float
    region: str = "global"


class PortfolioAllocator:
    """Portfolio-level exposure control across every discovered market.

    Discovering more markets must not silently multiply correlated risk: every
    prospective order is checked against per-order, per-market, per-event,
    per-correlation-group, per-deadline-week, daily, and total caps plus a
    simultaneous-position limit. State is persisted atomically so the caps
    survive restarts and are shared by every strategy the pipeline arms.
    """

    def __init__(self, state_path: Path, config: AllocatorConfig):
        self.state_path = state_path
        self.config = config

    @classmethod
    def from_ledger(cls, state_path: Path) -> "PortfolioAllocator":
        """Attach to an existing ledger using the caps persisted in it, so
        executors never carry their own copy of portfolio limits. Fails closed
        when the ledger has no caps (pipeline has not initialized it)."""
        with _ledger_lock(state_path):
            if not state_path.exists():
                raise ValueError(f"portfolio ledger {state_path} does not exist; run the discovery pipeline first")
            raw = _read_state(state_path)
        caps = raw.get("caps") if isinstance(raw, dict) else None
        if not isinstance(caps, dict):
            raise ValueError(f"portfolio ledger {state_path} has no caps; run the discovery pipeline to initialize it")
        known = {f: caps[f] for f in AllocatorConfig.__dataclass_fields__ if f in caps}  # type: ignore[attr-defined]
        return cls(state_path, AllocatorConfig(**known))

    def write_caps(self) -> None:
        """Persist the configured caps into the ledger so executor-side links
        (PortfolioLink) enforce exactly the caps the pipeline was run with."""
        with _ledger_lock(self.state_path):
            state = self._load_unlocked()
            state["caps"] = asdict(self.config)
            self._save_unlocked(state)

    # -- queries --

    def preview(self, request: AllocationRequest) -> tuple[float, list[str]]:
        """Maximum USD grantable for this request and the blockers that bound
        it to zero. Pure read: nothing is reserved."""
        with _ledger_lock(self.state_path):
            return self._preview_state(request, self._load_unlocked())

    def _preview_state(self, request: AllocationRequest, state: dict[str, Any]) -> tuple[float, list[str]]:
        blockers: list[str] = []
        remaining = [self.config.per_order_usd]

        def bound(name: str, cap: float, spent: float) -> None:
            left = cap - spent
            if left <= 0:
                blockers.append(name)
            remaining.append(max(0.0, left))

        bound("per_market_limit", self.config.per_market_usd, _get(state, "per_market", request.market_id))
        bound("per_event_limit", self.config.per_event_usd, _get(state, "per_event", request.event_slug))
        bound("correlation_group_limit", self.config.per_group_usd, _get(state, "per_group", request.correlation_group))
        bound("region_limit", self.config.per_region_usd, _get(state, "per_region", request.region))
        bound("daily_limit", self.config.daily_usd, _get(state, "per_day", _today()))
        bound("total_limit", self.config.total_usd, float(state.get("total", 0.0)))
        bound("deadline_week_limit", self.config.max_per_deadline_week_usd, _get(state, "per_deadline_week", _week(request.deadline_iso)))

        open_positions = state.get("open_positions", [])
        if request.market_id not in open_positions and len(open_positions) >= self.config.max_open_positions:
            blockers.append("max_open_positions")

        granted = 0.0 if blockers else round(min(min(remaining), request.usd), 2)
        if granted <= 0 and not blockers:
            blockers.append("no_remaining_allowance")
            granted = 0.0
        return granted, blockers

    # -- mutations --

    def commit(self, request: AllocationRequest) -> dict[str, Any]:
        """Reserve exposure for an order that is about to be placed. Fails
        closed if the preview no longer grants the requested amount.

        The check and reservation share one inter-process critical section.
        Atomic JSON replacement alone prevents torn files but cannot prevent
        two fleet workers from spending the same remaining allowance.
        """
        with _ledger_lock(self.state_path):
            state = self._load_unlocked()
            granted, blockers = self._preview_state(request, state)
            if blockers or granted + 1e-9 < request.usd:
                raise ValueError(f"allocation rejected for {request.market_id}: granted={granted} blockers={blockers}")
            _add(state, "per_market", request.market_id, request.usd)
            _add(state, "per_event", request.event_slug, request.usd)
            _add(state, "per_group", request.correlation_group, request.usd)
            _add(state, "per_region", request.region, request.usd)
            _add(state, "per_day", _today(), request.usd)
            _add(state, "per_deadline_week", _week(request.deadline_iso), request.usd)
            state["total"] = round(float(state.get("total", 0.0)) + request.usd, 2)
            _add(state, "cost_basis", request.market_id, request.usd)
            open_positions = set(state.get("open_positions", []))
            open_positions.add(request.market_id)
            state["open_positions"] = sorted(open_positions)
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save_unlocked(state)
            return state

    def release_position(self, market_id: str) -> dict[str, Any]:
        """Mark a position closed (spent exposure history is retained; only
        the simultaneous-position slot is freed)."""
        with _ledger_lock(self.state_path):
            state = self._load_unlocked()
            state["open_positions"] = sorted(set(state.get("open_positions", [])) - {market_id})
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save_unlocked(state)
            return state

    def reduce_basis(self, market_id: str, proceeds_usd: float) -> dict[str, Any]:
        """Partial exit (trim): proceeds reduce the open cost basis; realized
        P&L is only recognized when the position fully closes."""
        with _ledger_lock(self.state_path):
            state = self._load_unlocked()
            basis = _get(state, "cost_basis", market_id)
            state.setdefault("cost_basis", {})[market_id] = round(max(0.0, basis - proceeds_usd), 2)
            self._save_unlocked(state)
            return state

    def reconcile_entry_basis(self, market_id: str, reserved_usd: float, actual_cost_usd: float) -> dict[str, Any]:
        """Replace the latest order's conservative reserved basis with its
        actual executed cost.

        Attempt-based daily/market caps intentionally retain the full
        reservation; only open cost basis is corrected so partial paper fills
        cannot manufacture losses when the position later closes.
        """
        reserved = max(0.0, float(reserved_usd))
        actual = max(0.0, float(actual_cost_usd))
        if actual > reserved + 1e-6:
            raise ValueError(f"actual entry cost {actual} exceeds reserved amount {reserved}")
        with _ledger_lock(self.state_path):
            state = self._load_unlocked()
            basis = _get(state, "cost_basis", market_id)
            if basis + 1e-6 < reserved:
                raise ValueError(f"cost basis {basis} is below latest reservation {reserved} for {market_id}")
            state.setdefault("cost_basis", {})[market_id] = round(max(0.0, basis - reserved + actual), 2)
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save_unlocked(state)
            return state

    def settle(self, market_id: str, proceeds_usd: float | None) -> float:
        """Full close of the position's trading leg: realized = proceeds -
        remaining cost basis. proceeds None (e.g. wallet reconciled to flat
        with no fill data) drops the basis without recognizing P&L."""
        with _ledger_lock(self.state_path):
            state = self._load_unlocked()
            basis = _get(state, "cost_basis", market_id)
            state.setdefault("cost_basis", {}).pop(market_id, None)
            realized = 0.0
            if proceeds_usd is not None:
                realized = round(proceeds_usd - basis, 2)
                state["realized_net"] = round(float(state.get("realized_net", 0.0)) + realized, 2)
                _add(state, "realized_by_day", _today(), realized)
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save_unlocked(state)
            return realized

    def reconcile(self, *, is_open: Callable[[str], bool] | None = None, keep_days: int = 14) -> dict[str, Any]:
        """Ledger hygiene for long runs: prune stale daily/deadline-week
        buckets (only today's bucket bounds the daily cap; old ones are dead
        weight) and drop open-position slots whose market no longer holds
        anything according to `is_open`."""
        with _ledger_lock(self.state_path):
            state = self._load_unlocked()
            cutoff = (datetime.now(timezone.utc).date() - timedelta(days=keep_days)).isoformat()
            for bucket in ("per_day", "realized_by_day"):
                values = state.get(bucket)
                if isinstance(values, dict):
                    state[bucket] = {k: v for k, v in values.items() if str(k) >= cutoff}
            if is_open is not None:
                open_positions = [m for m in state.get("open_positions", []) if is_open(m)]
                dropped = sorted(set(state.get("open_positions", [])) - set(open_positions))
                state["open_positions"] = sorted(open_positions)
                for market_id in dropped:
                    # Slot freed with no fill data: drop the basis without P&L.
                    state.setdefault("cost_basis", {}).pop(market_id, None)
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save_unlocked(state)
            return state

    def realized_net(self) -> float:
        with _ledger_lock(self.state_path):
            value = self._load_unlocked().get("realized_net", 0.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def snapshot(self) -> dict[str, Any]:
        with _ledger_lock(self.state_path):
            return self._load_unlocked()

    # -- persistence --

    def _load_unlocked(self) -> dict[str, Any]:
        return _read_state(self.state_path)

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        _atomic_json_write(self.state_path, state)


@dataclass(frozen=True)
class PortfolioConfig:
    """Strategy-config section binding an executor to the shared ledger.

    Empty ledger_path disables portfolio accounting (hand-written standalone
    configs keep working unchanged). Emitted configs are generated with this
    section filled in, so live fills debit the same ledger the discovery
    pipeline budgets against.
    """

    ledger_path: str = ""
    market_id: str = ""
    event_slug: str = ""
    correlation_group: str = ""
    region: str = "global"
    deadline_iso: str = ""


class PortfolioLink:
    """Executor-side handle on the shared cross-market ledger."""

    def __init__(self, config: PortfolioConfig):
        self.config = config

    @classmethod
    def from_config(cls, config: PortfolioConfig) -> "PortfolioLink | None":
        if not config.ledger_path.strip():
            return None
        if not config.market_id.strip():
            raise ValueError("portfolio.ledger_path is set but portfolio.market_id is empty")
        return cls(config)

    def _request(self, usd: float) -> AllocationRequest:
        return AllocationRequest(
            market_id=self.config.market_id,
            event_slug=self.config.event_slug or self.config.market_id,
            correlation_group=self.config.correlation_group or "uncategorized",
            deadline_iso=self.config.deadline_iso,
            usd=usd,
            region=self.config.region or "global",
        )

    def allowed(self, requested_usd: float) -> tuple[float, list[str]]:
        """Clamp a prospective buy by the shared ledger. Any ledger problem
        fails closed (0 granted) rather than trading unaccounted."""
        try:
            allocator = PortfolioAllocator.from_ledger(Path(self.config.ledger_path))
            return allocator.preview(self._request(requested_usd))
        except (ValueError, OSError) as exc:
            log_event("portfolio_ledger_unavailable", ledger=self.config.ledger_path, error=str(exc))
            return 0.0, [f"portfolio_ledger_unavailable:{exc}"]

    def reserve(self, usd: float) -> None:
        """Debit the ledger for an order attempt (reserve-on-attempt, matching
        RiskState semantics: an unfilled order still consumed the allowance)."""
        allocator = PortfolioAllocator.from_ledger(Path(self.config.ledger_path))
        allocator.commit(self._request(usd))

    def reconcile_entry_basis(self, reserved_usd: float, actual_cost_usd: float) -> None:
        allocator = PortfolioAllocator.from_ledger(Path(self.config.ledger_path))
        allocator.reconcile_entry_basis(self.config.market_id, reserved_usd, actual_cost_usd)

    def release(self) -> None:
        """Free the simultaneous-position slot after the position is closed."""
        try:
            allocator = PortfolioAllocator.from_ledger(Path(self.config.ledger_path))
            allocator.release_position(self.config.market_id)
        except (ValueError, OSError) as exc:
            log_event("portfolio_release_failed", ledger=self.config.ledger_path, error=str(exc))

    def settle(self, proceeds_usd: float | None) -> None:
        """Recognize realized P&L for a full close (None drops basis only)."""
        try:
            allocator = PortfolioAllocator.from_ledger(Path(self.config.ledger_path))
            realized = allocator.settle(self.config.market_id, proceeds_usd)
            log_event("portfolio_settled", market_id=self.config.market_id, realized=realized)
        except (ValueError, OSError) as exc:
            log_event("portfolio_settle_failed", ledger=self.config.ledger_path, error=str(exc))

    def reduce_basis(self, proceeds_usd: float) -> None:
        try:
            allocator = PortfolioAllocator.from_ledger(Path(self.config.ledger_path))
            allocator.reduce_basis(self.config.market_id, proceeds_usd)
        except (ValueError, OSError) as exc:
            log_event("portfolio_reduce_basis_failed", ledger=self.config.ledger_path, error=str(exc))


def _get(state: dict[str, Any], bucket: str, key: str) -> float:
    values = state.get(bucket)
    if not isinstance(values, dict):
        return 0.0
    try:
        return float(values.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _add(state: dict[str, Any], bucket: str, key: str, usd: float) -> None:
    values = state.setdefault(bucket, {})
    values[key] = round(_get(state, bucket, key) + usd, 2)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _week(deadline_iso: str) -> str:
    text = (deadline_iso or "").strip().replace("Z", "+00:00")
    try:
        deadline = datetime.fromisoformat(text)
    except ValueError:
        return "unknown"
    iso = deadline.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ValueError(f"corrupt allocator state {path}; refusing to guess remaining budgets")
    if not isinstance(raw, dict):
        raise ValueError(f"invalid allocator state {path}; expected a JSON object")
    return raw


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path.absolute())
    with _LEDGER_THREAD_LOCKS_GUARD:
        return _LEDGER_THREAD_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _ledger_lock(state_path: Path) -> Iterator[None]:
    """Serialize the full ledger read/check/write transaction.

    A process-local lock covers worker threads; `flock` covers the independent
    fleet processes. Unsupported platforms fail closed instead of silently
    reverting to unsafe atomic-replace-only behavior.
    """
    if fcntl is None:
        raise RuntimeError("portfolio ledger locking requires a platform with fcntl.flock")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    with _thread_lock(state_path):
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "AllocationRequest",
    "AllocatorConfig",
    "PortfolioAllocator",
    "PortfolioConfig",
    "PortfolioLink",
]
