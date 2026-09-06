"""HTTP API дашборда."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import Engine, text

from flowlens import analytics, auth
from flowlens.analytics import Filters
from flowlens.core.advice import analyse
from flowlens.core.quality import build_report
from flowlens.db import make_engine
from flowlens.repository import load_quality_rows


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Фоновое обновление данных, если оно включено.

    По умолчанию выключено: локальный запуск не должен молча ходить в Jira.
    Включается FLOWLENS_SCHEDULER=1 — для сервиса, а не для чужого ноутбука.
    """
    task: asyncio.Task[None] | None = None
    if os.environ.get("FLOWLENS_SCHEDULER") == "1":
        from flowlens.scheduler import run_forever

        task = asyncio.create_task(run_forever(get_engine()))
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title="FlowLens",
    description="Анализ потока задач",
    version="0.1.0",
    lifespan=lifespan,
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
    completion: Annotated[Literal["terminal", "work_done"], Query()] = "terminal",
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
        completion=completion,
    )


def scoped_filters(
    request: Request,
    filters: Annotated[Filters, Depends(get_filters)],
) -> Filters:
    """Сузить запрос правами пользователя.

    Проверка здесь, а не в каждом обработчике: аналитических эндпоинтов много,
    и забыть один из них означало бы отдать чужие данные. Единая точка делает
    это невозможным.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    if user.is_admin:
        return filters

    if filters.team_id is not None:
        if not user.can_view(filters.team_id):
            raise HTTPException(status_code=403, detail="Нет доступа к этой команде")
        return filters

    # Без явной команды показываем единственную доступную. Молча сводить
    # несколько команд в одну сводку нельзя: это выдало бы чужие метрики
    # под видом общих.
    visible = user.visible_teams
    if not visible:
        raise HTTPException(status_code=403, detail="Вам не назначена ни одна команда")
    if len(visible) > 1:
        raise HTTPException(
            status_code=400,
            detail="Укажите команду: вам доступно несколько",
        )
    return replace(filters, team_id=visible[0])


FiltersDep = Annotated[Filters, Depends(scoped_filters)]
EngineDep = Annotated[Engine, Depends(get_engine)]


# --- авторизация -------------------------------------------------------------

# Открытые пути: всё остальное закрыто. Список именно разрешающий — новый
# эндпоинт по умолчанию защищён, и забыть его закрыть невозможно.
PUBLIC_PATHS = frozenset({"/api/health", "/login", "/static", "/favicon.ico"})


def _is_public(path: str) -> bool:
    return any(path == item or path.startswith(item + "/") for item in PUBLIC_PATHS)


def current_user(request: Request) -> auth.User:
    """Пользователь текущего запроса.

    Проставляется middleware; сюда попадает уже проверенным.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    return user


UserDep = Annotated[auth.User, Depends(current_user)]


def require_team_view(user: auth.User, team_id: int | None) -> None:
    """Проверить доступ к данным команды."""
    if not user.can_view(team_id):
        raise HTTPException(status_code=403, detail="Нет доступа к этой команде")


def require_team_manage(user: auth.User, team_id: int | None) -> None:
    """Проверить право настраивать команду."""
    if not user.can_manage(team_id):
        raise HTTPException(
            status_code=403, detail="Настраивать команду может её владелец или администратор"
        )


@app.middleware("http")
async def authenticate_request(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Проверить вход до того, как запрос дойдёт до обработчика."""
    if auth.auth_disabled() or _is_public(request.url.path):
        request.state.user = auth.ANONYMOUS
        return await call_next(request)

    header = request.headers.get("Authorization", "")
    user = None
    if header.startswith("Basic "):
        import base64
        import binascii

        try:
            decoded = base64.b64decode(header[6:]).decode()
            username, _, password = decoded.partition(":")
            user = auth.authenticate(get_engine(), username, password)
        except auth.TooManyAttempts as exc:
            # 429, а не 401: браузер иначе снова покажет форму входа,
            # человек введёт тот же пароль и решит, что сервис сломался
            return Response(
                content=json.dumps({"detail": str(exc)}, ensure_ascii=False),
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(exc.retry_after)},
            )
        except (ValueError, binascii.Error, UnicodeDecodeError):
            user = None

    if user is None:
        return Response(
            content=json.dumps({"detail": "Требуется вход"}, ensure_ascii=False),
            status_code=401,
            media_type="application/json",
            # браузер сам покажет форму входа — отдельная страница не нужна
            headers={"WWW-Authenticate": 'Basic realm="FlowLens"'},
        )

    request.state.user = user
    return await call_next(request)


@app.get("/api/me")
def get_me(user: UserDep) -> dict[str, Any]:
    """Кто вошёл и что ему доступно — для интерфейса."""
    return {**user.as_dict(), "auth_disabled": auth.auth_disabled()}


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


@app.get("/api/blockers")
def get_blockers(engine: EngineDep, filters: FiltersDep) -> dict[str, Any]:
    """Блокировки: вероятность, потери по причинам, что висит сейчас."""
    return analytics.blockers(engine, filters)


@app.get("/api/sle")
def get_sle(engine: EngineDep, filters: FiltersDep) -> dict[str, Any]:
    """Действующие обещания и доля попаданий в них."""
    return analytics.sle_attainment(engine, filters)


@app.post("/api/sle")
def post_sle(
    engine: EngineDep,
    filters: FiltersDep,
    percentile: Annotated[int, Query(ge=1, le=99)] = 85,
    note: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Зафиксировать обещание по текущим данным."""
    try:
        return analytics.fix_sle(engine, filters, percentile=percentile, note=note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/transitions")
def get_transitions(engine: EngineDep, filters: FiltersDep) -> dict[str, Any]:
    """Матрица переходов между статусами с выделением возвратов."""
    return analytics.transition_matrix(engine, filters)


@app.get("/api/backlog")
def get_backlog(engine: EngineDep, filters: FiltersDep) -> dict[str, Any]:
    """Размер очереди и возраст лежащих в ней задач."""
    return analytics.backlog_age(engine, filters)


@app.get("/api/service-classes")
def get_service_classes(
    engine: EngineDep,
    filters: FiltersDep,
    granularity: Annotated[Literal["week", "month"], Query()] = "month",
) -> dict[str, Any]:
    """Доля срочных задач во времени и состав классов обслуживания."""
    return analytics.expedite_share(engine, filters, granularity)


@app.get("/api/hidden-queue")
def get_hidden_queue(engine: EngineDep, filters: FiltersDep) -> dict[str, Any]:
    """Ожидание, спрятанное внутри статусов, помеченных активной работой."""
    return analytics.hidden_queue(engine, filters)


@app.get("/api/predictability")
def get_predictability(
    engine: EngineDep,
    filters: FiltersDep,
    granularity: Annotated[Literal["week", "month"], Query()] = "month",
) -> dict[str, Any]:
    """Отношение хвоста времени цикла к медиане по периодам."""
    return analytics.predictability(engine, filters, granularity)


@app.get("/api/export")
def get_export(
    engine: EngineDep,
    filters: FiltersDep,
    full: Annotated[bool, Query(description="Не обезличивать данные")] = False,
) -> Response:
    """Выгрузить текущий срез в файл контракта.

    По умолчанию обезличено: ключи и имена — устойчивые псевдонимы, заголовки
    убраны. Метрики от этого не меняются, а файл можно отдать наружу.
    """
    import tempfile

    from flowlens.export import export_tickets

    with tempfile.NamedTemporaryFile(suffix=".ndjson", delete=False) as handle:
        path = Path(handle.name)
    try:
        stats = export_tickets(engine, filters, path, anonymize=not full)
        payload = path.read_bytes()
    finally:
        path.unlink(missing_ok=True)

    suffix = "full" if full else "anon"
    stamp = date.today().isoformat()
    return Response(
        content=payload,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="flowlens-{stamp}-{suffix}.ndjson"',
            "X-Flowlens-Tickets": str(stats.tickets),
            "X-Flowlens-Events": str(stats.events),
        },
    )


@app.get("/api/import/format")
def get_import_format() -> dict[str, Any]:
    """Описание ожидаемого формата — чтобы файл можно было собрать самому."""
    from flowlens.contract import CONTRACT_VERSION

    return {
        "version": CONTRACT_VERSION,
        "media_type": "application/x-ndjson",
        "structure": (
            "NDJSON: первая строка — профиль источника, "
            "каждая следующая — один тикет со всей его историей."
        ),
        "required_ticket_fields": [
            "external_key", "project_key", "issue_type", "status", "created_at",
        ],
        "required_event_fields": ["kind", "occurred_at"],
        "notes": [
            "Все даты — с часовым поясом (ISO 8601).",
            "Первым событием тикета обязан быть created.",
            "Статусы передаются как есть; сопоставление с фазами — на стороне ядра.",
            "Файл, выгруженный кнопкой «Выгрузить срез», подходит для импорта.",
        ],
        "example": {
            "profile": {
                "source_kind": "csv",
                "source_name": "my-tracker",
                "changelog": "full",
            },
            "ticket": {
                "external_key": "PROJ-1",
                "project_key": "PROJ",
                "issue_type": "Task",
                "status": "done",
                "created_at": "2026-01-15T10:00:00+03:00",
                "events": [
                    {"kind": "created", "occurred_at": "2026-01-15T10:00:00+03:00"},
                    {
                        "kind": "status_change",
                        "occurred_at": "2026-01-16T11:00:00+03:00",
                        "field": "status",
                        "old_value": "new",
                        "new_value": "in progress",
                    },
                ],
            },
        },
    }


@app.post("/api/import")
async def post_import(
    engine: EngineDep,
    file: Annotated[UploadFile, File(description="NDJSON или CSV с выгрузкой")],
    replace_existing: Annotated[bool, Form()] = False,
) -> dict[str, Any]:
    """Загрузить выгрузку и пересчитать метрики.

    Формат определяется по расширению: NDJSON контракта либо CSV, который
    приводится к контракту автоматически.
    """
    import tempfile

    from flowlens.contract import read_ndjson
    from flowlens.importer import import_tickets
    from flowlens.pipeline import recompute_all
    from flowlens.repository import reset_data

    name = (file.filename or "upload").lower()
    suffix = ".csv" if name.endswith(".csv") else ".ndjson"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(await file.read())
        path = Path(handle.name)

    try:
        if suffix == ".csv":
            from flowlens.collectors.csv_source import read_csv

            # имя источника берём из загруженного файла: путь во временной
            # директории случайный и в интерфейсе выглядит мусором
            profile, tickets = read_csv(path, source_name=Path(name).stem)
        else:
            profile, tickets = read_ndjson(path)

        if not tickets:
            raise HTTPException(status_code=422, detail="В файле нет ни одного тикета")

        if replace_existing:
            reset_data(engine)
        stats = import_tickets(engine, profile, tickets)
        recompute_all(engine)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Файл не разобран: {exc}") from exc
    finally:
        path.unlink(missing_ok=True)

    return {
        "tickets": stats.tickets,
        "events": stats.events,
        "people": stats.people,
        "source": profile.source_name,
        "replaced": replace_existing,
    }


class JiraCheckRequest(BaseModel):
    """Параметры проверки подключения к Jira."""

    base_url: str
    token: str | None = None
    username: str | None = None
    password: str | None = None
    verify_ssl: bool = True


class JiraSyncRequest(JiraCheckRequest):
    """Параметры выгрузки."""

    source_name: str = "jira"
    jql: str
    mapping: dict[str, str] = {}
    limit: int | None = None
    replace_existing: bool = False


@app.post("/api/jira/check")
def post_jira_check(request: JiraCheckRequest) -> dict[str, Any]:
    """Проверить доступ и предложить сопоставление полей.

    Секреты приходят в теле запроса и никуда не сохраняются: сессия проверки
    живёт ровно до ответа. Для регулярной синхронизации выдаётся YAML,
    в котором вместо токена стоит имя переменной окружения.
    """
    from flowlens.collectors.jira_setup import check_connection

    result = check_connection(
        request.base_url,
        token=request.token,
        username=request.username,
        password=request.password,
        verify_ssl=request.verify_ssl,
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error or "Не удалось подключиться")

    return {
        "user": result.user,
        "statuses": result.statuses,
        "guesses": [
            {
                "purpose": g.purpose,
                "field_id": g.field_id,
                "field_name": g.field_name,
                "confidence": g.confidence,
                "candidates": g.candidates,
            }
            for g in result.guesses
        ],
    }


@app.post("/api/jira/sync")
def post_jira_sync(engine: EngineDep, request: JiraSyncRequest) -> dict[str, Any]:
    """Выгрузить из Jira и пересчитать метрики."""
    from flowlens.collectors.jira import collect
    from flowlens.collectors.jira_setup import build_config, config_to_yaml
    from flowlens.importer import import_tickets
    from flowlens.pipeline import recompute_all
    from flowlens.repository import reset_data

    config = build_config(
        source_name=request.source_name,
        base_url=request.base_url,
        jql=request.jql,
        token=request.token,
        username=request.username,
        password=request.password,
        verify_ssl=request.verify_ssl,
        mapping=request.mapping,
    )
    try:
        profile, tickets = collect(config, limit=request.limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Выгрузка не удалась: {exc}") from exc

    if not tickets:
        raise HTTPException(status_code=422, detail="По этому JQL не нашлось ни одной задачи")

    if request.replace_existing:
        reset_data(engine)
    stats = import_tickets(engine, profile, tickets)
    recompute_all(engine)

    return {
        "tickets": stats.tickets,
        "events": stats.events,
        "people": stats.people,
        # YAML отдаётся, чтобы дальше запускать синхронизацию по расписанию
        # без интерфейса; токена в нём нет
        "config_yaml": config_to_yaml(config),
    }


@app.get("/api/teams")
def get_teams(engine: EngineDep, user: UserDep) -> list[dict[str, Any]]:
    """Команды с числом задач — для переключателя в дашборде.

    Показываются только доступные: чужие названия тоже информация.
    """
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT t.id, t.name, c.name AS calendar, c.tz, "
                "       count(ti.id) AS tickets "
                "FROM team t "
                "JOIN calendar c ON c.id = t.calendar_id "
                "LEFT JOIN ticket ti ON ti.team_id = t.id "
                "GROUP BY t.id, t.name, c.name, c.tz ORDER BY t.name"
            )
        ).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "calendar": r.calendar,
            "tz": r.tz,
            "tickets": r.tickets,
            "can_manage": user.can_manage(r.id),
        }
        for r in rows
        if user.can_view(r.id)
    ]


@app.get("/api/teams/{team_id}/statuses")
def get_team_statuses(engine: EngineDep, user: UserDep, team_id: int) -> list[dict[str, Any]]:
    """Как команда классифицирует статусы своей доски.

    От этого зависят flow efficiency, время по фазам и WIP: статус, помеченный
    активной работой, попадает в touch time, а помеченный очередью — в ожидание.
    """
    require_team_view(user, team_id)
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, external_name, CAST(phase AS text) AS phase, "
                "       is_active_work, is_queue, is_terminal, board_order "
                "FROM workflow_status WHERE team_id = :team "
                "ORDER BY board_order NULLS LAST, external_name"
            ),
            {"team": team_id},
        ).all()
    return [dict(r._mapping) for r in rows]


class StatusUpdate(BaseModel):
    """Изменение классификации одного статуса."""

    phase: str | None = None
    is_active_work: bool | None = None
    is_queue: bool | None = None
    board_order: int | None = None


@app.patch("/api/statuses/{status_id}")
def patch_status(
    engine: EngineDep, user: UserDep, status_id: int, update: StatusUpdate
) -> dict[str, Any]:
    """Изменить классификацию статуса и пересчитать метрики.

    Пересчёт обязателен: интервалы и все производные метрики зависят от того,
    считается ли статус работой. Без него дашборд показывал бы старые цифры
    под новой настройкой — худший вид расхождения, потому что незаметный.
    """
    from flowlens.pipeline import recompute_all

    with engine.begin() as conn:
        owner_team = conn.execute(
            text("SELECT team_id FROM workflow_status WHERE id = :id"),
            {"id": status_id},
        ).scalar_one_or_none()
    if owner_team is None:
        raise HTTPException(status_code=404, detail="Статус не найден")
    require_team_manage(user, owner_team)

    changes = {k: v for k, v in update.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(status_code=422, detail="Нечего менять")

    assignments = ", ".join(
        f"{key} = CAST(:{key} AS canonical_phase)" if key == "phase" else f"{key} = :{key}"
        for key in changes
    )
    with engine.begin() as conn:
        row = conn.execute(
            text(
                f"UPDATE workflow_status SET {assignments} WHERE id = :id "
                "RETURNING id, external_name, CAST(phase AS text) AS phase, "
                "          is_active_work, is_queue, board_order"
            ),
            {**changes, "id": status_id},
        ).one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail="Статус не найден")

    recompute_all(engine)
    return dict(row._mapping)


class TeamSourceRequest(BaseModel):
    """Подключение команды к источнику."""

    team_id: int
    base_url: str
    jql: str
    # пустой токен при обновлении означает «оставить прежний»: форма его
    # не показывает, и присылать заново при каждой правке не нужно
    token: str | None = None
    username: str | None = None
    field_mapping: dict[str, str] = {}
    verify_ssl: bool = True
    sync_interval_minutes: int | None = None


@app.get("/api/sources")
def get_sources(
    engine: EngineDep, user: UserDep, team_id: Annotated[int | None, Query()] = None
) -> dict[str, Any]:
    """Настроенные подключения. Токены не отдаются никогда."""
    from flowlens import secrets, sources

    items = [
        item
        for item in sources.list_sources(engine, team_id)
        if user.can_manage(item.team_id)
    ]
    return {
        "sources": [
            {**item.as_dict(), "next_run_at": (
                sources.next_run_at(item).isoformat()
                if sources.next_run_at(item) else None
            )}
            for item in items
        ],
        # без ключа шифрования сохранять подключения нельзя, и интерфейс
        # должен сказать об этом прямо, а не отказывать без объяснения
        "secrets_available": secrets.available(),
    }


@app.post("/api/sources")
def post_source(
    engine: EngineDep, user: UserDep, request: TeamSourceRequest
) -> dict[str, Any]:
    """Сохранить подключение команды."""
    from flowlens import secrets, sources

    require_team_manage(user, request.team_id)
    if request.token and not secrets.available():
        raise HTTPException(
            status_code=422,
            detail=(
                f"Не задан {secrets.ENV_KEY}: сохранять токены некуда. "
                "Задайте ключ шифрования в окружении сервиса."
            ),
        )
    try:
        saved = sources.save_source(
            engine,
            team_id=request.team_id,
            base_url=request.base_url,
            jql=request.jql,
            secret=request.token,
            username=request.username,
            field_mapping=request.field_mapping,
            verify_ssl=request.verify_ssl,
            sync_interval_minutes=request.sync_interval_minutes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return saved.as_dict()


@app.delete("/api/sources/{source_id}")
def delete_source_endpoint(
    engine: EngineDep, user: UserDep, source_id: int
) -> dict[str, Any]:
    """Удалить подключение. Загруженные данные остаются."""
    from flowlens import sources

    existing = sources.get_source(engine, source_id)
    if existing is not None:
        require_team_manage(user, existing.team_id)
    if not sources.delete_source(engine, source_id):
        raise HTTPException(status_code=404, detail="Подключение не найдено")
    return {"deleted": source_id}


@app.post("/api/sources/{source_id}/sync")
def post_source_sync(
    engine: EngineDep,
    user: UserDep,
    source_id: int,
    full: Annotated[bool, Query(description="Выгрузить заново, игнорируя watermark")] = False,
) -> dict[str, Any]:
    """Запустить синхронизацию подключения."""
    from flowlens import sources

    source = sources.get_source(engine, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Подключение не найдено")
    require_team_manage(user, source.team_id)
    if not source.has_secret:
        raise HTTPException(status_code=422, detail="У подключения не задан токен")

    try:
        return sources.sync_source(engine, source, full=full)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Синхронизация не удалась: {exc}") from exc


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
    html = index.read_text(encoding="utf-8")

    # Браузер кэширует /static/dashboard.js по неизменному пути и после
    # обновления отдаёт старую копию. Незнакомые ей вкладки showTab() молча
    # сводит к «Обзору» — снаружи это выглядит как «все вкладки одинаковые».
    # Подмешиваем время изменения файла в URL: меняется файл — меняется адрес.
    for asset in ("dashboard.js", "style.css"):
        path = STATIC_DIR / asset
        if path.exists():
            version = int(path.stat().st_mtime)
            html = html.replace(f"/static/{asset}", f"/static/{asset}?v={version}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


__all__ = ["app"]
