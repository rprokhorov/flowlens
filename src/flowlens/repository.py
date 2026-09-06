"""Запись и чтение данных в Postgres.

Работа идёт через SQL Core: схема фиксирована DDL, ORM-модели избыточны.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime

from sqlalchemy import Engine, text

from flowlens.core.calendar import DEFAULT_WORKWEEK, WorkCalendar
from flowlens.core.domain import BOARD, BOARD_BY_NAME, Phase, StatusDef, TicketSeed
from flowlens.core.intervals import Interval
from flowlens.core.metrics import TicketMetrics
from flowlens.core.workload import DailyLoad


def reset_data(engine: Engine) -> None:
    """Очистить все данные, сохранив схему."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE ticket, ticket_event, ticket_interval, ticket_metrics, "
                "ticket_comment, ticket_declared_date, ticket_timeline_fact, "
                "ticket_link, person_workload_daily, person, person_alias, "
                "person_team, workflow_status, service_class, team, calendar, "
                "source, intervention, sync_run RESTART IDENTITY CASCADE"
            )
        )


def ensure_reference_data(
    engine: Engine, *, source_name: str = "demo", team_name: str = "core"
) -> dict[str, int]:
    """Создать источник, календарь, команду и статусы доски."""
    with engine.begin() as conn:
        source_id = conn.execute(
            text(
                "INSERT INTO source (kind, name, base_url, profile) "
                "VALUES ('synthetic', :name, NULL, :profile) "
                "ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind "
                "RETURNING id"
            ),
            {
                "name": source_name,
                "profile": json.dumps(
                    {
                        "declared_dates": {
                            "work_start": {"field": "customfield_start", "precision": "minute"},
                            "work_end": {"field": "customfield_end", "precision": "minute"},
                        },
                        "changelog": "full",
                        "reconciliation_hint": "prefer_declared",
                    }
                ),
            },
        ).scalar_one()

        calendar_id = conn.execute(
            text(
                "INSERT INTO calendar (name, tz, workweek, holidays, extra_workdays) "
                "VALUES (:name, :tz, :workweek, '{}', '{}') "
                "ON CONFLICT (name) DO UPDATE SET tz = EXCLUDED.tz "
                "RETURNING id"
            ),
            {
                "name": f"{team_name}-calendar",
                "tz": "Europe/Moscow",
                "workweek": json.dumps(DEFAULT_WORKWEEK),
            },
        ).scalar_one()

        team_id = conn.execute(
            text(
                "INSERT INTO team (name, parent_team_id, calendar_id, policy) "
                "VALUES (:name, NULL, :cal, '{}') "
                "ON CONFLICT (name, parent_team_id) DO UPDATE "
                "SET calendar_id = EXCLUDED.calendar_id "
                "RETURNING id"
            ),
            {"name": team_name, "cal": calendar_id},
        ).scalar_one()

        status_ids: dict[str, int] = {}
        for spec in BOARD:
            status_ids[spec.name] = conn.execute(
                text(
                    "INSERT INTO workflow_status "
                    "(source_id, team_id, external_name, phase, is_active_work, "
                    " is_queue, is_terminal, board_order) "
                    "VALUES (:src, :team, :name, CAST(:phase AS canonical_phase), :active, "
                    "        :queue, :terminal, :ord) "
                    "ON CONFLICT (source_id, team_id, external_name) "
                    "WHERE team_id IS NOT NULL DO UPDATE "
                    "SET phase = EXCLUDED.phase, is_active_work = EXCLUDED.is_active_work "
                    "RETURNING id"
                ),
                {
                    "src": source_id,
                    "team": team_id,
                    "name": spec.name,
                    "phase": spec.phase.value,
                    "active": spec.is_active_work,
                    "queue": spec.is_queue,
                    "terminal": spec.is_terminal,
                    "ord": spec.board_order,
                },
            ).scalar_one()

    return {
        "source_id": source_id,
        "calendar_id": calendar_id,
        "team_id": team_id,
        **{f"status:{k}": v for k, v in status_ids.items()},
    }


def upsert_people(engine: Engine, names: Iterable[str], source_id: int) -> dict[str, int]:
    """Создать людей и их алиасы в источнике."""
    ids: dict[str, int] = {}
    with engine.begin() as conn:
        for name in sorted(set(names)):
            person_id = conn.execute(
                text(
                    "INSERT INTO person (display_name, primary_email) "
                    "VALUES (:name, :email) "
                    "ON CONFLICT (primary_email) DO UPDATE "
                    "SET display_name = EXCLUDED.display_name "
                    "RETURNING id"
                ),
                {"name": name.capitalize(), "email": f"{name}@example.com"},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO person_alias (person_id, source_id, external_id, email) "
                    "VALUES (:pid, :src, :ext, :email) "
                    "ON CONFLICT (source_id, external_id) DO NOTHING"
                ),
                {
                    "pid": person_id,
                    "src": source_id,
                    "ext": name,
                    "email": f"{name}@example.com",
                },
            )
            ids[name] = person_id
    return ids


def insert_ticket(
    engine: Engine,
    seed: TicketSeed,
    refs: dict[str, int],
    people: dict[str, int],
) -> int:
    """Записать тикет с событиями, комментариями и заявленными датами."""
    with engine.begin() as conn:
        ticket_id = conn.execute(
            text(
                "INSERT INTO ticket (source_id, external_key, project_key, team_id, "
                "  issue_type, is_subtask, priority, components, labels, summary, "
                "  created_at, reporter_id, raw_fields) "
                "VALUES (:src, :key, :proj, :team, :type, :sub, :prio, :comp, :labels, "
                "        :summary, :created, :reporter, :raw) "
                "ON CONFLICT (source_id, external_key) DO UPDATE "
                "SET summary = EXCLUDED.summary, priority = EXCLUDED.priority "
                "RETURNING id"
            ),
            {
                "src": refs["source_id"],
                "key": seed.key,
                "proj": seed.key.split("-")[0],
                "team": refs["team_id"],
                "type": seed.issue_type,
                "sub": seed.is_subtask,
                "prio": seed.priority,
                "comp": seed.components,
                "labels": seed.labels,
                "summary": seed.summary,
                "created": seed.created_at,
                "reporter": people.get(seed.reporter),
                "raw": json.dumps({"scenario": seed.scenario}),
            },
        ).scalar_one()

        for ev in seed.events:
            conn.execute(
                text(
                    "INSERT INTO ticket_event (ticket_id, kind, occurred_at, actor_person_id, "
                    "  field, old_value, new_value, old_status_id, new_status_id, "
                    "  source_event_id, payload) "
                    "VALUES (:tid, CAST(:kind AS event_type), :at, :actor, :field, :old, :new, "
                    "        :old_st, :new_st, :sid, '{}') "
                    "ON CONFLICT (ticket_id, source_event_id, COALESCE(field, '')) DO NOTHING"
                ),
                {
                    "tid": ticket_id,
                    "kind": ev.kind.value,
                    "at": ev.occurred_at,
                    "actor": people.get(ev.actor) if ev.actor else None,
                    "field": ev.field_name,
                    "old": ev.old_value,
                    "new": ev.new_value,
                    "old_st": (
                        refs.get(f"status:{ev.old_value}")
                        if ev.field_name == "status"
                        else None
                    ),
                    "new_st": (
                        refs.get(f"status:{ev.new_value}")
                        if ev.field_name == "status"
                        else None
                    ),
                    "sid": ev.source_event_id,
                },
            )

        for i, c in enumerate(seed.comments):
            conn.execute(
                text(
                    "INSERT INTO ticket_comment (ticket_id, external_id, author_person_id, "
                    "  created_at, body, is_internal) "
                    "VALUES (:tid, :ext, :author, :at, :body, :internal) "
                    "ON CONFLICT (ticket_id, external_id) DO NOTHING"
                ),
                {
                    "tid": ticket_id,
                    "ext": f"{seed.key}-c{i}",
                    "author": people.get(c.author),
                    "at": c.created_at,
                    "body": c.body,
                    "internal": c.is_internal,
                },
            )

        for d in seed.declared:
            conn.execute(
                text(
                    "INSERT INTO ticket_declared_date (ticket_id, boundary, value_at, "
                    "  precision, source_field) "
                    "VALUES (:tid, :boundary, :at, :prec, :field) "
                    "ON CONFLICT (ticket_id, boundary) DO UPDATE "
                    "SET value_at = EXCLUDED.value_at, precision = EXCLUDED.precision"
                ),
                {
                    "tid": ticket_id,
                    "boundary": d.boundary,
                    "at": d.value_at,
                    "prec": d.precision,
                    "field": d.source_field,
                },
            )

    return ticket_id


def save_intervals(
    engine: Engine, ticket_id: int, intervals: list[Interval], refs: dict[str, int]
) -> None:
    """Перезаписать интервалы тикета."""
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ticket_interval WHERE ticket_id = :tid"), {"tid": ticket_id}
        )
        for iv in intervals:
            conn.execute(
                text(
                    "INSERT INTO ticket_interval (ticket_id, seq, status_id, phase, assignee_id, "
                    "  is_blocked, blocked_from_status_id, blocker_reason, "
                    "  started_at, ended_at, "
                    "  duration_calendar_s, duration_business_s, calendar_id) "
                    "VALUES (:tid, :seq, :status, CAST(:phase AS canonical_phase), :assignee, "
                    "        :blocked, :from_st, :reason, :start, :end, :cal_s, :bus_s, :cal_id)"
                ),
                {
                    "tid": ticket_id,
                    "seq": iv.seq,
                    "status": refs[f"status:{iv.status}"],
                    "phase": iv.phase.value,
                    "assignee": refs.get(f"person:{iv.assignee}") if iv.assignee else None,
                    "blocked": iv.is_blocked,
                    "from_st": (
                        refs.get(f"status:{iv.blocked_from_status}")
                        if iv.blocked_from_status
                        else None
                    ),
                    "reason": iv.blocker_reason,
                    "start": iv.started_at,
                    "end": iv.ended_at,
                    "cal_s": iv.duration_calendar_s,
                    "bus_s": iv.duration_business_s,
                    "cal_id": refs["calendar_id"],
                },
            )


def save_metrics(engine: Engine, ticket_id: int, m: TicketMetrics) -> None:
    """Записать метрики тикета."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ticket_metrics (ticket_id, lead_time_calendar_s, "
                "  lead_time_business_s, cycle_time_calendar_s, cycle_time_business_s, "
                "  touch_time_business_s, queue_time_business_s, blocked_time_business_s, "
                "  release_wait_business_s, flow_efficiency, reopen_count, "
                "  assignee_change_count, status_change_count, blocked_episode_count, "
                "  first_response_business_s, confidence) "
                "VALUES (:tid, :lc, :lb, :cc, :cb, :touch, :queue, :blocked, :release, "
                "        :fe, :reopen, :ach, :sch, :bep, :fr, NULL) "
                "ON CONFLICT (ticket_id) DO UPDATE SET "
                "  lead_time_calendar_s = EXCLUDED.lead_time_calendar_s, "
                "  lead_time_business_s = EXCLUDED.lead_time_business_s, "
                "  cycle_time_calendar_s = EXCLUDED.cycle_time_calendar_s, "
                "  cycle_time_business_s = EXCLUDED.cycle_time_business_s, "
                "  touch_time_business_s = EXCLUDED.touch_time_business_s, "
                "  queue_time_business_s = EXCLUDED.queue_time_business_s, "
                "  blocked_time_business_s = EXCLUDED.blocked_time_business_s, "
                "  release_wait_business_s = EXCLUDED.release_wait_business_s, "
                "  flow_efficiency = EXCLUDED.flow_efficiency, "
                "  reopen_count = EXCLUDED.reopen_count, "
                "  assignee_change_count = EXCLUDED.assignee_change_count, "
                "  status_change_count = EXCLUDED.status_change_count, "
                "  blocked_episode_count = EXCLUDED.blocked_episode_count, "
                "  first_response_business_s = EXCLUDED.first_response_business_s, "
                "  computed_at = now()"
            ),
            {
                "tid": ticket_id,
                "lc": m.lead_time_calendar_s,
                "lb": m.lead_time_business_s,
                "cc": m.cycle_time_calendar_s,
                "cb": m.cycle_time_business_s,
                "touch": m.touch_time_business_s,
                "queue": m.queue_time_business_s,
                "blocked": m.blocked_time_business_s,
                "release": m.release_wait_business_s,
                "fe": m.flow_efficiency,
                "reopen": m.reopen_count,
                "ach": m.assignee_change_count,
                "sch": m.status_change_count,
                "bep": m.blocked_episode_count,
                "fr": m.first_response_business_s,
            },
        )


def save_workload(
    engine: Engine,
    loads: dict[tuple[str, date], DailyLoad],
    refs: dict[str, int],
) -> None:
    """Перезаписать дневную нагрузку."""
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE person_workload_daily"))
        for (person, day), load in loads.items():
            person_id = refs.get(f"person:{person}")
            if person_id is None:
                continue
            conn.execute(
                text(
                    "INSERT INTO person_workload_daily (person_id, day, team_id, "
                    "  active_tickets, owned_business_s, touch_business_s, "
                    "  blocked_business_s, completed_count) "
                    "VALUES (:pid, :day, :team, :active, :owned, :touch, :blocked, :done) "
                    "ON CONFLICT (person_id, day) DO UPDATE SET "
                    "  active_tickets = EXCLUDED.active_tickets, "
                    "  owned_business_s = EXCLUDED.owned_business_s, "
                    "  touch_business_s = EXCLUDED.touch_business_s, "
                    "  blocked_business_s = EXCLUDED.blocked_business_s, "
                    "  completed_count = EXCLUDED.completed_count"
                ),
                {
                    "pid": person_id,
                    "day": day,
                    "team": refs["team_id"],
                    "active": load.active_ticket_count,
                    "owned": load.owned_business_s,
                    "touch": load.touch_business_s,
                    "blocked": load.blocked_business_s,
                    "done": load.completed_count,
                },
            )


def save_ticket_state(
    engine: Engine,
    ticket_id: int,
    *,
    closed_at: datetime | None,
    status_id: int | None,
    assignee_id: int | None,
) -> None:
    """Синхронизировать текущее состояние тикета с результатом пересчёта.

    closed_at выводится из интервалов, а не из полей источника: разные
    системы понимают «закрыт» по-разному, а терминальный статус — однозначен.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE ticket SET closed_at = :closed, "
                "  current_status_id = COALESCE(:status, current_status_id), "
                "  current_assignee_id = :assignee "
                "WHERE id = :tid"
            ),
            {
                "tid": ticket_id,
                "closed": closed_at,
                "status": status_id,
                "assignee": assignee_id,
            },
        )


def save_timeline_facts(engine: Engine, ticket_id: int, facts) -> None:
    """Записать результат согласования по обеим границам."""
    with engine.begin() as conn:
        for fact in facts:
            conn.execute(
                text(
                    "INSERT INTO ticket_timeline_fact (ticket_id, boundary, system_at, "
                    "  declared_at, effective_at, chosen_source, confidence, "
                    "  discrepancy_business_s, anomaly_flags, policy_version, computed_at) "
                    "VALUES (:tid, :boundary, :system, :declared, :effective, :source, "
                    "        :confidence, :discrepancy, :flags, :version, now()) "
                    "ON CONFLICT (ticket_id, boundary) DO UPDATE SET "
                    "  system_at = EXCLUDED.system_at, declared_at = EXCLUDED.declared_at, "
                    "  effective_at = EXCLUDED.effective_at, "
                    "  chosen_source = EXCLUDED.chosen_source, "
                    "  confidence = EXCLUDED.confidence, "
                    "  discrepancy_business_s = EXCLUDED.discrepancy_business_s, "
                    "  anomaly_flags = EXCLUDED.anomaly_flags, "
                    "  policy_version = EXCLUDED.policy_version, computed_at = now()"
                ),
                {
                    "tid": ticket_id,
                    "boundary": fact.boundary,
                    "system": fact.system_at,
                    "declared": fact.declared_at,
                    "effective": fact.effective_at,
                    "source": fact.source.value,
                    "confidence": fact.confidence.value,
                    "discrepancy": fact.discrepancy_business_s,
                    "flags": sorted(a.value for a in fact.anomalies),
                    "version": fact.policy_version,
                },
            )


def save_metrics_confidence(engine: Engine, ticket_id: int, confidence: str) -> None:
    """Проставить достоверность метрик тикета."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE ticket_metrics SET confidence = :c WHERE ticket_id = :tid"),
            {"c": confidence, "tid": ticket_id},
        )


def load_declared_dates(engine: Engine, ticket_id: int) -> dict[str, tuple[datetime, str]]:
    """Прочитать заявленные даты тикета."""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT boundary, value_at, precision FROM ticket_declared_date "
                "WHERE ticket_id = :tid"
            ),
            {"tid": ticket_id},
        ).all()
    return {boundary: (value, precision) for boundary, value, precision in rows}


def load_quality_rows(engine: Engine) -> list[dict]:
    """Данные для отчёта о качестве."""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT t.external_key AS key, f.confidence, f.chosen_source AS source, "
                "       f.anomaly_flags AS anomalies, "
                "       EXISTS (SELECT 1 FROM ticket_declared_date d "
                "               WHERE d.ticket_id = t.id) AS has_declared "
                "FROM ticket_timeline_fact f JOIN ticket t ON t.id = f.ticket_id"
            )
        ).all()
    return [dict(r._mapping) for r in rows]


def load_board(engine: Engine, team_id: int) -> dict[str, StatusDef]:
    """Классификация статусов команды — из БД, а не из константы.

    От неё зависят touch time, время по фазам и WIP, поэтому она настраивается
    на команду: `qa` у одной команды code review и работа, у другой — очередь
    на ручное тестирование. Своя строка команды перекрывает общую настройку
    источника; чего нет в БД, берётся из эталонной доски.
    """
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT external_name, CAST(phase AS text) AS phase, is_active_work, "
                "       is_queue, is_terminal, board_order, team_id "
                "FROM workflow_status "
                "WHERE team_id = :team OR team_id IS NULL "
                "ORDER BY (team_id IS NULL)"
            ),
            {"team": team_id},
        ).all()

    board: dict[str, StatusDef] = dict(BOARD_BY_NAME)
    seen: set[str] = set()
    for row in rows:
        if row.external_name in seen:
            continue
        seen.add(row.external_name)
        board[row.external_name] = StatusDef(
            name=row.external_name,
            phase=Phase(row.phase),
            is_active_work=row.is_active_work,
            is_queue=row.is_queue,
            is_terminal=row.is_terminal,
            board_order=row.board_order or 0,
        )
    return board


def load_calendar(engine: Engine, team_id: int) -> WorkCalendar:
    """Прочитать календарь команды."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT c.name, c.tz, c.workweek, c.holidays, c.extra_workdays "
                "FROM calendar c JOIN team t ON t.calendar_id = c.id WHERE t.id = :tid"
            ),
            {"tid": team_id},
        ).one()
    workweek = row.workweek if isinstance(row.workweek, dict) else json.loads(row.workweek)
    return WorkCalendar(
        name=row.name,
        tz=row.tz,
        workweek={k: [tuple(w) for w in v] for k, v in workweek.items()},  # type: ignore[misc]
        holidays=frozenset(row.holidays or ()),
        extra_workdays=frozenset(row.extra_workdays or ()),
    )


EventRow = tuple[str, datetime, str | None, str | None, str | None, str | None]


def fetch_events(engine: Engine, ticket_id: int) -> list[EventRow]:
    """Прочитать события тикета для пересчёта."""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT e.kind::text, e.occurred_at, p.display_name, e.field, "
                "       e.old_value, e.new_value "
                "FROM ticket_event e LEFT JOIN person p ON p.id = e.actor_person_id "
                "WHERE e.ticket_id = :tid ORDER BY e.occurred_at, e.id"
            ),
            {"tid": ticket_id},
        ).all()
    return [tuple(r) for r in rows]  # type: ignore[misc]


__all__ = [
    "ensure_reference_data",
    "fetch_events",
    "insert_ticket",
    "load_calendar",
    "reset_data",
    "save_intervals",
    "save_metrics",
    "save_workload",
    "upsert_people",
]
