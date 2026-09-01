"""Доменные типы ядра: фазы, статусы, события.

Не зависят от БД — используются и генератором, и построителем интервалов.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Phase(StrEnum):
    """Канонические фазы, к которым приводятся статусы любого источника."""

    BACKLOG = "backlog"
    TRIAGE = "triage"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    REVIEW = "review"
    VERIFY = "verify"
    DONE_PENDING = "done_pending"
    DONE = "done"
    CANCELLED = "cancelled"


class EventKind(StrEnum):
    CREATED = "created"
    STATUS_CHANGE = "status_change"
    ASSIGNEE_CHANGE = "assignee_change"
    FIELD_CHANGE = "field_change"
    LINK_CHANGE = "link_change"
    FLAG_CHANGE = "flag_change"
    COMMENT = "comment"
    RESOLVED = "resolved"
    REOPENED = "reopened"


@dataclass(frozen=True)
class StatusDef:
    """Определение статуса доски."""

    name: str
    phase: Phase
    is_active_work: bool
    is_queue: bool
    is_terminal: bool = False
    board_order: int = 0


# Доска команды пользователя (см. PLAN.md).
# qa = фактически code review, тесты автоматические → активная работа.
# release включён в cycle time: команда отвечает «от и до».
BOARD: tuple[StatusDef, ...] = (
    StatusDef("new", Phase.BACKLOG, is_active_work=False, is_queue=True, board_order=1),
    StatusDef("in progress", Phase.IN_PROGRESS, is_active_work=True, is_queue=False, board_order=2),
    StatusDef("blocked/hold", Phase.BLOCKED, is_active_work=False, is_queue=True, board_order=3),
    StatusDef("qa", Phase.VERIFY, is_active_work=True, is_queue=False, board_order=4),
    StatusDef("release", Phase.DONE_PENDING, is_active_work=False, is_queue=True, board_order=5),
    StatusDef(
        "done", Phase.DONE, is_active_work=False, is_queue=False, is_terminal=True, board_order=6
    ),
)

BOARD_BY_NAME: dict[str, StatusDef] = {s.name: s for s in BOARD}

ACTIVE_STATUSES: frozenset[str] = frozenset(s.name for s in BOARD if s.is_active_work)
TERMINAL_STATUSES: frozenset[str] = frozenset(s.name for s in BOARD if s.is_terminal)


@dataclass
class Event:
    """Событие изменения тикета (запись changelog)."""

    kind: EventKind
    occurred_at: datetime
    actor: str | None = None
    field_name: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    source_event_id: str | None = None


@dataclass
class DeclaredDate:
    """Заявленная вручную дата (start date / end date в Jira)."""

    boundary: str  # 'work_start' | 'work_end'
    value_at: datetime
    precision: str = "minute"  # 'day' | 'minute'
    source_field: str | None = None


@dataclass
class Comment:
    author: str
    created_at: datetime
    body: str
    is_internal: bool = False


@dataclass
class TicketSeed:
    """Полный набор данных одного синтетического тикета."""

    key: str
    issue_type: str
    priority: str
    created_at: datetime
    reporter: str
    events: list[Event] = field(default_factory=list)
    declared: list[DeclaredDate] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    is_subtask: bool = False
    parent_key: str | None = None
    epic_key: str | None = None
    story_points: float | None = None
    summary: str = ""
    scenario: str = ""
    # момент, на который сгенерирован тикет (аналог "сейчас" для открытых интервалов)
    observed_at: datetime | None = None
    # ожидаемые значения для сверки в тестах (только для синтетики)
    expected: dict[str, int | float | None] = field(default_factory=dict)


__all__ = [
    "ACTIVE_STATUSES",
    "BOARD",
    "BOARD_BY_NAME",
    "TERMINAL_STATUSES",
    "Comment",
    "DeclaredDate",
    "Event",
    "EventKind",
    "Phase",
    "StatusDef",
    "TicketSeed",
]
