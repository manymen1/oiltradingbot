from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from polybot.core.budget import ClassifierBudgetStore
from polybot.core.config import ClassifierConfig


def test_shared_sqlite_budget_serializes_final_slots_across_bots(tmp_path) -> None:
    db_path = tmp_path / "fleet-budget.sqlite3"
    config = ClassifierConfig(
        max_escalations_per_hour=10,
        max_escalations_per_day=100,
        max_classifier_errors_per_hour=10,
        budget_db_path=str(db_path),
    )
    stores = [
        ClassifierBudgetStore(tmp_path / f"market-{index}", db_path)
        for index in range(30)
    ]

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        results = list(pool.map(lambda store: store.reserve_attempts(config), stores))

    assert results.count(None) == 10
    assert results.count("classifier_budget_exhausted_hourly") == 20
    status = stores[0].status(config)
    assert status["attempts_this_hour"] == 10
    assert status["remaining_this_hour"] == 0
    assert status["block_reason"] == "classifier_budget_exhausted_hourly"


def test_multi_pass_reservation_is_all_or_nothing(tmp_path) -> None:
    db_path = tmp_path / "fleet-budget.sqlite3"
    config = ClassifierConfig(
        max_escalations_per_hour=3,
        max_escalations_per_day=3,
        budget_db_path=str(db_path),
    )
    first = ClassifierBudgetStore(tmp_path / "one", db_path)
    second = ClassifierBudgetStore(tmp_path / "two", db_path)

    assert first.reserve_attempts(config, 2) is None
    assert second.reserve_attempts(config, 2) == "classifier_budget_exhausted_hourly"
    assert first.status(config)["attempts_this_hour"] == 2


def test_shared_error_cap_and_notification_dedupe(tmp_path) -> None:
    db_path = tmp_path / "fleet-budget.sqlite3"
    config = ClassifierConfig(
        max_escalations_per_hour=10,
        max_escalations_per_day=10,
        max_classifier_errors_per_hour=1,
        budget_db_path=str(db_path),
    )
    first = ClassifierBudgetStore(tmp_path / "one", db_path)
    second = ClassifierBudgetStore(tmp_path / "two", db_path)

    first.record_error()
    assert second.block_reason(config) == "classifier_error_cap_exceeded"
    assert first.mark_notified_once("classifier_error_cap_exceeded", "hour") is True
    assert second.mark_notified_once("classifier_error_cap_exceeded", "hour") is False


def test_standalone_budget_keeps_local_json_backend(tmp_path) -> None:
    config = ClassifierConfig(max_escalations_per_hour=2)
    store = ClassifierBudgetStore(tmp_path)

    assert store.reserve_attempts(config) is None
    assert store.status(config)["backend"] == "json"
    assert (tmp_path / "classifier_budget.json").exists()


def _priority_store(tmp_path, *, hourly=10):
    db_path = tmp_path / "priority-budget.sqlite3"
    config = ClassifierConfig(
        max_escalations_per_hour=hourly,
        max_escalations_per_day=100,
        budget_db_path=str(db_path),
    )
    store = ClassifierBudgetStore(
        tmp_path,
        db_path,
        priority_quotas=True,
    )
    return store, config


def test_priority_plan_allocates_exploitation_before_lower_rank(tmp_path) -> None:
    store, config = _priority_store(tmp_path)
    plan = store.configure_priority_allocations(
        config,
        exploitation=[
            ("high", "h"),
            ("medium", "m"),
            ("low", "l"),
            ("lowest", "x"),
        ],
        exploration=[],
        calls_per_group=2,
    )
    assert plan["exploitation"]["calls_allocated"] == 6
    assert (
        store.reserve_attempts(
            config,
            2,
            market_id="high",
            purpose="exploitation",
            reservation_id="high-article",
        )
        is None
    )
    assert store.reserve_attempts(
        config,
        2,
        market_id="lowest",
        purpose="exploitation",
        reservation_id="lowest-article",
    ) == "classifier_market_not_allocated_exploitation"


def test_cold_start_exploration_has_reserved_quota(tmp_path) -> None:
    store, config = _priority_store(tmp_path)
    store.configure_priority_allocations(
        config,
        exploitation=[],
        exploration=[("cold", "cold-hash"), ("later", "later-hash")],
        calls_per_group=2,
    )
    assert (
        store.reserve_attempts(
            config,
            2,
            market_id="cold",
            purpose="exploration",
            reservation_id="cold-article",
        )
        is None
    )
    assert store.reserve_attempts(
        config,
        2,
        market_id="later",
        purpose="exploration",
        reservation_id="later-article",
    ) == "classifier_market_not_allocated_exploration"


def test_system_reserve_keeps_two_pass_group_atomic(tmp_path) -> None:
    store, config = _priority_store(tmp_path)
    reason = store.reserve_attempts(
        config,
        2,
        market_id="compiler",
        purpose="system",
        reservation_id="compile-v1",
    )
    assert reason == "classifier_system_quota_exhausted_hourly"
    assert store.status(config)["attempts_this_hour"] == 0


def test_retry_reservation_does_not_double_count(tmp_path) -> None:
    store, config = _priority_store(tmp_path)
    store.configure_priority_allocations(
        config,
        exploitation=[("high", "hash")],
        exploration=[],
        calls_per_group=2,
    )
    kwargs = {
        "market_id": "high",
        "purpose": "exploitation",
        "priority_score_sha256": "hash",
        "reservation_id": "same-evaluation",
    }
    assert store.reserve_attempts(config, 2, **kwargs) is None
    assert store.reserve_attempts(config, 2, **kwargs) is None
    assert store.status(config)["attempts_this_hour"] == 2


def test_priority_reservations_remain_atomic_under_concurrency(tmp_path) -> None:
    store, config = _priority_store(tmp_path, hourly=20)
    store.configure_priority_allocations(
        config,
        exploitation=[("high", "hash")],
        exploration=[],
        calls_per_group=2,
    )

    def reserve(index):
        return store.reserve_attempts(
            config,
            2,
            market_id="high",
            purpose="exploitation",
            reservation_id=f"article-{index}",
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(reserve, range(20)))
    assert results.count(None) == 7
    assert store.status(config)["attempts_this_hour"] == 14


def test_admission_log_persists_identity_purpose_and_denial(tmp_path) -> None:
    store, config = _priority_store(tmp_path)
    store.configure_priority_allocations(
        config,
        exploitation=[("high", "priority-hash")],
        exploration=[],
        calls_per_group=2,
    )
    assert (
        store.reserve_attempts(
            config,
            2,
            market_id="high",
            purpose="exploitation",
            priority_score_sha256="priority-hash",
            reservation_id="admitted",
        )
        is None
    )
    store.reserve_attempts(
        config,
        2,
        market_id="missing",
        purpose="exploitation",
        priority_score_sha256="missing-hash",
        reservation_id="denied",
    )
    admissions = store.admissions()
    by_id = {item["reservation_id"]: item for item in admissions}
    assert by_id["admitted"]["admitted_calls"] == 2
    assert by_id["admitted"]["priority_score_sha256"] == "priority-hash"
    assert by_id["denied"]["admitted_calls"] == 0
    assert (
        by_id["denied"]["denial_reason"]
        == "classifier_market_not_allocated_exploitation"
    )


def test_one_hour_allocation_plan_is_immutable(tmp_path) -> None:
    store, config = _priority_store(tmp_path)
    first = store.configure_priority_allocations(
        config,
        exploitation=[("a", "a")],
        exploration=[],
    )
    second = store.configure_priority_allocations(
        config,
        exploitation=[("b", "b")],
        exploration=[],
    )
    assert first["exploitation"]["status"] == "CONFIGURED"
    assert (
        second["exploitation"]["status"]
        == "DEFERRED_EXISTING_WINDOW_PLAN"
    )
