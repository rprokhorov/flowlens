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
    percentiles = reference.get("percentiles", {})
    p50 = percentiles.get("p50", 0)
    p85 = percentiles.get("p85", 0)
    p95 = percentiles.get("p95", 0)

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
            "over_p95": bool(p95 and (r.age_s or 0) > p95),
        }
        for r in rows
    ]
    return {
        "items": items,
        "reference": {"p50": p50, "p85": p85, "p95": p95},
        "total": len(items),
        "over_p85": sum(1 for i in items if i["over_p85"]),
        "over_p95": sum(1 for i in items if i["over_p95"]),
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
               percentile_disc(0.85) WITHIN GROUP (ORDER BY ti.duration_business_s) AS p85,
               percentile_disc(0.95) WITHIN GROUP (ORDER BY ti.duration_business_s) AS p95
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
                "p95_s": p.p95 or 0,
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
            percentile_disc(0.95) WITHIN GROUP (ORDER BY m.cycle_time_business_s) AS p95_cycle,
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
        "p95_cycle_s": row.p95_cycle or 0,
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
    "phase_time_rows",
    "summary",
    "throughput_history",
    "ticket_detail",
    "ticket_list",
]


# --- просмотр исходных данных ------------------------------------------------


def ticket_list(
    engine: Engine,
    filters: Filters,
    *,
    limit: int = 100,
    offset: int = 0,
    sort: str = "cycle_time",
    order: str = "desc",
    search: str | None = None,
    only_open: bool = False,
    min_cycle_s: int | None = None,
    max_cycle_s: int | None = None,
    anomaly: str | None = None,
) -> dict[str, Any]:
    """Список задач с метриками — то, из чего складываются графики."""
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    conditions = _ticket_conditions(filters, params)

    if only_open:
        conditions.append("t.closed_at IS NULL")
    if search:
        conditions.append("(t.external_key ILIKE :search OR t.summary ILIKE :search)")
        params["search"] = f"%{search}%"
    if min_cycle_s is not None:
        conditions.append(f"m.{filters.metric_column('cycle_time')} >= :min_cycle")
        params["min_cycle"] = min_cycle_s
    if max_cycle_s is not None:
        conditions.append(f"m.{filters.metric_column('cycle_time')} < :max_cycle")
        params["max_cycle"] = max_cycle_s
    if anomaly:
        conditions.append(
            "EXISTS (SELECT 1 FROM ticket_timeline_fact f "
            "WHERE f.ticket_id = t.id AND :anomaly = ANY(f.anomaly_flags))"
        )
        params["anomaly"] = anomaly

    confidence = _confidence_condition(filters, params)
    if confidence:
        conditions.append(confidence)

    sort_columns = {
        "cycle_time": f"m.{filters.metric_column('cycle_time')}",
        "lead_time": f"m.{filters.metric_column('lead_time')}",
        "created": "t.created_at",
        "key": "t.external_key",
        "blocked": "m.blocked_time_business_s",
        "flow_efficiency": "m.flow_efficiency",
        "reopens": "m.reopen_count",
    }
    sort_column = sort_columns.get(sort, sort_columns["cycle_time"])
    direction = "DESC" if order.lower() == "desc" else "ASC"

    where = _where(conditions)
    count_query = f"""
        SELECT count(*) FROM ticket t
        LEFT JOIN ticket_metrics m ON m.ticket_id = t.id
        {where}
    """
    query = f"""
        SELECT t.external_key, t.summary, t.issue_type, t.priority, t.components,
               t.created_at, t.closed_at, t.is_subtask,
               ws.external_name AS status,
               p.display_name AS assignee,
               m.{filters.metric_column('cycle_time')} AS cycle_s,
               m.{filters.metric_column('lead_time')} AS lead_s,
               m.touch_time_business_s, m.queue_time_business_s,
               m.blocked_time_business_s, m.flow_efficiency,
               m.reopen_count, m.assignee_change_count, m.status_change_count,
               m.confidence,
               (SELECT array_agg(DISTINCT a) FROM ticket_timeline_fact f,
                       unnest(f.anomaly_flags) a WHERE f.ticket_id = t.id) AS anomalies
        FROM ticket t
        LEFT JOIN ticket_metrics m ON m.ticket_id = t.id
        LEFT JOIN workflow_status ws ON ws.id = t.current_status_id
        LEFT JOIN person p ON p.id = t.current_assignee_id
        {where}
        ORDER BY {sort_column} {direction} NULLS LAST, t.id
        LIMIT :limit OFFSET :offset
    """

    with engine.begin() as conn:
        total = conn.execute(text(count_query), params).scalar_one()
        rows = conn.execute(text(query), params).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "unit": filters.unit,
        "items": [
            {
                "key": r.external_key,
                "summary": r.summary,
                "type": r.issue_type,
                "priority": r.priority,
                "components": list(r.components or []),
                "is_subtask": r.is_subtask,
                "status": r.status,
                "assignee": r.assignee,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "closed_at": r.closed_at.isoformat() if r.closed_at else None,
                "cycle_s": r.cycle_s,
                "lead_s": r.lead_s,
                "touch_s": r.touch_time_business_s,
                "queue_s": r.queue_time_business_s,
                "blocked_s": r.blocked_time_business_s,
                "flow_efficiency": (
                    float(r.flow_efficiency) if r.flow_efficiency is not None else None
                ),
                "reopens": r.reopen_count,
                "assignee_changes": r.assignee_change_count,
                "status_changes": r.status_change_count,
                "confidence": r.confidence,
                "anomalies": list(r.anomalies or []),
            }
            for r in rows
        ],
    }


def ticket_detail(engine: Engine, key: str) -> dict[str, Any] | None:
    """Полная история задачи: интервалы, события, согласование дат.

    Это тот уровень, на котором видно, откуда взялась метрика и почему
    ядро приняло именно такое решение о времени работы.
    """
    with engine.begin() as conn:
        ticket = conn.execute(
            text(
                "SELECT t.id, t.external_key, t.summary, t.issue_type, t.priority, "
                "       t.components, t.labels, t.is_subtask, t.created_at, "
                "       t.resolved_at, t.closed_at, t.story_points, t.raw_fields, "
                "       ws.external_name AS status, p.display_name AS assignee, "
                "       r.display_name AS reporter, s.name AS source "
                "FROM ticket t "
                "LEFT JOIN workflow_status ws ON ws.id = t.current_status_id "
                "LEFT JOIN person p ON p.id = t.current_assignee_id "
                "LEFT JOIN person r ON r.id = t.reporter_id "
                "LEFT JOIN source s ON s.id = t.source_id "
                "WHERE t.external_key = :key"
            ),
            {"key": key},
        ).one_or_none()

        if ticket is None:
            return None

        intervals = conn.execute(
            text(
                "SELECT i.seq, ws.external_name AS status, CAST(i.phase AS text) AS phase, "
                "       p.display_name AS assignee, i.is_blocked, "
                "       bs.external_name AS blocked_from, "
                "       i.started_at, i.ended_at, "
                "       i.duration_calendar_s, i.duration_business_s "
                "FROM ticket_interval i "
                "JOIN workflow_status ws ON ws.id = i.status_id "
                "LEFT JOIN workflow_status bs ON bs.id = i.blocked_from_status_id "
                "LEFT JOIN person p ON p.id = i.assignee_id "
                "WHERE i.ticket_id = :tid ORDER BY i.seq"
            ),
            {"tid": ticket.id},
        ).all()

        events = conn.execute(
            text(
                "SELECT CAST(e.kind AS text) AS kind, e.occurred_at, "
                "       p.display_name AS actor, e.field, e.old_value, e.new_value, "
                "       e.source_event_id "
                "FROM ticket_event e LEFT JOIN person p ON p.id = e.actor_person_id "
                "WHERE e.ticket_id = :tid ORDER BY e.occurred_at, e.id"
            ),
            {"tid": ticket.id},
        ).all()

        declared = conn.execute(
            text(
                "SELECT boundary, value_at, precision, source_field "
                "FROM ticket_declared_date WHERE ticket_id = :tid"
            ),
            {"tid": ticket.id},
        ).all()

        facts = conn.execute(
            text(
                "SELECT boundary, system_at, declared_at, effective_at, chosen_source, "
                "       confidence, discrepancy_business_s, anomaly_flags, policy_version "
                "FROM ticket_timeline_fact WHERE ticket_id = :tid ORDER BY boundary"
            ),
            {"tid": ticket.id},
        ).all()

        metrics = conn.execute(
            text("SELECT * FROM ticket_metrics WHERE ticket_id = :tid"),
            {"tid": ticket.id},
        ).one_or_none()

        comments = conn.execute(
            text(
                "SELECT p.display_name AS author, c.created_at, c.body, c.is_internal "
                "FROM ticket_comment c LEFT JOIN person p ON p.id = c.author_person_id "
                "WHERE c.ticket_id = :tid ORDER BY c.created_at"
            ),
            {"tid": ticket.id},
        ).all()

        links = conn.execute(
            text(
                "SELECT l.link_type, t2.external_key AS to_key "
                "FROM ticket_link l JOIN ticket t2 ON t2.id = l.to_ticket_id "
                "WHERE l.from_ticket_id = :tid"
            ),
            {"tid": ticket.id},
        ).all()

    return {
        "key": ticket.external_key,
        "summary": ticket.summary,
        "type": ticket.issue_type,
        "priority": ticket.priority,
        "components": list(ticket.components or []),
        "labels": list(ticket.labels or []),
        "is_subtask": ticket.is_subtask,
        "status": ticket.status,
        "assignee": ticket.assignee,
        "reporter": ticket.reporter,
        "source": ticket.source,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
        "closed_at": ticket.closed_at.isoformat() if ticket.closed_at else None,
        "story_points": float(ticket.story_points) if ticket.story_points else None,
        "raw_fields": ticket.raw_fields,
        "intervals": [
            {
                "seq": i.seq,
                "status": i.status,
                "phase": i.phase,
                "assignee": i.assignee,
                "is_blocked": i.is_blocked,
                "blocked_from": i.blocked_from,
                "started_at": i.started_at.isoformat(),
                "ended_at": i.ended_at.isoformat() if i.ended_at else None,
                "calendar_s": i.duration_calendar_s,
                "business_s": i.duration_business_s,
            }
            for i in intervals
        ],
        "events": [
            {
                "kind": e.kind,
                "occurred_at": e.occurred_at.isoformat(),
                "actor": e.actor,
                "field": e.field,
                "old_value": e.old_value,
                "new_value": e.new_value,
                "source_event_id": e.source_event_id,
            }
            for e in events
        ],
        "declared_dates": [
            {
                "boundary": d.boundary,
                "value_at": d.value_at.isoformat(),
                "precision": d.precision,
                "source_field": d.source_field,
            }
            for d in declared
        ],
        "timeline_facts": [
            {
                "boundary": f.boundary,
                "system_at": f.system_at.isoformat() if f.system_at else None,
                "declared_at": f.declared_at.isoformat() if f.declared_at else None,
                "effective_at": f.effective_at.isoformat() if f.effective_at else None,
                "chosen_source": f.chosen_source,
                "confidence": f.confidence,
                "discrepancy_business_s": f.discrepancy_business_s,
                "anomalies": list(f.anomaly_flags or []),
                "policy_version": f.policy_version,
            }
            for f in facts
        ],
        "metrics": (
            {
                "lead_calendar_s": metrics.lead_time_calendar_s,
                "lead_business_s": metrics.lead_time_business_s,
                "cycle_calendar_s": metrics.cycle_time_calendar_s,
                "cycle_business_s": metrics.cycle_time_business_s,
                "touch_s": metrics.touch_time_business_s,
                "queue_s": metrics.queue_time_business_s,
                "blocked_s": metrics.blocked_time_business_s,
                "release_wait_s": metrics.release_wait_business_s,
                "flow_efficiency": (
                    float(metrics.flow_efficiency)
                    if metrics.flow_efficiency is not None
                    else None
                ),
                "reopens": metrics.reopen_count,
                "assignee_changes": metrics.assignee_change_count,
                "status_changes": metrics.status_change_count,
                "blocked_episodes": metrics.blocked_episode_count,
                "confidence": metrics.confidence,
            }
            if metrics
            else None
        ),
        "comments": [
            {
                "author": c.author,
                "created_at": c.created_at.isoformat(),
                "body": c.body,
                "is_internal": c.is_internal,
            }
            for c in comments
        ],
        "links": [{"type": link.link_type, "to": link.to_key} for link in links],
    }


def phase_time_rows(engine: Engine, filters: Filters, phase: str | None = None) -> dict[str, Any]:
    """Интервалы по фазам — исходные данные для графика распределения времени."""
    params: dict[str, Any] = {}
    conditions = _ticket_conditions(filters, params)
    conditions.append("i.ended_at IS NOT NULL")
    if phase:
        conditions.append("CAST(i.phase AS text) = :phase")
        params["phase"] = phase

    query = f"""
        SELECT t.external_key, ws.external_name AS status, CAST(i.phase AS text) AS phase,
               p.display_name AS assignee, i.started_at, i.ended_at,
               i.duration_business_s, i.duration_calendar_s, i.is_blocked
        FROM ticket_interval i
        JOIN ticket t ON t.id = i.ticket_id
        JOIN workflow_status ws ON ws.id = i.status_id
        LEFT JOIN person p ON p.id = i.assignee_id
        {_where(conditions)}
        ORDER BY i.duration_business_s DESC NULLS LAST
        LIMIT 500
    """
    with engine.begin() as conn:
        rows = conn.execute(text(query), params).all()

    return {
        "items": [
            {
                "key": r.external_key,
                "status": r.status,
                "phase": r.phase,
                "assignee": r.assignee,
                "started_at": r.started_at.isoformat(),
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                "business_s": r.duration_business_s,
                "calendar_s": r.duration_calendar_s,
                "is_blocked": r.is_blocked,
            }
            for r in rows
        ]
    }
