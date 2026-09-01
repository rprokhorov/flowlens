"""Тесты HTTP-клиента Jira на мок-транспорте.

Живое подключение не проверяется — только поведение пагинации и ретраев.
"""

from __future__ import annotations

import httpx
import pytest
from tests.fixtures.jira_samples import issue_happy_path, issue_subtask, search_page

from flowlens.collectors.jira_client import JiraClient, JiraConfig


def client_with(handler, **overrides) -> JiraClient:
    """Клиент с подменённым транспортом."""
    config = JiraConfig(
        base_url="https://jira.example.com",
        token="t",
        max_retries=3,
        retry_base_delay_s=0.0,  # в тестах не ждём
        **overrides,
    )
    return JiraClient(config, transport=httpx.MockTransport(handler))


def test_search_single_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=search_page([issue_happy_path()], 0, 1))

    with client_with(handler) as client:
        issues = list(client.search("project = PROJ"))
    assert len(issues) == 1
    assert issues[0]["key"] == "PROJ-101"


def test_search_paginates() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("startAt", 0))
        calls.append(start)
        if start == 0:
            return httpx.Response(200, json=search_page([issue_happy_path()], 0, 2))
        return httpx.Response(200, json=search_page([issue_subtask()], 1, 2))

    with client_with(handler) as client:
        issues = list(client.search("project = PROJ"))
    assert len(issues) == 2
    assert calls == [0, 1]


def test_search_stops_on_empty_page() -> None:
    """Пустая страница прекращает обход, даже если total завышен."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=search_page([], 0, 999))

    with client_with(handler) as client:
        issues = list(client.search("project = PROJ"))
    assert issues == []


def test_retries_on_server_error() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    with client_with(handler) as client:
        result = client.get("/rest/api/2/myself")
    assert result == {"ok": True}
    assert attempts["n"] == 3


def test_retries_on_rate_limit() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    with client_with(handler) as client:
        assert client.get("/rest/api/2/myself") == {"ok": True}
    assert attempts["n"] == 2


def test_gives_up_after_max_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with client_with(handler) as client, pytest.raises(RuntimeError, match="недоступна"):
        client.get("/rest/api/2/myself")


def test_does_not_retry_client_error() -> None:
    """404 не повторяется — это не временная ошибка."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404)

    with client_with(handler) as client, pytest.raises(httpx.HTTPStatusError):
        client.get("/rest/api/2/issue/NOPE-1")
    assert attempts["n"] == 1


def test_changelog_paginates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("startAt", 0))
        if start == 0:
            return httpx.Response(
                200, json={"values": [{"id": "1"}, {"id": "2"}], "total": 3}
            )
        return httpx.Response(200, json={"values": [{"id": "3"}], "total": 3})

    with client_with(handler) as client:
        entries = client.changelog("PROJ-101")
    assert [e["id"] for e in entries] == ["1", "2", "3"]


def test_comments_paginate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("startAt", 0))
        if start == 0:
            return httpx.Response(200, json={"comments": [{"id": "c1"}], "total": 2})
        return httpx.Response(200, json={"comments": [{"id": "c2"}], "total": 2})

    with client_with(handler) as client:
        comments = client.comments("PROJ-101")
    assert [c["id"] for c in comments] == ["c1", "c2"]


def test_requires_context_manager() -> None:
    client = JiraClient(JiraConfig(base_url="https://x", token="t"))
    with pytest.raises(RuntimeError, match="context manager"):
        client.get("/rest/api/2/myself")


def test_expand_changelog_requested() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["expand"] = request.url.params.get("expand", "")
        return httpx.Response(200, json=search_page([], 0, 0))

    with client_with(handler) as client:
        list(client.search("project = PROJ", expand=["changelog"]))
    assert seen["expand"] == "changelog"
