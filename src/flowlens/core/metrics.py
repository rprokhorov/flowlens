"""Расчёт метрик тикета поверх интервалов.

Все длительности считаются и в календарных, и в рабочих секундах.
Cycle time включает release: команда отвечает за задачу «от и до».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from flowlens.core.calendar import WorkCalendar
from flowlens.core.domain import (
    BOARD_BY_NAME,
    Comment,
    Event,
    EventKind,
    Phase,
    StatusDef,
)
from flowlens.core.intervals import Interval


@dataclass
class TicketMetrics:
    """Метрики одного тикета."""

    lead_time_calendar_s: int | None = None
    lead_time_business_s: int | None = None
    cycle_time_calendar_s: int | None = None
    cycle_time_business_s: int | None = None
    touch_time_business_s: int = 0
    queue_time_business_s: int = 0
    blocked_time_business_s: int = 0
    release_wait_business_s: int = 0
    flow_efficiency: float | None = None
    reopen_count: int = 0
    assignee_change_count: int = 0
    status_change_count: int = 0
    blocked_episode_count: int = 0
    first_response_business_s: int | None = None
    work_started_at: datetime | None = None
    completed_at: datetime | None = None
    is_completed: bool = False
    time_by_status: dict[str, int] = field(default_factory=dict)
    time_by_person: dict[str, int] = field(default_factory=dict)


def compute_metrics(
    *,
    created_at: datetime,
    intervals: list[Interval],
    events: list[Event],
    comments: list[Comment],
    calendar: WorkCalendar,
    reporter: str | None = None,
    board: dict[str, StatusDef] | None = None,
) -> TicketMetrics:
    """Посчитать метрики тикета.

    `board` задаёт классификацию статусов команды: от того, считается ли
    статус активной работой, зависят touch time и эффективность потока.
    """
    board = board or BOARD_BY_NAME
    m = TicketMetrics()

    for iv in intervals:
        secs = iv.duration_business_s or 0
        spec = board[iv.status]
        if secs:
            m.time_by_status[iv.status] = m.time_by_status.get(iv.status, 0) + secs
            if iv.assignee:
                m.time_by_person[iv.assignee] = m.time_by_person.get(iv.assignee, 0) + secs
        if spec.is_active_work:
            m.touch_time_business_s += secs
        if spec.is_queue:
            m.queue_time_business_s += secs
        if iv.is_blocked:
            m.blocked_time_business_s += secs
        if spec.phase == Phase.DONE_PENDING:
            m.release_wait_business_s += secs

    # первый вход в активную работу
    for iv in intervals:
        if board[iv.status].is_active_work:
            m.work_started_at = iv.started_at
            break

    # завершение = начало последнего терминального интервала
    last = intervals[-1]
    if board[last.status].is_terminal:
        m.is_completed = True
        m.completed_at = last.started_at

    if m.is_completed and m.completed_at is not None:
        m.lead_time_calendar_s = int((m.completed_at - created_at).total_seconds())
        m.lead_time_business_s = calendar.business_seconds_between(created_at, m.completed_at)
        if m.work_started_at is not None:
            m.cycle_time_calendar_s = int((m.completed_at - m.work_started_at).total_seconds())
            m.cycle_time_business_s = calendar.business_seconds_between(
                m.work_started_at, m.completed_at
            )

    denominator = m.touch_time_business_s + m.queue_time_business_s
    m.flow_efficiency = (
        round(m.touch_time_business_s / denominator, 6) if denominator else None
    )

    m.blocked_episode_count = _count_blocked_episodes(intervals)
    m.status_change_count = sum(1 for e in events if e.kind == EventKind.STATUS_CHANGE)
    m.assignee_change_count = _count_assignee_changes(events)
    m.reopen_count = _count_reopens(events, board)
    m.first_response_business_s = _first_response(created_at, comments, calendar, reporter)
    return m


def _count_blocked_episodes(intervals: list[Interval]) -> int:
    """Число эпизодов блокировки (подряд идущие интервалы — один эпизод)."""
    count = 0
    previous_blocked = False
    for iv in intervals:
        if iv.is_blocked and not previous_blocked:
            count += 1
        previous_blocked = iv.is_blocked
    return count


def _count_assignee_changes(events: list[Event]) -> int:
    """Смены исполнителя; первое назначение не считается сменой."""
    changes = 0
    for ev in events:
        if ev.kind == EventKind.ASSIGNEE_CHANGE and ev.old_value:
            changes += 1
    return changes


def _count_reopens(
    events: list[Event], board: dict[str, StatusDef] | None = None
) -> int:
    """Выходы из терминального статуса обратно в работу."""
    reopens = 0
    for ev in events:
        if ev.kind != EventKind.STATUS_CHANGE or not ev.old_value or not ev.new_value:
            continue
        was_terminal = (board or BOARD_BY_NAME)[ev.old_value].is_terminal
        now_terminal = (board or BOARD_BY_NAME)[ev.new_value].is_terminal
        if was_terminal and not now_terminal:
            reopens += 1
    return reopens


def _first_response(
    created_at: datetime,
    comments: list[Comment],
    calendar: WorkCalendar,
    reporter: str | None,
) -> int | None:
    """Время до первого комментария не от автора тикета."""
    candidates = [
        c for c in sorted(comments, key=lambda c: c.created_at) if c.author != reporter
    ]
    if not candidates:
        return None
    return calendar.business_seconds_between(created_at, candidates[0].created_at)


def percentile(values: list[float], p: float) -> float | None:
    """Перцентиль методом ближайшего ранга (без интерполяции).

    Для распределений cycle time интерполяция даёт значения, которых
    не было в реальности, поэтому берём фактическое наблюдение.
    """
    if not values:
        return None
    if not 0 < p <= 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered = sorted(values)
    from math import ceil

    rank = max(1, ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


__all__ = ["TicketMetrics", "compute_metrics", "percentile"]
