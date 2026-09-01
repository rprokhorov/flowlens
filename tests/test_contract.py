"""Тесты контракта обмена коллектор↔ядро."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from flowlens.contract import (
    RawEvent,
    RawTicket,
    SourceProfile,
    iter_ndjson,
    read_ndjson,
    write_ndjson,
)

MSK = timezone(timedelta(hours=3))


def sample_ticket(key: str = "PROJ-1") -> RawTicket:
    return RawTicket(
        external_key=key,
        project_key="PROJ",
        issue_type="Bug",
        status="done",
        created_at=datetime(2026, 3, 2, 11, tzinfo=MSK),
        events=[
            RawEvent(
                kind="created",
                occurred_at=datetime(2026, 3, 2, 11, tzinfo=MSK),
                source_event_id=f"{key}-created",
            )
        ],
    )


def sample_profile() -> SourceProfile:
    return SourceProfile(source_kind="jira_dc", source_name="test")


# --- валидация ---------------------------------------------------------------


def test_naive_datetime_rejected() -> None:
    """Время без таймзоны нельзя сопоставлять между источниками."""
    with pytest.raises(ValidationError, match="timezone-aware"):
        RawTicket(
            external_key="P-1",
            project_key="P",
            issue_type="Bug",
            status="new",
            created_at=datetime(2026, 3, 2, 11),
        )


def test_event_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        RawEvent(kind="created", occurred_at=datetime(2026, 3, 2, 11))


def test_unknown_field_rejected() -> None:
    """Контракт строгий: неизвестные поля — ошибка, а не тихое игнорирование."""
    with pytest.raises(ValidationError):
        RawTicket.model_validate(
            {
                "external_key": "P-1",
                "project_key": "P",
                "issue_type": "Bug",
                "status": "new",
                "created_at": "2026-03-02T11:00:00+03:00",
                "неизвестное_поле": 1,
            }
        )


def test_events_require_created() -> None:
    with pytest.raises(ValidationError, match="created"):
        RawTicket(
            external_key="P-1",
            project_key="P",
            issue_type="Bug",
            status="new",
            created_at=datetime(2026, 3, 2, 11, tzinfo=MSK),
            events=[
                RawEvent(
                    kind="status_change",
                    occurred_at=datetime(2026, 3, 2, 12, tzinfo=MSK),
                )
            ],
        )


def test_empty_events_allowed() -> None:
    """Тикет без истории допустим: источник может её не отдавать."""
    ticket = RawTicket(
        external_key="P-1",
        project_key="P",
        issue_type="Bug",
        status="new",
        created_at=datetime(2026, 3, 2, 11, tzinfo=MSK),
    )
    assert ticket.events == []


def test_invalid_boundary_rejected() -> None:
    from flowlens.contract import RawDeclaredDate

    with pytest.raises(ValidationError):
        RawDeclaredDate(
            boundary="что-то",  # type: ignore[arg-type]
            value_at=datetime(2026, 3, 2, 11, tzinfo=MSK),
        )


# --- запись и чтение ---------------------------------------------------------


def test_roundtrip(tmp_path) -> None:
    path = tmp_path / "out.ndjson"
    tickets = [sample_ticket("P-1"), sample_ticket("P-2")]
    written = write_ndjson(path, tickets, sample_profile())
    assert written == 2

    profile, restored = read_ndjson(path)
    assert profile.source_name == "test"
    assert [t.external_key for t in restored] == ["P-1", "P-2"]
    assert restored[0].created_at == tickets[0].created_at


def test_first_line_is_profile(tmp_path) -> None:
    path = tmp_path / "out.ndjson"
    write_ndjson(path, [sample_ticket()], sample_profile())
    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert "_profile" in first


def test_missing_profile_rejected(tmp_path) -> None:
    path = tmp_path / "bad.ndjson"
    path.write_text('{"external_key": "P-1"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="_profile"):
        read_ndjson(path)


def test_malformed_line_reports_number(tmp_path) -> None:
    """Ошибка указывает на строку — иначе в выгрузке на 6000 тикетов не найти."""
    path = tmp_path / "bad.ndjson"
    write_ndjson(path, [sample_ticket()], sample_profile())
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"external_key": "broken"}\n')
    with pytest.raises(ValueError, match=":3:"):
        read_ndjson(path)


def test_blank_lines_ignored(tmp_path) -> None:
    path = tmp_path / "out.ndjson"
    write_ndjson(path, [sample_ticket()], sample_profile())
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n\n")
    _profile, tickets = read_ndjson(path)
    assert len(tickets) == 1


def test_streaming_read(tmp_path) -> None:
    path = tmp_path / "out.ndjson"
    write_ndjson(path, [sample_ticket(f"P-{i}") for i in range(50)], sample_profile())
    profile, stream = iter_ndjson(path)
    assert profile.source_name == "test"
    assert sum(1 for _ in stream) == 50


def test_unicode_preserved(tmp_path) -> None:
    path = tmp_path / "out.ndjson"
    ticket = sample_ticket()
    ticket.summary = "Починить экспорт «отчётов»"
    write_ndjson(path, [ticket], sample_profile())
    _profile, restored = read_ndjson(path)
    assert restored[0].summary == "Починить экспорт «отчётов»"


def test_profile_allows_extra_keys() -> None:
    """Профиль расширяемый: источник может сообщить что-то своё."""
    profile = SourceProfile.model_validate(
        {"source_kind": "custom", "source_name": "x", "своё_поле": 42}
    )
    assert profile.source_name == "x"
