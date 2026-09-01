"""Тесты согласования противоречивых сигналов о времени работы."""

from __future__ import annotations

import pytest
from tests.conftest import dt

from flowlens.core.calendar import WorkCalendar
from flowlens.core.reconciliation import (
    Anomaly,
    Confidence,
    ReconciliationPolicy,
    Source,
    TimelineSignals,
    reconcile,
)

HOUR = 3600
WORKDAY = 9 * HOUR


@pytest.fixture
def policy() -> ReconciliationPolicy:
    return ReconciliationPolicy()


def signals(**overrides) -> TimelineSignals:
    """Сигналы по умолчанию: аккуратно заполненный завершённый тикет."""
    base = {
        "created_at": dt(2026, 3, 2, 11),
        "system_start": dt(2026, 3, 2, 14),
        "system_end": dt(2026, 3, 5, 16),
        "declared_start": dt(2026, 3, 2, 14),
        "declared_end": dt(2026, 3, 5, 16),
        "is_completed": True,
        "transition_times": [dt(2026, 3, 2, 14), dt(2026, 3, 4, 12), dt(2026, 3, 5, 16)],
    }
    base.update(overrides)
    return TimelineSignals(**base)  # type: ignore[arg-type]


NOW = dt(2026, 3, 10, 12)


# --- согласованные данные ----------------------------------------------------


def test_matching_dates_use_declared(cal: WorkCalendar, policy) -> None:
    start, end = reconcile(signals(), policy, cal, now=NOW)
    assert start.source == Source.DECLARED
    assert start.confidence == Confidence.HIGH
    assert start.effective_at == dt(2026, 3, 2, 14)
    assert start.anomalies == set()
    assert end.effective_at == dt(2026, 3, 5, 16)


def test_small_discrepancy_tolerated(cal: WorkCalendar, policy) -> None:
    """Расхождение меньше порога не считается противоречием."""
    start, _end = reconcile(
        signals(declared_start=dt(2026, 3, 2, 16)), policy, cal, now=NOW
    )
    assert start.source == Source.DECLARED
    assert start.confidence == Confidence.HIGH
    assert Anomaly.DECLARED_CONTRADICTS_CHANGELOG not in start.anomalies
    assert start.discrepancy_business_s == 2 * HOUR


# --- отсутствующие даты ------------------------------------------------------


def test_missing_declared_falls_back_to_system(cal: WorkCalendar, policy) -> None:
    start, _end = reconcile(
        signals(declared_start=None, declared_end=None), policy, cal, now=NOW
    )
    assert start.source == Source.SYSTEM
    assert start.effective_at == dt(2026, 3, 2, 14)
    assert start.confidence == Confidence.MEDIUM
    assert Anomaly.DECLARED_MISSING in start.anomalies


def test_partial_declared_flagged(cal: WorkCalendar, policy) -> None:
    """Заполнено только начало — тикет попадает в отчёт о качестве."""
    start, end = reconcile(signals(declared_end=None), policy, cal, now=NOW)
    assert Anomaly.DECLARED_ONLY_PARTIAL in start.anomalies
    assert Anomaly.DECLARED_ONLY_PARTIAL in end.anomalies


def test_no_signals_at_all(cal: WorkCalendar, policy) -> None:
    """Тикет без активной работы и без дат: считать нечего."""
    start, _end = reconcile(
        signals(system_start=None, declared_start=None, is_completed=False),
        policy,
        cal,
        now=NOW,
    )
    assert start.source == Source.NONE
    assert start.effective_at is None
    assert Anomaly.NO_SYSTEM_SIGNAL in start.anomalies


def test_completed_without_work_flagged(cal: WorkCalendar, policy) -> None:
    """Закрыт, но никогда не был в работе — подозрительно."""
    start, _end = reconcile(
        signals(system_start=None, declared_start=None, is_completed=True),
        policy,
        cal,
        now=NOW,
    )
    assert Anomaly.COMPLETED_WITHOUT_WORK in start.anomalies


# --- невозможные даты --------------------------------------------------------


def test_declared_before_created_rejected(cal: WorkCalendar, policy) -> None:
    """Работа не могла начаться раньше создания тикета."""
    start, _end = reconcile(
        signals(declared_start=dt(2026, 2, 16, 10)), policy, cal, now=NOW
    )
    assert Anomaly.DECLARED_BEFORE_CREATED in start.anomalies
    assert start.source == Source.SYSTEM
    assert start.effective_at == dt(2026, 3, 2, 14)
    assert start.confidence == Confidence.LOW


def test_declared_in_future_rejected(cal: WorkCalendar, policy) -> None:
    start, _end = reconcile(
        signals(declared_start=dt(2026, 4, 1, 10)), policy, cal, now=NOW
    )
    assert Anomaly.DECLARED_AFTER_NOW in start.anomalies
    assert start.source == Source.SYSTEM


def test_impossible_date_without_system_infers_created(cal: WorkCalendar, policy) -> None:
    start, _end = reconcile(
        signals(declared_start=dt(2026, 2, 1, 10), system_start=None),
        policy,
        cal,
        now=NOW,
    )
    assert start.source == Source.INFERRED
    assert start.effective_at == dt(2026, 3, 2, 11)
    assert start.confidence == Confidence.LOW


def test_end_before_start_falls_back_to_system(cal: WorkCalendar, policy) -> None:
    start, end = reconcile(
        signals(declared_start=dt(2026, 3, 5, 10), declared_end=dt(2026, 3, 3, 10)),
        policy,
        cal,
        now=NOW,
    )
    assert Anomaly.END_BEFORE_START in start.anomalies
    assert Anomaly.END_BEFORE_START in end.anomalies
    assert start.effective_at == dt(2026, 3, 2, 14)  # системные значения
    assert end.effective_at == dt(2026, 3, 5, 16)
    assert start.confidence == Confidence.LOW


# --- противоречие с changelog ------------------------------------------------


def test_large_discrepancy_flagged_but_declared_wins(cal: WorkCalendar, policy) -> None:
    """По умолчанию верим человеку, но помечаем расхождение."""
    start, _end = reconcile(
        signals(system_start=dt(2026, 3, 5, 15), declared_start=dt(2026, 3, 2, 11)),
        policy,
        cal,
        now=NOW,
    )
    assert Anomaly.DECLARED_CONTRADICTS_CHANGELOG in start.anomalies
    assert start.source == Source.DECLARED
    assert start.confidence == Confidence.LOW


def test_policy_can_prefer_system_on_conflict(cal: WorkCalendar) -> None:
    """Команда может настроить обратное поведение."""
    strict = ReconciliationPolicy(on_conflict="use_system_flag_anomaly")
    start, _end = reconcile(
        signals(system_start=dt(2026, 3, 5, 15), declared_start=dt(2026, 3, 2, 11)),
        strict,
        cal,
        now=NOW,
    )
    assert start.source == Source.SYSTEM
    assert start.effective_at == dt(2026, 3, 5, 15)


def test_system_only_policy_ignores_declared(cal: WorkCalendar) -> None:
    strict = ReconciliationPolicy(prefer="system_only")
    start, _end = reconcile(
        signals(declared_start=dt(2026, 3, 1, 11)), strict, cal, now=NOW
    )
    assert start.source == Source.SYSTEM
    assert start.effective_at == dt(2026, 3, 2, 14)


def test_threshold_is_configurable(cal: WorkCalendar) -> None:
    lenient = ReconciliationPolicy(discrepancy_threshold_business_days=10)
    start, _end = reconcile(
        signals(system_start=dt(2026, 3, 5, 15), declared_start=dt(2026, 3, 2, 11)),
        lenient,
        cal,
        now=NOW,
    )
    assert Anomaly.DECLARED_CONTRADICTS_CHANGELOG not in start.anomalies
    assert start.confidence == Confidence.HIGH


# --- дневная точность --------------------------------------------------------


def test_day_precision_start_at_workday_start(cal: WorkCalendar, policy) -> None:
    """«Начал 2 марта» → начало рабочего дня, а не полночь."""
    start, _end = reconcile(
        signals(
            declared_start=dt(2026, 3, 2, 0),
            declared_start_precision="day",
        ),
        policy,
        cal,
        now=NOW,
    )
    # рабочий день начинается в 10:00, но тикет создан в 11:00 —
    # работа не могла начаться раньше создания
    assert start.effective_at == dt(2026, 3, 2, 11)


def test_day_precision_end_at_workday_end(cal: WorkCalendar, policy) -> None:
    _start, end = reconcile(
        signals(
            declared_end=dt(2026, 3, 5, 0),
            declared_end_precision="day",
        ),
        policy,
        cal,
        now=NOW,
    )
    assert end.effective_at == dt(2026, 3, 5, 19)


def test_day_precision_on_weekend_start_moves_forward(cal: WorkCalendar, policy) -> None:
    """Заявлено начало в субботу — берём ближайшее рабочее время."""
    start, _end = reconcile(
        signals(
            declared_start=dt(2026, 3, 7, 0),  # суббота
            declared_start_precision="day",
            system_start=dt(2026, 3, 9, 11),
            declared_end=None,
            system_end=dt(2026, 3, 10, 11),
        ),
        policy,
        cal,
        now=NOW,
    )
    assert start.effective_at is not None
    assert start.effective_at.weekday() < 5


def test_minute_precision_unchanged(cal: WorkCalendar, policy) -> None:
    start, _end = reconcile(
        signals(declared_start=dt(2026, 3, 2, 14, 37)), policy, cal, now=NOW
    )
    assert start.effective_at == dt(2026, 3, 2, 14, 37)


# --- аномалии уровня тикета --------------------------------------------------


def test_bulk_move_detected(cal: WorkCalendar, policy) -> None:
    """Все переходы за полминуты — пятничная разгребка доски."""
    base = dt(2026, 3, 6, 18, 45)
    start, end = reconcile(
        signals(
            transition_times=[base, base.replace(second=15), base.replace(second=30)],
            system_start=base,
            system_end=base.replace(second=30),
            declared_start=None,
            declared_end=None,
        ),
        policy,
        cal,
        now=NOW,
    )
    assert Anomaly.BULK_MOVE in start.anomalies
    assert Anomaly.BULK_MOVE in end.anomalies
    assert start.confidence == Confidence.LOW


def test_bulk_move_not_triggered_by_normal_flow(cal: WorkCalendar, policy) -> None:
    start, _end = reconcile(signals(), policy, cal, now=NOW)
    assert Anomaly.BULK_MOVE not in start.anomalies


def test_zero_duration_lifecycle_detected(cal: WorkCalendar, policy) -> None:
    """Тикет создан и закрыт вне рабочего времени: работы не было."""
    start, _end = reconcile(
        signals(
            system_start=dt(2026, 3, 7, 12),  # суббота
            system_end=dt(2026, 3, 7, 13),
            declared_start=None,
            declared_end=None,
            transition_times=[dt(2026, 3, 7, 12), dt(2026, 3, 7, 13)],
        ),
        policy,
        cal,
        now=NOW,
    )
    assert Anomaly.ZERO_DURATION_LIFECYCLE in start.anomalies


def test_bulk_move_with_declared_keeps_declared(cal: WorkCalendar, policy) -> None:
    """Ключевой сценарий: changelog схлопнут, но человек заявил реальные даты."""
    base = dt(2026, 3, 6, 18, 45)
    start, end = reconcile(
        signals(
            transition_times=[base, base.replace(second=15), base.replace(second=30)],
            system_start=base,
            system_end=base.replace(second=30),
            declared_start=dt(2026, 3, 2, 0),
            declared_end=dt(2026, 3, 5, 0),
            declared_start_precision="day",
            declared_end_precision="day",
        ),
        policy,
        cal,
        now=NOW,
    )
    assert Anomaly.BULK_MOVE in start.anomalies
    assert start.source == Source.DECLARED
    assert start.effective_at == dt(2026, 3, 2, 11)  # прижато к моменту создания
    assert end.effective_at == dt(2026, 3, 5, 19)
    # именно ради этого случая строился слой согласования:
    # changelog говорил о 30 секундах работы, заявленные даты — о четырёх днях
    worked = cal.business_seconds_between(start.effective_at, end.effective_at)
    assert worked == 4 * WORKDAY - HOUR  # минус час до создания тикета


# --- версионирование ---------------------------------------------------------


def test_policy_version_recorded(cal: WorkCalendar, policy) -> None:
    start, end = reconcile(signals(), policy, cal, now=NOW)
    assert start.policy_version == policy.version
    assert end.policy_version == policy.version


def test_custom_policy_version(cal: WorkCalendar) -> None:
    custom = ReconciliationPolicy(version="team-a-2.0")
    start, _end = reconcile(signals(), custom, cal, now=NOW)
    assert start.policy_version == "team-a-2.0"
