from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import requests
import websocket

from .config import SETTINGS
from .log import log_event


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class TokenBook:
    token_id: str
    market_id: str = ""
    best_ask: float | None = None
    best_bid: float | None = None
    asks: list[tuple[float, float]] = field(default_factory=list)
    bids: list[tuple[float, float]] = field(default_factory=list)
    updated_mono: float | None = None
    received_at: str = ""
    source_at: str = ""
    book_hash: str = ""
    min_order_size: float | None = None
    tick_size: float | None = None
    neg_risk: bool | None = None
    last_trade_price: float | None = None
    revision: int = 0

    def staleness(self) -> float | None:
        if self.updated_mono is None:
            return None
        return max(0.0, time.monotonic() - self.updated_mono)


class BookCache:
    def __init__(
        self,
        token_ids: list[str],
        clob_host: str = SETTINGS.clob_host,
        *,
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        heartbeat_seconds: float = 10.0,
        reconnect_min_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
        max_snapshot_levels: int = 20,
    ):
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        if reconnect_min_seconds <= 0 or reconnect_max_seconds < reconnect_min_seconds:
            raise ValueError("invalid websocket reconnect bounds")
        if max_snapshot_levels <= 0:
            raise ValueError("max_snapshot_levels must be positive")
        self.token_ids = [str(token_id) for token_id in token_ids]
        self.clob_host = clob_host.rstrip("/")
        self.ws_url = ws_url
        self.heartbeat_seconds = heartbeat_seconds
        self.reconnect_min_seconds = reconnect_min_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self.max_snapshot_levels = max_snapshot_levels
        self.books = {token_id: TokenBook(token_id=token_id) for token_id in self.token_ids}
        self._lock = threading.Lock()
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._connected = False
        self._connection_generation = 0
        self._reconnects = 0
        self._last_open_at = ""
        self._last_close_at = ""
        self._last_error = ""
        self._opened_mono: float | None = None

    def best_ask(self, token_id: str) -> float | None:
        with self._lock:
            return self.books[token_id].best_ask

    def staleness(self, token_id: str) -> float | None:
        with self._lock:
            return self.books[token_id].staleness()

    def depth_under_cap(self, token_id: str, cap_price: float) -> float:
        with self._lock:
            book = self.books[token_id]
            return sum(price * size for price, size in book.asks if price <= cap_price)

    def snapshot_state(self, token_id: str) -> dict[str, Any]:
        with self._lock:
            book = self.books[token_id]
            return {
                "token_id": token_id,
                "market_id": book.market_id,
                "best_ask": book.best_ask,
                "best_bid": book.best_bid,
                "asks": book.asks[: self.max_snapshot_levels],
                "bids": book.bids[: self.max_snapshot_levels],
                "staleness": book.staleness(),
                "received_at": book.received_at,
                "source_at": book.source_at,
                "book_hash": book.book_hash,
                "min_order_size": book.min_order_size,
                "tick_size": book.tick_size,
                "neg_risk": book.neg_risk,
                "last_trade_price": book.last_trade_price,
                "revision": book.revision,
            }

    def connection_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self._connected,
                "generation": self._connection_generation,
                "reconnects": self._reconnects,
                "last_open_at": self._last_open_at,
                "last_close_at": self._last_close_at,
                "last_error": self._last_error,
            }

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def rest_snapshot(self, token_id: str) -> None:
        response = requests.get(f"{self.clob_host}/book", params={"token_id": token_id}, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("CLOB book response must be an object")
        self._apply_book(token_id, payload, event_type="rest_book")
        log_event("book_snapshot", token_id=token_id, source="rest", book=self.snapshot_state(token_id))

    def start_ws(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_ws_loop,
            name="polybot-book-stream",
            daemon=True,
        )
        self._thread.start()

    def stop_ws(self) -> None:
        self._stop.set()
        self._emit(
            {
                "event_type": "ws_stop",
                "received_at": _utc_now(),
                "source_at": "",
                "token_ids": list(self.token_ids),
            }
        )
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=2)

    def _run_ws_loop(self) -> None:
        delay = self.reconnect_min_seconds
        first = True
        while not self._stop.is_set():
            ws = websocket.WebSocketApp(
                self.ws_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            with self._lock:
                self._ws = ws
                if not first:
                    self._reconnects += 1
            first = False
            try:
                ws.run_forever()
            except Exception as exc:
                self._on_error(ws, exc)
            if self._stop.is_set():
                break
            self._stop.wait(delay)
            with self._lock:
                opened_mono = self._opened_mono
            if (
                opened_mono is not None
                and time.monotonic() - opened_mono
                >= self.heartbeat_seconds * 2.0
            ):
                delay = self.reconnect_min_seconds
            else:
                delay = min(self.reconnect_max_seconds, delay * 2.0)

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        ws.send(json.dumps({"type": "market", "assets_ids": self.token_ids, "custom_feature_enabled": True}))
        at = _utc_now()
        with self._lock:
            self._connected = True
            self._connection_generation += 1
            self._last_open_at = at
            self._last_error = ""
            self._opened_mono = time.monotonic()
        log_event("book_ws_subscribe", token_ids=self.token_ids)
        self._emit(
            {
                "event_type": "ws_open",
                "received_at": at,
                "source_at": "",
                "token_ids": list(self.token_ids),
                "connection": self.connection_state(),
            }
        )
        threading.Thread(
            target=self._heartbeat_loop,
            args=(ws,),
            name="polybot-book-heartbeat",
            daemon=True,
        ).start()

    def _heartbeat_loop(self, ws: websocket.WebSocketApp) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            with self._lock:
                active = self._ws is ws and self._connected
            if not active:
                return
            try:
                ws.send("PING")
            except Exception as exc:
                self._on_error(ws, exc)
                try:
                    ws.close()
                except Exception:
                    pass
                return

    def _on_error(self, _ws: websocket.WebSocketApp, error: Any) -> None:
        at = _utc_now()
        with self._lock:
            self._last_error = str(error)
        log_event("book_ws_error", error=str(error))
        self._emit(
            {
                "event_type": "ws_error",
                "received_at": at,
                "source_at": "",
                "token_ids": list(self.token_ids),
                "error": str(error),
            }
        )

    def _on_close(
        self,
        _ws: websocket.WebSocketApp,
        code: Any,
        reason: Any,
    ) -> None:
        at = _utc_now()
        with self._lock:
            self._connected = False
            self._last_close_at = at
        log_event("book_ws_close", code=code, reason=reason)
        self._emit(
            {
                "event_type": "ws_close",
                "received_at": at,
                "source_at": "",
                "token_ids": list(self.token_ids),
                "code": code,
                "reason": reason,
            }
        )

    def _on_message(self, _ws: websocket.WebSocketApp, raw: str) -> None:
        if raw.strip().upper() == "PONG":
            self._emit(
                {
                    "event_type": "ws_pong",
                    "received_at": _utc_now(),
                    "source_at": "",
                    "token_ids": list(self.token_ids),
                }
            )
            return
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return
        messages = decoded if isinstance(decoded, list) else [decoded]
        for message in messages:
            if isinstance(message, dict):
                self._apply_event(_normalize_ws_event(message))

    def _apply_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("event_type")
        if event_type == "book":
            token_id = str(event.get("asset_id") or event.get("token_id") or "")
            if token_id in self.books:
                self._apply_book(token_id, event, event_type="book")
            return
        if event_type == "best_bid_ask":
            token_id = str(event.get("asset_id") or "")
            if token_id in self.books:
                with self._lock:
                    book = self.books[token_id]
                    ask = _as_float(event.get("best_ask"))
                    bid = _as_float(event.get("best_bid"))
                    if ask is not None:
                        book.best_ask = ask
                    if bid is not None:
                        book.best_bid = bid
                    book.market_id = str(event.get("market") or book.market_id)
                self._emit_book_event("best_bid_ask", token_id, event)
            return
        if event_type == "price_change":
            changes = event.get("price_changes")
            if not isinstance(changes, list):
                changes = [event]
            for change in changes:
                if not isinstance(change, dict):
                    continue
                token_id = str(change.get("asset_id") or event.get("asset_id") or "")
                if token_id not in self.books:
                    continue
                received_at = _utc_now()
                with self._lock:
                    book = self.books[token_id]
                    price = _as_float(change.get("price"))
                    size = _as_float(change.get("size"))
                    side = str(change.get("side") or "").upper()
                    if price is not None and size is not None and side in {"BUY", "SELL"}:
                        _replace_level(
                            book.bids if side == "BUY" else book.asks,
                            price,
                            size,
                            reverse=side == "BUY",
                        )
                    ask = _as_float(change.get("best_ask"))
                    bid = _as_float(change.get("best_bid"))
                    if ask is not None:
                        book.best_ask = ask
                    elif book.asks:
                        book.best_ask = book.asks[0][0]
                    if bid is not None:
                        book.best_bid = bid
                    elif book.bids:
                        book.best_bid = book.bids[0][0]
                    book.updated_mono = time.monotonic()
                    book.received_at = received_at
                    book.source_at = _source_at(
                        change.get("timestamp") or event.get("timestamp")
                    )
                    book.market_id = str(event.get("market") or book.market_id)
                    book.book_hash = str(change.get("hash") or book.book_hash)
                    book.revision += 1
                self._emit_book_event("price_change", token_id, event)
            return
        if event_type == "last_trade_price":
            token_id = str(event.get("asset_id") or "")
            if token_id in self.books:
                received_at = _utc_now()
                source_at = _source_at(event.get("timestamp"))
                with self._lock:
                    book = self.books[token_id]
                    book.last_trade_price = _as_float(event.get("price"))
                    book.market_id = str(event.get("market") or book.market_id)
                self._emit_book_event(
                    "last_trade_price",
                    token_id,
                    event,
                    received_at=received_at,
                    source_at=source_at,
                )
            return
        if event_type == "tick_size_change":
            token_id = str(event.get("asset_id") or "")
            if token_id in self.books:
                with self._lock:
                    book = self.books[token_id]
                    book.tick_size = _as_float(event.get("new_tick_size"))
                    book.market_id = str(event.get("market") or book.market_id)
                self._emit_book_event("tick_size_change", token_id, event)
            return
        if event_type == "market_resolved":
            self._emit(
                {
                    "event_type": "market_resolved",
                    "received_at": _utc_now(),
                    "source_at": _source_at(event.get("timestamp")),
                    "event": event,
                }
            )

    def _apply_book(
        self,
        token_id: str,
        payload: dict[str, Any],
        *,
        event_type: str,
    ) -> None:
        asks = _levels(payload.get("asks"))
        bids = _levels(payload.get("bids"))
        received_at = _utc_now()
        with self._lock:
            book = self.books[token_id]
            book.asks = sorted(asks, key=lambda level: level[0])
            book.bids = sorted(bids, key=lambda level: level[0], reverse=True)
            book.best_ask = book.asks[0][0] if book.asks else _as_float(payload.get("best_ask"))
            book.best_bid = book.bids[0][0] if book.bids else _as_float(payload.get("best_bid"))
            book.updated_mono = time.monotonic()
            book.received_at = received_at
            book.source_at = _source_at(payload.get("timestamp"))
            book.market_id = str(payload.get("market") or payload.get("condition_id") or "")
            book.book_hash = str(payload.get("hash") or "")
            book.min_order_size = _as_float(
                payload.get("min_order_size") or payload.get("minOrderSize")
            )
            book.tick_size = _as_float(
                payload.get("tick_size") or payload.get("tickSize")
            )
            if "neg_risk" in payload or "negRisk" in payload:
                book.neg_risk = bool(
                    payload.get("neg_risk") or payload.get("negRisk")
                )
            book.last_trade_price = _as_float(
                payload.get("last_trade_price")
                or payload.get("lastTradePrice")
            )
            book.revision += 1
        self._emit_book_event(event_type, token_id, payload)

    def _emit_book_event(
        self,
        event_type: str,
        token_id: str,
        event: dict[str, Any],
        *,
        received_at: str | None = None,
        source_at: str | None = None,
    ) -> None:
        snapshot = self.snapshot_state(token_id)
        self._emit(
            {
                "event_type": event_type,
                "received_at": received_at or snapshot["received_at"],
                "source_at": (
                    source_at
                    if source_at is not None
                    else snapshot["source_at"]
                ),
                "token_id": token_id,
                "event": event,
                "snapshot": snapshot,
            }
        )

    def _emit(self, record: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(record)
            except Exception as exc:
                log_event(
                    "book_listener_failed",
                    event_type=record.get("event_type"),
                    error=str(exc),
                )


def _levels(raw: Any) -> list[tuple[float, float]]:
    if not isinstance(raw, list):
        return []
    levels: list[tuple[float, float]] = []
    for level in raw:
        if not isinstance(level, dict):
            continue
        price = _as_float(level.get("price"))
        size = _as_float(level.get("size"))
        if price is not None and size is not None and size > 0:
            levels.append((price, size))
    return levels


def _replace_level(
    levels: list[tuple[float, float]],
    price: float,
    size: float,
    *,
    reverse: bool,
) -> None:
    remaining = [level for level in levels if abs(level[0] - price) > 1e-12]
    if size > 0:
        remaining.append((price, size))
    remaining.sort(key=lambda level: level[0], reverse=reverse)
    levels[:] = remaining


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_at(value: Any) -> str:
    if value is None or value == "":
        return ""
    text = str(value).strip()
    try:
        if text.replace(".", "", 1).isdigit():
            stamp = float(text)
            if stamp > 10_000_000_000:
                stamp /= 1000.0
            return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return ""


def _normalize_ws_event(message: dict[str, Any]) -> dict[str, Any]:
    """Accept both the raw market socket and current SDK envelope shapes."""
    payload = message.get("payload")
    if (
        message.get("topic") == "market"
        and isinstance(payload, dict)
        and message.get("type")
    ):
        event = dict(payload)
        event["event_type"] = str(message["type"])
    else:
        event = dict(message)

    aliases = {
        "tokenId": "asset_id",
        "priceChanges": "price_changes",
        "bestBid": "best_bid",
        "bestAsk": "best_ask",
        "newTickSize": "new_tick_size",
        "oldTickSize": "old_tick_size",
        "winningTokenId": "winning_asset_id",
        "winningAssetId": "winning_asset_id",
        "winningOutcome": "winning_outcome",
        "token_ids": "assets_ids",
    }
    for current, normalized in aliases.items():
        if current in event and normalized not in event:
            event[normalized] = event[current]
    changes = event.get("price_changes")
    if isinstance(changes, list):
        normalized_changes = []
        for item in changes:
            if not isinstance(item, dict):
                continue
            change = dict(item)
            for current, normalized in aliases.items():
                if current in change and normalized not in change:
                    change[normalized] = change[current]
            normalized_changes.append(change)
        event["price_changes"] = normalized_changes
    return event
