"""Генератор синтетических тикетов с заранее известными метриками.

Принцип: длительности задаются в РАБОЧИХ секундах, календарь переводит их
в моменты времени. Поэтому ожидаемые метрики известны по построению и
построитель интервалов можно проверить до секунды.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from flowlens.core.calendar import WorkCalendar
from flowlens.core.domain import (
    BOARD_BY_NAME,
    Comment,
    DeclaredDate,
    Event,
    EventKind,
    TicketSeed,
)

HOUR = 3600
WORKDAY = 9 * HOUR


@dataclass
class Step:
    """Шаг пути тикета: побыть в статусе N рабочих секунд."""

    status: str
    business_seconds: int
    assignee: str | None = None


@dataclass
class TicketBuilder:
    """Пошаговое построение тикета вдоль рабочего календаря."""

    key: str
    calendar: WorkCalendar
    created_at: datetime
    reporter: str
    issue_type: str = "Task"
    priority: str = "Medium"
    initial_assignee: str | None = None
    # предел, за который не должен уходить курсор («сейчас»)
    horizon: datetime | None = None

    _cursor: datetime = field(init=False)
    _status: str = field(init=False, default="new")
    _assignee: str | None = field(init=False, default=None)
    _events: list[Event] = field(init=False, default_factory=list)
    _comments: list[Comment] = field(init=False, default_factory=list)
    _declared: list[DeclaredDate] = field(init=False, default_factory=list)
    _seq: int = field(init=False, default=0)
    # накопители ожидаемых метрик, в рабочих секундах
    _by_status: dict[str, int] = field(init=False, default_factory=dict)
    _first_active_at: datetime | None = field(init=False, default=None)
    _reopens: int = field(init=False, default=0)
    _assignee_changes: int = field(init=False, default=0)
    _blocked_episodes: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._cursor = self.created_at
        self._assignee = self.initial_assignee
        self._events.append(
            Event(
                kind=EventKind.CREATED,
                occurred_at=self.created_at,
                actor=self.reporter,
                new_value="new",
                source_event_id=self._next_id(),
            )
        )

    @property
    def cursor(self) -> datetime:
        """Текущий момент построения тикета."""
        return self._cursor

    def _next_id(self) -> str:
        self._seq += 1
        return f"{self.key}-ev{self._seq}"

    def _advance(self, business_seconds: int) -> None:
        """Продвинуть курсор на N рабочих секунд, засчитав их текущему статусу."""
        if business_seconds <= 0:
            return
        target = self.calendar.add_business_seconds(self._cursor, business_seconds)
        if self.horizon is not None and target > self.horizon:
            # упёрлись в «сейчас»: засчитываем только то время, что реально прошло
            target = max(self._cursor, self.horizon)
            business_seconds = self.calendar.business_seconds_between(self._cursor, target)
            if business_seconds <= 0:
                return
        self._by_status[self._status] = self._by_status.get(self._status, 0) + business_seconds
        self._cursor = target

    def stay(self, business_seconds: int) -> TicketBuilder:
        """Пробыть в текущем статусе."""
        self._advance(business_seconds)
        return self

    def past_horizon(self) -> bool:
        """Курсор достиг предела генерации."""
        return self.horizon is not None and self._cursor >= self.horizon

    def move_to(self, status: str, actor: str | None = None) -> TicketBuilder:
        """Перейти в другой статус.

        За горизонтом переходы не записываются: событий в будущем быть не может.
        """
        if status not in BOARD_BY_NAME:
            raise ValueError(f"unknown status: {status}")
        if self.past_horizon():
            return self
        previous = self._status
        if BOARD_BY_NAME[previous].is_terminal and not BOARD_BY_NAME[status].is_terminal:
            self._reopens += 1
        if BOARD_BY_NAME[status].phase.value == "blocked":
            self._blocked_episodes += 1

        self._events.append(
            Event(
                kind=EventKind.STATUS_CHANGE,
                occurred_at=self._cursor,
                actor=actor or self._assignee or self.reporter,
                field_name="status",
                old_value=previous,
                new_value=status,
                source_event_id=self._next_id(),
            )
        )
        self._status = status
        if BOARD_BY_NAME[status].is_active_work and self._first_active_at is None:
            self._first_active_at = self._cursor
        return self

    def assign(self, person: str) -> TicketBuilder:
        """Сменить исполнителя."""
        if person == self._assignee or self.past_horizon():
            return self
        self._events.append(
            Event(
                kind=EventKind.ASSIGNEE_CHANGE,
                occurred_at=self._cursor,
                actor=person,
                field_name="assignee",
                old_value=self._assignee,
                new_value=person,
                source_event_id=self._next_id(),
            )
        )
        if self._assignee is not None:
            self._assignee_changes += 1
        self._assignee = person
        return self

    def comment(self, author: str, body: str, offset_seconds: int = 0) -> TicketBuilder:
        """Оставить комментарий (не двигает курсор)."""
        at = (
            self.calendar.add_business_seconds(self._cursor, offset_seconds)
            if offset_seconds
            else self._cursor
        )
        self._comments.append(Comment(author=author, created_at=at, body=body))
        self._events.append(
            Event(
                kind=EventKind.COMMENT,
                occurred_at=at,
                actor=author,
                source_event_id=self._next_id(),
            )
        )
        return self

    def declare(
        self, boundary: str, at: datetime, precision: str = "minute"
    ) -> TicketBuilder:
        """Проставить заявленную вручную дату."""
        field_name = "customfield_start" if boundary == "work_start" else "customfield_end"
        self._declared.append(
            DeclaredDate(
                boundary=boundary,
                value_at=at,
                precision=precision,
                source_field=field_name,
            )
        )
        return self

    def declare_from_actual(self, precision: str = "minute") -> TicketBuilder:
        """Заявить даты, совпадающие с реальными переходами (аккуратный сотрудник)."""
        if self._first_active_at:
            self.declare("work_start", self._first_active_at, precision)
        if BOARD_BY_NAME[self._status].is_terminal:
            self.declare("work_end", self._cursor, precision)
        return self

    def build(self, scenario: str = "", **extra: object) -> TicketSeed:
        active = sum(
            secs for st, secs in self._by_status.items() if BOARD_BY_NAME[st].is_active_work
        )
        queue = sum(secs for st, secs in self._by_status.items() if BOARD_BY_NAME[st].is_queue)
        blocked = sum(
            secs
            for st, secs in self._by_status.items()
            if BOARD_BY_NAME[st].phase.value == "blocked"
        )
        release_wait = self._by_status.get("release", 0)
        is_done = BOARD_BY_NAME[self._status].is_terminal

        lead = (
            self.calendar.business_seconds_between(self.created_at, self._cursor)
            if is_done
            else None
        )
        cycle = (
            self.calendar.business_seconds_between(self._first_active_at, self._cursor)
            if is_done and self._first_active_at
            else None
        )
        denom = active + queue
        expected: dict[str, int | float | None] = {
            "lead_time_business_s": lead,
            "cycle_time_business_s": cycle,
            "touch_time_business_s": active,
            "queue_time_business_s": queue,
            "blocked_time_business_s": blocked,
            "release_wait_business_s": release_wait,
            "flow_efficiency": round(active / denom, 6) if denom else None,
            "reopen_count": self._reopens,
            "assignee_change_count": self._assignee_changes,
            "blocked_episode_count": self._blocked_episodes,
            "status_change_count": sum(
                1 for e in self._events if e.kind == EventKind.STATUS_CHANGE
            ),
        }
        expected.update({f"in_status:{k}": v for k, v in self._by_status.items()})

        return TicketSeed(
            key=self.key,
            issue_type=self.issue_type,
            priority=self.priority,
            created_at=self.created_at,
            reporter=self.reporter,
            events=sorted(self._events, key=lambda e: e.occurred_at),
            declared=self._declared,
            comments=self._comments,
            scenario=scenario,
            observed_at=self._cursor,
            expected=expected,
            **extra,  # type: ignore[arg-type]
        )
