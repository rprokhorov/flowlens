"""Тесты расчёта метрик тикета."""

from __future__ import annotations

import random

import pytest
from tests.conftest import dt

from flowlens.core.calendar import WorkCalendar
from flowlens.core.intervals import build_intervals
from flowlens.core.metrics import TicketMetrics, compute_metrics, percentile
from flowlens.testing.scenarios import all_scenarios, random_ticket
from flowlens.testing.synthetic import HOUR, WORKDAY, TicketBuilder

COMPARED = (
    "lead_time_business_s",
    "cycle_time_business_s",
    "touch_time_business_s",
    "queue_time_business_s",
    "blocked_time_business_s",
    "release_wait_business_s",
    "flow_efficiency",
    "reopen_count",
    "assignee_change_count",
    "blocked_episode_count",
    "status_change_count",
)


def metrics_for(seed, cal: WorkCalendar) -> TicketMetrics:
    intervals = build_intervals(seed.events, cal, now=seed.observed_at)
    return compute_metrics(
        created_at=seed.created_at,
        intervals=intervals,
        events=seed.events,
        comments=seed.comments,
        calendar=cal,
        reporter=seed.reporter,
    )


# --- сверка со всеми сценариями ----------------------------------------------


@pytest.mark.parametrize("field", COMPARED)
def test_metrics_match_expected(cal: WorkCalendar, field: str) -> None:
    for seed in all_scenarios(cal):
        m = metrics_for(seed, cal)
        assert getattr(m, field) == seed.expected.get(field), f"{seed.key}.{field}"


# --- отдельные метрики -------------------------------------------------------


def test_lead_greater_than_cycle_when_queued(cal: WorkCalendar) -> None:
    """Lead включает ожидание в бэклоге, cycle — нет."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "happy_path")
    m = metrics_for(seed, cal)
    assert m.lead_time_business_s is not None and m.cycle_time_business_s is not None
    assert m.lead_time_business_s - m.cycle_time_business_s == 4 * HOUR


def test_release_included_in_cycle_time(cal: WorkCalendar) -> None:
    """Команда отвечает «от и до»: release входит в cycle time."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "happy_path")
    m = metrics_for(seed, cal)
    assert m.release_wait_business_s == 3 * HOUR
    assert m.cycle_time_business_s == 2 * WORKDAY + WORKDAY + 3 * HOUR


def test_flow_efficiency_formula(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "with_blocking")
    m = metrics_for(seed, cal)
    expected = m.touch_time_business_s / (m.touch_time_business_s + m.queue_time_business_s)
    assert m.flow_efficiency == pytest.approx(expected)
    assert m.flow_efficiency < 0.5  # блокировка съела больше половины


def test_blocked_time_counted_in_queue(cal: WorkCalendar) -> None:
    """Блокировка — это ожидание, входит в queue time."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "with_blocking")
    m = metrics_for(seed, cal)
    assert m.blocked_time_business_s == 3 * WORKDAY
    assert m.queue_time_business_s >= m.blocked_time_business_s


def test_open_ticket_has_no_lead_or_cycle(cal: WorkCalendar) -> None:
    for scenario in ("still_open", "never_started"):
        seed = next(s for s in all_scenarios(cal) if s.scenario == scenario)
        m = metrics_for(seed, cal)
        assert m.lead_time_business_s is None
        assert m.cycle_time_business_s is None
        assert not m.is_completed


def test_never_started_has_no_work_start(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "never_started")
    m = metrics_for(seed, cal)
    assert m.work_started_at is None
    assert m.touch_time_business_s == 0
    assert m.flow_efficiency == 0.0


def test_bulk_move_yields_zero_cycle(cal: WorkCalendar) -> None:
    """Ключевой случай: по changelog работа мгновенна — метрика заведомо ложна."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "bulk_move")
    m = metrics_for(seed, cal)
    assert m.cycle_time_business_s == 0
    assert m.touch_time_business_s == 0
    assert m.is_completed


def test_reopen_metrics(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "reopened")
    m = metrics_for(seed, cal)
    assert m.reopen_count == 1
    assert m.is_completed


def test_time_by_person_sums_to_total(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "assignee_handoff")
    m = metrics_for(seed, cal)
    assert sum(m.time_by_person.values()) <= sum(m.time_by_status.values())
    assert set(m.time_by_person) == {"ivan", "maria", "petr"}


def test_first_response_from_non_reporter(cal: WorkCalendar) -> None:
    b = TicketBuilder(key="C-1", calendar=cal, created_at=dt(2026, 3, 2, 11), reporter="olga")
    b.stay(HOUR)
    b.comment("olga", "уточнение от автора")  # не считается ответом
    b.stay(HOUR)
    b.comment("ivan", "беру в работу")  # первый ответ
    b.move_to("in progress")
    seed = b.build()
    m = metrics_for(seed, cal)
    assert m.first_response_business_s == 2 * HOUR


def test_first_response_none_without_comments(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "happy_path")
    m = metrics_for(seed, cal)
    assert m.first_response_business_s is None


def test_calendar_time_exceeds_business_time(cal: WorkCalendar) -> None:
    """Календарное время всегда не меньше рабочего."""
    for seed in all_scenarios(cal):
        m = metrics_for(seed, cal)
        if m.lead_time_calendar_s is not None and m.lead_time_business_s is not None:
            assert m.lead_time_calendar_s >= m.lead_time_business_s


# --- инварианты на массиве ---------------------------------------------------


def test_random_tickets_metric_invariants(cal: WorkCalendar) -> None:
    rng = random.Random(99)
    completed = 0
    for i in range(300):
        seed = random_ticket(cal, f"RND-{i}", dt(2026, 3, 2, 11), rng)
        m = metrics_for(seed, cal)
        assert m.touch_time_business_s >= 0
        assert m.blocked_time_business_s <= m.queue_time_business_s
        if m.flow_efficiency is not None:
            assert 0.0 <= m.flow_efficiency <= 1.0
        if m.is_completed:
            completed += 1
            assert m.lead_time_business_s is not None
            assert m.lead_time_business_s >= (m.cycle_time_business_s or 0)
    assert completed > 200  # большинство должно доезжать до done


# --- перцентили --------------------------------------------------------------


def test_percentile_nearest_rank() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert percentile(values, 50) == 5.0
    assert percentile(values, 85) == 9.0
    assert percentile(values, 100) == 10.0


def test_percentile_returns_actual_observation() -> None:
    """Без интерполяции: результат — реально наблюдавшееся значение."""
    values = [10.0, 20.0, 100.0]
    assert percentile(values, 50) in values


def test_percentile_empty_is_none() -> None:
    assert percentile([], 50) is None


def test_percentile_single_value() -> None:
    assert percentile([42.0], 95) == 42.0


def test_percentile_invalid_rejected() -> None:
    with pytest.raises(ValueError, match="percentile"):
        percentile([1.0], 0)
    with pytest.raises(ValueError, match="percentile"):
        percentile([1.0], 101)
