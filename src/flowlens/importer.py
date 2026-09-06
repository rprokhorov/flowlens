"""Импорт данных контракта в базу.

Идемпотентность обеспечивается естественными ключами:
тикет — (source, external_key), событие — (ticket, source_event_id, field).
Повторный импорт той же выгрузки не создаёт дублей.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, text

from flowlens.contract import RawTicket, SourceProfile, iter_ndjson
from flowlens.core.domain import BOARD, Phase
from flowlens.core.people import PersonIdentity, PersonResolver

log = logging.getLogger(__name__)

# Статусы, не описанные в конфиге, попадают сюда, чтобы импорт не падал.
UNKNOWN_PHASE = Phase.TRIAGE


@dataclass
class ImportStats:
    tickets: int = 0
    events: int = 0
    comments: int = 0
    declared: int = 0
    links: int = 0
    people: int = 0
    unknown_statuses: set[str] = field(default_factory=set)

    def summary(self) -> str:
        parts = [
            f"{self.tickets} тикетов",
            f"{self.events} событий",
            f"{self.comments} комментариев",
            f"{self.declared} заявленных дат",
            f"{self.people} человек",
        ]
        if self.unknown_statuses:
            parts.append(f"неизвестных статусов: {len(self.unknown_statuses)}")
        return ", ".join(parts)


def import_file(
    engine: Engine,
    path: Path,
    *,
    team_name: str = "core",
    batch_size: int = 200,
) -> ImportStats:
    """Импортировать NDJSON-выгрузку."""
    profile, tickets = iter_ndjson(path)
    return import_tickets(
        engine, profile, tickets, team_name=team_name, batch_size=batch_size
    )


def import_tickets(
    engine: Engine,
    profile: SourceProfile,
    tickets,  # Iterable[RawTicket]
    *,
    team_name: str = "core",
    batch_size: int = 200,
) -> ImportStats:
    """Импортировать поток тикетов, приводя статусы к каноническим фазам."""
    stats = ImportStats()
    source_id = _ensure_source(engine, profile)
    team_id, calendar_id = _ensure_team(engine, team_name)
    status_cache = _load_statuses(engine, source_id, team_id)
    resolver = PersonResolver()
    person_ids: dict[str, int] = {}

    run_id = _start_sync_run(engine, source_id)
    batch: list[RawTicket] = []

    try:
        for ticket in tickets:
            batch.append(ticket)
            if len(batch) >= batch_size:
                _import_batch(
                    engine,
                    batch,
                    profile,
                    source_id,
                    team_id,
                    status_cache,
                    resolver,
                    person_ids,
                    stats,
                )
                batch.clear()

        if batch:
            _import_batch(
                engine,
                batch,
                profile,
                source_id,
                team_id,
                status_cache,
                resolver,
                person_ids,
                stats,
            )

        stats.people = len(person_ids)
        assign_service_classes(engine, source_id)
        _finish_sync_run(engine, run_id, stats, status="ok")
    except Exception as exc:
        _finish_sync_run(engine, run_id, stats, status="failed", error=str(exc))
        raise

    if stats.unknown_statuses:
        log.warning(
            "статусы без сопоставления (отнесены к '%s'): %s",
            UNKNOWN_PHASE.value,
            ", ".join(sorted(stats.unknown_statuses)),
        )
    return stats


def _import_batch(
    engine: Engine,
    batch: list[RawTicket],
    profile: SourceProfile,
    source_id: int,
    team_id: int,
    status_cache: dict[str, int],
    resolver: PersonResolver,
    person_ids: dict[str, int],
    stats: ImportStats,
) -> None:
    with engine.begin() as conn:
        for ticket in batch:
            for person in ticket.people:
                external = person.external_id
                if external in person_ids:
                    continue
                identity = PersonIdentity(
                    external_id=external,
                    display_name=person.display_name,
                    email=person.email,
                )
                local_id = resolver.resolve(identity)
                db_id = _upsert_person(
                    conn,
                    display_name=resolver.best_display_name(local_id) or external,
                    email=resolver.best_email(local_id),
                )
                _upsert_alias(conn, db_id, source_id, external, person.email)
                person_ids[external] = db_id

            _ensure_status(
                conn, source_id, team_id, ticket.status, profile, status_cache, stats
            )
            for event in ticket.events:
                for value in (event.old_value, event.new_value):
                    if event.kind == "status_change" and value:
                        _ensure_status(
                            conn, source_id, team_id, value, profile, status_cache, stats
                        )

            ticket_id = _upsert_ticket(conn, ticket, source_id, team_id, status_cache, person_ids)
            stats.tickets += 1
            stats.events += _insert_events(conn, ticket_id, ticket, status_cache, person_ids)
            stats.comments += _insert_comments(conn, ticket_id, ticket, person_ids)
            stats.declared += _insert_declared(conn, ticket_id, ticket)

    # связи проставляются вторым проходом: целевые тикеты могут быть ещё не созданы
    with engine.begin() as conn:
        for ticket in batch:
            stats.links += _insert_links(conn, source_id, ticket)


def _ensure_source(engine: Engine, profile: SourceProfile) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO source (kind, name, profile) VALUES (:kind, :name, :profile) "
                "ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind, "
                "  profile = EXCLUDED.profile RETURNING id"
            ),
            {
                "kind": profile.source_kind,
                "name": profile.source_name,
                "profile": json.dumps(profile.model_dump(mode="json")),
            },
        ).scalar_one()


def _ensure_team(engine: Engine, team_name: str) -> tuple[int, int]:
    from flowlens.core.calendar import DEFAULT_WORKWEEK

    with engine.begin() as conn:
        calendar_id = conn.execute(
            text(
                "INSERT INTO calendar (name, tz, workweek) "
                "VALUES (:name, 'Europe/Moscow', :workweek) "
                "ON CONFLICT (name) DO UPDATE SET tz = EXCLUDED.tz RETURNING id"
            ),
            {"name": f"{team_name}-calendar", "workweek": json.dumps(DEFAULT_WORKWEEK)},
        ).scalar_one()
        team_id = conn.execute(
            text(
                "INSERT INTO team (name, calendar_id) VALUES (:name, :cal) "
                "ON CONFLICT (name, COALESCE(parent_team_id, 0)) DO UPDATE "
                "SET calendar_id = EXCLUDED.calendar_id RETURNING id"
            ),
            {"name": team_name, "cal": calendar_id},
        ).scalar_one()
    return team_id, calendar_id


def assign_service_classes(engine: Engine, source_id: int) -> int:
    """Проставить класс обслуживания по правилу issue_type × priority.

    Класс — решение ядра, а не поле источника: в Jira его нет, но правила
    обработки у срочной баги и у плановой задачи разные, и без этого различия
    агрегаты вроде «WIP = 12» ничего не значат. Двенадцать standard — здоровая
    система; шесть expedite среди них — команда в режиме тушения пожара.
    """
    with engine.begin() as conn:
        # правила по умолчанию: создаются один раз, дальше их можно править руками
        conn.execute(
            text(
                "INSERT INTO service_class (source_id, issue_type, priority, name) "
                "SELECT :src, t.issue_type, t.priority, "
                "  CASE "
                "    WHEN t.priority = 'Blocker' THEN 'expedite' "
                "    WHEN t.priority = 'High' AND t.issue_type = 'Bug' THEN 'expedite' "
                "    WHEN t.priority = 'Low' THEN 'intangible' "
                "    ELSE 'standard' "
                "  END "
                "FROM (SELECT DISTINCT issue_type, priority FROM ticket "
                "      WHERE source_id = :src AND priority IS NOT NULL) t "
                "ON CONFLICT (source_id, issue_type, priority) DO NOTHING"
            ),
            {"src": source_id},
        )
        result = conn.execute(
            text(
                "UPDATE ticket t SET service_class_id = sc.id "
                "FROM service_class sc "
                "WHERE sc.source_id = t.source_id "
                "  AND sc.issue_type = t.issue_type "
                "  AND sc.priority IS NOT DISTINCT FROM t.priority "
                "  AND t.source_id = :src "
                "  AND t.service_class_id IS DISTINCT FROM sc.id"
            ),
            {"src": source_id},
        )
    return result.rowcount


def _load_statuses(engine: Engine, source_id: int, team_id: int) -> dict[str, int]:
    """Статусы команды, с откатом на настройку источника по умолчанию.

    Своя строка команды перекрывает общую: так первая настроившаяся команда
    не навязывает свою классификацию остальным.
    """
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                # источник не участвует: классификация принадлежит команде,
                # а не тому, откуда приехали данные
                "SELECT external_name, id, team_id FROM workflow_status "
                "WHERE team_id = :team OR (team_id IS NULL AND source_id = :src) "
                "ORDER BY (team_id IS NULL)"
            ),
            {"src": source_id, "team": team_id},
        ).all()
    cache: dict[str, int] = {}
    for name, status_id, _ in rows:
        cache.setdefault(name, status_id)
    return cache


def _ensure_status(
    conn,
    source_id: int,
    team_id: int,
    name: str,
    profile: SourceProfile,
    cache: dict[str, int],
    stats: ImportStats,
) -> int:
    """Создать статус, приведя его к канонической фазе."""
    if name in cache:
        return cache[name]

    canonical = profile.status_mapping.get(name, name)
    spec = next((s for s in BOARD if s.name == canonical), None)

    if spec is None:
        stats.unknown_statuses.add(name)
        phase, active, queue, terminal, order = UNKNOWN_PHASE, False, True, False, 99
    else:
        phase = spec.phase
        active, queue, terminal, order = (
            spec.is_active_work,
            spec.is_queue,
            spec.is_terminal,
            spec.board_order,
        )

    status_id = conn.execute(
        text(
            "INSERT INTO workflow_status (source_id, team_id, external_name, phase, "
            "  is_active_work, is_queue, is_terminal, board_order) "
            "VALUES (:src, :team, :name, CAST(:phase AS canonical_phase), :active, :queue, "
            "        :terminal, :ord) "
            # существующую строку не трогаем: команда могла перенастроить фазу
            # руками, и синхронизация не должна возвращать значение по умолчанию
            "ON CONFLICT (team_id, external_name) WHERE team_id IS NOT NULL "
            "DO UPDATE "
            "SET external_name = workflow_status.external_name RETURNING id"
        ),
        {
            "src": source_id,
            "team": team_id,
            "name": name,
            "phase": phase.value,
            "active": active,
            "queue": queue,
            "terminal": terminal,
            "ord": order,
        },
    ).scalar_one()
    cache[name] = status_id
    return status_id


def _upsert_person(conn, *, display_name: str, email: str | None) -> int:
    if email:
        return conn.execute(
            text(
                "INSERT INTO person (display_name, primary_email) VALUES (:name, :email) "
                "ON CONFLICT (primary_email) DO UPDATE "
                "SET display_name = EXCLUDED.display_name RETURNING id"
            ),
            {"name": display_name, "email": email},
        ).scalar_one()
    existing = conn.execute(
        text("SELECT id FROM person WHERE display_name = :name AND primary_email IS NULL"),
        {"name": display_name},
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    return conn.execute(
        text("INSERT INTO person (display_name) VALUES (:name) RETURNING id"),
        {"name": display_name},
    ).scalar_one()


def _upsert_alias(
    conn, person_id: int, source_id: int, external_id: str, email: str | None
) -> None:
    conn.execute(
        text(
            "INSERT INTO person_alias (person_id, source_id, external_id, email) "
            "VALUES (:pid, :src, :ext, :email) "
            "ON CONFLICT (source_id, external_id) DO UPDATE SET person_id = EXCLUDED.person_id"
        ),
        {"pid": person_id, "src": source_id, "ext": external_id, "email": email},
    )


def _upsert_ticket(
    conn,
    ticket: RawTicket,
    source_id: int,
    team_id: int,
    status_cache: dict[str, int],
    person_ids: dict[str, int],
) -> int:
    return conn.execute(
        text(
            "INSERT INTO ticket (source_id, external_key, external_id, project_key, team_id, "
            "  issue_type, is_subtask, priority, components, labels, summary, created_at, "
            "  resolved_at, closed_at, current_status_id, current_assignee_id, reporter_id, "
            "  story_points, raw_fields) "
            "VALUES (:src, :key, :ext, :proj, :team, :type, :sub, :prio, :comp, :labels, "
            "        :summary, :created, :resolved, :closed, :status, :assignee, :reporter, "
            "        :points, :raw) "
            "ON CONFLICT (source_id, external_key) DO UPDATE SET "
            "  summary = EXCLUDED.summary, priority = EXCLUDED.priority, "
            "  issue_type = EXCLUDED.issue_type, components = EXCLUDED.components, "
            "  labels = EXCLUDED.labels, resolved_at = EXCLUDED.resolved_at, "
            "  closed_at = EXCLUDED.closed_at, current_status_id = EXCLUDED.current_status_id, "
            "  current_assignee_id = EXCLUDED.current_assignee_id, "
            "  story_points = EXCLUDED.story_points, raw_fields = EXCLUDED.raw_fields, "
            "  ingested_at = now() "
            "RETURNING id"
        ),
        {
            "src": source_id,
            "key": ticket.external_key,
            "ext": ticket.external_id,
            "proj": ticket.project_key,
            "team": team_id,
            "type": ticket.issue_type,
            "sub": ticket.is_subtask,
            "prio": ticket.priority,
            "comp": ticket.components,
            "labels": ticket.labels,
            "summary": ticket.summary,
            "created": ticket.created_at,
            "resolved": ticket.resolved_at,
            "closed": ticket.closed_at or ticket.resolved_at,
            "status": status_cache.get(ticket.status),
            "assignee": person_ids.get(ticket.assignee) if ticket.assignee else None,
            "reporter": person_ids.get(ticket.reporter) if ticket.reporter else None,
            "points": ticket.story_points,
            "raw": json.dumps(ticket.raw_fields),
        },
    ).scalar_one()


def _insert_events(
    conn,
    ticket_id: int,
    ticket: RawTicket,
    status_cache: dict[str, int],
    person_ids: dict[str, int],
) -> int:
    written = 0
    for index, event in enumerate(ticket.events):
        source_event_id = event.source_event_id or f"{ticket.external_key}-auto{index}"
        result = conn.execute(
            text(
                "INSERT INTO ticket_event (ticket_id, kind, occurred_at, actor_person_id, "
                "  field, old_value, new_value, old_status_id, new_status_id, source_event_id) "
                "VALUES (:tid, CAST(:kind AS event_type), :at, :actor, :field, :old, :new, "
                "        :old_st, :new_st, :sid) "
                "ON CONFLICT (ticket_id, source_event_id, COALESCE(field, '')) DO NOTHING"
            ),
            {
                "tid": ticket_id,
                "kind": event.kind,
                "at": event.occurred_at,
                "actor": person_ids.get(event.actor) if event.actor else None,
                "field": event.field,
                "old": event.old_value,
                "new": event.new_value,
                "old_st": (
                    status_cache.get(event.old_value)
                    if event.kind == "status_change"
                    else None
                ),
                "new_st": (
                    status_cache.get(event.new_value)
                    if event.kind == "status_change"
                    else None
                ),
                "sid": source_event_id,
            },
        )
        written += result.rowcount or 0
    return written


def _insert_comments(conn, ticket_id: int, ticket: RawTicket, person_ids: dict[str, int]) -> int:
    written = 0
    for index, comment in enumerate(ticket.comments):
        result = conn.execute(
            text(
                "INSERT INTO ticket_comment (ticket_id, external_id, author_person_id, "
                "  created_at, updated_at, body, is_internal) "
                "VALUES (:tid, :ext, :author, :at, :upd, :body, :internal) "
                "ON CONFLICT (ticket_id, external_id) DO NOTHING"
            ),
            {
                "tid": ticket_id,
                "ext": comment.external_id or f"{ticket.external_key}-c{index}",
                "author": person_ids.get(comment.author) if comment.author else None,
                "at": comment.created_at,
                "upd": comment.updated_at,
                "body": comment.body,
                "internal": comment.is_internal,
            },
        )
        written += result.rowcount or 0
    return written


def _insert_declared(conn, ticket_id: int, ticket: RawTicket) -> int:
    written = 0
    for declared in ticket.declared_dates:
        conn.execute(
            text(
                "INSERT INTO ticket_declared_date (ticket_id, boundary, value_at, precision, "
                "  source_field) VALUES (:tid, :boundary, :at, :prec, :field) "
                "ON CONFLICT (ticket_id, boundary) DO UPDATE SET "
                "  value_at = EXCLUDED.value_at, precision = EXCLUDED.precision, "
                "  observed_at = now()"
            ),
            {
                "tid": ticket_id,
                "boundary": declared.boundary,
                "at": declared.value_at,
                "prec": declared.precision,
                "field": declared.source_field,
            },
        )
        written += 1
    return written


def _insert_links(conn, source_id: int, ticket: RawTicket) -> int:
    written = 0
    for link in ticket.links:
        result = conn.execute(
            text(
                "INSERT INTO ticket_link (from_ticket_id, to_ticket_id, link_type, created_at) "
                "SELECT f.id, t.id, :type, :at FROM ticket f, ticket t "
                "WHERE f.source_id = :src AND f.external_key = :from_key "
                "  AND t.source_id = :src AND t.external_key = :to_key "
                "ON CONFLICT (from_ticket_id, to_ticket_id, link_type) DO NOTHING"
            ),
            {
                "src": source_id,
                "from_key": ticket.external_key,
                "to_key": link.to_key,
                "type": link.link_type,
                "at": link.created_at,
            },
        )
        written += result.rowcount or 0
    return written


def link_hierarchy(engine: Engine, source_id: int) -> int:
    """Проставить parent_id и epic_id после импорта всех тикетов."""
    with engine.begin() as conn:
        updated = conn.execute(
            text(
                "UPDATE ticket c SET parent_id = p.id "
                "FROM ticket p WHERE p.source_id = c.source_id "
                "  AND p.external_key = c.raw_fields->>'parent_key' "
                "  AND c.source_id = :src AND c.parent_id IS NULL"
            ),
            {"src": source_id},
        ).rowcount
    return updated or 0


def _start_sync_run(engine: Engine, source_id: int) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text("INSERT INTO sync_run (source_id) VALUES (:src) RETURNING id"),
            {"src": source_id},
        ).scalar_one()


def _finish_sync_run(
    engine: Engine, run_id: int, stats: ImportStats, *, status: str, error: str | None = None
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE sync_run SET finished_at = now(), status = :status, "
                "  tickets_seen = :tickets, events_written = :events, error = :error "
                "WHERE id = :id"
            ),
            {
                "id": run_id,
                "status": status,
                "tickets": stats.tickets,
                "events": stats.events,
                "error": error,
            },
        )


def last_watermark(engine: Engine, source_name: str) -> datetime | None:
    """Момент последней успешной синхронизации — для инкрементальной выгрузки."""
    with engine.begin() as conn:
        return conn.execute(
            text(
                "SELECT max(r.watermark) FROM sync_run r JOIN source s ON s.id = r.source_id "
                "WHERE s.name = :name AND r.status = 'ok'"
            ),
            {"name": source_name},
        ).scalar_one_or_none()


def set_watermark(engine: Engine, source_name: str, moment: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE sync_run SET watermark = :at WHERE id = ("
                "  SELECT r.id FROM sync_run r JOIN source s ON s.id = r.source_id "
                "  WHERE s.name = :name ORDER BY r.id DESC LIMIT 1)"
            ),
            {"name": source_name, "at": moment},
        )


__all__ = ["ImportStats", "import_file", "import_tickets", "last_watermark", "set_watermark"]
