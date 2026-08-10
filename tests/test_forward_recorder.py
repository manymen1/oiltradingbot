from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from polybot.book import BookCache, _normalize_ws_event
from polybot.core.fees import explicit_zero_fee_schedule
from polybot.core.types import Article
from polybot.discovery.config import (
    ForwardRecorderConfig,
    forward_recorder_db_path,
    load_discovery_config,
)
from polybot.discovery.sources import build_source_plan
from polybot.discovery.store import DiscoveryStore
from polybot.rules.compiler import fixture_semantics
from polybot.rules.contracts import RuleSpec
from polybot.rules.forward import (
    ForwardBookService,
    ForwardRecorderStore,
    RecordedClobQuoteAdapter,
    _evidence_policy_payload,
    build_forward_timeline,
    forward_completeness_report,
    record_forward_resolutions,
    rotate_forward_recorder,
)
from polybot.rules.replay import load_rule_replay_timeline
from polybot.rules.store import RuleStore
from test_rule_contracts import _golden_rules, context_for_case


def _setup(tmp_path: Path) -> tuple[Path, object, RuleSpec, object]:
    case = next(
        item
        for item in _golden_rules()
        if item["expected_family"] == "OCCURRENCE_BEFORE_DEADLINE"
    )
    context = replace(
        context_for_case(case, strong_analysis=True),
        state="PAPER_ELIGIBLE",
    )
    spec = RuleSpec.from_context(
        context,
        fixture_semantics(context),
        compiler_model="fixture",
        compiled_at="2026-07-25T00:00:00+00:00",
    )
    plan = build_source_plan(context, spec)
    data_dir = tmp_path / "data"
    discovery = DiscoveryStore(data_dir)
    discovery.save_context(context)
    discovery.save_source_plan(plan)
    RuleStore(data_dir / "rules.sqlite3").save_spec(spec)
    config_path = tmp_path / "discovery.yaml"
    config_path.write_text(
        f"""
classifier:
  provider: rule_based
rule_compiler:
  enabled: true
  db_path: {data_dir / "rules.sqlite3"}
rule_runner:
  enabled: true
  extraction_passes: 2
forward_recorder:
  enabled: true
  db_path: {data_dir / "forward.sqlite3"}
  quote_survival_horizons_ms: [100, 1000, 10000]
  max_sample_lag_ms: 250
fleet:
  position_mode: alert_only
data_dir: {data_dir}
logs_dir: {tmp_path / "logs"}
""",
        encoding="utf-8",
    )
    return config_path, context, spec, plan


def _snapshot(
    token_id: str,
    at: str,
    *,
    bid: float,
    ask: float,
    revision: int = 1,
) -> dict:
    return {
        "token_id": token_id,
        "market_id": "condition",
        "best_bid": bid,
        "best_ask": ask,
        "bids": [[bid, 100.0]],
        "asks": [[ask, 100.0]],
        "staleness": 0.0,
        "received_at": at,
        "source_at": at,
        "book_hash": f"book-{token_id}-{revision}",
        "revision": revision,
    }


def _record_book(
    store: ForwardRecorderStore,
    binding: str,
    session_id: str,
    snapshot: dict,
) -> None:
    store.record_book_event(
        session_id=session_id,
        binding_sha256=binding,
        record={
            "event_type": "book",
            "received_at": snapshot["received_at"],
            "source_at": snapshot["source_at"],
            "token_id": snapshot["token_id"],
            "event": {"event_type": "book"},
            "snapshot": snapshot,
        },
    )


def test_schema_v3_migrates_existing_bindings_as_semantic(
    tmp_path: Path,
) -> None:
    path = tmp_path / "forward.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE bindings (
                binding_sha256 TEXT PRIMARY KEY,
                market_id TEXT NOT NULL,
                event_slug TEXT NOT NULL,
                rule_spec_sha256 TEXT NOT NULL,
                source_plan_sha256 TEXT NOT NULL,
                context_json TEXT NOT NULL,
                rule_spec_json TEXT NOT NULL,
                source_plan_json TEXT NOT NULL,
                recorder_policy_sha256 TEXT NOT NULL,
                evidence_policy_sha256 TEXT NOT NULL DEFAULT '',
                evidence_policy_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO bindings VALUES(
                'legacy', 'market', 'event', 'spec', 'plan',
                '{}', '{}', '{}', 'policy', '', '{}', '2026-01-01'
            )
            """
        )

    ForwardRecorderStore(path)

    with sqlite3.connect(path) as connection:
        kind = connection.execute(
            """
            SELECT binding_kind FROM bindings
            WHERE binding_sha256='legacy'
            """
        ).fetchone()[0]
    assert kind == "SEMANTIC"


def test_book_cache_reconstructs_depth_and_accepts_current_sdk_envelope() -> None:
    cache = BookCache(["token"])
    records: list[dict] = []
    cache.add_listener(records.append)
    cache._apply_event(
        {
            "event_type": "book",
            "asset_id": "token",
            "market": "condition",
            "timestamp": "1784973600000",
            "hash": "initial",
            "bids": [
                {"price": "0.40", "size": "10"},
                {"price": "0.39", "size": "20"},
            ],
            "asks": [
                {"price": "0.42", "size": "30"},
                {"price": "0.43", "size": "40"},
            ],
        }
    )
    cache._apply_event(
        _normalize_ws_event(
            {
                "topic": "market",
                "type": "price_change",
                "payload": {
                    "market": "condition",
                    "timestamp": "1784973600100",
                    "priceChanges": [
                        {
                            "tokenId": "token",
                            "price": "0.41",
                            "size": "12",
                            "side": "BUY",
                            "bestBid": "0.41",
                            "bestAsk": "0.42",
                        }
                    ],
                },
            }
        )
    )
    cache._apply_event(
        {
            "event_type": "price_change",
            "timestamp": "1784973600200",
            "price_changes": [
                {
                    "asset_id": "token",
                    "price": "0.42",
                    "size": "0",
                    "side": "SELL",
                    "best_ask": "0.43",
                }
            ],
        }
    )
    cache._apply_event(
        {
            "event_type": "last_trade_price",
            "asset_id": "token",
            "price": "0.425",
            "timestamp": "1784973600300",
        }
    )
    state = cache.snapshot_state("token")
    assert state["bids"][0] == (0.41, 12.0)
    assert state["asks"] == [(0.43, 40.0)]
    assert state["best_bid"] == 0.41
    assert state["best_ask"] == 0.43
    assert state["revision"] == 3
    assert state["last_trade_price"] == 0.425
    assert state["source_at"].startswith("2026-")
    assert [item["event_type"] for item in records] == [
        "book",
        "price_change",
        "price_change",
        "last_trade_price",
    ]
    cache._on_message(SimpleNamespace(), "PONG")
    assert records[-1]["event_type"] == "ws_pong"
    assert records[-1]["token_ids"] == ["token"]


def test_forward_store_builds_replayable_content_addressed_timeline(
    tmp_path: Path,
) -> None:
    config_path, context, spec, plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    store = ForwardRecorderStore(forward_recorder_db_path(config))
    binding = store.ensure_binding(
        context,
        spec,
        plan,
        config.forward_recorder,
        evidence_policy=_evidence_policy_payload(config),
        created_at="2026-07-25T10:00:00+00:00",
    )
    store.start_session(
        binding,
        session_id="session",
        started_at="2026-07-25T10:00:00+00:00",
    )
    outcome = spec.outcomes[0]
    _record_book(
        store,
        binding,
        "session",
        _snapshot(
            outcome.yes_token_id,
            "2026-07-25T10:00:00+00:00",
            bid=0.78,
            ask=0.80,
        ),
    )
    store.record_trade_print(
        session_id="session",
        binding_sha256=binding,
        record={
            "event_type": "last_trade_price",
            "token_id": outcome.yes_token_id,
            "received_at": "2026-07-25T10:00:00.075000+00:00",
            "source_at": "2026-07-25T10:00:00.070000+00:00",
            "event": {
                "market": "condition",
                "asset_id": outcome.yes_token_id,
                "price": "0.80",
                "size": "25",
                "side": "BUY",
                "fee_rate_bps": "0",
                "transaction_hash": "0xtrade",
            },
        },
    )
    # A reconnect can redeliver the same public print at a later local time.
    # Content identity must keep that from inflating maker evidence.
    store.record_trade_print(
        session_id="reconnected-session",
        binding_sha256=binding,
        record={
            "event_type": "last_trade_price",
            "token_id": outcome.yes_token_id,
            "received_at": "2026-07-25T10:00:00.090000+00:00",
            "source_at": "2026-07-25T10:00:00.070000+00:00",
            "event": {
                "market": "condition",
                "asset_id": outcome.yes_token_id,
                "price": "0.80",
                "size": "25",
                "side": "BUY",
                "fee_rate_bps": "0",
                "transaction_hash": "0xtrade",
            },
        },
    )
    _record_book(
        store,
        binding,
        "session",
        _snapshot(
            outcome.no_token_id,
            "2026-07-25T10:00:00.050000+00:00",
            bid=0.20,
            ask=0.22,
        ),
    )
    article = Article(
        url="https://reuters.com/forward",
        domain="reuters.com",
        title="Talks began",
        published_at="2026-07-25T10:00:00+00:00",
        fetched_at="2026-07-25T10:00:01+00:00",
        raw_text=(
            "Both senior delegations entered the room and talks began."
        ),
        hash="reuters-forward",
        source_kind="article",
    )
    store.record_article(
        binding_sha256=binding,
        article=article,
        first_observed_at="2026-07-25T10:00:01+00:00",
        cycle_id="cycle",
    )
    store.record_extraction(
        binding_sha256=binding,
        article_id=article.hash,
        started_at="2026-07-25T10:00:01+00:00",
        completed_at="2026-07-25T10:00:01.100000+00:00",
        duration_ms=100,
        result={"status": "AGREED"},
    )

    report = forward_completeness_report(
        config_path,
        context.market_id,
    )
    assert report["status"] == "READY_FOR_REPLAY"
    assert report["book_tokens_seen"] == 2
    assert report["articles_with_extraction"] == 1
    assert report["trade_prints"] == 1
    assert report["latency_ms"]["publisher_to_fetch"]["p50"] == 1000.0
    assert report["latency_ms"]["two_pass_extraction"]["p50"] == 100.0

    first = build_forward_timeline(config_path, context.market_id)
    second = build_forward_timeline(config_path, context.market_id)
    assert first["timeline_sha256"] == second["timeline_sha256"]
    assert first["event_count"] == 3
    assert first["trade_events"] == 1
    timeline_path = Path(first["timeline_path"])
    assert timeline_path.exists()
    loaded = load_rule_replay_timeline(timeline_path, spec)
    assert [item.event_type for item in loaded] == [
        "BOOK",
        "TRADE",
        "ARTICLE",
    ]


def test_quote_survival_is_measured_at_original_and_stressed_price(
    tmp_path: Path,
) -> None:
    config_path, context, spec, plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    store = ForwardRecorderStore(forward_recorder_db_path(config))
    binding = store.ensure_binding(context, spec, plan, config.forward_recorder)
    outcome = spec.outcomes[0]
    anchor_at = datetime(2026, 7, 25, 10, tzinfo=timezone.utc)
    proof = SimpleNamespace(
        proof_sha256="a" * 64,
        token_id=outcome.yes_token_id,
        side="YES",
        allocation_usd=50.0,
    )
    store.record_quote_anchor(
        binding_sha256=binding,
        proof=proof,
        snapshot=_snapshot(
            outcome.yes_token_id,
            anchor_at.isoformat(),
            bid=0.79,
            ask=0.80,
        ),
        anchor_at=anchor_at.isoformat(),
    )
    pending = store.pending_quote_samples(
        [100],
        due_before=(anchor_at + timedelta(milliseconds=100)).isoformat(),
    )
    assert len(pending) == 1
    sampled = _snapshot(
        outcome.yes_token_id,
        (anchor_at + timedelta(milliseconds=110)).isoformat(),
        bid=0.79,
        ask=0.80,
    )
    sampled["asks"] = [[0.80, 62.5]]
    store.record_quote_sample(
        anchor=pending[0],
        sampled_at=sampled["received_at"],
        status="OK",
        snapshot=sampled,
        max_sample_lag_ms=250,
    )
    report = store.completeness(
        binding_sha256=binding,
        token_ids=[],
        horizons_ms=[100],
        max_sample_lag_ms=250,
    )
    sample = report["quote_survival"]["100"]
    assert sample["coverage"] == 1.0
    assert sample["quote_survival_rate"] == 1.0
    assert sample["one_cent_survival_rate"] == 1.0


def test_quote_sampler_rejects_cached_depth_after_stream_failure(
    tmp_path: Path,
) -> None:
    config_path, context, spec, plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    store = ForwardRecorderStore(forward_recorder_db_path(config))
    binding = store.ensure_binding(context, spec, plan, config.forward_recorder)
    outcome = spec.outcomes[0]
    opened_at = "2026-07-25T10:00:00+00:00"
    store.start_session(binding, session_id="stream", started_at=opened_at)
    store.record_operational_event(
        session_id="stream",
        binding_sha256=binding,
        event_type="ws_open",
        observed_at=opened_at,
        payload={"token_ids": [outcome.yes_token_id]},
    )
    assert store.stream_available(
        binding_sha256=binding,
        token_id=outcome.yes_token_id,
        at="2026-07-25T10:00:00.040000+00:00",
    )
    store.record_operational_event(
        session_id="stream",
        binding_sha256=binding,
        event_type="ws_error",
        observed_at="2026-07-25T10:00:00.050000+00:00",
        payload={"token_ids": [outcome.yes_token_id]},
    )
    assert not store.stream_available(
        binding_sha256=binding,
        token_id=outcome.yes_token_id,
        at="2026-07-25T10:00:00.300000+00:00",
    )
    adapter = RecordedClobQuoteAdapter(
        config=config,
        context=context,
        spec=spec,
        source_plan=plan,
    )
    book = _snapshot(
        outcome.yes_token_id,
        opened_at,
        bid=0.79,
        ask=0.80,
    )
    _record_book(store, binding, "stream", book)
    store.record_quote_anchor(
        binding_sha256=binding,
        proof=SimpleNamespace(
            proof_sha256="b" * 64,
            token_id=outcome.yes_token_id,
            side="YES",
            allocation_usd=50.0,
        ),
        snapshot=book,
        anchor_at=opened_at,
    )
    assert adapter.sample_due(
        now=datetime(
            2026,
            7,
            25,
            10,
            0,
            0,
            100000,
            tzinfo=timezone.utc,
        )
    ) == 1
    report = store.completeness(
        binding_sha256=binding,
        token_ids=[outcome.yes_token_id],
        horizons_ms=[100],
        max_sample_lag_ms=250,
    )
    assert report["quote_survival"]["100"]["captured"] == 1
    assert report["quote_survival"]["100"]["on_time"] == 0
    assert "quote_survival_sampling_incomplete" in report["blockers"]


def test_shared_book_service_shards_and_routes_depth_without_network(
    tmp_path: Path,
) -> None:
    config_path, context, spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    config = replace(
        config,
        forward_recorder=replace(
            config.forward_recorder,
            rest_seed=False,
            max_tokens_per_connection=1,
        ),
    )
    service = ForwardBookService(config)
    status = service.poll_once([context])
    assert status["tokens"] == 2
    assert status["connections"] == 2
    outcome = spec.outcomes[0]
    cache = next(
        item
        for item in service._caches
        if outcome.yes_token_id in item.token_ids
    )
    cache._apply_event(
        {
            "event_type": "book",
            "asset_id": outcome.yes_token_id,
            "market": outcome.condition_id,
            "timestamp": "1784973600000",
            "hash": "shared-book",
            "bids": [{"price": "0.78", "size": "100"}],
            "asks": [{"price": "0.80", "size": "100"}],
        }
    )
    capture_binding = service.store.latest_book_binding(context.market_id)
    assert capture_binding is not None
    snapshot = service.store.latest_book_snapshot(
        binding_sha256=capture_binding,
        token_id=outcome.yes_token_id,
    )
    assert snapshot["best_ask"] == 0.80
    assert snapshot["source"] == "forward_recorder_shared_store"

    # Once semantic assets exist, the paper binding can read matching raw
    # history without copying it or claiming a mismatched recorder policy.
    semantic_binding = service.store.ensure_binding(
        context,
        spec,
        _plan,
        config.forward_recorder,
    )
    assert service.store.latest_binding(context.market_id) == semantic_binding
    semantic_snapshot = service.store.latest_book_snapshot(
        binding_sha256=semantic_binding,
        token_id=outcome.yes_token_id,
    )
    assert semantic_snapshot["best_ask"] == 0.80

    mismatched = service.store.ensure_binding(
        context,
        spec,
        _plan,
        replace(config.forward_recorder, max_book_levels=19),
    )
    assert (
        service.store.latest_book_snapshot(
            binding_sha256=mismatched,
            token_id=outcome.yes_token_id,
        )["source"]
        == "forward_recorder_store_missing"
    )
    service.stop()


def test_operational_rows_drop_book_state_and_foreign_shard_tokens(
    tmp_path: Path,
) -> None:
    """Operational rows are liveness evidence, not a second copy of the book.

    Socket-lifecycle events arrive carrying the whole shard subscription and
    are written once per binding, so an unnarrowed list is stored once per
    binding per pong. Book-bearing events likewise duplicated the snapshot
    already held in book_events.
    """
    config_path, context, spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    config = replace(
        config,
        forward_recorder=replace(config.forward_recorder, rest_seed=False),
    )
    service = ForwardBookService(config)
    service.poll_once([context])
    outcome = spec.outcomes[0]
    owned = {outcome.yes_token_id, outcome.no_token_id}
    foreign = [f"foreign-shard-token-{index}" for index in range(50)]

    service._on_stream_event(
        {
            "event_type": "ws_pong",
            "received_at": "2026-07-25T00:00:01+00:00",
            "source_at": "",
            "token_ids": sorted(owned) + foreign,
        },
        generation=service._generation,
    )
    service._on_stream_event(
        {
            "event_type": "best_bid_ask",
            "token_id": outcome.yes_token_id,
            "received_at": "2026-07-25T00:00:02+00:00",
            "source_at": "2026-07-25T00:00:02+00:00",
            "event": {"best_bid": "0.78", "best_ask": "0.80"},
            "snapshot": {"bids": [[0.78, 100]], "asks": [[0.80, 100]]},
        },
        generation=service._generation,
    )

    binding = service.store.latest_book_binding(context.market_id)
    assert binding is not None
    with sqlite3.connect(forward_recorder_db_path(config)) as connection:
        connection.row_factory = sqlite3.Row
        rows = {
            str(row["event_type"]): json.loads(str(row["payload_json"]))
            for row in connection.execute(
                "SELECT event_type, payload_json FROM operational_events"
                " WHERE binding_sha256=?",
                (binding,),
            )
        }

    # The pong keeps only the tokens this binding actually owns.
    assert set(rows["ws_pong"]["token_ids"]) == owned
    assert not set(rows["ws_pong"]["token_ids"]) & set(foreign)

    # Derived BBO notifications are redundant with the authoritative book and
    # price-change stream, so they are counted but not stored a second time.
    assert "best_bid_ask" not in rows
    assert service.status()["derived_bbo_events_ignored"] == 1

    # Liveness still resolves from the narrowed row, and a token this
    # binding does not own still does not read as available.
    assert service.store.stream_available(
        binding_sha256=binding,
        token_id=outcome.yes_token_id,
        at="2026-07-25T00:00:05+00:00",
    )
    assert not service.store.stream_available(
        binding_sha256=binding,
        token_id=foreign[0],
        at="2026-07-25T00:00:05+00:00",
    )
    service.stop()


def test_shared_book_service_records_ungraded_context_without_semantic_assets(
    tmp_path: Path,
) -> None:
    config_path, context, _spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    config = replace(
        config,
        forward_recorder=replace(config.forward_recorder, rest_seed=False),
    )
    ungraded = replace(context, state="DISCOVERED")
    service = ForwardBookService(config)

    status = service.poll_once([ungraded])

    assert status["selected_contexts"] == 1
    assert status["tokens"] == 2
    assert service.store.latest_book_binding(context.market_id) is not None
    service.stop()


def test_new_capture_session_retires_stale_active_session(
    tmp_path: Path,
) -> None:
    config_path, context, _spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    store = ForwardRecorderStore(forward_recorder_db_path(config))
    binding = store.ensure_book_binding(context, config.forward_recorder)
    store.start_session(
        binding,
        session_id="stale",
        started_at="2026-07-25T09:00:00+00:00",
    )
    store.start_session(
        binding,
        session_id="current",
        started_at="2026-07-25T10:00:00+00:00",
    )

    with store._connect(read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT session_id, ended_at, close_reason
            FROM sessions ORDER BY started_at
            """
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        (
            "stale",
            "2026-07-25T10:00:00+00:00",
            "superseded_by_new_session",
        ),
        ("current", None, ""),
    ]
    assert store.capture_status()["active_capture_sessions"] == 1


def test_capture_binding_ignores_operational_health_policy(
    tmp_path: Path,
) -> None:
    config_path, context, _spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    store = ForwardRecorderStore(forward_recorder_db_path(config))

    baseline = store.ensure_book_binding(
        context,
        config.forward_recorder,
    )
    changed_health = store.ensure_book_binding(
        context,
        replace(
            config.forward_recorder,
            health_startup_grace_seconds=5,
            health_stale_after_seconds=15,
            health_growth_window_seconds=20,
            health_alert_cooldown_seconds=30,
        ),
    )

    assert changed_health == baseline


def test_capture_binding_rotates_when_rule_timing_changes(
    tmp_path: Path,
) -> None:
    config_path, context, _spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    store = ForwardRecorderStore(forward_recorder_db_path(config))

    baseline = store.ensure_book_binding(context, config.forward_recorder)
    changed_outcome = replace(
        context.outcomes[0],
        rule_deadline_iso="2026-09-30T23:59:00-04:00",
        deadline_timezone="America/New_York",
        post_deadline_window="P3D",
    )
    changed_timing = store.ensure_book_binding(
        replace(context, outcomes=[changed_outcome]),
        config.forward_recorder,
    )

    assert changed_timing != baseline


def test_service_startup_closes_orphaned_capture_sessions(
    tmp_path: Path,
) -> None:
    config_path, context, _spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    store = ForwardRecorderStore(forward_recorder_db_path(config))
    binding = store.ensure_book_binding(context, config.forward_recorder)
    store.start_session(
        binding,
        session_id="orphan",
        started_at="2026-07-25T09:00:00+00:00",
    )

    ForwardBookService(config)

    assert store.capture_status()["active_capture_sessions"] == 0
    with store._connect(read_only=True) as connection:
        row = connection.execute(
            "SELECT close_reason FROM sessions WHERE session_id='orphan'"
        ).fetchone()
    assert row["close_reason"] == "service_startup_orphan_recovery"


def test_rotation_refuses_active_store_then_archives_without_deletion(
    tmp_path: Path,
) -> None:
    config_path, context, _spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    database_path = forward_recorder_db_path(config)
    store = ForwardRecorderStore(database_path)
    binding = store.ensure_book_binding(context, config.forward_recorder)

    with pytest.raises(RuntimeError, match="recorder is active"):
        rotate_forward_recorder(
            config_path,
            archive_dir=tmp_path / "archive",
        )

    store.close()
    manifest = rotate_forward_recorder(
        config_path,
        archive_dir=tmp_path / "archive",
        full_check=True,
    )
    archive_path = Path(manifest["archive_path"])
    assert archive_path.exists()
    assert Path(manifest["manifest_path"]).exists()
    assert manifest["quick_check"] == "ok"
    assert manifest["verification_mode"] == "full_quick_check"
    assert manifest["archived_bytes"] > 0

    archived = ForwardRecorderStore(archive_path)
    assert archived.latest_book_binding(context.market_id) == binding
    archived.close()
    fresh = ForwardRecorderStore(database_path)
    assert fresh.latest_book_binding(context.market_id) is None
    fresh.close()


def test_websocket_starts_before_rest_seed_and_shutdown_discards_old_seed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path, context, _spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    rest_started = threading.Event()
    release_rest = threading.Event()
    websocket_started = threading.Event()

    class BlockingBookCache:
        def __init__(self, token_ids, **_kwargs):
            self.token_ids = list(token_ids)
            self.listeners = []

        def add_listener(self, listener):
            self.listeners.append(listener)

        def start_ws(self):
            websocket_started.set()

        def stop_ws(self):
            release_rest.set()

        def connection_state(self):
            return {"connected": True, "reconnects": 0}

        def rest_snapshot(self, token_id):
            rest_started.set()
            assert websocket_started.is_set()
            assert release_rest.wait(2)
            record = {
                "event_type": "rest_book",
                "token_id": token_id,
                "received_at": "2026-07-25T10:00:00+00:00",
                "source_at": "2026-07-25T10:00:00+00:00",
                "event": {},
                "snapshot": _snapshot(
                    token_id,
                    "2026-07-25T10:00:00+00:00",
                    bid=0.40,
                    ask=0.60,
                ),
            }
            for listener in self.listeners:
                listener(record)

    monkeypatch.setattr(
        "polybot.rules.forward.BookCache",
        BlockingBookCache,
    )
    service = ForwardBookService(config)

    status = service.sync([context])

    assert websocket_started.is_set()
    assert rest_started.wait(1)
    assert status["rest_seed_in_progress"] is True
    service.stop()
    assert service._seed_thread is not None
    service._seed_thread.join(timeout=2)
    assert service.status()["rest_seed_in_progress"] is False
    assert service.store.capture_status()["book_events"] == 0


def test_refresh_discards_obsolete_seed_work(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path, context, _spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    old_seed_started = threading.Event()
    release_old_seed = threading.Event()
    old_seed_emitted = threading.Event()
    cache_count = 0

    class RefreshBookCache:
        def __init__(self, token_ids, **_kwargs):
            nonlocal cache_count
            self.token_ids = list(token_ids)
            self.listeners = []
            self.is_old = cache_count == 0
            cache_count += 1

        def add_listener(self, listener):
            self.listeners.append(listener)

        def start_ws(self):
            return None

        def stop_ws(self):
            if self.is_old:
                release_old_seed.set()

        def connection_state(self):
            return {"connected": True, "reconnects": 0}

        def rest_snapshot(self, token_id):
            if self.is_old:
                old_seed_started.set()
                assert release_old_seed.wait(2)
            record = {
                "event_type": "rest_book",
                "token_id": token_id,
                "received_at": "2026-07-25T10:00:00+00:00",
                "source_at": "2026-07-25T10:00:00+00:00",
                "event": {},
                "snapshot": _snapshot(
                    token_id,
                    "2026-07-25T10:00:00+00:00",
                    bid=0.40,
                    ask=0.60,
                ),
            }
            for listener in self.listeners:
                listener(record)
            if self.is_old:
                old_seed_emitted.set()

    monkeypatch.setattr(
        "polybot.rules.forward.BookCache",
        RefreshBookCache,
    )
    service = ForwardBookService(config)
    service.sync([context])
    assert old_seed_started.wait(1)
    old_binding = service.store.latest_book_binding(context.market_id)
    assert old_binding is not None
    old_token = context.outcomes[0].yes_token_id

    new_outcome = replace(
        context.outcomes[0],
        condition_id="new-condition",
        yes_token_id="new-yes-token",
        no_token_id="new-no-token",
    )
    refreshed = replace(
        context,
        market_id="new-market",
        event_slug="new-event",
        outcomes=[new_outcome],
    )
    service.sync([refreshed])

    assert old_seed_emitted.wait(1)
    assert (
        service.store.latest_book_snapshot(
            binding_sha256=old_binding,
            token_id=old_token,
        )["source"]
        == "forward_recorder_store_missing"
    )
    service.stop()


def test_seed_progress_survives_locked_diagnostic_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path, context, _spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)

    class FailedSeedBookCache:
        def __init__(self, token_ids, **_kwargs):
            self.token_ids = list(token_ids)

        def add_listener(self, _listener):
            return None

        def start_ws(self):
            return None

        def stop_ws(self):
            return None

        def connection_state(self):
            return {"connected": True, "reconnects": 0}

        def rest_snapshot(self, _token_id):
            raise RuntimeError("book unavailable")

    monkeypatch.setattr(
        "polybot.rules.forward.BookCache",
        FailedSeedBookCache,
    )
    service = ForwardBookService(config)

    def locked_write(**_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        service.store,
        "record_operational_event",
        locked_write,
    )
    service.sync([context])
    assert service._seed_thread is not None
    service._seed_thread.join(timeout=2)

    status = service.status()
    assert status["rest_seed_total"] == 2
    assert status["rest_seed_completed"] == 2
    assert status["rest_seed_errors"] == 2
    assert status["rest_seed_in_progress"] is False
    assert status["storage_errors"] == 2
    service.stop()


def test_rest_seed_keeps_only_a_bounded_future_window(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path, context, _spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    base = context.outcomes[0]
    context = replace(
        context,
        outcomes=[
            replace(
                base,
                name=f"outcome-{index}",
                condition_id=f"condition-{index}",
                yes_token_id=f"yes-{index}",
                no_token_id=f"no-{index}",
            )
            for index in range(30)
        ],
    )

    class FastBookCache:
        def __init__(self, token_ids, **_kwargs):
            self.token_ids = list(token_ids)

        def add_listener(self, _listener):
            return None

        def start_ws(self):
            return None

        def stop_ws(self):
            return None

        def connection_state(self):
            return {"connected": True, "reconnects": 0}

        def rest_snapshot(self, _token_id):
            return None

    monkeypatch.setattr(
        "polybot.rules.forward.BookCache",
        FastBookCache,
    )
    service = ForwardBookService(config)
    service.sync([context])
    assert service._seed_thread is not None
    service._seed_thread.join(timeout=2)

    status = service.status()
    assert status["rest_seed_total"] == 60
    assert status["rest_seed_completed"] == 60
    assert (
        status["rest_seed_max_pending"]
        <= config.forward_recorder.rest_seed_workers * 2
    )
    service.stop()


def test_rest_seed_persists_not_found_cooldown_across_service_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path, context, _spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    calls: list[str] = []

    class MissingBookCache:
        def __init__(self, token_ids, **_kwargs):
            self.token_ids = list(token_ids)

        def add_listener(self, _listener):
            return None

        def start_ws(self):
            return None

        def stop_ws(self):
            return None

        def connection_state(self):
            return {"connected": True, "reconnects": 0}

        def rest_snapshot(self, token_id):
            calls.append(token_id)
            response = SimpleNamespace(status_code=404)
            error = RuntimeError("404 Client Error: Not Found")
            error.response = response
            raise error

    monkeypatch.setattr(
        "polybot.rules.forward.BookCache",
        MissingBookCache,
    )
    first = ForwardBookService(config)
    first.sync([context])
    assert first._seed_thread is not None
    first._seed_thread.join(timeout=2)
    first_status = first.status()
    assert first_status["rest_seed_errors"] == 2
    assert first_status["rest_seed_not_found"] == 2
    assert first_status["rest_seed_skipped_not_found"] == 0
    first.stop()

    second = ForwardBookService(config)
    second.sync([context])
    assert second._seed_thread is not None
    second._seed_thread.join(timeout=2)
    second_status = second.status()
    assert len(calls) == 2
    assert second_status["rest_seed_total"] == 2
    assert second_status["rest_seed_completed"] == 2
    assert second_status["rest_seed_errors"] == 0
    assert second_status["rest_seed_skipped_not_found"] == 2
    second.stop()


def test_live_status_snapshot_contains_connection_and_seed_metrics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path, context, _spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    config = replace(
        config,
        forward_recorder=replace(
            config.forward_recorder,
            rest_seed=False,
        ),
    )

    class ConnectedBookCache:
        def __init__(self, token_ids, **_kwargs):
            self.token_ids = list(token_ids)

        def add_listener(self, _listener):
            return None

        def start_ws(self):
            return None

        def stop_ws(self):
            return None

        def connection_state(self):
            return {"connected": True, "reconnects": 2}

    monkeypatch.setattr(
        "polybot.rules.forward.BookCache",
        ConnectedBookCache,
    )
    service = ForwardBookService(config)
    service.sync([context])
    service._publish_status_file()

    raw = json.loads(
        (config.data_dir / "forward_books_status.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw["selected_contexts"] == 1
    assert raw["tokens"] == 2
    assert raw["connections"] == 1
    assert raw["connected"] == 1
    assert raw["rest_seed_enabled"] is False
    assert raw["database_size_bytes"] > 0
    assert raw["database_size_gib"] >= 0
    assert raw["storage_paused"] is False
    assert raw["storage_dropped_events"] == 0
    assert raw["published_at"]
    service.stop()


def test_storage_status_forecasts_warning_and_hard_limit(
    tmp_path: Path,
) -> None:
    config_path, context, _spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    config = replace(
        config,
        forward_recorder=replace(
            config.forward_recorder,
            rest_seed=False,
            storage_warning_gib=2.0,
            storage_hard_limit_gib=3.0,
        ),
    )
    service = ForwardBookService(config)
    service.poll_once([context])
    current = service.store.database_size_bytes()
    with service._lock:
        service._storage_baseline_monotonic = time.monotonic() - 3_600.0
        service._storage_baseline_size_bytes = max(
            0,
            current - 1024**3,
        )
    service._refresh_storage_pressure(
        database_size_bytes=current,
        force=True,
    )

    status = service.status()
    assert status["storage_growth_gib_per_day"] > 0.0
    assert status["storage_forecast_observation_seconds"] >= 3_599.0
    assert status["storage_hours_to_warning"] is not None
    assert status["storage_hours_to_hard_limit"] is not None
    assert (
        status["storage_hours_to_warning"]
        < status["storage_hours_to_hard_limit"]
    )
    service.stop()


def test_shared_recorder_pauses_writes_at_storage_hard_limit(
    tmp_path: Path,
) -> None:
    config_path, context, spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    config = replace(
        config,
        forward_recorder=replace(
            config.forward_recorder,
            rest_seed=False,
            storage_warning_gib=0.000000001,
            storage_hard_limit_gib=0.000000002,
            storage_check_seconds=0.001,
        ),
    )
    service = ForwardBookService(config)
    status = service.poll_once([context])
    assert status["storage_warning"] is True
    assert status["storage_paused"] is True
    assert any(
        str(item).startswith("storage_hard_limit_reached:")
        for item in status["health_blockers"]
    )

    outcome = spec.outcomes[0]
    snapshot = _snapshot(
        outcome.yes_token_id,
        "2026-07-25T00:00:01+00:00",
        bid=0.78,
        ask=0.80,
    )
    service._on_stream_event(
        {
            "event_type": "book",
            "received_at": snapshot["received_at"],
            "source_at": snapshot["source_at"],
            "token_id": snapshot["token_id"],
            "event": {"event_type": "book"},
            "snapshot": snapshot,
        },
        generation=service._generation,
    )
    assert service.store.capture_status()["book_events"] == 0
    assert service.status()["storage_dropped_events"] == 1
    service.stop()


def test_live_health_alerts_once_and_reports_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path, context, _spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    config = replace(
        config,
        forward_recorder=replace(
            config.forward_recorder,
            rest_seed=False,
            health_startup_grace_seconds=0,
            health_stale_after_seconds=1,
            health_growth_window_seconds=1,
            health_alert_cooldown_seconds=300,
        ),
    )
    connection = {"connected": False}

    class HealthBookCache:
        def __init__(self, token_ids, **_kwargs):
            self.token_ids = list(token_ids)

        def add_listener(self, _listener):
            return None

        def start_ws(self):
            return None

        def stop_ws(self):
            return None

        def connection_state(self):
            return {
                "connected": connection["connected"],
                "reconnects": 0,
            }

    class Notifier:
        def __init__(self):
            self.messages = []

        def notify(self, message, **fields):
            self.messages.append((message, fields))

    monkeypatch.setattr(
        "polybot.rules.forward.BookCache",
        HealthBookCache,
    )
    monkeypatch.setattr(
        ForwardBookService,
        "_start_status_publisher",
        lambda self: None,
    )
    notifier = Notifier()
    service = ForwardBookService(config, notifier=notifier)
    service.sync([context])
    service._sync_started_monotonic -= 2

    unhealthy = service.status()
    assert unhealthy["healthy"] is False
    assert "disconnected_shards:0/1" in unhealthy["health_blockers"]
    assert "no_book_events" in unhealthy["health_blockers"]
    assert "book_event_growth_stalled" in unhealthy["health_blockers"]
    service._maybe_alert_health(unhealthy)
    service._maybe_alert_health(unhealthy)
    assert [item[0] for item in notifier.messages] == [
        "Forward recorder unhealthy"
    ]

    connection["connected"] = True
    token_id = context.outcomes[0].yes_token_id
    now = datetime.now(timezone.utc).isoformat()
    service._on_stream_event(
        {
            "event_type": "book",
            "token_id": token_id,
            "received_at": now,
            "source_at": now,
            "snapshot": _snapshot(
                token_id,
                now,
                bid=0.40,
                ask=0.60,
            ),
        },
        generation=service._generation,
    )
    recovered = service.status()
    assert recovered["healthy"] is True
    assert recovered["book_events_since_sync"] == 1
    assert recovered["book_event_rate_per_minute"] > 0
    assert recovered["latest_book_age_seconds"] is not None
    service._maybe_alert_health(recovered)
    assert [item[0] for item in notifier.messages] == [
        "Forward recorder unhealthy",
        "Forward recorder recovered",
    ]
    service.stop()


def test_gamma_resolution_sync_is_immutable_and_automatic(
    tmp_path: Path,
) -> None:
    config_path, context, _spec, _plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    records = [
        {
            "market_id": context.market_id,
            "outcome": context.outcomes[0].name,
            "resolved_yes": True,
        }
    ]
    assert record_forward_resolutions(
        config,
        records,
        observed_at="2026-09-30T23:59:59+00:00",
    ) == 1
    # Identical finalization is idempotent.
    assert record_forward_resolutions(
        config,
        records,
        observed_at="2026-09-30T23:59:59+00:00",
    ) == 1
    report = forward_completeness_report(
        config_path,
        context.market_id,
    )
    assert report["resolved_outcomes"] == 1
    with pytest.raises(ValueError, match="conflicting forward resolution"):
        record_forward_resolutions(
            config,
            [{**records[0], "resolved_yes": False}],
            observed_at="2026-10-01T00:00:00+00:00",
        )


def test_forward_binding_ignores_fee_observation_refresh_but_not_fee_policy(
    tmp_path: Path,
) -> None:
    config_path, context, spec, plan = _setup(tmp_path)
    config = load_discovery_config(config_path)
    store = ForwardRecorderStore(forward_recorder_db_path(config))
    first_context = replace(
        context,
        outcomes=[
            replace(
                context.outcomes[0],
                fee_schedule=explicit_zero_fee_schedule(
                    "2026-07-25T10:00:00+00:00"
                ),
            )
        ],
    )
    second_context = replace(
        first_context,
        outcomes=[
            replace(
                first_context.outcomes[0],
                fee_schedule=explicit_zero_fee_schedule(
                    "2026-07-25T11:00:00+00:00"
                ),
            )
        ],
    )
    first_binding = store.ensure_binding(
        first_context,
        spec,
        plan,
        config.forward_recorder,
    )
    assert first_binding == store.ensure_binding(
        second_context,
        spec,
        plan,
        config.forward_recorder,
    )
    assert first_binding == store.ensure_binding(
        second_context,
        spec,
        replace(plan, created_at="2026-07-25T12:00:00+00:00"),
        config.forward_recorder,
    )


def test_forward_recorder_config_rejects_unsafe_or_ambiguous_settings(
    tmp_path: Path,
) -> None:
    unsorted = tmp_path / "unsorted.yaml"
    unsorted.write_text(
        """
rule_compiler:
  enabled: true
rule_runner:
  enabled: true
forward_recorder:
  enabled: true
  quote_survival_horizons_ms: [1000, 100]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="sorted and unique"):
        load_discovery_config(unsorted)

    no_runner = tmp_path / "no-runner.yaml"
    no_runner.write_text(
        """
rule_compiler:
  enabled: true
forward_recorder:
  enabled: true
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requires rule_runner"):
        load_discovery_config(no_runner)

    bad_health = tmp_path / "bad-health.yaml"
    bad_health.write_text(
        """
rule_compiler:
  enabled: true
rule_runner:
  enabled: true
forward_recorder:
  enabled: true
  health_stale_after_seconds: 0
""",
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError,
        match="health_stale_after_seconds",
    ):
        load_discovery_config(bad_health)

    bad_storage = tmp_path / "bad-storage.yaml"
    bad_storage.write_text(
        """
rule_compiler:
  enabled: true
rule_runner:
  enabled: true
forward_recorder:
  enabled: true
  storage_warning_gib: 80
  storage_hard_limit_gib: 70
""",
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError,
        match="storage_warning_gib",
    ):
        load_discovery_config(bad_storage)

    bad_retry = tmp_path / "bad-retry.yaml"
    bad_retry.write_text(
        """
rule_compiler:
  enabled: true
rule_runner:
  enabled: true
forward_recorder:
  enabled: true
  rest_seed_not_found_retry_seconds: 0
""",
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError,
        match="rest_seed_not_found_retry_seconds",
    ):
        load_discovery_config(bad_retry)

    assert (
        ForwardRecorderConfig().quote_survival_horizons_ms
        == [100, 250, 500, 1000, 2000, 5000, 10000]
    )
    assert ForwardRecorderConfig().health_stale_after_seconds == 60.0
    assert ForwardRecorderConfig().storage_hard_limit_gib == 0.0
