"""Тесты коллектора Jira: разбор ответов API в контракт."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from tests.fixtures.jira_samples import (
    comments_sample,
    issue_bulk_move,
    issue_happy_path,
    issue_subtask,
    issue_with_blocking,
)

from flowlens.collectors.jira import (
    CollectorConfig,
    FieldMapping,
    issue_to_ticket,
    parse_jira_datetime,
)
from flowlens.collectors.jira_client import JiraConfig

MSK = timezone(timedelta(hours=3))


@pytest.fixture
def mapping() -> FieldMapping:
    return FieldMapping(
        work_start="customfield_10014",
        work_end="customfield_10015",
        story_points="customfield_10016",
        epic_link="customfield_10017",
        declared_precision="minute",
    )


# --- разбор дат --------------------------------------------------------------


def test_parse_jira_datetime_with_offset() -> None:
    parsed = parse_jira_datetime("2026-03-02T11:00:00.000+0300")
    assert parsed == datetime(2026, 3, 2, 11, 0, tzinfo=MSK)


def test_parse_jira_datetime_utc() -> None:
    parsed = parse_jira_datetime("2026-03-02T08:00:00.000+0000")
    assert parsed is not None
    assert parsed.astimezone(UTC).hour == 8


def test_parse_jira_datetime_none() -> None:
    assert parse_jira_datetime(None) is None
    assert parse_jira_datetime("") is None


def test_parse_jira_datetime_garbage() -> None:
    assert parse_jira_datetime("не дата") is None


def test_parse_rejects_naive_datetime() -> None:
    """Дата без таймзоны отбрасывается: сравнивать её не с чем."""
    assert parse_jira_datetime("2026-03-02T11:00:00.000") is None


# --- базовые поля ------------------------------------------------------------


def test_basic_fields_mapped(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_happy_path(), mapping)
    assert ticket.external_key == "PROJ-101"
    assert ticket.external_id == "10001"
    assert ticket.project_key == "PROJ"
    assert ticket.issue_type == "Bug"
    assert ticket.priority == "High"
    assert ticket.status == "done"
    assert ticket.summary == "Починить экспорт отчётов"
    assert ticket.components == ["reports", "api"]
    assert ticket.labels == ["regression"]
    assert not ticket.is_subtask


def test_custom_fields_mapped(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_happy_path(), mapping)
    assert ticket.story_points == 5.0
    assert ticket.epic_key == "PROJ-50"


def test_subtask_detected(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_subtask(), mapping)
    assert ticket.is_subtask
    assert ticket.parent_key == "PROJ-102"


def test_unassigned_ticket(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_subtask(), mapping)
    assert ticket.assignee is None


def test_missing_created_rejected(mapping: FieldMapping) -> None:
    issue = issue_happy_path()
    issue["fields"]["created"] = None
    with pytest.raises(ValueError, match="created"):
        issue_to_ticket(issue, mapping)


# --- события -----------------------------------------------------------------


def test_created_event_always_first(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_happy_path(), mapping)
    assert ticket.events[0].kind == "created"
    assert ticket.events[0].occurred_at == ticket.created_at


def test_status_changes_extracted(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_happy_path(), mapping)
    transitions = [
        (e.old_value, e.new_value) for e in ticket.events if e.kind == "status_change"
    ]
    assert transitions == [
        ("new", "in progress"),
        ("in progress", "qa"),
        ("qa", "release"),
        ("release", "done"),
    ]


def test_assignee_change_extracted(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_happy_path(), mapping)
    changes = [e for e in ticket.events if e.kind == "assignee_change"]
    assert len(changes) == 1
    assert changes[0].new_value == "Иван Петров"


def test_resolution_becomes_resolved_event(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_happy_path(), mapping)
    assert any(e.kind == "resolved" for e in ticket.events)


def test_events_sorted_chronologically(mapping: FieldMapping) -> None:
    for factory in (issue_happy_path, issue_with_blocking, issue_bulk_move):
        ticket = issue_to_ticket(factory(), mapping)
        times = [e.occurred_at for e in ticket.events]
        assert times == sorted(times), ticket.external_key


def test_event_ids_unique(mapping: FieldMapping) -> None:
    """Идемпотентность импорта опирается на уникальность source_event_id."""
    ticket = issue_to_ticket(issue_happy_path(), mapping)
    ids = [e.source_event_id for e in ticket.events]
    assert len(ids) == len(set(ids))


def test_ticket_without_changelog(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_subtask(), mapping)
    assert len(ticket.events) == 1
    assert ticket.events[0].kind == "created"


# --- заявленные даты ---------------------------------------------------------


def test_declared_dates_extracted(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_happy_path(), mapping)
    assert len(ticket.declared_dates) == 2
    start = next(d for d in ticket.declared_dates if d.boundary == "work_start")
    assert start.value_at == datetime(2026, 3, 2, 14, 0, tzinfo=MSK)
    assert start.source_field == "customfield_10014"


def test_declared_dates_absent_when_not_filled(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_with_blocking(), mapping)
    assert ticket.declared_dates == []


def test_declared_dates_skipped_without_mapping() -> None:
    """Без указания полей в конфиге даты не собираются."""
    ticket = issue_to_ticket(issue_happy_path(), FieldMapping())
    assert ticket.declared_dates == []


def test_bulk_move_keeps_both_signals(mapping: FieldMapping) -> None:
    """Коллектор отдаёт и лживый changelog, и правдивые declared-даты.

    Разрешать противоречие будет ядро — коллектор не решает за него.
    """
    ticket = issue_to_ticket(issue_bulk_move(), mapping)
    transitions = [e for e in ticket.events if e.kind == "status_change"]
    span = transitions[-1].occurred_at - transitions[0].occurred_at
    assert span.total_seconds() == 30  # весь путь за полминуты
    assert len(ticket.declared_dates) == 2  # но заявлено четыре дня работы


# --- связи и люди ------------------------------------------------------------


def test_links_extracted(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_with_blocking(), mapping)
    assert len(ticket.links) == 1
    assert ticket.links[0].to_key == "PROJ-200"
    assert ticket.links[0].link_type == "Blocks"


def test_people_collected(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_happy_path(), mapping)
    ids = {p.external_id for p in ticket.people}
    assert ids == {"olga", "ivan", "petr"}
    ivan = next(p for p in ticket.people if p.external_id == "ivan")
    assert ivan.email == "ivan@example.com"
    assert ivan.display_name == "Иван Петров"


def test_people_deduplicated(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_happy_path(), mapping)
    ids = [p.external_id for p in ticket.people]
    assert len(ids) == len(set(ids))


# --- комментарии -------------------------------------------------------------


def test_comments_parsed(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_happy_path(), mapping, comments=comments_sample())
    assert len(ticket.comments) == 2
    assert ticket.comments[0].author == "olga"
    assert ticket.comments[0].body == "Воспроизводится на проде"


def test_restricted_comment_marked_internal(mapping: FieldMapping) -> None:
    ticket = issue_to_ticket(issue_happy_path(), mapping, comments=comments_sample())
    assert not ticket.comments[0].is_internal
    assert ticket.comments[1].is_internal


# --- конфигурация ------------------------------------------------------------


def test_field_list_includes_custom_fields(mapping: FieldMapping) -> None:
    fields = mapping.jira_fields()
    assert "customfield_10014" in fields
    assert "customfield_10016" in fields
    assert "status" in fields
    assert "created" in fields


def test_config_from_yaml(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JIRA_TOKEN", "secret-token")
    config_file = tmp_path / "collector.yml"
    config_file.write_text(
        """
source_name: prod-jira
jql: project = PROJ
jira:
  base_url: https://jira.example.com
  token_env: JIRA_TOKEN
  page_size: 50
mapping:
  work_start: customfield_10014
  work_end: customfield_10015
status_mapping:
  "В работе": in progress
""",
        encoding="utf-8",
    )
    config = CollectorConfig.from_yaml(config_file)
    assert config.source_name == "prod-jira"
    assert config.jira.token == "secret-token"
    assert config.jira.page_size == 50
    assert config.mapping.work_start == "customfield_10014"
    assert config.status_mapping["В работе"] == "in progress"


def test_token_not_stored_in_config_file(tmp_path, monkeypatch) -> None:
    """Секреты берутся из окружения, а не из файла конфигурации."""
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    config_file = tmp_path / "collector.yml"
    config_file.write_text(
        "source_name: s\njql: project = P\njira:\n  base_url: https://x\n",
        encoding="utf-8",
    )
    config = CollectorConfig.from_yaml(config_file)
    assert config.jira.token is None


def test_auth_headers_prefer_token() -> None:
    config = JiraConfig(base_url="https://x", token="t", username="u", password="p")
    assert config.auth_headers() == {"Authorization": "Bearer t"}
    assert config.basic_auth() is None


def test_basic_auth_fallback() -> None:
    config = JiraConfig(base_url="https://x", username="u", password="p")
    assert config.auth_headers() == {}
    assert config.basic_auth() == ("u", "p")
