"""Метрики использования самого сервиса.

Отвечает на вопрос «чем в продукте пользуются»: какие разделы открывают,
сколько людей заходит, что настроено и работает ли синхронизация.

Счётчики агрегатные — сутки, пользователь, раздел. Из них видно, какие
разделы живут, а какие никто не открывает, но нельзя восстановить, что
человек делал в конкретную минуту. Инструмент анализа потока не должен
превращаться в инструмент наблюдения за сотрудниками.

Запись в память с периодическим сбросом: обращений к API десятки на каждое
открытие вкладки, и запись в базу на каждое сделала бы дашборд медленнее
ради статистики.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

# Путь запроса → раздел интерфейса. Человек думает разделами, а не путями:
# путей десятки, и они меняются при рефакторинге.
_SECTIONS: dict[str, str] = {
    "/api/summary": "Обзор",
    "/api/advice": "Обзор",
    "/api/explain": "Разбор текстом",
    "/api/cfd": "Поток",
    "/api/arrival-throughput": "Поток",
    "/api/backlog": "Поток",
    "/api/cycle-time": "Время цикла",
    "/api/flow-efficiency": "Где время",
    "/api/hidden-queue": "Где время",
    "/api/transitions": "Где время",
    "/api/aging-wip": "Незавершённое",
    "/api/blockers": "Блокировки",
    "/api/sle": "Обещания",
    "/api/predictability": "Обещания",
    "/api/service-classes": "Обещания",
    "/api/people": "Нагрузка",
    "/api/forecast": "Прогноз",
    "/api/quality": "Качество данных",
    "/api/tickets": "Задачи",
    "/api/teams": "Настройки",
    "/api/sources": "Настройки",
    "/api/statuses": "Настройки",
    "/api/import": "Импорт",
    "/api/jira": "Импорт",
    "/api/export": "Выгрузка",
}

_buffer: dict[tuple[date, int, str], int] = defaultdict(int)
_lock = threading.Lock()


def section_for(path: str) -> str | None:
    """Раздел, к которому относится запрос.

    Служебные обращения (проверка живости, профиль, статика) не считаются:
    они говорят о работе браузера, а не о том, чем пользуется человек.
    """
    if path in _SECTIONS:
        return _SECTIONS[path]
    for prefix, section in _SECTIONS.items():
        if path.startswith(prefix + "/"):
            return section
    return None


def record(user_id: int, path: str, now: datetime | None = None) -> None:
    """Отметить обращение. Копится в памяти, в базу уходит пачкой."""
    section = section_for(path)
    if section is None or not user_id:
        return
    moment = now or datetime.now(UTC)
    with _lock:
        _buffer[(moment.date(), user_id, section)] += 1


def flush(engine: Engine) -> int:
    """Сбросить накопленное в базу.

    Буфер забирается целиком под замком, а запись идёт уже без него: иначе
    обращения к API ждали бы завершения записи.
    """
    with _lock:
        if not _buffer:
            return 0
        batch = dict(_buffer)
        _buffer.clear()

    with engine.begin() as conn:
        for (day, user_id, section), hits in batch.items():
            conn.execute(
                text(
                    "INSERT INTO usage_daily (day, user_id, section, hits) "
                    "VALUES (:day, :uid, :section, :hits) "
                    "ON CONFLICT (day, user_id, section) DO UPDATE "
                    "SET hits = usage_daily.hits + EXCLUDED.hits"
                ),
                {"day": day, "uid": user_id, "section": section, "hits": hits},
            )
    return len(batch)


def overview(engine: Engine, days: int = 30) -> dict[str, Any]:
    """Сводка по использованию продукта для админской панели."""
    since = date.today() - timedelta(days=days)
    flush(engine)

    with engine.begin() as conn:
        totals = conn.execute(
            text(
                "SELECT count(DISTINCT user_id) AS people, "
                "       COALESCE(sum(hits), 0) AS hits, "
                "       count(DISTINCT day) AS active_days "
                "FROM usage_daily WHERE day >= :since"
            ),
            {"since": since},
        ).one()

        sections = conn.execute(
            text(
                "SELECT section, COALESCE(sum(hits), 0) AS hits, "
                "       count(DISTINCT user_id) AS people "
                "FROM usage_daily WHERE day >= :since "
                "GROUP BY section ORDER BY hits DESC"
            ),
            {"since": since},
        ).all()

        by_day = conn.execute(
            text(
                "SELECT day, COALESCE(sum(hits), 0) AS hits, "
                "       count(DISTINCT user_id) AS people "
                "FROM usage_daily WHERE day >= :since "
                "GROUP BY day ORDER BY day"
            ),
            {"since": since},
        ).all()

        people = conn.execute(
            text(
                "SELECT u.username, u.display_name, u.is_admin, u.last_login_at, "
                "       COALESCE(sum(d.hits), 0) AS hits, "
                "       count(DISTINCT d.day) AS days "
                "FROM app_user u "
                "LEFT JOIN usage_daily d ON d.user_id = u.id AND d.day >= :since "
                "GROUP BY u.id, u.username, u.display_name, u.is_admin, u.last_login_at "
                "ORDER BY hits DESC, u.username"
            ),
            {"since": since},
        ).all()

        # Состояние продукта: сколько команд заведено, сколько данных, работают
        # ли синхронизации. Это то, что показывает, живёт сервис или стоит.
        state = conn.execute(
            text(
                "SELECT "
                "  (SELECT count(*) FROM team) AS teams, "
                "  (SELECT count(*) FROM app_user WHERE is_active) AS users, "
                "  (SELECT count(*) FROM ticket) AS tickets, "
                "  (SELECT count(*) FROM team_source) AS connections, "
                "  (SELECT count(*) FROM team_source "
                "     WHERE last_sync_status = 'failed') AS failed_syncs, "
                "  (SELECT count(*) FROM team_source "
                "     WHERE sync_interval_minutes IS NOT NULL) AS scheduled"
            )
        ).one()

    return {
        "period_days": days,
        "active_people": totals.people,
        "hits": int(totals.hits),
        "active_days": totals.active_days,
        "sections": [
            {"section": r.section, "hits": int(r.hits), "people": r.people}
            for r in sections
        ],
        "by_day": [
            {"day": r.day.isoformat(), "hits": int(r.hits), "people": r.people}
            for r in by_day
        ],
        "people": [
            {
                "username": r.username,
                "display_name": r.display_name or r.username,
                "is_admin": r.is_admin,
                "last_login_at": r.last_login_at.isoformat() if r.last_login_at else None,
                "hits": int(r.hits),
                "days": r.days,
            }
            for r in people
        ],
        "state": {
            "teams": state.teams,
            "users": state.users,
            "tickets": state.tickets,
            "connections": state.connections,
            "failed_syncs": state.failed_syncs,
            "scheduled_syncs": state.scheduled,
        },
    }


def unused_sections(engine: Engine, days: int = 30) -> list[str]:
    """Разделы, которые никто не открывал.

    Полезнее популярных: показывает, что построено зря или что люди
    не нашли.
    """
    data = overview(engine, days)
    used = {item["section"] for item in data["sections"]}
    return sorted(set(_SECTIONS.values()) - used)


__all__ = ["flush", "overview", "record", "section_for", "unused_sections"]
