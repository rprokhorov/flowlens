"""Аналитические запросы: из интервалов в графики.

Все срезы строятся поверх ticket_interval — одной таблицы, хранящей
разложение истории по статусам, исполнителям и блокировкам.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

from sqlalchemy import Engine, text

TimeUnit = Literal["business", "calendar"]


@dataclass
class Filters:
    """Общие условия отбора для всех отчётов."""

    date_from: date | None = None
    date_to: date | None = None
    team_id: int | None = None
    issue_types: list[str] = field(default_factory=list)
    priorities: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    include_subtasks: bool = True
    # фильтр по достоверности: не показывать метрики, которым нельзя верить
    min_confidence: Literal["low", "medium", "high"] = "low"
    unit: TimeUnit = "business"

    def duration_column(self, prefix: str = "ti") -> str:
        return f"{prefix}.duration_{self.unit}_s"

    def metric_column(self, base: str) -> str:
        return f"{base}_{self.unit}_s"


CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}


def _ticket_conditions(filters: Filters, params: dict[str, Any], alias: str = "t") -> list[str]:
    """Условия по тикету, общие для большинства запросов."""
    conditions: list[str] = []

    if filters.date_from:
        conditions.append(f"{alias}.created_at >= :date_from")
        params["date_from"] = filters.date_from
    if filters.date_to:
        conditions.append(f"{alias}.created_at < :date_to")
        params["date_to"] = filters.date_to + timedelta(days=1)
    if filters.team_id:
        conditions.append(f"{alias}.team_id = :team_id")
        params["team_id"] = filters.team_id
    if filters.issue_types:
        conditions.append(f"{alias}.issue_type = ANY(:issue_types)")
        params["issue_types"] = filters.issue_types
    if filters.priorities:
        conditions.append(f"{alias}.priority = ANY(:priorities)")
        params["priorities"] = filters.priorities
    if filters.components:
        conditions.append(f"{alias}.components && :components")
        params["components"] = filters.components
    if not filters.include_subtasks:
        conditions.append(f"NOT {alias}.is_subtask")

    return conditions


def _confidence_condition(filters: Filters, params: dict[str, Any]) -> str | None:
    """Отсечение недостоверных метрик."""
    if filters.min_confidence == "low":
        return None
    allowed = [
        level
        for level, rank in CONFIDENCE_RANK.items()
        if rank >= CONFIDENCE_RANK[filters.min_confidence]
    ]
    params["confidence_levels"] = allowed
    return "m.confidence = ANY(:confidence_levels)"


def _where(conditions: list[str]) -> str:
    return ("WHERE " + " AND ".join(conditions)) if conditions else ""


# --- распределение времени цикла ---------------------------------------------


def cycle_time_distribution(engine: Engine, filters: Filters) -> dict[str, Any]:
    """Гистограмма времени цикла с перцентилями.

    Перцентили считаются методом ближайшего ранга: показываем реально
    наблюдавшееся значение, а не интерполяцию между двумя задачами.
    """
    params: dict[str, Any] = {}
    conditions = _ticket_conditions(filters, params)
    column = f"m.{filters.metric_column('cycle_time')}"
    conditions.append(f"{column} IS NOT NULL")
    confidence = _confidence_condition(filters, params)
    if confidence:
        conditions.append(confidence)

    query = f"""
        SELECT {column} AS value, t.external_key, t.issue_type, t.priority
        FROM ticket_metrics m
        JOIN ticket t ON t.id = m.ticket_id
        {_where(conditions)}
        ORDER BY value
    """
    with engine.begin() as conn:
        rows = conn.execute(text(query), params).all()

    values = [row.value for row in rows]
    if not values:
        return {"count": 0, "percentiles": {}, "histogram": [], "unit": filters.unit}

    return {
        "count": len(values),
        "unit": filters.unit,
        "percentiles": {
            "p50": _percentile(values, 50),
            "p70": _percentile(values, 70),
            "p85": _percentile(values, 85),
            "p95": _percentile(values, 95),
        },
        "min": values[0],
        "max": values[-1],
        "histogram": _histogram(values),
        "slowest": [
            {"key": r.external_key, "value": r.value, "type": r.issue_type}
            for r in rows[-10:][::-1]
        ],
    }


def _percentile(sorted_values: list[int], p: float) -> int:
    """Ближайший ранг — значение из выборки, а не интерполяция."""
    from math import ceil

    if not sorted_values:
        return 0
    rank = max(1, ceil(p / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def _histogram(sorted_values: list[int], buckets: int = 20) -> list[dict[str, Any]]:
    """Гистограмма с шириной корзины, кратной рабочему дню."""
    if not sorted_values:
        return []
    workday = 9 * 3600
    top = _percentile(sorted_values, 95)
    step = max(workday // 2, (top // buckets // (workday // 2) or 1) * (workday // 2))

    counts: dict[int, int] = {}
    for value in sorted_values:
        index = min(value // step, buckets)
        counts[index] = counts.get(index, 0) + 1

    return [
        {
            "from": index * step,
            "to": (index + 1) * step if index < buckets else None,
            "count": counts.get(index, 0),
        }
        for index in range(max(counts) + 1)
    ]


# --- накопительная диаграмма потока ------------------------------------------


def cumulative_flow(engine: Engine, filters: Filters, granularity: str = "day") -> dict[str, Any]:
    """CFD: сколько задач находилось в каждой фазе на каждую дату.

    Считается из интервалов: задача попадает в фазу за день, если интервал
    этой фазы пересекает день.
    """
    params: dict[str, Any] = {}
    conditions = _ticket_conditions(filters, params)

    # CFD через дельты: вместо соединения "каждый интервал × каждый день"
    # берём +1 на входе в фазу и -1 на выходе, затем накопительную сумму.
    # Линейно по числу интервалов вместо квадратичного роста.
    query = f"""
        WITH scoped AS (
            SELECT ti.ticket_id, ti.phase, ti.started_at, ti.ended_at
            FROM ticket_interval ti
            JOIN ticket t ON t.id = ti.ticket_id
            {_where(conditions)}
        ),
        bounds AS (
            SELECT
                COALESCE(CAST(:date_from AS date), CAST(min(started_at) AS date)) AS start_day,
                COALESCE(CAST(:date_to AS date), CURRENT_DATE) AS end_day
            FROM scoped
        ),
        days AS (
            SELECT CAST(generate_series(start_day, end_day, '1 day') AS date) AS day
            FROM bounds
        ),
        deltas AS (
            SELECT CAST(started_at AS date) AS day, phase, 1 AS delta FROM scoped
            UNION ALL
            SELECT CAST(ended_at AS date) + 1, phase, -1 FROM scoped
            WHERE ended_at IS NOT NULL
        ),
        daily AS (
            SELECT day, phase, sum(delta) AS delta
            FROM deltas GROUP BY day, phase
        ),
        phases AS (SELECT DISTINCT phase FROM scoped)
        SELECT d.day, CAST(p.phase AS text) AS phase,
               -- приведение к bigint обязательно: numeric сериализуется в строку,
               -- и график начинает сравнивать значения лексикографически
               CAST(COALESCE(sum(daily.delta) OVER (
                   PARTITION BY p.phase ORDER BY d.day
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
               ), 0) AS bigint) AS tickets
        FROM days d
        CROSS JOIN phases p
        LEFT JOIN daily ON daily.day = d.day AND daily.phase = p.phase
        ORDER BY d.day, p.phase
    """
    params.setdefault("date_from", filters.date_from)
    params.setdefault("date_to", filters.date_to)

    with engine.begin() as conn:
        rows = conn.execute(text(query), params).all()

    series: dict[str, dict[str, int]] = {}
    for row in rows:
        series.setdefault(row.phase, {})[row.day.isoformat()] = row.tickets

    days = sorted({row.day.isoformat() for row in rows})
    return {
        "days": days,
        "series": [
            {"phase": phase, "values": [points.get(day, 0) for day in days]}
            for phase, points in sorted(series.items())
        ],
    }


# --- поступление и пропускная способность ------------------------------------


def arrival_vs_throughput(
    engine: Engine, filters: Filters, granularity: str = "week"
) -> dict[str, Any]:
    """Сколько задач приходит и сколько закрывается за период.

    Если поступление устойчиво обгоняет закрытие, очередь растёт —
    это главный ранний признак перегрузки команды.
    """
    params: dict[str, Any] = {"granularity": granularity}
    conditions = _ticket_conditions(filters, params)

    arrivals_query = f"""
        SELECT CAST(date_trunc(:granularity, t.created_at) AS date) AS period, count(*) AS count
        FROM ticket t
        {_where(conditions)}
        GROUP BY period ORDER BY period
    """
    done_conditions = [*conditions, "ws.is_terminal"]
    throughput_query = f"""
        SELECT CAST(date_trunc(:granularity, ti.started_at) AS date) AS period,
               count(DISTINCT ti.ticket_id) AS count
        FROM ticket_interval ti
        JOIN workflow_status ws ON ws.id = ti.status_id
        JOIN ticket t ON t.id = ti.ticket_id
        {_where(done_conditions)}
        GROUP BY period ORDER BY period
    """

    with engine.begin() as conn:
        arrivals = {r.period: r.count for r in conn.execute(text(arrivals_query), params).all()}
        throughput = {
            r.period: r.count for r in conn.execute(text(throughput_query), params).all()
        }

    periods = sorted(set(arrivals) | set(throughput))
    arrived = [arrivals.get(p, 0) for p in periods]
    completed = [throughput.get(p, 0) for p in periods]

    backlog: list[int] = []
    running = 0
    for a, c in zip(arrived, completed, strict=True):
        running += a - c
        backlog.append(running)

    return {
        "periods": [p.isoformat() for p in periods],
        "arrived": arrived,
        "completed": completed,
        "backlog_delta": backlog,
        "net_per_period": [a - c for a, c in zip(arrived, completed, strict=True)],
    }


# --- возраст незавершённых задач ---------------------------------------------


def aging_wip(engine: Engine, filters: Filters) -> dict[str, Any]:
    """Что висит прямо сейчас и сколько уже висит.

    Самый практичный отчёт для ежедневной работы: задачи, превысившие
    85-й перцентиль времени цикла, требуют внимания сегодня.
    """
    params: dict[str, Any] = {}
    conditions = _ticket_conditions(filters, params)
    conditions.append("ti.ended_at IS NULL")
    conditions.append("NOT ws.is_terminal")

    query = f"""
        SELECT t.external_key, t.summary, t.issue_type, t.priority,
               ws.external_name AS status, CAST(ti.phase AS text) AS phase,
               ti.{filters.unit == 'business' and 'duration_business_s' or 'duration_calendar_s'}
                   AS age_s,
               ti.is_blocked, p.display_name AS assignee,
               ws.board_order
        FROM ticket_interval ti
        JOIN ticket t ON t.id = ti.ticket_id
        JOIN workflow_status ws ON ws.id = ti.status_id
        LEFT JOIN person p ON p.id = ti.assignee_id
        {_where(conditions)}
        ORDER BY age_s DESC NULLS LAST
    """
    with engine.begin() as conn:
        rows = conn.execute(text(query), params).all()

    reference = cycle_time_distribution(engine, filters)
    p85 = reference.get("percentiles", {}).get("p85", 0)
    p50 = reference.get("percentiles", {}).get("p50", 0)

    items = [
        {
            "key": r.external_key,
            "summary": r.summary,
            "type": r.issue_type,
            "priority": r.priority,
            "status": r.status,
            "phase": r.phase,
            "age_s": r.age_s or 0,
            "is_blocked": r.is_blocked,
            "assignee": r.assignee,
            "board_order": r.board_order,
            "over_p85": bool(p85 and (r.age_s or 0) > p85),
        }
        for r in rows
    ]
    return {
        "items": items,
        "reference": {"p50": p50, "p85": p85},
        "total": len(items),
        "over_p85": sum(1 for i in items if i["over_p85"]),
        "blocked": sum(1 for i in items if i["is_blocked"]),
        "unit": filters.unit,
    }


# --- эффективность потока ----------------------------------------------------


def flow_efficiency(engine: Engine, filters: Filters) -> dict[str, Any]:
    """Какая доля времени тратится на работу, а не на ожидание."""
    params: dict[str, Any] = {}
    conditions = _ticket_conditions(filters, params)
    confidence = _confidence_condition(filters, params)
    if confidence:
        conditions.append(confidence)

    query = f"""
        SELECT
            CAST(sum(m.touch_time_business_s) AS bigint) AS touch,
            CAST(sum(m.queue_time_business_s) AS bigint) AS queue,
            CAST(sum(m.blocked_time_business_s) AS bigint) AS blocked,
            CAST(sum(m.release_wait_business_s) AS bigint) AS release_wait,
            avg(m.flow_efficiency) AS avg_efficiency,
            count(*) AS tickets
        FROM ticket_metrics m
        JOIN ticket t ON t.id = m.ticket_id
        {_where(conditions)}
    """
    phases_query = f"""
        SELECT CAST(ti.phase AS text) AS phase,
               CAST(sum(ti.duration_business_s) AS bigint) AS total,
               count(*) AS intervals,
               percentile_disc(0.5) WITHIN GROUP (ORDER BY ti.duration_business_s) AS p50,
               percentile_disc(0.85) WITHIN GROUP (ORDER BY ti.duration_business_s) AS p85
        FROM ticket_interval ti
        JOIN ticket t ON t.id = ti.ticket_id
        {_where([*conditions[:len(conditions) - (1 if confidence else 0)],
                 "ti.ended_at IS NOT NULL"])}
        GROUP BY ti.phase
        ORDER BY total DESC NULLS LAST
    """

    with engine.begin() as conn:
        totals = conn.execute(text(query), params).one()
        phase_params = {k: v for k, v in params.items() if k != "confidence_levels"}
        phases = conn.execute(text(phases_query), phase_params).all()

    touch = totals.touch or 0
    queue = totals.queue or 0
    denominator = touch + queue

    return {
        "tickets": totals.tickets or 0,
        "touch_s": touch,
        "queue_s": queue,
        "blocked_s": totals.blocked or 0,
        "release_wait_s": totals.release_wait or 0,
        "efficiency": round(touch / denominator, 4) if denominator else None,
        "avg_ticket_efficiency": (
            round(float(totals.avg_efficiency), 4) if totals.avg_efficiency else None
        ),
        "by_phase": [
            {
                "phase": p.phase,
                "total_s": p.total or 0,
                "intervals": p.intervals,
                "p50_s": p.p50 or 0,
                "p85_s": p.p85 or 0,
            }
            for p in phases
        ],
    }


# --- нагрузка по людям -------------------------------------------------------


def people_load(engine: Engine, filters: Filters) -> dict[str, Any]:
    """Распределение нагрузки между людьми.

    Показывает, кто перегружен и у кого задачи стоят из-за блокировок.
    Это не рейтинг производительности: время владения задачей не равно
    затраченным усилиям.
    """
    params: dict[str, Any] = {}
    conditions: list[str] = []
    if filters.date_from:
        conditions.append("w.day >= :date_from")
        params["date_from"] = filters.date_from
    if filters.date_to:
        conditions.append("w.day <= :date_to")
        params["date_to"] = filters.date_to
    if filters.team_id:
        conditions.append("w.team_id = :team_id")
        params["team_id"] = filters.team_id

    query = f"""
        SELECT p.display_name AS person,
               CAST(sum(w.owned_business_s) AS bigint) AS owned_s,
               CAST(sum(w.touch_business_s) AS bigint) AS touch_s,
               CAST(sum(w.blocked_business_s) AS bigint) AS blocked_s,
               CAST(sum(w.completed_count) AS bigint) AS completed,
               round(avg(w.active_tickets), 1) AS avg_active,
               max(w.active_tickets) AS max_active,
               count(DISTINCT w.day) AS active_days
        FROM person_workload_daily w
        JOIN person p ON p.id = w.person_id
        {_where(conditions)}
        GROUP BY p.display_name
        ORDER BY owned_s DESC NULLS LAST
    """
    with engine.begin() as conn:
        rows = conn.execute(text(query), params).all()

    return {
        "people": [
            {
                "person": r.person,
                "owned_s": r.owned_s or 0,
                "touch_s": r.touch_s or 0,
                "blocked_s": r.blocked_s or 0,
                "completed": r.completed or 0,
                "avg_active_tickets": float(r.avg_active or 0),
                "max_active_tickets": r.max_active or 0,
                "active_days": r.active_days,
            }
            for r in rows
        ]
    }


# --- сводка ------------------------------------------------------------------


def summary(engine: Engine, filters: Filters) -> dict[str, Any]:
    """Ключевые показатели одним запросом — для верхних плиток дашборда."""
    params: dict[str, Any] = {}
    conditions = _ticket_conditions(filters, params)

    query = f"""
        SELECT
            count(*) AS total,
            count(*) FILTER (WHERE t.closed_at IS NULL) AS open_tickets,
            count(*) FILTER (WHERE m.cycle_time_business_s IS NOT NULL) AS completed,
            percentile_disc(0.5) WITHIN GROUP (ORDER BY m.cycle_time_business_s) AS p50_cycle,
            percentile_disc(0.85) WITHIN GROUP (ORDER BY m.cycle_time_business_s) AS p85_cycle,
            avg(m.flow_efficiency) AS avg_efficiency,
            CAST(sum(m.reopen_count) AS bigint) AS reopens,
            count(*) FILTER (WHERE m.blocked_episode_count > 0) AS ever_blocked,
            count(*) FILTER (WHERE m.confidence = 'high') AS trustworthy
        FROM ticket t
        LEFT JOIN ticket_metrics m ON m.ticket_id = t.id
        {_where(conditions)}
    """
    with engine.begin() as conn:
        row = conn.execute(text(query), params).one()

    total = row.total or 0
    return {
        "total_tickets": total,
        "open_tickets": row.open_tickets or 0,
        "completed": row.completed or 0,
        "p50_cycle_s": row.p50_cycle or 0,
        "p85_cycle_s": row.p85_cycle or 0,
        "avg_flow_efficiency": (
            round(float(row.avg_efficiency), 4) if row.avg_efficiency else None
        ),
        "reopens": row.reopens or 0,
        "ever_blocked": row.ever_blocked or 0,
        "trustworthy_pct": round(100 * (row.trustworthy or 0) / total, 1) if total else 0.0,
    }


def throughput_history(
    engine: Engine, filters: Filters, granularity: str = "week", periods: int = 12
) -> list[int]:
    """Сколько задач закрывалось за каждый из последних периодов.

    Последний период исключается: он почти всегда неполный и занизил бы прогноз.
    """
    params: dict[str, Any] = {"granularity": granularity}
    conditions = _ticket_conditions(filters, params)
    conditions.append("ws.is_terminal")

    query = f"""
        SELECT CAST(date_trunc(:granularity, ti.started_at) AS date) AS period,
               count(DISTINCT ti.ticket_id) AS count
        FROM ticket_interval ti
        JOIN workflow_status ws ON ws.id = ti.status_id
        JOIN ticket t ON t.id = ti.ticket_id
        {_where(conditions)}
        GROUP BY period ORDER BY period DESC
        LIMIT :limit
    """
    params["limit"] = periods + 1

    with engine.begin() as conn:
        rows = conn.execute(text(query), params).all()

    if not rows:
        return []
    # rows отсортированы от новых к старым; отбрасываем текущий неполный период
    values = [r.count for r in rows][1:]
    return list(reversed(values))


def open_backlog_size(engine: Engine, filters: Filters) -> int:
    """Сколько задач сейчас не завершено."""
    params: dict[str, Any] = {}
    conditions = _ticket_conditions(filters, params)
    conditions.append("t.closed_at IS NULL")
    query = f"SELECT count(*) FROM ticket t {_where(conditions)}"
    with engine.begin() as conn:
        return conn.execute(text(query), params).scalar_one()


def arrivals_by_weekday(engine: Engine, filters: Filters) -> dict[int, list[int]]:
    """Поступление задач по дням недели — для анализа неравномерности."""
    params: dict[str, Any] = {}
    conditions = _ticket_conditions(filters, params)

    query = f"""
        SELECT CAST(EXTRACT(ISODOW FROM t.created_at) AS int) - 1 AS weekday,
               CAST(t.created_at AS date) AS day,
               count(*) AS count
        FROM ticket t
        {_where(conditions)}
        GROUP BY weekday, day
    """
    with engine.begin() as conn:
        rows = conn.execute(text(query), params).all()

    out: dict[int, list[int]] = {}
    for row in rows:
        out.setdefault(row.weekday, []).append(row.count)
    return out


def average_wip(engine: Engine, filters: Filters, days: int = 30) -> float:
    """Среднее число незавершённых задач за последние дни."""
    params: dict[str, Any] = {"days": days}
    conditions = _ticket_conditions(filters, params)
    conditions.append("ti.ended_at IS NULL")
    conditions.append("NOT ws.is_terminal")

    query = f"""
        SELECT count(DISTINCT ti.ticket_id) AS wip
        FROM ticket_interval ti
        JOIN workflow_status ws ON ws.id = ti.status_id
        JOIN ticket t ON t.id = ti.ticket_id
        {_where(conditions)}
    """
    with engine.begin() as conn:
        return float(conn.execute(text(query), params).scalar_one() or 0)


def interventions(engine: Engine, filters: Filters) -> list[dict[str, Any]]:
    """Отметки о значимых изменениях — накладываются на графики."""
    params: dict[str, Any] = {}
    conditions: list[str] = []
    if filters.team_id:
        conditions.append("team_id = :team_id")
        params["team_id"] = filters.team_id
    if filters.date_from:
        conditions.append("occurred_at >= :date_from")
        params["date_from"] = filters.date_from
    if filters.date_to:
        conditions.append("occurred_at <= :date_to")
        params["date_to"] = filters.date_to

    query = f"""
        SELECT id, occurred_at, title, description, kind
        FROM intervention {_where(conditions)} ORDER BY occurred_at
    """
    with engine.begin() as conn:
        rows = conn.execute(text(query), params).all()
    return [
        {
            "id": r.id,
            "occurred_at": r.occurred_at.isoformat(),
            "title": r.title,
            "description": r.description,
            "kind": r.kind,
        }
        for r in rows
    ]


__all__ = [
    "Filters",
    "aging_wip",
    "arrivals_by_weekday",
    "average_wip",
    "arrival_vs_throughput",
    "cumulative_flow",
    "cycle_time_distribution",
    "flow_efficiency",
    "interventions",
    "open_backlog_size",
    "people_load",
    "summary",
    "throughput_history",
]
