"""Тесты дневной нагрузки по людям."""

from __future__ import annotations

import random
from datetime import date

from tests.conftest import dt

from flowlens.core.calendar import WorkCalendar
from flowlens.core.intervals import build_intervals
from flowlens.core.workload import accumulate_workload
from flowlens.testing.scenarios import all_scenarios, random_ticket
from flowlens.testing.synthetic import HOUR, WORKDAY, TicketBuilder


def load_for(seed, cal: WorkCalendar):
    intervals = build_intervals(seed.events, cal, now=seed.observed_at)
    return accumulate_workload(seed.key, intervals, cal), intervals


def test_handoff_splits_between_people(cal: WorkCalendar) -> None:
    """Тикет с двумя исполнителями делится между ними корректно."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "assignee_handoff")
    acc, _ = load_for(seed, cal)
    by_person: dict[str, int] = {}
    for (person, _day), load in acc.items():
        by_person[person] = by_person.get(person, 0) + load.owned_business_s
    assert by_person["ivan"] == WORKDAY
    assert by_person["maria"] == 2 * WORKDAY
    assert by_person["petr"] == 4 * HOUR


def test_sum_matches_interval_totals(cal: WorkCalendar) -> None:
    """Сумма по дням равна сумме по интервалам (закрытым, с исполнителем)."""
    for seed in all_scenarios(cal):
        acc, intervals = load_for(seed, cal)
        from_workload = sum(load.owned_business_s for load in acc.values())
        from_intervals = sum(
            iv.duration_business_s or 0
            for iv in intervals
            if iv.assignee is not None and iv.ended_at is not None
        )
        assert from_workload == from_intervals, seed.key


def test_interval_split_across_days(cal: WorkCalendar) -> None:
    """Интервал длиной несколько дней разносится по дням."""
    b = TicketBuilder(key="D-1", calendar=cal, created_at=dt(2026, 3, 4, 17), reporter="olga")
    b.assign("ivan").move_to("in progress")
    b.stay(2 * HOUR + WORKDAY + HOUR)  # ср 17-19, чт целиком, пт 10-11
    b.move_to("done")
    seed = b.build()
    acc, _ = load_for(seed, cal)
    days = {day: load.owned_business_s for (_p, day), load in acc.items()}
    assert days[date(2026, 3, 4)] == 2 * HOUR
    assert days[date(2026, 3, 5)] == WORKDAY
    assert days[date(2026, 3, 6)] == HOUR


def test_weekend_days_absent(cal: WorkCalendar) -> None:
    """Выходные не попадают в нагрузку."""
    b = TicketBuilder(key="D-2", calendar=cal, created_at=dt(2026, 3, 6, 18), reporter="olga")
    b.assign("ivan").move_to("in progress")
    b.stay(2 * HOUR)  # пт 18-19 + пн 10-11
    b.move_to("done")
    seed = b.build()
    acc, _ = load_for(seed, cal)
    days = {day for (_p, day), _load in acc.items()}
    assert date(2026, 3, 7) not in days
    assert date(2026, 3, 8) not in days
    assert days == {date(2026, 3, 6), date(2026, 3, 9)}


def test_touch_excludes_queue_statuses(cal: WorkCalendar) -> None:
    """touch считает только активную работу, owned — всё владение."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "with_blocking")
    acc, _ = load_for(seed, cal)
    total_owned = sum(load.owned_business_s for load in acc.values())
    total_touch = sum(load.touch_business_s for load in acc.values())
    total_blocked = sum(load.blocked_business_s for load in acc.values())
    assert total_touch < total_owned
    assert total_blocked == 3 * WORKDAY


def test_active_tickets_counted_once_per_day(cal: WorkCalendar) -> None:
    """Один тикет в один день считается один раз, даже при нескольких интервалах."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "with_rework")
    acc, _ = load_for(seed, cal)
    for load in acc.values():
        assert load.active_ticket_count == 1


def test_multiple_tickets_accumulate(cal: WorkCalendar) -> None:
    """Накопление по нескольким тикетам в один аккумулятор."""
    acc: dict = {}
    for i in range(3):
        b = TicketBuilder(
            key=f"M-{i}", calendar=cal, created_at=dt(2026, 3, 4, 10), reporter="olga"
        )
        b.assign("ivan").move_to("in progress").stay(2 * HOUR).move_to("done")
        seed = b.build()
        intervals = build_intervals(seed.events, cal, now=seed.observed_at)
        accumulate_workload(seed.key, intervals, cal, into=acc)
    load = acc[("ivan", date(2026, 3, 4))]
    assert load.active_ticket_count == 3
    assert load.owned_business_s == 6 * HOUR
    assert load.completed_count == 3


def test_completed_attributed_to_last_owner(cal: WorkCalendar) -> None:
    seed = next(s for s in all_scenarios(cal) if s.scenario == "assignee_handoff")
    acc, _ = load_for(seed, cal)
    completed = {p: load.completed_count for (p, _d), load in acc.items() if load.completed_count}
    assert completed == {"petr": 1}


def test_unassigned_time_ignored(cal: WorkCalendar) -> None:
    """Время без исполнителя не приписывается никому."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "happy_path")
    acc, _ = load_for(seed, cal)
    assert all(person is not None for (person, _day) in acc)


def test_open_interval_excluded(cal: WorkCalendar) -> None:
    """Незакрытый интервал не учитывается в дневной нагрузке."""
    seed = next(s for s in all_scenarios(cal) if s.scenario == "still_open")
    acc, intervals = load_for(seed, cal)
    assert intervals[-1].is_open
    total = sum(load.owned_business_s for load in acc.values())
    closed_owned = sum(
        iv.duration_business_s or 0
        for iv in intervals
        if iv.ended_at is not None and iv.assignee is not None
    )
    assert total == closed_owned


def test_random_workload_invariants(cal: WorkCalendar) -> None:
    rng = random.Random(11)
    acc: dict = {}
    for i in range(200):
        seed = random_ticket(cal, f"RND-{i}", dt(2026, 3, 2, 11), rng)
        intervals = build_intervals(seed.events, cal, now=seed.observed_at)
        accumulate_workload(seed.key, intervals, cal, into=acc)
    assert acc
    for load in acc.values():
        assert load.touch_business_s <= load.owned_business_s
        assert load.blocked_business_s <= load.owned_business_s
        assert load.active_ticket_count >= 1
        # в сутках не может быть больше рабочего времени, чем в календаре,
        # умноженного на число одновременно удерживаемых тикетов
        assert load.owned_business_s <= load.active_ticket_count * 9 * HOUR
