"""Контракт обмена между коллектором и ядром.

Коллектор нормализует ФОРМУ данных источника, но не решает, что ПРАВДА:
и changelog, и заявленные вручную даты передаются как есть, рядом.
Согласование — задача ядра (этап 3).

Формат: NDJSON, одна строка = один тикет со всей историей.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTRACT_VERSION = "1.0"


class RawEvent(BaseModel):
    """Событие изменения тикета, как его отдал источник."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "created",
        "status_change",
        "assignee_change",
        "field_change",
        "link_change",
        "flag_change",
        "comment",
        "resolved",
        "reopened",
    ]
    occurred_at: datetime
    actor: str | None = None
    field: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    source_event_id: str | None = None

    @field_validator("occurred_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value


class RawDeclaredDate(BaseModel):
    """Дата, заявленная человеком вручную (start date / end date).

    Хранится сырой: противоречия с changelog разрешает ядро.
    """

    model_config = ConfigDict(extra="forbid")

    boundary: Literal["work_start", "work_end"]
    value_at: datetime
    precision: Literal["day", "minute"] = "minute"
    source_field: str | None = None

    @field_validator("value_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("value_at must be timezone-aware")
        return value


class RawComment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str | None = None
    author: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    body: str = ""
    is_internal: bool = False


class RawLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_key: str
    link_type: str
    created_at: datetime | None = None


class RawPerson(BaseModel):
    """Человек в терминах источника. Дедупликацию делает ядро."""

    model_config = ConfigDict(extra="forbid")

    external_id: str
    display_name: str | None = None
    email: str | None = None


class RawTicket(BaseModel):
    """Тикет со всей историей — единица обмена."""

    model_config = ConfigDict(extra="forbid")

    external_key: str
    external_id: str | None = None
    project_key: str
    issue_type: str
    is_subtask: bool = False
    priority: str | None = None
    status: str
    summary: str = ""
    components: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)

    created_at: datetime
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    updated_at: datetime | None = None

    reporter: str | None = None
    assignee: str | None = None
    parent_key: str | None = None
    epic_key: str | None = None
    story_points: float | None = None

    events: list[RawEvent] = Field(default_factory=list)
    declared_dates: list[RawDeclaredDate] = Field(default_factory=list)
    comments: list[RawComment] = Field(default_factory=list)
    links: list[RawLink] = Field(default_factory=list)
    people: list[RawPerson] = Field(default_factory=list)
    raw_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @field_validator("events")
    @classmethod
    def _require_created_event(cls, events: list[RawEvent]) -> list[RawEvent]:
        if events and not any(e.kind == "created" for e in events):
            raise ValueError("event list must contain a 'created' event")
        return events


class SourceProfile(BaseModel):
    """Декларация того, как устроена реальность в конкретном источнике.

    Это данные, а не логика: ядро читает их и выбирает политику согласования.
    """

    model_config = ConfigDict(extra="allow")

    contract_version: str = CONTRACT_VERSION
    source_kind: str
    source_name: str
    changelog: Literal["full", "partial", "none"] = "full"
    reconciliation_hint: Literal["prefer_declared", "prefer_system", "system_only"] = (
        "prefer_declared"
    )
    declared_dates: dict[str, dict[str, Any]] = Field(default_factory=dict)
    status_mapping: dict[str, str] = Field(default_factory=dict)
    blocked_signal: dict[str, Any] = Field(default_factory=dict)
    exported_at: datetime | None = None
    ticket_count: int | None = None


def write_ndjson(path: Path, tickets: list[RawTicket], profile: SourceProfile) -> int:
    """Записать выгрузку: первая строка — профиль, дальше по тикету на строку."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        header = {"_profile": profile.model_dump(mode="json")}
        fh.write(json.dumps(header, ensure_ascii=False) + "\n")
        for ticket in tickets:
            fh.write(json.dumps(ticket.model_dump(mode="json"), ensure_ascii=False) + "\n")
    return len(tickets)


def read_ndjson(path: Path) -> tuple[SourceProfile, list[RawTicket]]:
    """Прочитать выгрузку с валидацией по контракту."""
    profile: SourceProfile | None = None
    tickets: list[RawTicket] = []

    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if lineno == 1 and "_profile" in payload:
                profile = SourceProfile.model_validate(payload["_profile"])
                continue
            try:
                tickets.append(RawTicket.model_validate(payload))
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"{path}:{lineno}: {exc}") from exc

    if profile is None:
        raise ValueError(f"{path}: missing _profile header line")
    return profile, tickets


def iter_ndjson(path: Path) -> tuple[SourceProfile, Any]:
    """Потоковое чтение: профиль сразу, тикеты — генератором.

    Нужно для больших выгрузок, которые нежелательно держать в памяти.
    """
    fh = path.open(encoding="utf-8")
    first = fh.readline().strip()
    payload = json.loads(first) if first else {}
    if "_profile" not in payload:
        fh.close()
        raise ValueError(f"{path}: missing _profile header line")
    profile = SourceProfile.model_validate(payload["_profile"])

    def generator():  # type: ignore[no-untyped-def]
        try:
            for lineno, line in enumerate(fh, start=2):
                line = line.strip()
                if line:
                    try:
                        yield RawTicket.model_validate(json.loads(line))
                    except Exception as exc:  # noqa: BLE001
                        raise ValueError(f"{path}:{lineno}: {exc}") from exc
        finally:
            fh.close()

    return profile, generator()


__all__ = [
    "CONTRACT_VERSION",
    "RawComment",
    "RawDeclaredDate",
    "RawEvent",
    "RawLink",
    "RawPerson",
    "RawTicket",
    "SourceProfile",
    "iter_ndjson",
    "read_ndjson",
    "write_ndjson",
]
