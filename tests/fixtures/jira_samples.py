"""Фикстуры: ответы Jira DC в том виде, в каком их отдаёт API.

Структура повторяет реальные ответы api/2/search?expand=changelog.
"""

from __future__ import annotations

from typing import Any

USER_IVAN = {
    "key": "ivan",
    "name": "ivan",
    "displayName": "Иван Петров",
    "emailAddress": "ivan@example.com",
}
USER_OLGA = {
    "key": "olga",
    "name": "olga",
    "displayName": "Ольга Смирнова",
    "emailAddress": "olga@example.com",
}
USER_PETR = {
    "key": "petr",
    "name": "petr",
    "displayName": "Пётр Иванов",
    "emailAddress": "petr@example.com",
}


def issue_happy_path() -> dict[str, Any]:
    """Обычный тикет: new → in progress → qa → release → done."""
    return {
        "id": "10001",
        "key": "PROJ-101",
        "fields": {
            "summary": "Починить экспорт отчётов",
            "issuetype": {"name": "Bug", "subtask": False},
            "status": {"name": "done"},
            "priority": {"name": "High"},
            "created": "2026-03-02T11:00:00.000+0300",
            "updated": "2026-03-05T16:30:00.000+0300",
            "resolutiondate": "2026-03-05T16:30:00.000+0300",
            "reporter": USER_OLGA,
            "assignee": USER_IVAN,
            "components": [{"name": "reports"}, {"name": "api"}],
            "labels": ["regression"],
            "parent": None,
            "issuelinks": [],
            "customfield_10014": "2026-03-02T14:00:00.000+0300",
            "customfield_10015": "2026-03-05T16:00:00.000+0300",
            "customfield_10016": 5.0,
            "customfield_10017": "PROJ-50",
        },
        "changelog": {
            "total": 4,
            "histories": [
                {
                    "id": "20001",
                    "created": "2026-03-02T14:00:00.000+0300",
                    "author": USER_IVAN,
                    "items": [
                        {
                            "field": "status",
                            "fromString": "new",
                            "toString": "in progress",
                        },
                        {
                            "field": "assignee",
                            "fromString": None,
                            "toString": "Иван Петров",
                            "to": "ivan",
                        },
                    ],
                },
                {
                    "id": "20002",
                    "created": "2026-03-04T12:00:00.000+0300",
                    "author": USER_IVAN,
                    "items": [
                        {"field": "status", "fromString": "in progress", "toString": "qa"}
                    ],
                },
                {
                    "id": "20003",
                    "created": "2026-03-05T15:00:00.000+0300",
                    "author": USER_PETR,
                    "items": [
                        {"field": "status", "fromString": "qa", "toString": "release"}
                    ],
                },
                {
                    "id": "20004",
                    "created": "2026-03-05T16:30:00.000+0300",
                    "author": USER_PETR,
                    "items": [
                        {"field": "status", "fromString": "release", "toString": "done"},
                        {
                            "field": "resolution",
                            "fromString": None,
                            "toString": "Fixed",
                        },
                    ],
                },
            ],
        },
    }


def issue_with_blocking() -> dict[str, Any]:
    """Тикет с блокировкой и без заявленных дат."""
    return {
        "id": "10002",
        "key": "PROJ-102",
        "fields": {
            "summary": "Интеграция с биллингом",
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": "blocked/hold"},
            "priority": {"name": "Medium"},
            "created": "2026-03-03T10:00:00.000+0300",
            "updated": "2026-03-06T10:00:00.000+0300",
            "resolutiondate": None,
            "reporter": USER_OLGA,
            "assignee": USER_IVAN,
            "components": [],
            "labels": [],
            "issuelinks": [
                {
                    "type": {"name": "Blocks"},
                    "inwardIssue": {"key": "PROJ-200"},
                }
            ],
        },
        "changelog": {
            "total": 2,
            "histories": [
                {
                    "id": "20010",
                    "created": "2026-03-03T11:00:00.000+0300",
                    "author": USER_IVAN,
                    "items": [
                        {"field": "status", "fromString": "new", "toString": "in progress"}
                    ],
                },
                {
                    "id": "20011",
                    "created": "2026-03-04T15:00:00.000+0300",
                    "author": USER_IVAN,
                    "items": [
                        {
                            "field": "status",
                            "fromString": "in progress",
                            "toString": "blocked/hold",
                        }
                    ],
                },
            ],
        },
    }


def issue_subtask() -> dict[str, Any]:
    """Подзадача с родителем."""
    return {
        "id": "10003",
        "key": "PROJ-103",
        "fields": {
            "summary": "Написать миграцию",
            "issuetype": {"name": "Sub-task", "subtask": True},
            "status": {"name": "in progress"},
            "priority": {"name": "Low"},
            "created": "2026-03-04T09:30:00.000+0300",
            "updated": "2026-03-04T18:00:00.000+0300",
            "reporter": USER_IVAN,
            "assignee": None,
            "components": [],
            "labels": [],
            "parent": {"key": "PROJ-102"},
            "issuelinks": [],
        },
        "changelog": {"total": 0, "histories": []},
    }


def issue_bulk_move() -> dict[str, Any]:
    """Пятничная разгребка: переходы за секунды, даты проставлены руками."""
    return {
        "id": "10004",
        "key": "PROJ-104",
        "fields": {
            "summary": "Обновить документацию",
            "issuetype": {"name": "Task", "subtask": False},
            "status": {"name": "done"},
            "priority": {"name": "Low"},
            "created": "2026-03-02T10:00:00.000+0300",
            "updated": "2026-03-06T18:45:30.000+0300",
            "resolutiondate": "2026-03-06T18:45:30.000+0300",
            "reporter": USER_OLGA,
            "assignee": USER_IVAN,
            "components": [],
            "labels": [],
            "issuelinks": [],
            "customfield_10014": "2026-03-02T00:00:00.000+0300",
            "customfield_10015": "2026-03-05T00:00:00.000+0300",
        },
        "changelog": {
            "total": 3,
            "histories": [
                {
                    "id": "20020",
                    "created": "2026-03-06T18:45:00.000+0300",
                    "author": USER_IVAN,
                    "items": [
                        {"field": "status", "fromString": "new", "toString": "in progress"}
                    ],
                },
                {
                    "id": "20021",
                    "created": "2026-03-06T18:45:15.000+0300",
                    "author": USER_IVAN,
                    "items": [{"field": "status", "fromString": "in progress", "toString": "qa"}],
                },
                {
                    "id": "20022",
                    "created": "2026-03-06T18:45:30.000+0300",
                    "author": USER_IVAN,
                    "items": [{"field": "status", "fromString": "qa", "toString": "done"}],
                },
            ],
        },
    }


def comments_sample() -> list[dict[str, Any]]:
    return [
        {
            "id": "30001",
            "author": USER_OLGA,
            "created": "2026-03-02T12:00:00.000+0300",
            "updated": "2026-03-02T12:00:00.000+0300",
            "body": "Воспроизводится на проде",
        },
        {
            "id": "30002",
            "author": USER_IVAN,
            "created": "2026-03-02T13:30:00.000+0300",
            "body": "Беру в работу",
            "visibility": {"type": "role", "value": "Developers"},
        },
    ]


def search_page(issues: list[dict[str, Any]], start_at: int, total: int) -> dict[str, Any]:
    return {
        "startAt": start_at,
        "maxResults": len(issues),
        "total": total,
        "issues": issues,
    }
