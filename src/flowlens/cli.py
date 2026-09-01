"""CLI FlowLens."""

from __future__ import annotations

import time
from typing import Annotated

import typer
from sqlalchemy import text

from flowlens.db import make_engine
from flowlens.pipeline import recompute_all, seed_demo

app = typer.Typer(help="FlowLens — анализ потока задач", no_args_is_help=True)


@app.command("seed-demo")
def seed_demo_command(
    tickets: Annotated[int, typer.Option(help="Сколько случайных тикетов создать")] = 500,
    months: Annotated[int, typer.Option(help="Глубина истории в месяцах")] = 12,
    seed: Annotated[int, typer.Option(help="Зерно генератора")] = 2026,
    keep: Annotated[bool, typer.Option("--keep", help="Не очищать базу")] = False,
) -> None:
    """Заполнить базу синтетическими данными."""
    engine = make_engine()
    started = time.monotonic()
    result = seed_demo(
        engine, ticket_count=tickets, months=months, seed=seed, reset=not keep
    )
    elapsed = time.monotonic() - started
    typer.echo(
        f"Создано: {result.tickets} тикетов, {result.events} событий, "
        f"{result.people} человек за {elapsed:.1f} с"
    )


@app.command()
def recompute(
    all_tickets: Annotated[bool, typer.Option("--all", help="Пересчитать всё")] = False,
    ticket: Annotated[str | None, typer.Option(help="Ключ одного тикета")] = None,
) -> None:
    """Пересчитать интервалы, метрики и нагрузку."""
    if not all_tickets and not ticket:
        typer.echo("Укажите --all или --ticket KEY", err=True)
        raise typer.Exit(1)

    engine = make_engine()
    started = time.monotonic()
    result = recompute_all(engine)
    elapsed = time.monotonic() - started
    typer.echo(
        f"Пересчитано: {result['tickets']} тикетов, "
        f"{result['intervals']} интервалов за {elapsed:.1f} с"
    )


@app.command()
def stats() -> None:
    """Показать сводку по данным."""
    engine = make_engine()
    with engine.begin() as conn:
        counts = conn.execute(
            text(
                "SELECT (SELECT count(*) FROM ticket) AS tickets, "
                "       (SELECT count(*) FROM ticket_event) AS events, "
                "       (SELECT count(*) FROM ticket_interval) AS intervals, "
                "       (SELECT count(*) FROM ticket_metrics) AS metrics, "
                "       (SELECT count(*) FROM person_workload_daily) AS workload"
            )
        ).one()
        flow = conn.execute(
            text(
                "SELECT count(*) FILTER (WHERE cycle_time_business_s IS NOT NULL) AS completed, "
                "       round(avg(flow_efficiency)::numeric, 3) AS avg_fe, "
                "       round(avg(cycle_time_business_s) / 3600.0, 1) AS avg_cycle_h "
                "FROM ticket_metrics"
            )
        ).one()

    typer.echo(f"Тикетов:    {counts.tickets}")
    typer.echo(f"Событий:    {counts.events}")
    typer.echo(f"Интервалов: {counts.intervals}")
    typer.echo(f"Метрик:     {counts.metrics}")
    typer.echo(f"Нагрузка:   {counts.workload} записей")
    typer.echo("")
    typer.echo(f"Завершено:            {flow.completed}")
    typer.echo(f"Средний flow eff.:    {flow.avg_fe}")
    typer.echo(f"Средний cycle time:   {flow.avg_cycle_h} рабочих часов")


if __name__ == "__main__":
    app()
