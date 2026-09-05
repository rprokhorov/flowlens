"""Тесты универсального CSV-коллектора."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from flowlens.collectors.csv_source import (
    describe_columns,
    parse_moment,
    read_csv,
)

MSK = ZoneInfo("Europe/Moscow")


def write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


TRANSITIONS = """key,type,status_from,status_to,changed_at,author
PROJ-1,Bug,new,in progress,2026-01-15T10:00:00+03:00,ivan
PROJ-1,Bug,in progress,qa,2026-01-16T14:30:00+03:00,ivan
PROJ-1,Bug,qa,done,2026-01-17T11:00:00+03:00,maria
PROJ-2,Task,new,in progress,2026-01-16T09:00:00+03:00,petr
"""

FLAT = """key,type,status,created,started,resolved
PROJ-10,Task,done,2026-01-15,2026-01-16,2026-01-20
PROJ-11,Bug,done,2026-01-16,2026-01-17,2026-01-19
PROJ-12,Task,in progress,2026-01-18,2026-01-19,
"""


# --- разбор дат --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "2026-01-15T10:00:00+03:00",
        "2026-01-15 10:00",
        "15.01.2026 10:00",
        "2026-01-15",
        "15.01.2026",
    ],
)
def test_parse_moment_accepts_common_formats(raw: str) -> None:
    parsed = parse_moment(raw)
    assert parsed is not None
    assert parsed.tzinfo is not None, "дата без пояса ломает расчёт интервалов"


def test_parse_moment_empty() -> None:
    assert parse_moment(None) is None
    assert parse_moment("  ") is None


def test_parse_moment_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="не разобрана дата"):
        parse_moment("позавчера")


def test_parse_moment_assumes_team_timezone() -> None:
    """Выгрузки почти никогда не содержат пояс — берём пояс команды."""
    parsed = parse_moment("2026-01-15 10:00", tz=MSK)
    assert parsed == datetime(2026, 1, 15, 10, 0, tzinfo=MSK)


# --- форма с историей переходов ----------------------------------------------


def test_transitions_shape_detected(tmp_path: Path) -> None:
    path = write(tmp_path, "t.csv", TRANSITIONS)
    assert describe_columns(path)["shape"] == "transitions"

    profile, tickets = read_csv(path)
    assert profile.changelog == "full"
    assert len(tickets) == 2


def test_transitions_build_event_log(tmp_path: Path) -> None:
    _, tickets = read_csv(write(tmp_path, "t.csv", TRANSITIONS))
    first = next(t for t in tickets if t.external_key == "PROJ-1")

    assert first.events[0].kind == "created"
    statuses = [e.new_value for e in first.events if e.kind == "status_change"]
    assert statuses == ["in progress", "qa", "done"]
    assert first.status == "done"


def test_transitions_created_precedes_first_move(tmp_path: Path) -> None:
    """Событие created обязано быть первым, иначе контракт не примет тикет."""
    _, tickets = read_csv(write(tmp_path, "t.csv", TRANSITIONS))
    for ticket in tickets:
        assert ticket.events[0].kind == "created"
        assert all(ticket.events[0].occurred_at <= e.occurred_at for e in ticket.events)


def test_transitions_keep_author(tmp_path: Path) -> None:
    _, tickets = read_csv(write(tmp_path, "t.csv", TRANSITIONS))
    first = next(t for t in tickets if t.external_key == "PROJ-1")
    authors = {e.actor for e in first.events if e.actor}
    assert authors == {"ivan", "maria"}


# --- плоская форма -----------------------------------------------------------


def test_flat_shape_detected(tmp_path: Path) -> None:
    path = write(tmp_path, "f.csv", FLAT)
    assert describe_columns(path)["shape"] == "flat"

    profile, tickets = read_csv(path)
    assert len(tickets) == 3
    # без истории переходов спорить с заявленными датами нечем
    assert profile.changelog == "none"
    assert profile.reconciliation_hint == "system_only"


def test_flat_builds_events_from_dates(tmp_path: Path) -> None:
    _, tickets = read_csv(write(tmp_path, "f.csv", FLAT))
    done = next(t for t in tickets if t.external_key == "PROJ-10")
    assert [e.kind for e in done.events] == ["created", "status_change", "status_change"]

    open_task = next(t for t in tickets if t.external_key == "PROJ-12")
    # незавершённая задача не получает события завершения
    assert len(open_task.events) == 2
    assert open_task.closed_at is None


# --- распознавание колонок ---------------------------------------------------


def test_russian_headers_and_semicolons(tmp_path: Path) -> None:
    content = (
        "Ключ;Тип;Из статуса;В статус;Дата;Автор\n"
        "PROJ-1;Bug;new;in progress;15.01.2026 10:00;Иван\n"
        "PROJ-1;Bug;in progress;done;16.01.2026 14:30;Иван\n"
    )
    profile, tickets = read_csv(write(tmp_path, "ru.csv", content))
    assert profile.changelog == "full"
    assert tickets[0].issue_type == "Bug"
    assert tickets[0].status == "done"


def test_tab_separated(tmp_path: Path) -> None:
    content = "key\tstatus\tcreated\nPROJ-1\tdone\t2026-01-15\n"
    _, tickets = read_csv(write(tmp_path, "t.tsv", content))
    assert len(tickets) == 1


def test_missing_key_column_explains_itself(tmp_path: Path) -> None:
    """Отказ должен объяснять, что искали и что нашли."""
    path = write(tmp_path, "bad.csv", "foo,bar\n1,2\n")
    with pytest.raises(ValueError, match="ключ"):
        read_csv(path)


def test_no_dates_at_all_is_rejected(tmp_path: Path) -> None:
    path = write(tmp_path, "nodates.csv", "key,status\nPROJ-1,done\n")
    with pytest.raises(ValueError, match="дата создания"):
        read_csv(path)


def test_empty_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="пуст"):
        read_csv(write(tmp_path, "empty.csv", "key,created\n"))


def test_describe_lists_unmatched_columns(tmp_path: Path) -> None:
    """Нераспознанное показывается, а не игнорируется молча."""
    content = "key,created,story points,epic link\nPROJ-1,2026-01-15,5,EPIC-1\n"
    described = describe_columns(write(tmp_path, "x.csv", content))
    assert "story points" in described["unmatched"]
    assert "epic link" in described["unmatched"]
