"""CLI FlowLens."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import text

from flowlens.db import make_engine
from flowlens.importer import import_file, last_watermark, set_watermark
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


@app.command("export-demo")
def export_demo_command(
    output: Annotated[Path, typer.Option(help="Куда записать NDJSON")] = Path("demo.ndjson"),
    tickets: Annotated[int, typer.Option(help="Сколько случайных тикетов")] = 500,
    months: Annotated[int, typer.Option(help="Глубина истории в месяцах")] = 12,
    seed: Annotated[int, typer.Option(help="Зерно генератора")] = 2026,
) -> None:
    """Выгрузить синтетику в формате контракта (как это сделал бы коллектор)."""
    import random
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from flowlens.contract import write_ndjson
    from flowlens.core.calendar import WorkCalendar
    from flowlens.testing.export import seed_to_raw_ticket, synthetic_profile
    from flowlens.testing.scenarios import all_scenarios, random_ticket

    msk = ZoneInfo("Europe/Moscow")
    cal = WorkCalendar(name="team", tz="Europe/Moscow")
    rng = random.Random(seed)

    end = datetime.now(msk).replace(hour=11, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=months * 30)
    span = max(1.0, (end - start).total_seconds())

    seeds = list(all_scenarios(cal))
    for i in range(tickets):
        created = start + timedelta(seconds=rng.random() * span)
        created = created.replace(hour=11, minute=0, second=0, microsecond=0)
        if created.weekday() >= 5:
            created += timedelta(days=7 - created.weekday())
        recency = (created - start).total_seconds() / span
        seeds.append(
            random_ticket(
                cal,
                f"FLOW-{i + 1000}",
                created,
                rng,
                open_chance=0.6 * recency**8,
                horizon=end,
            )
        )

    raw = [seed_to_raw_ticket(s) for s in seeds]
    count = write_ndjson(output, raw, synthetic_profile(ticket_count=len(raw)))
    size_kb = output.stat().st_size / 1024
    typer.echo(f"Записано {count} тикетов в {output} ({size_kb:.0f} КБ)")


@app.command("import")
def import_command(
    path: Annotated[Path, typer.Argument(help="NDJSON-файл выгрузки")],
    team: Annotated[str, typer.Option(help="Команда, к которой отнести тикеты")] = "core",
    recompute_after: Annotated[
        bool, typer.Option("--recompute/--no-recompute", help="Пересчитать после импорта")
    ] = True,
) -> None:
    """Импортировать выгрузку коллектора в базу."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not path.exists():
        typer.echo(f"Файл не найден: {path}", err=True)
        raise typer.Exit(1)

    engine = make_engine()
    started = time.monotonic()
    stats = import_file(engine, path, team_name=team)
    typer.echo(f"Импортировано: {stats.summary()} за {time.monotonic() - started:.1f} с")

    if stats.unknown_statuses:
        typer.echo(
            "Статусы без сопоставления: " + ", ".join(sorted(stats.unknown_statuses)),
            err=True,
        )

    if recompute_after:
        started = time.monotonic()
        result = recompute_all(engine)
        typer.echo(
            f"Пересчитано: {result['tickets']} тикетов, {result['intervals']} интервалов "
            f"за {time.monotonic() - started:.1f} с"
        )


@app.command("sync")
def sync_command(
    config: Annotated[Path, typer.Option(help="YAML-конфиг коллектора")],
    full: Annotated[bool, typer.Option("--full", help="Игнорировать watermark")] = False,
    limit: Annotated[int | None, typer.Option(help="Ограничить число тикетов")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Только выгрузить в файл, не писать в базу")
    ] = False,
    output: Annotated[Path | None, typer.Option(help="Файл для выгрузки")] = None,
) -> None:
    """Синхронизировать данные из Jira.

    Без --full выгружаются только тикеты, изменённые после прошлой синхронизации.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from flowlens.collectors.jira import CollectorConfig, collect
    from flowlens.contract import write_ndjson
    from flowlens.importer import import_tickets

    collector_config = CollectorConfig.from_yaml(config)
    engine = make_engine()

    since = None if full else last_watermark(engine, collector_config.source_name)
    if since:
        typer.echo(f"Инкрементальная выгрузка с {since:%Y-%m-%d %H:%M}")
    else:
        typer.echo("Полная выгрузка")

    started = time.monotonic()
    profile, tickets = collect(collector_config, since=since, limit=limit)
    typer.echo(f"Получено {len(tickets)} тикетов за {time.monotonic() - started:.1f} с")

    if output:
        write_ndjson(output, tickets, profile)
        typer.echo(f"Выгрузка записана в {output}")

    if dry_run:
        typer.echo("Режим --dry-run: база не изменена")
        return

    stats = import_tickets(engine, profile, tickets)
    typer.echo(f"Импортировано: {stats.summary()}")

    newest = max((t.updated_at for t in tickets if t.updated_at), default=None)
    if newest:
        set_watermark(engine, collector_config.source_name, newest)

    result = recompute_all(engine)
    typer.echo(f"Пересчитано: {result['tickets']} тикетов, {result['intervals']} интервалов")


@app.command("quality")
def quality_command(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Показать примеры задач")
    ] = False,
) -> None:
    """Отчёт о качестве данных.

    Показывает, какой доле метрик можно доверять и что мешает доверять остальным.
    """
    from flowlens.core.quality import build_report
    from flowlens.repository import load_quality_rows

    engine = make_engine()
    rows = load_quality_rows(engine)
    if not rows:
        typer.echo("Нет данных. Сначала выполните import или seed-demo.", err=True)
        raise typer.Exit(1)

    report = build_report(rows)

    typer.echo(f"Всего задач:              {report.total_tickets}")
    typer.echo(f"С заявленными датами:     {report.declared_coverage_pct}%")
    typer.echo(f"Надёжных метрик:          {report.trustworthy_pct}%")
    typer.echo("")

    typer.echo("Достоверность:")
    for level in ("high", "medium", "low"):
        count = report.by_confidence.get(level, 0)
        if count:
            share = 100 * count / report.total_tickets
            typer.echo(f"  {level:8} {count:6}  ({share:.1f}%)")

    typer.echo("")
    typer.echo("Источник итогового значения:")
    for source, count in sorted(report.by_source.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {source:10} {count:6}")

    if report.anomalies:
        typer.echo("")
        typer.echo("Проблемы:")
        for group in report.anomalies:
            typer.echo(f"  {group.count:5}  {group.label}")
            if verbose:
                typer.echo(f"         {group.hint}")
                if group.sample_keys:
                    typer.echo(f"         например: {', '.join(group.sample_keys)}")

    typer.echo("")
    typer.echo(report.verdict())


@app.command("recompute-policy")
def recompute_policy_command(
    prefer: Annotated[
        str, typer.Option(help="Что предпочитать: declared | system | system_only")
    ] = "declared",
    threshold_days: Annotated[
        float, typer.Option(help="Порог расхождения в рабочих днях")
    ] = 3.0,
    on_conflict: Annotated[
        str,
        typer.Option(help="При конфликте: use_declared_flag_anomaly | use_system_flag_anomaly"),
    ] = "use_declared_flag_anomaly",
    version: Annotated[str, typer.Option(help="Метка версии политики")] = "custom",
) -> None:
    """Пересчитать метрики с другой политикой согласования.

    Обращения к источнику не требуется: всё строится заново из event log.
    """
    from flowlens.core.reconciliation import ReconciliationPolicy

    policy = ReconciliationPolicy(
        version=version,
        prefer=prefer,
        discrepancy_threshold_business_days=threshold_days,
        on_conflict=on_conflict,
    )
    engine = make_engine()
    started = time.monotonic()
    result = recompute_all(engine, policy=policy)
    typer.echo(
        f"Пересчитано по политике '{version}': {result['tickets']} задач, "
        f"с аномалиями {result['anomalous']}, за {time.monotonic() - started:.1f} с"
    )


if __name__ == "__main__":
    app()
