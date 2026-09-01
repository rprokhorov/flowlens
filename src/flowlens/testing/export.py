"""Экспорт синтетических тикетов в формат контракта.

Позволяет проверить весь путь коллектор → NDJSON → импорт → пересчёт
без доступа к Jira.
"""

from __future__ import annotations

from datetime import datetime

from flowlens.contract import (
    CONTRACT_VERSION,
    RawComment,
    RawDeclaredDate,
    RawEvent,
    RawPerson,
    RawTicket,
    SourceProfile,
)
from flowlens.core.domain import BOARD_BY_NAME, EventKind, TicketSeed


def seed_to_raw_ticket(seed: TicketSeed) -> RawTicket:
    """Преобразовать синтетический тикет в формат контракта."""
    people: dict[str, RawPerson] = {}

    def remember(name: str | None) -> str | None:
        if not name:
            return None
        people.setdefault(
            name,
            RawPerson(
                external_id=name,
                display_name=name.capitalize(),
                email=f"{name}@example.com",
            ),
        )
        return name

    remember(seed.reporter)

    events: list[RawEvent] = []
    current_status = "new"
    assignee: str | None = None

    for event in seed.events:
        remember(event.actor)
        if event.kind == EventKind.STATUS_CHANGE and event.new_value:
            current_status = event.new_value
        if event.kind == EventKind.ASSIGNEE_CHANGE:
            assignee = event.new_value
            remember(event.new_value)

        events.append(
            RawEvent(
                kind=event.kind.value,  # type: ignore[arg-type]
                occurred_at=event.occurred_at,
                actor=event.actor,
                field=event.field_name,
                old_value=event.old_value,
                new_value=event.new_value,
                source_event_id=event.source_event_id,
            )
        )

    resolved_at: datetime | None = None
    if BOARD_BY_NAME[current_status].is_terminal:
        terminal_events = [
            e
            for e in seed.events
            if e.kind == EventKind.STATUS_CHANGE
            and e.new_value
            and BOARD_BY_NAME[e.new_value].is_terminal
        ]
        if terminal_events:
            resolved_at = terminal_events[-1].occurred_at

    return RawTicket(
        external_key=seed.key,
        project_key=seed.key.split("-")[0],
        issue_type=seed.issue_type,
        is_subtask=seed.is_subtask,
        priority=seed.priority,
        status=current_status,
        summary=seed.summary,
        components=seed.components,
        labels=seed.labels,
        created_at=seed.created_at,
        resolved_at=resolved_at,
        updated_at=seed.observed_at,
        reporter=seed.reporter,
        assignee=assignee,
        parent_key=seed.parent_key,
        epic_key=seed.epic_key,
        story_points=seed.story_points,
        events=events,
        declared_dates=[
            RawDeclaredDate(
                boundary=d.boundary,  # type: ignore[arg-type]
                value_at=d.value_at,
                precision=d.precision,  # type: ignore[arg-type]
                source_field=d.source_field,
            )
            for d in seed.declared
        ],
        comments=[
            RawComment(
                external_id=f"{seed.key}-c{i}",
                author=remember(c.author),
                created_at=c.created_at,
                body=c.body,
                is_internal=c.is_internal,
            )
            for i, c in enumerate(seed.comments)
        ],
        people=list(people.values()),
        raw_fields={"scenario": seed.scenario},
    )


def synthetic_profile(source_name: str = "synthetic", ticket_count: int = 0) -> SourceProfile:
    """Профиль синтетического источника."""
    return SourceProfile(
        contract_version=CONTRACT_VERSION,
        source_kind="synthetic",
        source_name=source_name,
        changelog="full",
        reconciliation_hint="prefer_declared",
        declared_dates={
            "work_start": {"field": "customfield_start", "precision": "minute"},
            "work_end": {"field": "customfield_end", "precision": "minute"},
        },
        exported_at=datetime.now().astimezone(),
        ticket_count=ticket_count,
    )


__all__ = ["seed_to_raw_ticket", "synthetic_profile"]
