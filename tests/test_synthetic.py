"""Тесты генератора синтетических данных."""

from __future__ import annotations

import random

import pytest
from tests.conftest import dt

from flowlens.core.calendar import WorkCalendar
from flowlens.core.domain import BOARD_BY_NAME, EventKind
from flowlens.testing.scenarios import all_scenarios, random_ticket
from flowlens.testing.synthetic import HOUR, WORKDAY, TicketBuilder


def test_all_scenarios_build(cal: WorkCalendar) -> None:
    seeds = all_scenarios(cal)
    assert len(seeds) == 10
    assert len({s.key for s in seeds}) == 10


def test_events_are_chronological(cal: WorkCalendar) -> None:
    for seed in all_scenarios(cal):
        times = [e.occurred_at for e in seed.events]
        assert times == sorted(times), f"{seed.key} events out of order"


def test_first_event_is_created(cal: WorkCalendar) -> None:
    for seed in all_scenarios(cal):
        assert seed.events[0].kind == EventKind.CREATED
        assert seed.events[0].occurred_at == seed.created_at


def test_source_event_ids_unique(cal: WorkCalendar) -> None:
    for seed in all_scenarios(cal):
        ids = [e.source_event_id for e in seed.events]
        assert len(ids) == len(set(ids))


def test_status_transitions_are_continuous(cal: WorkCalendar) -> None:
    """old_value каждого перехода равен new_value предыдущего."""
    for seed in all_scenarios(cal):
        current = "new"
        for ev in seed.events:
            if ev.kind == EventKind.STATUS_CHANGE:
                assert ev.old_value == current, f"{seed.key}: gap at {ev.occurred_at}"
                current = ev.new_value or current


def test_known_durations_happy_path(cal: WorkCalendar) -> None:
    """Явная сверка happy_path с расчётом вручную."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "happy_path")
    exp = seed.expected
    # new 4ч + in progress 2д + qa 1д + release 3ч
    assert exp["in_status:new"] == 4 * HOUR
    assert exp["in_status:in progress"] == 2 * WORKDAY
    assert exp["in_status:qa"] == WORKDAY
    assert exp["in_status:release"] == 3 * HOUR
    # touch = активные статусы = in progress + qa
    assert exp["touch_time_business_s"] == 2 * WORKDAY + WORKDAY
    # lead = всё время жизни
    assert exp["lead_time_business_s"] == 4 * HOUR + 2 * WORKDAY + WORKDAY + 3 * HOUR


def test_blocking_accumulates_blocked_time(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "with_blocking")
    assert seed.expected["blocked_time_business_s"] == 3 * WORKDAY
    assert seed.expected["blocked_episode_count"] == 1


def test_reopen_counted(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "reopened")
    assert seed.expected["reopen_count"] == 1


def test_handoff_counts_assignee_changes(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "assignee_handoff")
    # ivan -> maria -> petr, первое назначение не считается сменой
    assert seed.expected["assignee_change_count"] == 2


def test_bulk_move_has_zero_system_cycle(cal: WorkCalendar) -> None:
    """Ключевой проблемный случай: по changelog работа мгновенна."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "bulk_move")
    assert seed.expected["cycle_time_business_s"] == 0
    assert seed.expected["touch_time_business_s"] == 0
    # но заявленные даты говорят о четырёх днях
    assert len(seed.declared) == 2
    start = next(d for d in seed.declared if d.boundary == "work_start")
    end = next(d for d in seed.declared if d.boundary == "work_end")
    assert cal.business_seconds_between(start.value_at, end.value_at) == 4 * WORKDAY


def test_open_ticket_has_no_lead_time(cal: WorkCalendar) -> None:
    for name in ("still_open", "never_started"):
        seed = next(s for s in all_scenarios(cal) if s.scenario == name)
        assert seed.expected["lead_time_business_s"] is None
        assert seed.expected["cycle_time_business_s"] is None


def test_never_started_has_no_active_time(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "never_started")
    assert seed.expected["touch_time_business_s"] == 0
    assert seed.expected["in_status:new"] == 10 * WORKDAY


def test_broken_declared_order_detectable(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "broken_declared_order")
    start = next(d for d in seed.declared if d.boundary == "work_start")
    end = next(d for d in seed.declared if d.boundary == "work_end")
    assert end.value_at < start.value_at


def test_declared_before_created_detectable(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "declared_contradicts")
    start = next(d for d in seed.declared if d.boundary == "work_start")
    assert start.value_at < seed.created_at


def test_sum_of_statuses_equals_lifetime(cal: WorkCalendar) -> None:
    """Сумма времени по статусам равна времени жизни тикета."""
    for seed in all_scenarios(cal):
        by_status = sum(v for k, v in seed.expected.items() if k.startswith("in_status:"))
        lead = seed.expected["lead_time_business_s"]
        if lead is not None:
            assert by_status == lead, f"{seed.key}: {by_status} != {lead}"


def test_unknown_status_rejected(cal: WorkCalendar) -> None:
    b = TicketBuilder(key="X-1", calendar=cal, created_at=dt(2026, 3, 2), reporter="olga")
    with pytest.raises(ValueError, match="unknown status"):
        b.move_to("нет такого")


def test_random_tickets_are_valid(cal: WorkCalendar) -> None:
    rng = random.Random(42)
    for i in range(200):
        seed = random_ticket(cal, f"RND-{i}", dt(2026, 3, 2, 11), rng)
        times = [e.occurred_at for e in seed.events]
        assert times == sorted(times)
        current = "new"
        for ev in seed.events:
            if ev.kind == EventKind.STATUS_CHANGE:
                assert ev.old_value == current
                assert ev.new_value in BOARD_BY_NAME
                current = ev.new_value or current


def test_random_is_deterministic(cal: WorkCalendar) -> None:
    a = random_ticket(cal, "RND-1", dt(2026, 3, 2, 11), random.Random(7))
    b = random_ticket(cal, "RND-1", dt(2026, 3, 2, 11), random.Random(7))
    assert [e.occurred_at for e in a.events] == [e.occurred_at for e in b.events]
    assert a.expected == b.expected
