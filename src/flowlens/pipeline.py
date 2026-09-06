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
from flowlens.core.reconciliation import (
    ReconciliationPolicy,
    TimelineSignals,
    reconcile,
)
from flowlens.core.workload import DailyLoad, accumulate_workload
from flowlens.importer import assign_service_classes
from flowlens.repository import (
    ensure_reference_data,
    insert_ticket,
    load_board,
    load_calendar,
    load_declared_dates,
    reset_data,
    save_intervals,
    save_metrics,
    save_metrics_confidence,
    save_ticket_state,
    save_timeline_facts,
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

    # горизонт — фактический текущий момент: события не должны уходить в будущее
    end = datetime.now(MSK).replace(second=0, microsecond=0)
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

    assign_service_classes(engine, refs["source_id"])
    return SeedResult(tickets=len(seeds), events=events, people=len(people))


def _random_workday_moment(
    start: datetime, end: datetime, cal: WorkCalendar, rng: random.Random
) -> datetime:
    """Случайный рабочий момент в интервале.

    Поток заявок неравномерен: понедельник и вторник нагруженнее пятницы,
    но час внутри дня распределён равномерно — иначе в накопительной
    диаграмме появляется искусственная пила.
    """
    span_days = max(1, (end - start).days)
    weekday_weights = [1.2, 1.15, 1.0, 0.95, 0.8]  # пн..пт

    for _ in range(200):
        day = start.date() + timedelta(days=rng.randrange(span_days))
        if day.weekday() >= 5:
            continue
        if rng.random() > weekday_weights[day.weekday()] / 1.2:
            continue
        windows = cal.day_intervals(day)
        if not windows:
            continue
        # равномерно внутри рабочего дня
        begin, finish = windows[0][0], windows[-1][1]
        offset = rng.random() * (finish - begin).total_seconds()
        candidate = begin + timedelta(seconds=offset)
        if cal.is_working_moment(candidate):
            return candidate

    return cal.add_business_seconds(start, 0)


def recompute_all(
    engine: Engine,
    *,
    now: datetime | None = None,
    policy: ReconciliationPolicy | None = None,
) -> dict[str, int]:
    """Пересчитать интервалы, согласование, метрики и нагрузку.

    Всё производное строится заново из event log, поэтому смена политики
    согласования или календаря не требует обращения к источнику.
    """
    moment = now or datetime.now(MSK)
    active_policy = policy or ReconciliationPolicy()

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
    # классификация статусов команды: от неё зависит, что считается работой
    board = load_board(engine, team_id)

    total_intervals = 0
    anomalous = 0
    workload: dict[tuple[str, object], DailyLoad] = {}

    for row in rows:
        events, comments = _load_ticket_history(engine, row.id)
        if not events:
            continue
        intervals = build_intervals(events, cal, now=moment, board=board)
        save_intervals(engine, row.id, intervals, refs)
        total_intervals += len(intervals)

        metrics = compute_metrics(
            created_at=row.created_at,
            intervals=intervals,
            events=events,
            comments=comments,
            calendar=cal,
            reporter=(row.display_name or "").lower() or None,
            board=board,
        )
        save_metrics(engine, row.id, metrics)

        last = intervals[-1]
        save_ticket_state(
            engine,
            row.id,
            closed_at=metrics.completed_at,
            status_id=refs.get(f"status:{last.status}"),
            assignee_id=(
                refs.get(f"person:{last.assignee}") if last.assignee else None
            ),
        )

        declared = load_declared_dates(engine, row.id)
        signals = _build_signals(row.created_at, intervals, metrics, declared)
        start_fact, end_fact = reconcile(signals, active_policy, cal, now=moment)
        save_timeline_facts(engine, row.id, (start_fact, end_fact))

        confidence = min(
            (start_fact.confidence, end_fact.confidence),
            key=lambda c: {"high": 3, "medium": 2, "low": 1}[c.value],
        )
        save_metrics_confidence(engine, row.id, confidence.value)
        if start_fact.anomalies or end_fact.anomalies:
            anomalous += 1

        accumulate_workload(row.external_key, intervals, cal, into=workload)  # type: ignore[arg-type]

    save_workload(engine, workload, refs)  # type: ignore[arg-type]
    return {
        "tickets": len(rows),
        "intervals": total_intervals,
        "anomalous": anomalous,
    }


def _build_signals(
    created_at: datetime,
    intervals: list,
    metrics,
    declared: dict[str, tuple[datetime, str]],
) -> TimelineSignals:
    """Собрать сырые сигналы тикета для согласования."""
    declared_start = declared.get("work_start")
    declared_end = declared.get("work_end")

    # моменты переходов между статусами — для детекта разгребки доски
    transitions = [
        iv.started_at for iv in intervals[1:] if iv.started_at is not None
    ]

    return TimelineSignals(
        created_at=created_at,
        system_start=metrics.work_started_at,
        system_end=metrics.completed_at,
        declared_start=declared_start[0] if declared_start else None,
        declared_end=declared_end[0] if declared_end else None,
        declared_start_precision=declared_start[1] if declared_start else "minute",
        declared_end_precision=declared_end[1] if declared_end else "minute",
        is_completed=metrics.is_completed,
        transition_times=transitions,
    )


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


__all__ = ["SeedResult", "build_narrative_request", "recompute_all", "seed_demo"]


def build_narrative_request(engine: Engine, filters, period_label: str):
    """Собрать метрики для текстового разбора."""
    from flowlens import analytics
    from flowlens.core.advice import analyse
    from flowlens.core.forecast import (
        ThroughputSample,
        forecast_how_long,
        forecast_how_many,
        wip_health,
    )
    from flowlens.core.narrative import NarrativeRequest
    from flowlens.core.quality import build_report
    from flowlens.repository import load_quality_rows

    stats = analytics.summary(engine, filters)
    flow = analytics.flow_efficiency(engine, filters)
    arrival = analytics.arrival_vs_throughput(engine, filters)
    aging = analytics.aging_wip(engine, filters)
    people = analytics.people_load(engine, filters)
    report = build_report(load_quality_rows(engine))

    history = analytics.throughput_history(engine, filters, periods=12)
    backlog = analytics.open_backlog_size(engine, filters)
    per_day = (sum(history) / len(history) / 7) if history else 0.0
    cycle_days = (stats["p50_cycle_s"] / 3600 / 9) if stats["p50_cycle_s"] else 0.0
    sample = ThroughputSample(values=history, period_days=7)

    forecast_data = {
        "how_long": {"percentiles": forecast_how_long(backlog, sample).percentiles},
        "how_many": {"percentiles": forecast_how_many(4, sample).percentiles},
        "wip_health": wip_health(analytics.average_wip(engine, filters), per_day, cycle_days),
    }

    quality_payload = {
        "trustworthy_pct": report.trustworthy_pct,
        "declared_coverage_pct": report.declared_coverage_pct,
        "anomalies": [
            {"code": g.code, "label": g.label, "count": g.count} for g in report.anomalies
        ],
    }

    findings = analyse(
        summary=stats,
        flow=flow,
        arrival=arrival,
        aging=aging,
        people=people,
        forecast=forecast_data,
        quality=quality_payload,
    )

    return NarrativeRequest(
        period_label=period_label,
        summary=stats,
        flow=flow,
        arrival=arrival,
        aging=aging,
        quality=quality_payload,
        findings=[
            {"title": f.title, "detail": f.detail, "severity": f.severity.value}
            for f in findings
        ],
        forecast=forecast_data,
        interventions=analytics.interventions(engine, filters),
    )
