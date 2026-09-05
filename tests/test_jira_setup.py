"""Тесты мастера подключения к Jira."""

from __future__ import annotations

import httpx

from flowlens.collectors.jira_setup import (
    build_config,
    check_connection,
    config_to_yaml,
    guess_fields,
)

FIELDS = [
    {"id": "summary", "name": "Summary", "custom": False},
    {"id": "customfield_10014", "name": "Start date", "custom": True},
    {"id": "customfield_10015", "name": "Дата завершения", "custom": True},
    {"id": "customfield_10020", "name": "Story Points", "custom": True},
    {"id": "customfield_10030", "name": "Epic Link", "custom": True},
    {"id": "customfield_10099", "name": "Планируемая дата начала", "custom": True},
]


# --- угадывание полей --------------------------------------------------------


def test_guess_finds_exact_matches() -> None:
    by_purpose = {g.purpose: g for g in guess_fields(FIELDS)}
    assert by_purpose["work_start"].field_id == "customfield_10014"
    assert by_purpose["work_end"].field_id == "customfield_10015"
    assert by_purpose["story_points"].field_id == "customfield_10020"


def test_guess_marks_confidence() -> None:
    """Точное совпадение и «похоже» — разные вещи, и человек должен их различать."""
    by_purpose = {g.purpose: g for g in guess_fields(FIELDS)}
    assert by_purpose["work_start"].confidence == "exact"


def test_guess_keeps_alternatives() -> None:
    """Кандидаты показываются, потому что подстановка не того поля портит метрики."""
    by_purpose = {g.purpose: g for g in guess_fields(FIELDS)}
    ids = {c["id"] for c in by_purpose["work_start"].candidates}
    assert "customfield_10099" in ids


def test_guess_reports_missing() -> None:
    minimal = [{"id": "customfield_1", "name": "Что-то своё", "custom": True}]
    for guess in guess_fields(minimal):
        assert guess.field_id is None
        assert guess.confidence == "none"


def test_guess_ignores_system_fields() -> None:
    """Системные поля не предлагаются: заявленные даты всегда кастомные."""
    system_only = [{"id": "duedate", "name": "Due date", "custom": False}]
    for guess in guess_fields(system_only):
        assert guess.field_id is None


# --- проверка подключения ----------------------------------------------------


def make_transport(*, fail: bool = False) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(401, json={"errorMessages": ["Unauthorized"]})
        path = request.url.path
        if path.endswith("/myself"):
            return httpx.Response(200, json={"displayName": "Иван Петров"})
        if path.endswith("/status"):
            return httpx.Response(
                200, json=[{"name": "in progress"}, {"name": "done"}, {"name": "done"}]
            )
        if path.endswith("/field"):
            return httpx.Response(200, json=FIELDS)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def test_check_connection_succeeds() -> None:
    result = check_connection(
        "https://jira.example.com", token="secret", transport=make_transport()
    )

    assert result.ok
    assert result.user == "Иван Петров"
    # статусы приходят с повторами — в отчёте они должны быть уникальны
    assert result.statuses == ["done", "in progress"]
    assert any(g.field_id == "customfield_10014" for g in result.guesses)


def test_check_connection_reports_error() -> None:
    """Причина отказа должна дойти до человека, а не превратиться в «ошибка»."""
    result = check_connection(
        "https://jira.example.com", token="wrong", transport=make_transport(fail=True)
    )

    assert not result.ok
    assert result.error


# --- сборка конфигурации -----------------------------------------------------


def test_build_config_applies_mapping() -> None:
    config = build_config(
        source_name="prod",
        base_url="https://jira.example.com",
        jql="project = PROJ",
        token="secret",
        mapping={"work_start": "customfield_10014", "work_end": "customfield_10015"},
    )
    assert config.mapping.work_start == "customfield_10014"
    assert config.jira.token == "secret"


def test_yaml_never_contains_secrets() -> None:
    """В файл попадает имя переменной окружения, а не сам токен."""
    config = build_config(
        source_name="prod",
        base_url="https://jira.example.com",
        jql="project = PROJ",
        token="super-secret-token",
        mapping={"work_start": "customfield_10014"},
    )
    dumped = config_to_yaml(config)

    assert "super-secret-token" not in dumped
    assert "token_env: JIRA_TOKEN" in dumped
    assert "customfield_10014" in dumped


def test_yaml_omits_empty_mapping() -> None:
    config = build_config(
        source_name="prod", base_url="https://jira.example.com", jql="project = PROJ"
    )
    assert "mapping" not in config_to_yaml(config)
