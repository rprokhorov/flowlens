"""Выгрузка накопленных данных обратно в формат контракта.

Нужна, чтобы показать конкретный срез человеку, у которого нет доступа ни
к базе, ни к трекеру: файл открывается импортом на другой машине.

Выгрузка по умолчанию обезличена. Ключи и заголовки задач — внутренняя
информация, и файл, уехавший из контура, забирает её с собой; поэтому полный
вариант включается явным флагом, а не молчаливо.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from flowlens.analytics import Filters, _ticket_conditions, _where
from flowlens.contract import (
    RawDeclaredDate,
    RawEvent,
    RawPerson,
    RawTicket,
    SourceProfile,
    write_ndjson,
)

@dataclass
class ExportStats:
    tickets: int
    events: int
    anonymized: bool


def _pseudonym(value: str, salt: str, prefix: str) -> str:
    """Устойчивый псевдоним: одинаковый вход даёт одинаковый выход.

    Связи между задачами и авторство переходов сохраняются, но восстановить
    исходное значение по файлу нельзя.
    """
    digest = hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()
    return f"{prefix}-{digest[:8]}"


def export_tickets(
    engine: Engine,
    filters: Filters,
    output: Path,
    *,
    anonymize: bool = True,
    salt: str | None = None,
) -> ExportStats:
    """Собрать тикеты с историей и записать в NDJSON контракта."""
    params: dict[str, Any] = {}
    conditions = _ticket_conditions(filters, params)

    tickets_query = f"""
        SELECT t.id, t.external_key, t.external_id, t.project_key, t.issue_type,
               t.is_subtask, t.priority, t.summary, t.components, t.labels,
               t.created_at, t.resolved_at, t.closed_at, t.story_points,
               ws.external_name AS status,
               reporter.display_name AS reporter,
               assignee.display_name AS assignee,
               parent.external_key AS parent_key,
               epic.external_key AS epic_key
        FROM ticket t
        LEFT JOIN workflow_status ws ON ws.id = t.current_status_id
        LEFT JOIN person reporter ON reporter.id = t.reporter_id
        LEFT JOIN person assignee ON assignee.id = t.current_assignee_id
        LEFT JOIN ticket parent ON parent.id = t.parent_id
        LEFT JOIN ticket epic ON epic.id = t.epic_id
        {_where(conditions)}
        ORDER BY t.created_at
    """
    with engine.begin() as conn:
        rows = conn.execute(text(tickets_query), params).all()
        if not rows:
            return ExportStats(tickets=0, events=0, anonymized=anonymize)

        ids = [r.id for r in rows]
        events = conn.execute(
            text(
                "SELECT e.ticket_id, CAST(e.kind AS text) AS kind, e.occurred_at, "
                "       e.field, e.old_value, e.new_value, e.source_event_id, "
                "       p.display_name AS actor, "
                "       old_st.external_name AS old_status, "
                "       new_st.external_name AS new_status "
                "FROM ticket_event e "
                "LEFT JOIN person p ON p.id = e.actor_person_id "
                "LEFT JOIN workflow_status old_st ON old_st.id = e.old_status_id "
                "LEFT JOIN workflow_status new_st ON new_st.id = e.new_status_id "
                "WHERE e.ticket_id = ANY(:ids) "
                "ORDER BY e.ticket_id, e.occurred_at"
            ),
            {"ids": ids},
        ).all()
        declared = conn.execute(
            text(
                "SELECT ticket_id, boundary, value_at, precision, source_field "
                "FROM ticket_declared_date WHERE ticket_id = ANY(:ids)"
            ),
            {"ids": ids},
        ).all()

    events_by_ticket: dict[int, list[Any]] = {}
    for row in events:
        events_by_ticket.setdefault(row.ticket_id, []).append(row)
    declared_by_ticket: dict[int, list[Any]] = {}
    for row in declared:
        declared_by_ticket.setdefault(row.ticket_id, []).append(row)

    # соль привязана к выгрузке: два файла нельзя сопоставить между собой
    seed = salt or datetime.now().isoformat()

    def person(name: str | None) -> str | None:
        if name is None:
            return None
        return _pseudonym(name, seed, "user") if anonymize else name

    def key(value: str | None) -> str | None:
        if value is None:
            return None
        return _pseudonym(value, seed, "TASK") if anonymize else value

    tickets: list[RawTicket] = []

    for row in rows:
        ticket_people: set[str] = set()
        raw_events: list[RawEvent] = []
        for e in events_by_ticket.get(row.id, []):
            actor = person(e.actor)
            if actor:
                ticket_people.add(actor)
            # значения статусов нужны как есть: на них держится вся модель фаз
            old_value = e.old_status or (None if anonymize else e.old_value)
            new_value = e.new_status or (None if anonymize else e.new_value)
            if e.kind == "assignee_change":
                old_value = person(e.old_value)
                new_value = person(e.new_value)
            raw_events.append(
                RawEvent(
                    kind=e.kind,
                    occurred_at=e.occurred_at,
                    actor=actor,
                    field=e.field,
                    old_value=old_value,
                    new_value=new_value,
                    source_event_id=e.source_event_id,
                )
            )

        raw_declared = [
            RawDeclaredDate(
                boundary=d.boundary,
                value_at=d.value_at,
                precision=d.precision,
                source_field=d.source_field,
            )
            for d in declared_by_ticket.get(row.id, [])
        ]

        reporter = person(row.reporter)
        assignee = person(row.assignee)
        for name in (reporter, assignee):
            if name:
                ticket_people.add(name)

        tickets.append(
            RawTicket(
                external_key=key(row.external_key) or row.external_key,
                external_id=None if anonymize else row.external_id,
                project_key="PROJ" if anonymize else row.project_key,
                issue_type=row.issue_type,
                is_subtask=row.is_subtask,
                priority=row.priority,
                status=row.status or "new",
                summary="" if anonymize else (row.summary or ""),
                components=[] if anonymize else list(row.components or []),
                labels=[] if anonymize else list(row.labels or []),
                created_at=row.created_at,
                resolved_at=row.resolved_at,
                closed_at=row.closed_at,
                reporter=reporter,
                assignee=assignee,
                parent_key=key(row.parent_key),
                epic_key=key(row.epic_key),
                story_points=float(row.story_points) if row.story_points else None,
                events=raw_events,
                declared_dates=raw_declared,
                # люди перечисляются на своём тикете, а не общим списком:
                # иначе один и тот же человек попал бы в каждую строку файла
                people=[
                    RawPerson(external_id=name, display_name=name)
                    for name in sorted(ticket_people)
                ],
            )
        )

    profile = SourceProfile(
        source_kind="flowlens_export",
        source_name="flowlens-export",
        changelog="full",
        exported_at=datetime.now(UTC),
        ticket_count=len(tickets),
    )
    written = write_ndjson(output, tickets, profile)
    return ExportStats(
        tickets=written,
        events=sum(len(t.events) for t in tickets),
        anonymized=anonymize,
    )


__all__ = ["ExportStats", "export_tickets"]
