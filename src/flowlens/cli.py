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

    # горизонт — фактический текущий момент, иначе события уедут в будущее
    end = datetime.now(msk).replace(second=0, microsecond=0)
    start = end - timedelta(days=months * 30)
    span = max(1.0, (end - start).total_seconds())

    from flowlens.pipeline import _random_workday_moment

    seeds = list(all_scenarios(cal))
    for i in range(tickets):
        created = _random_workday_moment(start, end, cal, rng)
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


@app.command()
def advice(
    days: Annotated[int, typer.Option(help="За сколько последних дней смотреть")] = 90,
) -> None:
    """Что стоит посмотреть в процессе."""
    from datetime import timedelta

    from flowlens import analytics
    from flowlens.analytics import Filters
    from flowlens.core.advice import Severity, analyse
    from flowlens.core.quality import build_report
    from flowlens.repository import load_quality_rows

    engine = make_engine()
    filters = Filters(date_from=(datetime.now().date() - timedelta(days=days)))

    from flowlens.core.forecast import wip_health

    stats = analytics.summary(engine, filters)
    history = analytics.throughput_history(engine, filters, periods=12)
    per_day = (sum(history) / len(history) / 7) if history else 0.0
    cycle_days = (stats["p50_cycle_s"] / 3600 / 9) if stats["p50_cycle_s"] else 0.0

    report = build_report(load_quality_rows(engine))
    findings = analyse(
        summary=stats,
        flow=analytics.flow_efficiency(engine, filters),
        arrival=analytics.arrival_vs_throughput(engine, filters),
        aging=analytics.aging_wip(engine, filters),
        people=analytics.people_load(engine, filters),
        blockers=analytics.blockers(engine, filters),
        sle=analytics.sle_attainment(engine, filters),
        hidden=analytics.hidden_queue(engine, filters),
        predictability=analytics.predictability(engine, filters),
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

    if not findings:
        typer.echo(f"За последние {days} дней ничего примечательного не найдено.")
        return

    marks = {Severity.ACT: "!!", Severity.WATCH: " !", Severity.INFO: "  "}
    typer.echo(f"Наблюдения за последние {days} дней:\n")
    for finding in findings:
        typer.echo(f"{marks[finding.severity]} {finding.title}")
        typer.echo(f"     {finding.detail}")
        typer.echo(f"     → {finding.suggestion}")
        if finding.ticket_keys:
            typer.echo(f"     задачи: {', '.join(finding.ticket_keys)}")
        typer.echo("")


@app.command("import-csv")
def import_csv_command(
    path: Annotated[Path, typer.Argument(help="CSV-файл выгрузки")],
    replace: Annotated[
        bool, typer.Option("--replace", help="Заменить текущие данные")
    ] = False,
    describe: Annotated[
        bool, typer.Option("--describe", help="Только показать, что распозналось")
    ] = False,
) -> None:
    """Импортировать CSV-выгрузку из любого трекера."""
    from flowlens.collectors.csv_source import describe_columns, read_csv
    from flowlens.importer import import_tickets
    from flowlens.pipeline import recompute_all
    from flowlens.repository import reset_data

    if describe:
        info = describe_columns(path)
        shape = "история переходов" if info["shape"] == "transitions" else "плоский список"
        typer.echo(f"Форма: {shape}")
        typer.echo("Распознано:")
        for field, column in info["matched"].items():
            typer.echo(f"  {field:12} ← {column}")
        if info["unmatched"]:
            typer.echo(f"Не распознано (будет пропущено): {', '.join(info['unmatched'])}")
        return

    try:
        profile, tickets = read_csv(path)
    except ValueError as exc:
        typer.echo(f"Не могу разобрать файл: {exc}")
        raise typer.Exit(code=1) from exc

    engine = make_engine()
    if replace:
        reset_data(engine)
    stats = import_tickets(engine, profile, tickets)
    recompute_all(engine)

    typer.echo(f"Импортировано {stats.tickets} задач, {stats.events} событий.")
    if profile.changelog == "none":
        typer.echo(
            "В файле нет истории переходов: время по фазам и возвраты "
            "посчитать не из чего. Время цикла и пропускная способность работают."
        )


@app.command("export")
def export_command(
    output: Annotated[Path, typer.Option(help="Куда записать срез")] = Path("slice.ndjson"),
    days: Annotated[int, typer.Option(help="За сколько последних дней")] = 180,
    full: Annotated[
        bool,
        typer.Option("--full", help="Не обезличивать: сохранить ключи, заголовки, имена"),
    ] = False,
) -> None:
    """Выгрузить срез данных в файл — показать метрики без доступа к базе.

    По умолчанию обезличено: ключи и имена заменяются устойчивыми псевдонимами,
    заголовки убираются. Метрики от этого не меняются — они считаются
    по статусам и времени.
    """
    from datetime import timedelta

    from flowlens.analytics import Filters
    from flowlens.export import export_tickets

    engine = make_engine()
    filters = Filters(date_from=(datetime.now().date() - timedelta(days=days)))
    stats = export_tickets(engine, filters, output, anonymize=not full)

    kind = "как есть" if full else "обезличено"
    typer.echo(
        f"Выгружено {stats.tickets} задач и {stats.events} событий в {output} ({kind})."
    )
    if full:
        typer.echo(
            "Внимание: файл содержит ключи задач, заголовки и имена людей. "
            "Он уносит эти данные за пределы вашего контура."
        )


@app.command("sle")
def sle_command(
    fix: Annotated[
        bool, typer.Option("--fix", help="Зафиксировать обещание по текущим данным")
    ] = False,
    percentile: Annotated[int, typer.Option(help="Перцентиль обещания")] = 85,
    issue_type: Annotated[
        str | None, typer.Option(help="Только для одного типа задач")
    ] = None,
    days: Annotated[int, typer.Option(help="За сколько последних дней брать данные")] = 90,
    note: Annotated[str | None, typer.Option(help="Пометка о причине фиксации")] = None,
) -> None:
    """Ожидаемый уровень сервиса: зафиксировать обещание или проверить его."""
    from datetime import timedelta

    from flowlens import analytics
    from flowlens.analytics import Filters

    engine = make_engine()
    filters = Filters(
        date_from=(datetime.now().date() - timedelta(days=days)),
        issue_types=[issue_type] if issue_type else [],
    )

    if fix:
        try:
            result = analytics.fix_sle(engine, filters, percentile=percentile, note=note)
        except ValueError as exc:
            typer.echo(f"Не могу зафиксировать: {exc}")
            raise typer.Exit(code=1) from exc
        scope = result["issue_type"] or "все типы"
        hours = result["target_business_s"] / 3600
        typer.echo(
            f"Обещание зафиксировано: {result['percentile']}% задач ({scope}) "
            f"за {hours:.1f} рабочих часов, по выборке из {result['sample_size']} задач."
        )
        return

    report = analytics.sle_attainment(engine, filters)
    if not report["promises"]:
        typer.echo("Обещаний пока нет. Зафиксировать: flowlens sle --fix")
        return

    typer.echo("Действующие обещания:")
    for promise in report["promises"]:
        scope = promise["issue_type"] or "все типы"
        hours = promise["target_business_s"] / 3600
        typer.echo(
            f"  {promise['percentile']}% · {scope}: {hours:.1f} ч "
            f"(зафиксировано {promise['fixed_at'][:10]}, n={promise['sample_size']})"
        )

    if not report["periods"]:
        return
    typer.echo(f"\nПопадание при цели {report['target']:.0%}:")
    for period, value, total in zip(
        report["periods"], report["attainment"], report["counts"], strict=True
    ):
        if value is None:
            continue
        mark = "  " if value >= report["target"] - 0.05 else " !"
        typer.echo(f"{mark} {period}  {value:6.1%}  (n={total})")


@app.command()
def forecast(
    weeks: Annotated[int, typer.Option(help="Горизонт прогноза в неделях")] = 4,
    history: Annotated[int, typer.Option(help="Сколько недель истории брать")] = 12,
) -> None:
    """Вероятностный прогноз по историческому темпу."""
    from datetime import date as date_type

    from flowlens import analytics
    from flowlens.analytics import Filters
    from flowlens.core.forecast import (
        ThroughputSample,
        forecast_how_long,
        forecast_how_many,
        wip_health,
    )

    engine = make_engine()
    filters = Filters()
    values = analytics.throughput_history(engine, filters, periods=history)
    backlog = analytics.open_backlog_size(engine, filters)
    sample = ThroughputSample(values=values, period_days=7)

    typer.echo(f"Темп закрытия по неделям: {values}")
    typer.echo(f"Незавершённых задач: {backlog}\n")

    how_long = forecast_how_long(backlog, sample, start=date_type.today())
    if how_long.warning:
        typer.echo(f"! {how_long.warning}\n")
    if how_long.percentiles:
        typer.echo("Когда закончим текущий объём:")
        for level in (50, 70, 85, 95):
            periods = how_long.percentiles.get(level)
            when = how_long.dates.get(level, "")
            typer.echo(f"  с вероятностью {level}%: {periods:3} нед  {when}")
        typer.echo("")

    how_many = forecast_how_many(weeks, sample)
    if how_many.percentiles:
        typer.echo(f"Сколько закроем за {weeks} нед:")
        for level in (50, 70, 85, 95):
            typer.echo(
                f"  с вероятностью {level}%: не менее {how_many.percentiles[level]:4} задач"
            )
        typer.echo("")

    stats = analytics.summary(engine, filters)
    wip = analytics.average_wip(engine, filters)
    per_day = (sum(values) / len(values) / 7) if values else 0.0
    cycle_days = (stats["p50_cycle_s"] / 3600 / 9) if stats["p50_cycle_s"] else 0.0
    health = wip_health(wip, per_day, cycle_days)
    if health.get("verdict"):
        typer.echo(
            f"Незавершённая работа: {wip:.0f} задач при темпе {per_day:.1f} в день."
        )
        typer.echo(
            f"  измеренное время цикла: {health['measured_days']} дн, "
            f"следует из объёма: {health['implied_days']} дн"
        )
        typer.echo(f"  {health['verdict']}")


@app.command()
def explain(
    days: Annotated[int, typer.Option(help="За сколько дней разбирать")] = 90,
    model: Annotated[str | None, typer.Option(help="Модель Claude")] = None,
) -> None:
    """Текстовый разбор метрик.

    Требует ANTHROPIC_API_KEY в окружении. Остальные отчёты работают без него.
    """
    from datetime import timedelta

    from flowlens.analytics import Filters
    from flowlens.core.narrative import MODEL, NarrativeUnavailable, generate
    from flowlens.pipeline import build_narrative_request

    engine = make_engine()
    since = datetime.now().date() - timedelta(days=days)
    filters = Filters(date_from=since)
    label = f"последние {days} дней (с {since:%d.%m.%Y})"

    typer.echo("Собираю метрики…")
    request = build_narrative_request(engine, filters, label)

    typer.echo("Запрашиваю разбор…\n")
    try:
        text = generate(request, model=model or MODEL)
    except NarrativeUnavailable as exc:
        typer.echo(f"{exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(text)


@app.command("sources")
def sources_command(
    sync: Annotated[
        bool, typer.Option("--sync", help="Синхронизировать те, которым пора")
    ] = False,
) -> None:
    """Подключения команд к источникам: список или запуск обновления."""
    from flowlens import secrets
    from flowlens import sources as sources_module

    engine = make_engine()

    if sync:
        from flowlens.scheduler import run_due

        results = run_due(engine)
        if not results:
            typer.echo("Нет подключений, которым пора обновиться.")
            return
        for item in results:
            if item["status"] == "ok":
                typer.echo(f"  #{item['source_id']}: загружено {item['tickets']} задач")
            else:
                typer.echo(f"  #{item['source_id']}: ошибка — {item['error']}")
        return

    items = sources_module.list_sources(engine)
    if not items:
        typer.echo(
            "Подключений нет. Настроить: кнопка «Импорт данных» в дашборде "
            "→ «Подключить Jira»."
        )
        if not secrets.available():
            typer.echo(
                f"Внимание: не задан {secrets.ENV_KEY} — сохранять токены будет некуда."
            )
        return

    for item in items:
        schedule = (
            f"каждые {item.sync_interval_minutes} мин"
            if item.sync_interval_minutes
            else "вручную"
        )
        status = item.last_sync_status or "ещё не запускалась"
        typer.echo(f"#{item.id}  {item.base_url}  команда {item.team_id}  [{schedule}]")
        typer.echo(f"     JQL: {item.jql}")
        typer.echo(f"     последняя синхронизация: {status}")
        if item.last_sync_error:
            typer.echo(f"     ошибка: {item.last_sync_error}")
        if not item.has_secret:
            typer.echo("     токен не задан — синхронизация невозможна")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Адрес")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Порт")] = 8000,
    reload: Annotated[bool, typer.Option("--reload", help="Перезапуск при правках")] = False,
) -> None:
    """Запустить дашборд."""
    import uvicorn

    typer.echo(f"Дашборд: http://{host}:{port}")
    uvicorn.run(
        "flowlens.api.app:app", host=host, port=port, reload=reload, log_level="info"
    )


if __name__ == "__main__":
    app()
