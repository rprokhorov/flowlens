"""Свёртка event log в интервалы.

Интервал — это отрезок жизни тикета, на котором неизменны статус и исполнитель.
Рвётся при смене любого из них. Из intervals считается всё остальное:
CFD, WIP, время по фазам, нагрузка людей, flow efficiency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from flowlens.core.calendar import WorkCalendar
from flowlens.core.domain import BOARD_BY_NAME, Event, EventKind, Phase


@dataclass
class Interval:
    """Отрезок, на котором статус и исполнитель постоянны."""

    seq: int
    status: str
    phase: Phase
    assignee: str | None
    is_blocked: bool
    blocked_from_status: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_calendar_s: int | None
    duration_business_s: int | None

    @property
    def is_open(self) -> bool:
        return self.ended_at is None


def build_intervals(
    events: list[Event],
    calendar: WorkCalendar,
    *,
    initial_status: str = "new",
    now: datetime | None = None,
) -> list[Interval]:
    """Построить интервалы из упорядоченного списка событий.

    Учитываются только события, меняющие статус или исполнителя;
    комментарии и прочие изменения полей игнорируются.

    Последний интервал остаётся открытым, если тикет не в терминальном статусе.
    Если передан `now`, для открытого интервала считается длительность на этот момент.
    """
    if not events:
        raise ValueError("event list is empty")

    ordered = sorted(events, key=lambda e: (e.occurred_at, _kind_rank(e.kind)))
    created = ordered[0]
    if created.kind != EventKind.CREATED:
        raise ValueError("first event must be 'created'")

    status = initial_status
    assignee: str | None = None
    blocked_from: str | None = None
    started = created.occurred_at

    intervals: list[Interval] = []
    seq = 0

    def close(at: datetime) -> None:
        nonlocal seq, started
        # нулевые интервалы сохраняются: мгновенный проход через статус —
        # это факт (признак bulk move), важный для анализа качества данных
        seq += 1
        intervals.append(
            _make_interval(
                seq=seq,
                status=status,
                assignee=assignee,
                blocked_from=blocked_from,
                started_at=started,
                ended_at=at,
                calendar=calendar,
            )
        )
        started = at

    for ev in ordered[1:]:
        if ev.kind == EventKind.STATUS_CHANGE and ev.new_value:
            if ev.new_value == status:
                continue
            close(ev.occurred_at)
            previous = status
            status = ev.new_value
            if _is_blocked(status):
                # запоминаем, откуда ушли в блокировку
                blocked_from = previous if not _is_blocked(previous) else blocked_from
            else:
                blocked_from = None
        elif ev.kind == EventKind.ASSIGNEE_CHANGE:
            if ev.new_value == assignee:
                continue
            close(ev.occurred_at)
            assignee = ev.new_value

    # финальный интервал
    seq += 1
    is_terminal = BOARD_BY_NAME[status].is_terminal
    intervals.append(
        _make_interval(
            seq=seq,
            status=status,
            assignee=assignee,
            blocked_from=blocked_from,
            started_at=started,
            ended_at=None if not is_terminal else None,
            calendar=calendar,
            open_until=now if not is_terminal else None,
            terminal=is_terminal,
        )
    )
    return intervals


def _kind_rank(kind: EventKind) -> int:
    """Порядок обработки событий с одинаковым timestamp.

    Смена исполнителя раньше смены статуса: при переводе в qa с переназначением
    новый статус должен уже принадлежать новому исполнителю.
    """
    order = {
        EventKind.CREATED: 0,
        EventKind.ASSIGNEE_CHANGE: 1,
        EventKind.STATUS_CHANGE: 2,
    }
    return order.get(kind, 3)


def _is_blocked(status: str) -> bool:
    return BOARD_BY_NAME[status].phase == Phase.BLOCKED


def _make_interval(
    *,
    seq: int,
    status: str,
    assignee: str | None,
    blocked_from: str | None,
    started_at: datetime,
    ended_at: datetime | None,
    calendar: WorkCalendar,
    open_until: datetime | None = None,
    terminal: bool = False,
) -> Interval:
    blocked = _is_blocked(status)
    finish = ended_at or open_until
    if terminal:
        # терминальный статус завершает жизнь тикета: длительности нет
        cal_s: int | None = 0
        bus_s: int | None = 0
    elif finish is not None:
        cal_s = int((finish - started_at).total_seconds())
        bus_s = calendar.business_seconds_between(started_at, finish)
    else:
        cal_s = None
        bus_s = None

    return Interval(
        seq=seq,
        status=status,
        phase=BOARD_BY_NAME[status].phase,
        assignee=assignee,
        is_blocked=blocked,
        blocked_from_status=blocked_from if blocked else None,
        started_at=started_at,
        ended_at=ended_at,
        duration_calendar_s=cal_s,
        duration_business_s=bus_s,
    )


def time_by_status(intervals: list[Interval]) -> dict[str, int]:
    """Суммарное рабочее время по статусам."""
    out: dict[str, int] = {}
    for iv in intervals:
        if iv.duration_business_s:
            out[iv.status] = out.get(iv.status, 0) + iv.duration_business_s
    return out


def time_by_assignee(intervals: list[Interval]) -> dict[str | None, int]:
    """Суммарное рабочее время владения по людям."""
    out: dict[str | None, int] = {}
    for iv in intervals:
        if iv.duration_business_s:
            out[iv.assignee] = out.get(iv.assignee, 0) + iv.duration_business_s
    return out


__all__ = ["Interval", "build_intervals", "time_by_assignee", "time_by_status"]
