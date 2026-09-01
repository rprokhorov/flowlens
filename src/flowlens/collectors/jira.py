"""Коллектор Jira: разбор ответов API в контракт FlowLens.

Задача коллектора — привести форму данных к контракту. Решения о том,
какая из противоречивых дат верна, принимает ядро (этап 3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from flowlens.collectors.jira_client import JiraClient, JiraConfig
from flowlens.contract import (
    CONTRACT_VERSION,
    RawComment,
    RawDeclaredDate,
    RawEvent,
    RawLink,
    RawPerson,
    RawTicket,
    SourceProfile,
)

log = logging.getLogger(__name__)

# Поля, запрашиваемые у Jira всегда
BASE_FIELDS = [
    "summary",
    "issuetype",
    "status",
    "priority",
    "created",
    "updated",
    "resolutiondate",
    "reporter",
    "assignee",
    "components",
    "labels",
    "parent",
    "issuelinks",
]


@dataclass
class FieldMapping:
    """Соответствие полей Jira смыслам FlowLens.

    Заполняется из конфига: имена customfield различаются между инсталляциями.
    """

    work_start: str | None = None  # customfield_XXXXX
    work_end: str | None = None
    story_points: str | None = None
    epic_link: str | None = None
    declared_precision: str = "minute"
    extra_fields: dict[str, str] = field(default_factory=dict)

    def jira_fields(self) -> list[str]:
        """Полный список полей для запроса."""
        custom = [
            f
            for f in (self.work_start, self.work_end, self.story_points, self.epic_link)
            if f
        ]
        return BASE_FIELDS + custom + list(self.extra_fields.values())


@dataclass
class CollectorConfig:
    """Конфигурация коллектора: подключение, JQL, маппинг."""

    source_name: str
    jira: JiraConfig
    jql: str
    mapping: FieldMapping = field(default_factory=FieldMapping)
    status_mapping: dict[str, str] = field(default_factory=dict)
    fetch_comments: bool = True
    changelog_page_threshold: int = 100

    @classmethod
    def from_yaml(cls, path: Path) -> CollectorConfig:
        """Прочитать конфиг. Секреты берутся из окружения, не из файла."""
        import os

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        jira_raw = raw.get("jira", {})

        token_env = jira_raw.get("token_env", "JIRA_TOKEN")
        token = os.environ.get(token_env)
        user_env = jira_raw.get("username_env", "JIRA_USER")
        pass_env = jira_raw.get("password_env", "JIRA_PASSWORD")

        jira = JiraConfig(
            base_url=jira_raw["base_url"],
            token=token,
            username=os.environ.get(user_env),
            password=os.environ.get(pass_env),
            timeout_s=float(jira_raw.get("timeout_s", 30)),
            page_size=int(jira_raw.get("page_size", 100)),
            max_retries=int(jira_raw.get("max_retries", 5)),
            min_interval_s=float(jira_raw.get("min_interval_s", 0)),
            verify_ssl=bool(jira_raw.get("verify_ssl", True)),
        )

        mapping_raw = raw.get("mapping", {})
        mapping = FieldMapping(
            work_start=mapping_raw.get("work_start"),
            work_end=mapping_raw.get("work_end"),
            story_points=mapping_raw.get("story_points"),
            epic_link=mapping_raw.get("epic_link"),
            declared_precision=mapping_raw.get("declared_precision", "minute"),
            extra_fields=mapping_raw.get("extra_fields", {}) or {},
        )

        return cls(
            source_name=raw["source_name"],
            jira=jira,
            jql=raw["jql"],
            mapping=mapping,
            status_mapping=raw.get("status_mapping", {}) or {},
            fetch_comments=bool(raw.get("fetch_comments", True)),
        )


def parse_jira_datetime(value: str | None) -> datetime | None:
    """Разобрать дату Jira ('2026-03-02T11:00:00.000+0300')."""
    if not value:
        return None
    normalized = value.strip()
    # Jira DC отдаёт смещение без двоеточия — fromisoformat до 3.11 это не понимал
    if len(normalized) > 5 and normalized[-5] in "+-" and ":" not in normalized[-5:]:
        normalized = normalized[:-2] + ":" + normalized[-2:]
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        log.warning("не удалось разобрать дату: %r", value)
        return None
    if parsed.tzinfo is None:
        log.warning("дата без таймзоны: %r", value)
        return None
    return parsed


def _person_id(raw: dict[str, Any] | None) -> str | None:
    """Идентификатор пользователя. В DC это `key` или `name`, не accountId."""
    if not raw:
        return None
    return raw.get("key") or raw.get("name") or raw.get("accountId")


def _person(raw: dict[str, Any] | None) -> RawPerson | None:
    external = _person_id(raw)
    if not external or raw is None:
        return None
    return RawPerson(
        external_id=external,
        display_name=raw.get("displayName"),
        email=raw.get("emailAddress"),
    )


def issue_to_ticket(
    issue: dict[str, Any],
    mapping: FieldMapping,
    *,
    changelog: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
) -> RawTicket:
    """Преобразовать issue Jira в тикет контракта."""
    fields = issue.get("fields", {})
    key = issue["key"]

    created_at = parse_jira_datetime(fields.get("created"))
    if created_at is None:
        raise ValueError(f"{key}: отсутствует поле created")

    people: dict[str, RawPerson] = {}

    def remember(raw: dict[str, Any] | None) -> str | None:
        person = _person(raw)
        if person is None:
            return None
        people.setdefault(person.external_id, person)
        return person.external_id

    reporter = remember(fields.get("reporter"))
    assignee = remember(fields.get("assignee"))

    issue_type_raw = fields.get("issuetype") or {}
    status_raw = fields.get("status") or {}
    priority_raw = fields.get("priority") or {}
    parent_raw = fields.get("parent") or {}

    events = _build_events(
        key=key,
        created_at=created_at,
        reporter=reporter,
        changelog=changelog or _embedded_changelog(issue),
        remember=remember,
    )

    declared = _build_declared_dates(fields, mapping)
    links = _build_links(fields.get("issuelinks") or [])
    parsed_comments = _build_comments(comments or _embedded_comments(fields), remember)

    story_points = None
    if mapping.story_points:
        value = fields.get(mapping.story_points)
        story_points = float(value) if isinstance(value, int | float) else None

    epic_key = None
    if mapping.epic_link:
        epic_key = fields.get(mapping.epic_link) or None

    raw_fields: dict[str, Any] = {}
    for name, jira_field in mapping.extra_fields.items():
        raw_fields[name] = fields.get(jira_field)

    return RawTicket(
        external_key=key,
        external_id=issue.get("id"),
        project_key=key.split("-")[0],
        issue_type=issue_type_raw.get("name", "Task"),
        is_subtask=bool(issue_type_raw.get("subtask", False)),
        priority=priority_raw.get("name"),
        status=status_raw.get("name", "unknown"),
        summary=fields.get("summary") or "",
        components=[c.get("name", "") for c in (fields.get("components") or [])],
        labels=list(fields.get("labels") or []),
        created_at=created_at,
        resolved_at=parse_jira_datetime(fields.get("resolutiondate")),
        updated_at=parse_jira_datetime(fields.get("updated")),
        reporter=reporter,
        assignee=assignee,
        parent_key=parent_raw.get("key"),
        epic_key=epic_key,
        story_points=story_points,
        events=events,
        declared_dates=declared,
        comments=parsed_comments,
        links=links,
        people=list(people.values()),
        raw_fields=raw_fields,
    )


def _embedded_changelog(issue: dict[str, Any]) -> list[dict[str, Any]]:
    return (issue.get("changelog") or {}).get("histories", [])


def _embedded_comments(fields: dict[str, Any]) -> list[dict[str, Any]]:
    return (fields.get("comment") or {}).get("comments", [])


def _build_events(
    *,
    key: str,
    created_at: datetime,
    reporter: str | None,
    changelog: list[dict[str, Any]],
    remember: Any,
) -> list[RawEvent]:
    """Разложить changelog в события контракта."""
    events: list[RawEvent] = [
        RawEvent(
            kind="created",
            occurred_at=created_at,
            actor=reporter,
            new_value=None,
            source_event_id=f"{key}-created",
        )
    ]

    for entry in changelog:
        at = parse_jira_datetime(entry.get("created"))
        if at is None:
            continue
        actor = remember(entry.get("author"))
        entry_id = str(entry.get("id", ""))

        for item in entry.get("items", []):
            field_name = item.get("field")
            from_value = item.get("fromString") or item.get("from")
            to_value = item.get("toString") or item.get("to")

            if field_name == "status":
                kind = "status_change"
            elif field_name == "assignee":
                kind = "assignee_change"
            elif field_name in ("Flagged", "flagged"):
                kind = "flag_change"
            elif field_name == "Link":
                kind = "link_change"
            elif field_name == "resolution":
                kind = "resolved" if to_value else "reopened"
            else:
                kind = "field_change"

            events.append(
                RawEvent(
                    kind=kind,  # type: ignore[arg-type]
                    occurred_at=at,
                    actor=actor,
                    field=field_name,
                    old_value=str(from_value) if from_value is not None else None,
                    new_value=str(to_value) if to_value is not None else None,
                    source_event_id=f"{entry_id}:{field_name}",
                )
            )

    events.sort(key=lambda e: e.occurred_at)
    return events


def _build_declared_dates(
    fields: dict[str, Any], mapping: FieldMapping
) -> list[RawDeclaredDate]:
    """Считать заявленные вручную даты — как есть, без проверок."""
    out: list[RawDeclaredDate] = []
    for boundary, jira_field in (
        ("work_start", mapping.work_start),
        ("work_end", mapping.work_end),
    ):
        if not jira_field:
            continue
        parsed = parse_jira_datetime(fields.get(jira_field))
        if parsed is None:
            continue
        out.append(
            RawDeclaredDate(
                boundary=boundary,  # type: ignore[arg-type]
                value_at=parsed,
                precision=mapping.declared_precision,  # type: ignore[arg-type]
                source_field=jira_field,
            )
        )
    return out


def _build_links(raw_links: list[dict[str, Any]]) -> list[RawLink]:
    out: list[RawLink] = []
    for link in raw_links:
        link_type = (link.get("type") or {}).get("name", "relates")
        for direction in ("outwardIssue", "inwardIssue"):
            target = link.get(direction)
            if target and target.get("key"):
                out.append(RawLink(to_key=target["key"], link_type=link_type))
    return out


def _build_comments(raw_comments: list[dict[str, Any]], remember: Any) -> list[RawComment]:
    out: list[RawComment] = []
    for comment in raw_comments:
        created = parse_jira_datetime(comment.get("created"))
        if created is None:
            continue
        out.append(
            RawComment(
                external_id=str(comment.get("id")) if comment.get("id") else None,
                author=remember(comment.get("author")),
                created_at=created,
                updated_at=parse_jira_datetime(comment.get("updated")),
                body=comment.get("body") or "",
                is_internal=_is_internal(comment),
            )
        )
    return out


def _is_internal(comment: dict[str, Any]) -> bool:
    visibility = comment.get("visibility")
    return bool(visibility)


def collect(
    config: CollectorConfig,
    *,
    since: datetime | None = None,
    limit: int | None = None,
) -> tuple[SourceProfile, list[RawTicket]]:
    """Выгрузить тикеты из Jira.

    `since` добавляет к JQL условие по updated — инкрементальная синхронизация.
    """
    jql = config.jql
    if since is not None:
        stamp = since.strftime("%Y-%m-%d %H:%M")
        jql = f"({jql}) AND updated >= '{stamp}'"
    jql = f"{jql} ORDER BY updated ASC"

    tickets: list[RawTicket] = []
    with JiraClient(config.jira) as client:
        for issue in client.search(
            jql,
            fields=config.mapping.jira_fields(),
            expand=["changelog"],
        ):
            key = issue["key"]
            changelog = _embedded_changelog(issue)
            # Jira усекает встроенный changelog — дочитываем отдельным запросом
            total = (issue.get("changelog") or {}).get("total", len(changelog))
            if total > len(changelog):
                log.info("%s: changelog усечён (%s из %s), дочитываю", key, len(changelog), total)
                changelog = client.changelog(key)

            comments = client.comments(key) if config.fetch_comments else []
            tickets.append(
                issue_to_ticket(issue, config.mapping, changelog=changelog, comments=comments)
            )
            if limit and len(tickets) >= limit:
                break

    profile = SourceProfile(
        contract_version=CONTRACT_VERSION,
        source_kind="jira_dc",
        source_name=config.source_name,
        changelog="full",
        reconciliation_hint="prefer_declared",
        declared_dates={
            "work_start": {
                "field": config.mapping.work_start,
                "precision": config.mapping.declared_precision,
            },
            "work_end": {
                "field": config.mapping.work_end,
                "precision": config.mapping.declared_precision,
            },
        },
        status_mapping=config.status_mapping,
        exported_at=datetime.now().astimezone(),
        ticket_count=len(tickets),
    )
    return profile, tickets


__all__ = [
    "BASE_FIELDS",
    "CollectorConfig",
    "FieldMapping",
    "collect",
    "issue_to_ticket",
    "parse_jira_datetime",
]
