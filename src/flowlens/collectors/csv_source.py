"""Универсальный вход: CSV-выгрузка из любого трекера.

Каждая система умеет отдавать CSV, поэтому один этот коллектор покрывает
и те трекеры, под которые отдельного коллектора нет и не будет.

Поддерживаются две формы, потому что выгрузки бывают именно такими:

**История переходов** — по строке на смену статуса. Лучший вариант: из него
восстанавливается полный event log, и работают все метрики.

    ticket,status_from,status_to,changed_at,author
    PROJ-1,new,in progress,2026-01-16T11:00:00+03:00,ivan

**Плоский список задач** — по строке на задачу, с датами начала и конца.
Здесь истории переходов нет, поэтому время по фазам и возвраты посчитать
не из чего; cycle time и throughput работают. Такую выгрузку honest-путь
помечает `changelog="none"`, и ядро само выбирает политику согласования.

    key,type,status,created,started,resolved
    PROJ-1,Task,done,2026-01-15,2026-01-16,2026-01-20

Имена колонок распознаются по синонимам — русским и английским. Что не
распозналось, перечисляется в ошибке: угадывать молча хуже, чем отказаться.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flowlens.contract import RawEvent, RawTicket, SourceProfile

DEFAULT_TZ = ZoneInfo("Europe/Moscow")

# Синонимы колонок. Порядок важен: более специфичное имя проверяется раньше,
# иначе «created» перехватит «created_by».
_ALIASES: dict[str, tuple[str, ...]] = {
    "key": ("key", "issue", "issue key", "ticket", "id", "ключ", "задача", "номер"),
    "status_from": ("status_from", "from", "from status", "old status", "из статуса", "было"),
    "status_to": ("status_to", "to", "to status", "new status", "в статус", "стало"),
    "changed_at": (
        "changed_at", "changed", "date", "timestamp", "when", "дата", "время", "изменено",
    ),
    "author": ("author", "actor", "user", "автор", "кто"),
    "status": ("status", "current status", "статус", "состояние"),
    "issue_type": ("issue_type", "type", "issuetype", "тип", "тип задачи"),
    "priority": ("priority", "приоритет"),
    "summary": ("summary", "title", "name", "заголовок", "название", "тема"),
    "project": ("project", "project key", "проект"),
    "assignee": ("assignee", "исполнитель", "назначен"),
    "reporter": ("reporter", "автор задачи", "постановщик"),
    "created": ("created", "created_at", "created date", "создано", "дата создания"),
    "started": (
        "started", "started_at", "start date", "work started", "начато", "дата начала",
    ),
    "resolved": (
        "resolved", "resolved_at", "closed", "closed_at", "done", "completed",
        "завершено", "дата завершения", "закрыто",
    ),
}


def _normalize(name: str) -> str:
    return name.strip().lower().replace("_", " ").replace("-", " ")


def _match_columns(header: list[str]) -> dict[str, str]:
    """Сопоставить колонки файла с полями контракта."""
    found: dict[str, str] = {}
    normalized = {_normalize(column): column for column in header}

    for field, aliases in _ALIASES.items():
        for alias in aliases:
            candidate = _normalize(alias)
            if candidate in normalized:
                found[field] = normalized[candidate]
                break
    return found


def parse_moment(value: str | None, *, tz: ZoneInfo = DEFAULT_TZ) -> datetime | None:
    """Разобрать дату в любом из ходовых форматов.

    Дата без времени считается началом дня: это заметно точнее, чем полночь UTC,
    и честнее, чем выдумывать час. Наивное время получает часовой пояс команды —
    выгрузки почти никогда его не содержат.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for fmt in (
            "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
            "%d/%b/%y %I:%M %p", "%d/%b/%Y %H:%M",
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
            "%m/%d/%Y %H:%M:%S", "%m/%d/%Y",
        ):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError(f"не разобрана дата: {value!r}") from None

    if isinstance(parsed, datetime) and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def _as_datetime(value: date | datetime, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time(9, 0), tzinfo=tz)


def _sniff(path: Path) -> csv.Dialect | type[csv.Dialect]:
    """Определить разделитель: выгрузки бывают и с запятой, и с точкой с запятой."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel


def read_csv(
    path: Path,
    *,
    tz: ZoneInfo = DEFAULT_TZ,
    source_name: str | None = None,
) -> tuple[SourceProfile, list[RawTicket]]:
    """Прочитать CSV и привести к контракту."""
    dialect = _sniff(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, dialect=dialect)
        header = reader.fieldnames or []
        columns = _match_columns(header)
        rows = list(reader)

    if "key" not in columns:
        raise ValueError(
            "не найдена колонка с ключом задачи. Ожидается одна из: "
            + ", ".join(_ALIASES["key"])
            + f". В файле есть: {', '.join(header) or '(пусто)'}"
        )
    if not rows:
        raise ValueError("файл пуст")

    has_transitions = "status_to" in columns and "changed_at" in columns
    if has_transitions:
        tickets = _from_transitions(rows, columns, tz)
        changelog = "full"
    else:
        tickets = _from_flat(rows, columns, tz)
        changelog = "none"

    profile = SourceProfile(
        source_kind="csv",
        source_name=source_name or path.stem,
        changelog=changelog,
        # без истории переходов заявленные даты — единственный источник границ,
        # и спорить с ними нечем
        reconciliation_hint="prefer_declared" if changelog == "full" else "system_only",
        exported_at=datetime.now(UTC),
        ticket_count=len(tickets),
    )
    return profile, tickets


def _cell(row: dict[str, str], columns: dict[str, str], field: str) -> str | None:
    column = columns.get(field)
    if column is None:
        return None
    value = (row.get(column) or "").strip()
    return value or None


def _from_transitions(
    rows: list[dict[str, str]], columns: dict[str, str], tz: ZoneInfo
) -> list[RawTicket]:
    """Собрать тикеты из строк-переходов."""
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = _cell(row, columns, "key")
        if key:
            grouped[key].append(row)

    tickets: list[RawTicket] = []
    for key, group in grouped.items():
        moments: list[tuple[datetime, dict[str, str]]] = []
        for row in group:
            at = parse_moment(_cell(row, columns, "changed_at"), tz=tz)
            if at is not None:
                moments.append((at, row))
        if not moments:
            continue
        moments.sort(key=lambda pair: pair[0])

        first_at, first_row = moments[0]
        created = parse_moment(_cell(first_row, columns, "created"), tz=tz) or first_at
        # Событие created обязано быть первым: если переход случился раньше
        # даты создания, доверяем переходу — он подтверждён историей.
        created = min(created, first_at)

        events: list[RawEvent] = [RawEvent(kind="created", occurred_at=created)]
        for at, row in moments:
            events.append(
                RawEvent(
                    kind="status_change",
                    occurred_at=at,
                    actor=_cell(row, columns, "author"),
                    field="status",
                    old_value=_cell(row, columns, "status_from"),
                    new_value=_cell(row, columns, "status_to"),
                )
            )

        last_row = moments[-1][1]
        status = _cell(last_row, columns, "status_to") or "new"
        resolved = parse_moment(_cell(last_row, columns, "resolved"), tz=tz)

        tickets.append(
            RawTicket(
                external_key=key,
                project_key=_cell(first_row, columns, "project") or key.split("-")[0],
                issue_type=_cell(first_row, columns, "issue_type") or "Task",
                priority=_cell(first_row, columns, "priority"),
                status=status,
                summary=_cell(first_row, columns, "summary") or "",
                created_at=created,
                resolved_at=resolved,
                closed_at=resolved,
                assignee=_cell(last_row, columns, "assignee"),
                reporter=_cell(first_row, columns, "reporter"),
                events=events,
            )
        )
    return tickets


def _from_flat(
    rows: list[dict[str, str]], columns: dict[str, str], tz: ZoneInfo
) -> list[RawTicket]:
    """Собрать тикеты из плоского списка с датами начала и конца.

    История переходов отсутствует, поэтому событий ровно столько, сколько дат:
    выдумывать промежуточные статусы нельзя — это подменило бы данные оценкой.
    """
    if "created" not in columns:
        raise ValueError(
            "в файле нет ни истории переходов, ни даты создания задач. "
            "Нужны либо колонки перехода (status_to + changed_at), "
            "либо дата создания."
        )

    tickets: list[RawTicket] = []
    for row in rows:
        key = _cell(row, columns, "key")
        created = parse_moment(_cell(row, columns, "created"), tz=tz)
        if not key or created is None:
            continue

        started = parse_moment(_cell(row, columns, "started"), tz=tz)
        resolved = parse_moment(_cell(row, columns, "resolved"), tz=tz)
        status = _cell(row, columns, "status") or ("done" if resolved else "new")

        events: list[RawEvent] = [RawEvent(kind="created", occurred_at=created)]
        if started and started >= created:
            events.append(
                RawEvent(
                    kind="status_change",
                    occurred_at=started,
                    field="status",
                    old_value="new",
                    new_value="in progress",
                )
            )
        if resolved and resolved >= created:
            events.append(
                RawEvent(
                    kind="status_change",
                    occurred_at=resolved,
                    field="status",
                    old_value="in progress" if started else "new",
                    new_value=status,
                )
            )

        tickets.append(
            RawTicket(
                external_key=key,
                project_key=_cell(row, columns, "project") or key.split("-")[0],
                issue_type=_cell(row, columns, "issue_type") or "Task",
                priority=_cell(row, columns, "priority"),
                status=status,
                summary=_cell(row, columns, "summary") or "",
                created_at=created,
                resolved_at=resolved,
                closed_at=resolved,
                assignee=_cell(row, columns, "assignee"),
                reporter=_cell(row, columns, "reporter"),
                events=events,
            )
        )
    return tickets


def describe_columns(path: Path) -> dict[str, Any]:
    """Что распозналось в файле — для подсказки перед импортом."""
    dialect = _sniff(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, dialect=dialect)
        header = reader.fieldnames or []
    columns = _match_columns(header)
    return {
        "header": header,
        "matched": columns,
        "unmatched": [c for c in header if c not in columns.values()],
        "shape": "transitions" if ("status_to" in columns and "changed_at" in columns) else "flat",
    }


__all__ = ["describe_columns", "parse_moment", "read_csv"]
