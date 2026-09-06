"""Дневная нагрузка по людям.

Атрибуция по времени владения: интервал режется по календарным дням,
каждому дню достаётся его доля рабочих секунд.

Метрика показывает распределение нагрузки (кто перегружен, у кого затык),
а не производительность отдельного человека.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from flowlens.core.calendar import WorkCalendar, business_seconds_by_day
from flowlens.core.domain import BOARD_BY_NAME, StatusDef
from flowlens.core.intervals import Interval


@dataclass
class DailyLoad:
    """Нагрузка одного человека за один день."""

    person: str
    day: date
    owned_business_s: int = 0
    touch_business_s: int = 0
    blocked_business_s: int = 0
    active_tickets: set[str] = field(default_factory=set)
    completed_count: int = 0

    @property
    def active_ticket_count(self) -> int:
        return len(self.active_tickets)


def accumulate_workload(
    ticket_key: str,
    intervals: list[Interval],
    calendar: WorkCalendar,
    into: dict[tuple[str, date, int], DailyLoad] | None = None,
    board: dict[str, StatusDef] | None = None,
    team_id: int = 0,
) -> dict[tuple[str, date, int], DailyLoad]:
    """Разложить владение тикетом по людям, дням и командам.

    Команда входит в ключ, потому что человек может работать в нескольких:
    платформенный разработчик, помогающий продуктовой команде, должен
    считаться в каждой отдельно, иначе строки перетирают друг друга.

    Результат накапливается в `into`, чтобы собирать по многим тикетам.
    """
    board = board or BOARD_BY_NAME
    acc = into if into is not None else {}

    for iv in intervals:
        if iv.assignee is None or iv.ended_at is None:
            continue
        spec = board[iv.status]
        per_day = business_seconds_by_day(calendar, iv.started_at, iv.ended_at)
        for day, seconds in per_day.items():
            key = (iv.assignee, day, team_id)
            load = acc.get(key)
            if load is None:
                load = DailyLoad(person=iv.assignee, day=day)
                acc[key] = load
            load.owned_business_s += seconds
            load.active_tickets.add(ticket_key)
            if spec.is_active_work:
                load.touch_business_s += seconds
            if iv.is_blocked:
                load.blocked_business_s += seconds

    # завершение тикета засчитывается последнему владельцу
    last = intervals[-1]
    if board[last.status].is_terminal:
        owner = _last_owner(intervals)
        if owner is not None:
            day = last.started_at.astimezone(calendar.zone).date()
            key = (owner, day, team_id)
            load = acc.get(key)
            if load is None:
                load = DailyLoad(person=owner, day=day)
                acc[key] = load
            load.completed_count += 1

    return acc


def _last_owner(intervals: list[Interval]) -> str | None:
    """Последний непустой исполнитель."""
    for iv in reversed(intervals):
        if iv.assignee is not None:
            return iv.assignee
    return None


def open_intervals_at(intervals: list[Interval], moment: date) -> list[Interval]:
    """Интервалы, действующие на указанную дату."""
    out = []
    for iv in intervals:
        start_day = iv.started_at.date()
        end_day = iv.ended_at.date() if iv.ended_at else None
        if start_day <= moment and (end_day is None or moment <= end_day):
            out.append(iv)
    return out


__all__ = ["DailyLoad", "accumulate_workload", "open_intervals_at"]
