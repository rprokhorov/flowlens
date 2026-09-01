"""HTTP API дашборда."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine, text

from flowlens import analytics
from flowlens.analytics import Filters
from flowlens.core.advice import analyse
from flowlens.core.quality import build_report
from flowlens.db import make_engine
from flowlens.repository import load_quality_rows

app = FastAPI(
    title="FlowLens",
    description="Анализ потока задач",
    version="0.1.0",
)

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_filters(
    date_from: Annotated[date | None, Query(description="Начало периода")] = None,
    date_to: Annotated[date | None, Query(description="Конец периода")] = None,
    team_id: Annotated[int | None, Query()] = None,
    issue_type: Annotated[list[str] | None, Query()] = None,
    priority: Annotated[list[str] | None, Query()] = None,
    component: Annotated[list[str] | None, Query()] = None,
    person: Annotated[list[str] | None, Query()] = None,
    include_subtasks: Annotated[bool, Query()] = True,
    min_confidence: Annotated[Literal["low", "medium", "high"], Query()] = "low",
    unit: Annotated[Literal["business", "calendar"], Query()] = "business",
) -> Filters:
    """Общие параметры отбора для всех отчётов."""
    return Filters(
        date_from=date_from,
        date_to=date_to,
        team_id=team_id,
        issue_types=issue_type or [],
        priorities=priority or [],
        components=component or [],
        people=person or [],
        include_subtasks=include_subtasks,
        min_confidence=min_confidence,
        unit=unit,
    )


FiltersDep = Annotated[Filters, Depends(get_filters)]
EngineDep = Annotated[Engine, Depends(get_engine)]


@app.get("/api/health")
def health(engine: EngineDep) -> dict[str, Any]:
    """Проверка доступности базы."""
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"База недоступна: {exc}") from exc
    return {"status": "ok"}


@app.get("/api/summary")
def get_summary(engine: EngineDep, filters: FiltersDep) -> dict[str, Any]:
    """Ключевые показатели для верхних плиток."""
    return analytics.summary(engine, filters)


@app.get("/api/cycle-time")
def get_cycle_time(engine: EngineDep, filters: FiltersDep) -> dict[str, Any]:
    """Распределение времени цикла с перцентилями."""
    return analytics.cycle_time_distribution(engine, filters)


@app.get("/api/cfd")
def get_cfd(engine: EngineDep, filters: FiltersDep) -> dict[str, Any]:
    """Накопительная диаграмма потока."""
    return analytics.cumulative_flow(engine, filters)


@app.get("/api/arrival-throughput")
def get_arrival_throughput(
    engine: EngineDep,
    filters: FiltersDep,
    granularity: Annotated[Literal["day", "week", "month"], Query()] = "week",
) -> dict[str, Any]:
    """Поступление задач против пропускной способности."""
    return analytics.arrival_vs_throughput(engine, filters, granularity)


@app.get("/api/aging-wip")
def get_aging_wip(engine: EngineDep, filters: FiltersDep) -> dict[str, Any]:
    """Незавершённые задачи и их возраст."""
    return analytics.aging_wip(engine, filters)


@app.get("/api/flow-efficiency")
def get_flow_efficiency(engine: EngineDep, filters: FiltersDep) -> dict[str, Any]:
    """Соотношение работы и ожидания."""
    return analytics.flow_efficiency(engine, filters)


@app.get("/api/people")
def get_people(engine: EngineDep, filters: FiltersDep) -> dict[str, Any]:
    """Распределение нагрузки между людьми."""
    return analytics.people_load(engine, filters)


@app.get("/api/quality")
def get_quality(engine: EngineDep) -> dict[str, Any]:
    """Достоверность данных."""
    rows = load_quality_rows(engine)
    report = build_report(rows)
    return {
        "total_tickets": report.total_tickets,
        "declared_coverage_pct": report.declared_coverage_pct,
        "trustworthy_pct": report.trustworthy_pct,
        "by_confidence": report.by_confidence,
        "by_source": report.by_source,
        "verdict": report.verdict(),
        "anomalies": [
            {
                "code": group.code,
                "label": group.label,
                "hint": group.hint,
                "count": group.count,
                "samples": group.sample_keys,
            }
            for group in report.anomalies
        ],
    }


@app.get("/api/advice")
def get_advice(engine: EngineDep, filters: FiltersDep) -> dict[str, Any]:
    """Наблюдения о процессе по детерминированным правилам."""
    quality_rows = load_quality_rows(engine)
    report = build_report(quality_rows)

    from flowlens.core.forecast import wip_health

    stats = analytics.summary(engine, filters)
    history = analytics.throughput_history(engine, filters, periods=12)
    per_day = (sum(history) / len(history) / 7) if history else 0.0
    cycle_days = (stats["p50_cycle_s"] / 3600 / 9) if stats["p50_cycle_s"] else 0.0

    findings = analyse(
        summary=stats,
        flow=analytics.flow_efficiency(engine, filters),
        arrival=analytics.arrival_vs_throughput(engine, filters),
        aging=analytics.aging_wip(engine, filters),
        people=analytics.people_load(engine, filters),
        forecast={
            "wip_health": wip_health(
                analytics.average_wip(engine, filters), per_day, cycle_days
            )
        },
        quality={
            "trustworthy_pct": report.trustworthy_pct,
            "anomalies": [
                {"code": g.code, "label": g.label, "count": g.count}
                for g in report.anomalies
            ],
        },
    )
    return {
        "findings": [
            {
                "code": f.code,
                "severity": f.severity.value,
                "title": f.title,
                "detail": f.detail,
                "suggestion": f.suggestion,
                "evidence": f.evidence,
                "tickets": f.ticket_keys,
            }
            for f in findings
        ]
    }


@app.get("/api/forecast")
def get_forecast(
    engine: EngineDep,
    filters: FiltersDep,
    horizon_periods: Annotated[int, Query(description="Горизонт в неделях")] = 4,
) -> dict[str, Any]:
    """Вероятностный прогноз по историческому темпу закрытия."""
    from datetime import date as date_type

    from flowlens.core.forecast import (
        ThroughputSample,
        analyse_seasonality,
        forecast_how_long,
        forecast_how_many,
        wip_health,
    )

    history = analytics.throughput_history(engine, filters, periods=12)
    backlog = analytics.open_backlog_size(engine, filters)
    sample = ThroughputSample(values=history, period_days=7)

    how_long = forecast_how_long(backlog, sample, start=date_type.today(), seed=None)
    how_many = forecast_how_many(horizon_periods, sample, seed=None)

    stats = analytics.summary(engine, filters)
    wip = analytics.average_wip(engine, filters)
    per_day = (sum(history) / len(history) / 7) if history else 0.0
    cycle_days = (stats["p50_cycle_s"] / 3600 / 9) if stats["p50_cycle_s"] else 0.0

    seasonality = analyse_seasonality(analytics.arrivals_by_weekday(engine, filters))

    return {
        "throughput_history": history,
        "backlog": backlog,
        "how_long": {
            "percentiles": how_long.percentiles,
            "dates": how_long.dates,
            "warning": how_long.warning,
        },
        "how_many": {
            "periods": horizon_periods,
            "percentiles": how_many.percentiles,
            "warning": how_many.warning,
        },
        "wip_health": wip_health(wip, per_day, cycle_days),
        "seasonality": {
            "busiest": seasonality.busiest_name(),
            "quietest": seasonality.quietest_name(),
            "ratio": seasonality.ratio,
            "by_weekday": seasonality.by_weekday,
        },
    }


@app.get("/api/interventions")
def get_interventions(engine: EngineDep, filters: FiltersDep) -> list[dict[str, Any]]:
    """Отметки о значимых изменениях в процессе."""
    return analytics.interventions(engine, filters)


@app.post("/api/explain")
def post_explain(engine: EngineDep, filters: FiltersDep) -> dict[str, Any]:
    """Текстовый разбор метрик. Требует ключа Anthropic в окружении."""
    from flowlens.core.narrative import NarrativeUnavailable, generate, is_available
    from flowlens.pipeline import build_narrative_request

    if not is_available():
        return {
            "available": False,
            "text": None,
            "reason": (
                "Разбор недоступен: не задан ANTHROPIC_API_KEY. "
                "Остальные отчёты работают без него."
            ),
        }

    label = "выбранный период"
    if filters.date_from and filters.date_to:
        label = f"{filters.date_from:%d.%m.%Y} — {filters.date_to:%d.%m.%Y}"
    elif filters.date_from:
        label = f"с {filters.date_from:%d.%m.%Y}"

    request = build_narrative_request(engine, filters, label)
    try:
        return {"available": True, "text": generate(request), "reason": None}
    except NarrativeUnavailable as exc:
        return {"available": False, "text": None, "reason": str(exc)}


@app.get("/api/tickets")
def get_tickets(
    engine: EngineDep,
    filters: FiltersDep,
    limit: Annotated[int, Query(le=500, description="Сколько записей вернуть")] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort: Annotated[str, Query()] = "cycle_time",
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
    search: Annotated[str | None, Query(description="Поиск по ключу или названию")] = None,
    only_open: Annotated[bool, Query()] = False,
    min_cycle_s: Annotated[int | None, Query()] = None,
    max_cycle_s: Annotated[int | None, Query()] = None,
    anomaly: Annotated[str | None, Query(description="Код аномалии")] = None,
) -> dict[str, Any]:
    """Список задач с метриками — данные, из которых построены графики."""
    return analytics.ticket_list(
        engine,
        filters,
        limit=limit,
        offset=offset,
        sort=sort,
        order=order,
        search=search,
        only_open=only_open,
        min_cycle_s=min_cycle_s,
        max_cycle_s=max_cycle_s,
        anomaly=anomaly,
    )


@app.get("/api/tickets/{key}")
def get_ticket(engine: EngineDep, key: str) -> dict[str, Any]:
    """Полная история задачи: события, интервалы, согласование дат."""
    detail = analytics.ticket_detail(engine, key)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Задача {key} не найдена")
    return detail


@app.get("/api/phase-intervals")
def get_phase_intervals(
    engine: EngineDep,
    filters: FiltersDep,
    phase: Annotated[str | None, Query(description="Фильтр по фазе")] = None,
) -> dict[str, Any]:
    """Интервалы по фазам — исходные данные графика распределения времени."""
    return analytics.phase_time_rows(engine, filters, phase)


@app.get("/api/filters")
def get_filter_options(engine: EngineDep) -> dict[str, Any]:
    """Доступные значения фильтров."""
    with engine.begin() as conn:
        types = conn.execute(
            text("SELECT DISTINCT issue_type FROM ticket ORDER BY 1")
        ).scalars().all()
        priorities = conn.execute(
            text("SELECT DISTINCT priority FROM ticket WHERE priority IS NOT NULL ORDER BY 1")
        ).scalars().all()
        components = conn.execute(
            text("SELECT DISTINCT unnest(components) AS c FROM ticket ORDER BY 1")
        ).scalars().all()
        teams = conn.execute(text("SELECT id, name FROM team ORDER BY name")).all()
        people = conn.execute(
            text("SELECT display_name FROM person ORDER BY display_name")
        ).scalars().all()
        period = conn.execute(
            text("SELECT min(created_at) AS earliest, max(created_at) AS latest FROM ticket")
        ).one()

    return {
        "issue_types": list(types),
        "priorities": list(priorities),
        "components": list(components),
        "teams": [{"id": t.id, "name": t.name} for t in teams],
        "people": list(people),
        "period": {
            "earliest": period.earliest.date().isoformat() if period.earliest else None,
            "latest": period.latest.date().isoformat() if period.latest else None,
        },
    }


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """Дашборд."""
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>FlowLens</h1><p>Файл дашборда не найден.</p>", status_code=404)
    return HTMLResponse(index.read_text(encoding="utf-8"))


__all__ = ["app"]
