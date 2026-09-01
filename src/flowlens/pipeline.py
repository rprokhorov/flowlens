"""Пайплайн: загрузка синтетики и пересчёт производных данных."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, text

from flowlens.core.calendar import WorkCalendar
from flowlens.core.domain import Comment, Event, EventKind, TicketSeed
from flowlens.core.intervals import build_intervals
from flowlens.core.metrics import compute_metrics
from flowlens.core.workload import DailyLoad, accumulate_workload
from flowlens.repository import (
    ensure_reference_data,
    insert_ticket,
    load_calendar,
    reset_data,
    save_intervals,
    save_metrics,
    save_workload,
    upsert_people,
)
from flowlens.testing.scenarios import PEOPLE, all_scenarios, random_ticket

MSK = ZoneInfo("Europe/Moscow")


@dataclass
class SeedResult:
    tickets: int
    events: int
    people: int


def seed_demo(
    engine: Engine,
    *,
    ticket_count: int = 500,
    months: int = 12,
    seed: int = 2026,
    reset: bool = True,
) -> SeedResult:
    """Заполнить базу синтетическими данными.

    Создаёт эталонные сценарии (для проверки) плюс случайный поток,
    распределённый по рабочим дням за указанный период.
    """
    if reset:
        reset_data(engine)

    refs = ensure_reference_data(engine)
    cal = load_calendar(engine, refs["team_id"])
    people = upsert_people(engine, PEOPLE + ("olga",), refs["source_id"])
    refs.update({f"person:{name}": pid for name, pid in people.items()})

    rng = random.Random(seed)
    seeds: list[TicketSeed] = list(all_scenarios(cal))

    end = datetime.now(MSK).replace(hour=11, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=months * 30)

    span = max(1.0, (end - start).total_seconds())
    for i in range(ticket_count):
        created = _random_workday_moment(start, end, cal, rng)
        # незакрытыми остаются практически только недавние тикеты:
        # за год работы старые задачи так или иначе доводят до конца
        recency = (created - start).total_seconds() / span
        open_chance = 0.6 * recency**8
        seeds.append(
            random_ticket(
                cal,
                f"FLOW-{i + 1000}",
                created,
                rng,
                open_chance=open_chance,
                horizon=end,
            )
        )

    events = 0
    for s in seeds:
        insert_ticket(engine, s, refs, people)
        events += len(s.events)

    return SeedResult(tickets=len(seeds), events=events, people=len(people))


def _random_workday_moment(
    start: datetime, end: datetime, cal: WorkCalendar, rng: random.Random
) -> datetime:
    """Случайный рабочий момент в интервале.

    Поток заявок неравномерен: понедельник и вторник нагруженнее пятницы.
    """
    span_days = max(1, (end - start).days)
    weekday_weights = [1.35, 1.25, 1.0, 0.95, 0.7]  # пн..пт
    for _ in range(100):
        candidate = start + timedelta(
            days=rng.randrange(span_days),
            hours=rng.randrange(0, 9),
            minutes=rng.randrange(0, 60),
        )
        candidate = candidate.replace(hour=10 + candidate.hour % 9)
        if not cal.is_working_moment(candidate):
            continue
        weight = weekday_weights[candidate.weekday()] if candidate.weekday() < 5 else 0
        if rng.random() < weight / 1.35:
            return candidate
    return cal.add_business_seconds(start, 0)


def recompute_all(engine: Engine, *, now: datetime | None = None) -> dict[str, int]:
    """Пересчитать интервалы, метрики и нагрузку для всех тикетов."""
    moment = now or datetime.now(MSK)

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT t.id, t.external_key, t.created_at, t.team_id, p.display_name "
                "FROM ticket t LEFT JOIN person p ON p.id = t.reporter_id ORDER BY t.id"
            )
        ).all()
        status_rows = conn.execute(
            text("SELECT id, external_name FROM workflow_status")
        ).all()
        person_rows = conn.execute(text("SELECT id, display_name FROM person")).all()

    refs: dict[str, int] = {f"status:{name}": sid for sid, name in status_rows}
    refs.update({f"person:{name.lower()}": pid for pid, name in person_rows})

    if not rows:
        return {"tickets": 0, "intervals": 0}

    team_id = rows[0].team_id
    cal = load_calendar(engine, team_id)
    refs["calendar_id"] = _calendar_id(engine, team_id)
    refs["team_id"] = team_id

    total_intervals = 0
    workload: dict[tuple[str, object], DailyLoad] = {}

    for row in rows:
        events, comments = _load_ticket_history(engine, row.id)
        if not events:
            continue
        intervals = build_intervals(events, cal, now=moment)
        save_intervals(engine, row.id, intervals, refs)
        total_intervals += len(intervals)

        metrics = compute_metrics(
            created_at=row.created_at,
            intervals=intervals,
            events=events,
            comments=comments,
            calendar=cal,
            reporter=(row.display_name or "").lower() or None,
        )
        save_metrics(engine, row.id, metrics)
        accumulate_workload(row.external_key, intervals, cal, into=workload)  # type: ignore[arg-type]

    save_workload(engine, workload, refs)  # type: ignore[arg-type]
    return {"tickets": len(rows), "intervals": total_intervals}


def _calendar_id(engine: Engine, team_id: int) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT calendar_id FROM team WHERE id = :tid"), {"tid": team_id}
        ).scalar_one()


def _load_ticket_history(
    engine: Engine, ticket_id: int
) -> tuple[list[Event], list[Comment]]:
    """Прочитать события и комментарии тикета из базы."""
    with engine.begin() as conn:
        event_rows = conn.execute(
            text(
                "SELECT e.kind::text AS kind, e.occurred_at, p.display_name AS actor, "
                "       e.field, e.old_value, e.new_value, e.source_event_id "
                "FROM ticket_event e LEFT JOIN person p ON p.id = e.actor_person_id "
                "WHERE e.ticket_id = :tid ORDER BY e.occurred_at, e.id"
            ),
            {"tid": ticket_id},
        ).all()
        comment_rows = conn.execute(
            text(
                "SELECT p.display_name AS author, c.created_at, c.body, c.is_internal "
                "FROM ticket_comment c LEFT JOIN person p ON p.id = c.author_person_id "
                "WHERE c.ticket_id = :tid ORDER BY c.created_at"
            ),
            {"tid": ticket_id},
        ).all()

    events = [
        Event(
            kind=EventKind(r.kind),
            occurred_at=r.occurred_at,
            actor=(r.actor or "").lower() or None,
            field_name=r.field,
            old_value=r.old_value,
            new_value=r.new_value,
            source_event_id=r.source_event_id,
        )
        for r in event_rows
    ]
    comments = [
        Comment(
            author=(r.author or "").lower(),
            created_at=r.created_at,
            body=r.body or "",
            is_internal=r.is_internal,
        )
        for r in comment_rows
    ]
    return events, comments


__all__ = ["SeedResult", "recompute_all", "seed_demo"]
