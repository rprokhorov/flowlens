"""Тесты построителя интервалов."""

from __future__ import annotations

import random

import pytest
from tests.conftest import dt

from flowlens.core.calendar import WorkCalendar
from flowlens.core.domain import Event, EventKind, Phase
from flowlens.core.intervals import (
    build_intervals,
    time_by_assignee,
    time_by_status,
)
from flowlens.testing.scenarios import all_scenarios, random_ticket
from flowlens.testing.synthetic import HOUR, WORKDAY, TicketBuilder

# --- сверка со всеми эталонными сценариями -----------------------------------


def test_all_scenarios_match_expected_status_time(cal: WorkCalendar) -> None:
    """Время по статусам совпадает с тем, что заложил генератор."""
    for seed in all_scenarios(cal):
        intervals = build_intervals(seed.events, cal, now=seed.observed_at)
        got = time_by_status(intervals)
        expected = {
            k.split(":", 1)[1]: v for k, v in seed.expected.items() if k.startswith("in_status:")
        }
        assert got == expected, f"{seed.key} ({seed.scenario})"


def test_intervals_are_contiguous(cal: WorkCalendar) -> None:
    """Интервалы стыкуются без разрывов и наложений."""
    for seed in all_scenarios(cal):
        intervals = build_intervals(seed.events, cal, now=seed.observed_at)
        assert intervals[0].started_at == seed.created_at
        for prev, nxt in zip(intervals, intervals[1:], strict=False):
            assert prev.ended_at == nxt.started_at, f"{seed.key}: gap at seq {nxt.seq}"


def test_seq_is_dense_and_ordered(cal: WorkCalendar) -> None:
    for seed in all_scenarios(cal):
        intervals = build_intervals(seed.events, cal, now=seed.observed_at)
        assert [iv.seq for iv in intervals] == list(range(1, len(intervals) + 1))


def test_only_last_interval_may_be_open(cal: WorkCalendar) -> None:
    for seed in all_scenarios(cal):
        intervals = build_intervals(seed.events, cal, now=seed.observed_at)
        for iv in intervals[:-1]:
            assert not iv.is_open, f"{seed.key}: interval {iv.seq} unexpectedly open"


def test_closed_ticket_has_no_open_interval(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "happy_path")
    intervals = build_intervals(seed.events, cal, now=seed.observed_at)
    assert intervals[-1].status == "done"
    assert intervals[-1].duration_business_s == 0


def test_open_ticket_has_open_interval(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "still_open")
    intervals = build_intervals(seed.events, cal, now=seed.observed_at)
    assert intervals[-1].is_open
    assert intervals[-1].status == "in progress"


# --- блокировки --------------------------------------------------------------


def test_blocked_interval_flagged(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "with_blocking")
    intervals = build_intervals(seed.events, cal, now=seed.observed_at)
    blocked = [iv for iv in intervals if iv.is_blocked]
    assert len(blocked) == 1
    assert blocked[0].phase == Phase.BLOCKED
    assert blocked[0].duration_business_s == 3 * WORKDAY


def test_blocked_from_status_recorded(cal: WorkCalendar) -> None:
    """Запоминается, из какого статуса ушли в блокировку."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "with_blocking")
    intervals = build_intervals(seed.events, cal, now=seed.observed_at)
    blocked = next(iv for iv in intervals if iv.is_blocked)
    assert blocked.blocked_from_status == "in progress"


def test_non_blocked_has_no_blocked_from(cal: WorkCalendar) -> None:
    for seed in all_scenarios(cal):
        for iv in build_intervals(seed.events, cal, now=seed.observed_at):
            if not iv.is_blocked:
                assert iv.blocked_from_status is None


# --- смена исполнителя -------------------------------------------------------


def test_assignee_change_splits_interval(cal: WorkCalendar) -> None:
    """Смена исполнителя рвёт интервал даже без смены статуса."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "assignee_handoff")
    intervals = build_intervals(seed.events, cal, now=seed.observed_at)
    # нулевые интервалы (назначение в момент перехода) отбрасываем
    in_progress = [
        iv for iv in intervals if iv.status == "in progress" and iv.duration_business_s
    ]
    assert len(in_progress) == 2
    assert in_progress[0].assignee == "ivan"
    assert in_progress[1].assignee == "maria"
    assert in_progress[0].duration_business_s == WORKDAY
    assert in_progress[1].duration_business_s == 2 * WORKDAY


def test_ownership_time_split_between_people(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "assignee_handoff")
    intervals = build_intervals(seed.events, cal, now=seed.observed_at)
    by_person = time_by_assignee(intervals)
    assert by_person["ivan"] == WORKDAY
    assert by_person["maria"] == 2 * WORKDAY
    # petr остаётся владельцем и в release: 3ч (qa) + 1ч (release)
    assert by_person["petr"] == 4 * HOUR


def test_unassigned_time_tracked(cal: WorkCalendar) -> None:
    """Время до первого назначения относится к None."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "happy_path")
    intervals = build_intervals(seed.events, cal, now=seed.observed_at)
    by_person = time_by_assignee(intervals)
    assert by_person[None] == 4 * HOUR


# --- переоткрытие ------------------------------------------------------------


def test_reopen_produces_second_done(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "reopened")
    intervals = build_intervals(seed.events, cal, now=seed.observed_at)
    done_intervals = [iv for iv in intervals if iv.status == "done"]
    assert len(done_intervals) == 2
    # первый "done" имеет длительность (тикет полежал закрытым), последний — нет
    assert done_intervals[0].duration_business_s == 2 * WORKDAY
    assert done_intervals[-1].duration_business_s == 0


# --- краевые случаи ----------------------------------------------------------


def test_empty_events_rejected(cal: WorkCalendar) -> None:
    with pytest.raises(ValueError, match="empty"):
        build_intervals([], cal)


def test_first_event_must_be_created(cal: WorkCalendar) -> None:
    events = [
        Event(kind=EventKind.STATUS_CHANGE, occurred_at=dt(2026, 3, 2, 11), new_value="in progress")
    ]
    with pytest.raises(ValueError, match="created"):
        build_intervals(events, cal)


def test_ticket_with_only_created_event(cal: WorkCalendar) -> None:
    events = [Event(kind=EventKind.CREATED, occurred_at=dt(2026, 3, 2, 11), new_value="new")]
    intervals = build_intervals(events, cal, now=dt(2026, 3, 2, 15))
    assert len(intervals) == 1
    assert intervals[0].status == "new"
    assert intervals[0].is_open
    assert intervals[0].duration_business_s == 4 * HOUR


def test_zero_duration_transitions(cal: WorkCalendar) -> None:
    """Мгновенное схлопывание (bulk move) не ломает построение."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "bulk_move")
    intervals = build_intervals(seed.events, cal, now=seed.observed_at)
    assert intervals[-1].status == "done"
    # весь путь от in progress до done занял 0 рабочих секунд
    assert sum(iv.duration_business_s or 0 for iv in intervals if iv.status != "new") == 0


def test_redundant_status_change_ignored(cal: WorkCalendar) -> None:
    """Переход в тот же статус не создаёт интервал."""
    base = dt(2026, 3, 2, 11)
    events = [
        Event(kind=EventKind.CREATED, occurred_at=base, new_value="new"),
        Event(
            kind=EventKind.STATUS_CHANGE,
            occurred_at=dt(2026, 3, 2, 12),
            old_value="new",
            new_value="new",
        ),
    ]
    intervals = build_intervals(events, cal, now=dt(2026, 3, 2, 15))
    assert len(intervals) == 1


def test_created_outside_working_hours(cal: WorkCalendar) -> None:
    """Тикет, созданный ночью, начинает копить время с утра."""
    events = [Event(kind=EventKind.CREATED, occurred_at=dt(2026, 3, 2, 3), new_value="new")]
    intervals = build_intervals(events, cal, now=dt(2026, 3, 2, 12))
    assert intervals[0].duration_business_s == 2 * HOUR  # 10:00-12:00
    assert intervals[0].duration_calendar_s == 9 * HOUR  # 03:00-12:00


def test_calendar_and_business_durations_differ_over_weekend(cal: WorkCalendar) -> None:
    b = TicketBuilder(key="W-1", calendar=cal, created_at=dt(2026, 3, 6, 18), reporter="olga")
    b.stay(2 * HOUR)  # пятница 18:00 -> понедельник 11:00
    b.move_to("in progress")
    seed = b.build()
    intervals = build_intervals(seed.events, cal, now=seed.observed_at)
    first = intervals[0]
    assert first.duration_business_s == 2 * HOUR
    assert first.duration_calendar_s > 2 * 24 * HOUR  # прошли выходные


# --- инварианты на массиве ---------------------------------------------------


def test_random_tickets_invariants(cal: WorkCalendar) -> None:
    rng = random.Random(2026)
    for i in range(300):
        seed = random_ticket(cal, f"RND-{i}", dt(2026, 3, 2, 11), rng)
        intervals = build_intervals(seed.events, cal, now=seed.observed_at)
        # стыковка
        for prev, nxt in zip(intervals, intervals[1:], strict=False):
            assert prev.ended_at == nxt.started_at
        # сумма по статусам равна сумме по людям
        assert sum(time_by_status(intervals).values()) == sum(
            time_by_assignee(intervals).values()
        )
        # рабочее время не превышает календарное
        for iv in intervals:
            if iv.duration_business_s is not None and iv.duration_calendar_s is not None:
                assert iv.duration_business_s <= iv.duration_calendar_s


def test_builder_is_deterministic(cal: WorkCalendar) -> None:
    """Повторный вызов даёт идентичный результат."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "with_rework")
    a = build_intervals(seed.events, cal, now=seed.observed_at)
    b = build_intervals(seed.events, cal, now=seed.observed_at)
    assert a == b


def test_shuffled_events_produce_same_result(cal: WorkCalendar) -> None:
    """Порядок событий на входе не важен — сортируем сами."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "with_rework")
    straight = build_intervals(seed.events, cal, now=seed.observed_at)
    shuffled = list(seed.events)
    random.Random(1).shuffle(shuffled)
    assert build_intervals(shuffled, cal, now=seed.observed_at) == straight
