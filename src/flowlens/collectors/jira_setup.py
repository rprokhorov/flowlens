"""Подключение к Jira без ручного YAML.

Барьер входа у коллектора был не в сложности, а в customfield_*: чтобы
заполнить маппинг, надо было пойти в администраторский интерфейс и найти
идентификаторы полей. Здесь то же самое делается запросом к самой Jira —
она отдаёт список полей с названиями, а названия у дат начала и конца работы
в разных инсталляциях повторяются.

Угадывание всегда показывается пользователю до синхронизации: подстановка
не того поля молча испортит все метрики, а увидеть ошибку постфактум трудно.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from flowlens.collectors.jira import CollectorConfig, FieldMapping
from flowlens.collectors.jira_client import JiraClient, JiraConfig

# Названия полей, под которыми обычно живут заявленные даты. Порядок задаёт
# приоритет: точное совпадение важнее вхождения подстроки.
_GUESSES: dict[str, tuple[str, ...]] = {
    "work_start": (
        "start date", "дата начала", "начало работ", "work start",
        "actual start", "фактическое начало", "дата старта",
    ),
    "work_end": (
        "end date", "due date", "дата окончания", "дата завершения",
        "work end", "actual end", "фактическое окончание",
    ),
    "story_points": (
        "story points", "story point estimate", "оценка", "стори поинты",
    ),
    "epic_link": ("epic link", "эпик", "parent link"),
}


@dataclass
class FieldGuess:
    """Одно предположение о поле — с тем, на чём оно основано."""

    purpose: str
    field_id: str | None = None
    field_name: str | None = None
    confidence: str = "none"  # exact | partial | none
    candidates: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ConnectionCheck:
    """Результат проверки подключения."""

    ok: bool
    user: str | None = None
    error: str | None = None
    statuses: list[str] = field(default_factory=list)
    guesses: list[FieldGuess] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)


def _norm(value: str) -> str:
    return value.strip().lower()


def guess_fields(fields: list[dict[str, Any]]) -> list[FieldGuess]:
    """Сопоставить поля Jira с тем, что нужно FlowLens.

    Возвращает и выбранный вариант, и остальных кандидатов: решение остаётся
    за человеком, потому что цена ошибки — молча испорченные метрики.
    """
    custom = [
        {"id": f["id"], "name": f.get("name", f["id"])}
        for f in fields
        if f.get("custom") or str(f.get("id", "")).startswith("customfield_")
    ]

    results: list[FieldGuess] = []
    for purpose, aliases in _GUESSES.items():
        exact: dict[str, str] | None = None
        partial: dict[str, str] | None = None
        related: list[dict[str, str]] = []

        for item in custom:
            name = _norm(item["name"])
            if name in aliases:
                exact = exact or item
                related.append(item)
            elif any(alias in name for alias in aliases):
                partial = partial or item
                related.append(item)

        chosen = exact or partial
        results.append(
            FieldGuess(
                purpose=purpose,
                field_id=chosen["id"] if chosen else None,
                field_name=chosen["name"] if chosen else None,
                confidence="exact" if exact else "partial" if partial else "none",
                candidates=related[:8],
            )
        )
    return results


def check_connection(
    base_url: str,
    *,
    token: str | None = None,
    username: str | None = None,
    password: str | None = None,
    verify_ssl: bool = True,
    timeout_s: float = 15.0,
    transport: Any = None,
) -> ConnectionCheck:
    """Проверить доступ и собрать всё, что нужно для настройки.

    Один вызов вместо трёх шагов вручную: подтверждает авторизацию, забирает
    список статусов доски и предлагает маппинг полей.
    """
    config = JiraConfig(
        base_url=base_url,
        token=token,
        username=username,
        password=password,
        verify_ssl=verify_ssl,
        timeout_s=timeout_s,
        # проверка должна отвечать быстро: повторять попытки осмысленно
        # при выгрузке, а не когда человек ждёт ответа в форме
        max_retries=1,
    )
    try:
        # transport подменяется в тестах: он принадлежит клиенту, а не конфигу
        with JiraClient(config, transport=transport) as client:
            me = client.myself()
            statuses = [s.get("name", "") for s in client.statuses() if s.get("name")]
            guesses = guess_fields(client.fields())
    except Exception as exc:  # noqa: BLE001
        # Причина отказа важнее типа исключения: чаще всего это неверный токен
        # или недоступный адрес, и человеку нужно увидеть именно текст ошибки.
        return ConnectionCheck(ok=False, error=str(exc))

    return ConnectionCheck(
        ok=True,
        user=me.get("displayName") or me.get("name"),
        statuses=sorted(set(statuses)),
        guesses=guesses,
    )


def build_config(
    *,
    source_name: str,
    base_url: str,
    jql: str,
    token: str | None = None,
    username: str | None = None,
    password: str | None = None,
    verify_ssl: bool = True,
    mapping: dict[str, str] | None = None,
    status_mapping: dict[str, str] | None = None,
    fetch_comments: bool = True,
) -> CollectorConfig:
    """Собрать конфигурацию коллектора из значений формы."""
    chosen = mapping or {}
    return CollectorConfig(
        source_name=source_name,
        jira=JiraConfig(
            base_url=base_url,
            token=token,
            username=username,
            password=password,
            verify_ssl=verify_ssl,
        ),
        jql=jql,
        mapping=FieldMapping(
            work_start=chosen.get("work_start"),
            work_end=chosen.get("work_end"),
            story_points=chosen.get("story_points"),
            epic_link=chosen.get("epic_link"),
        ),
        status_mapping=status_mapping or {},
        fetch_comments=fetch_comments,
    )


def config_to_yaml(config: CollectorConfig, *, token_env: str = "JIRA_TOKEN") -> str:
    """Записать настройку в YAML — чтобы дальше запускать из cron без UI.

    Секреты не попадают в файл: остаётся только имя переменной окружения.
    """
    import yaml

    mapping = {
        key: value
        for key, value in {
            "work_start": config.mapping.work_start,
            "work_end": config.mapping.work_end,
            "story_points": config.mapping.story_points,
            "epic_link": config.mapping.epic_link,
        }.items()
        if value
    }
    payload: dict[str, Any] = {
        "source_name": config.source_name,
        "jql": config.jql,
        "jira": {
            "base_url": config.jira.base_url,
            "token_env": token_env,
            "verify_ssl": config.jira.verify_ssl,
        },
    }
    if mapping:
        payload["mapping"] = mapping
    if config.status_mapping:
        payload["status_mapping"] = config.status_mapping
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


__all__ = [
    "ConnectionCheck",
    "FieldGuess",
    "build_config",
    "check_connection",
    "config_to_yaml",
    "guess_fields",
]
