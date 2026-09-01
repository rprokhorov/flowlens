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

    findings = analyse(
        summary=analytics.summary(engine, filters),
        flow=analytics.flow_efficiency(engine, filters),
        arrival=analytics.arrival_vs_throughput(engine, filters),
        aging=analytics.aging_wip(engine, filters),
        people=analytics.people_load(engine, filters),
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


@app.get("/api/interventions")
def get_interventions(engine: EngineDep, filters: FiltersDep) -> list[dict[str, Any]]:
    """Отметки о значимых изменениях в процессе."""
    return analytics.interventions(engine, filters)


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
