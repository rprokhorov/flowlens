"""Подключения команд к источникам данных.

Владелец команды настраивает своё подключение один раз, дальше синхронизация
идёт кнопкой или по расписанию. Токен хранится зашифрованным и наружу
не отдаётся никогда — ни в списке подключений, ни в ответе на сохранение.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text

from flowlens import secrets


@dataclass
class TeamSource:
    """Настроенное подключение команды."""

    id: int
    team_id: int
    kind: str
    base_url: str
    jql: str
    username: str | None
    field_mapping: dict[str, str]
    verify_ssl: bool
    sync_interval_minutes: int | None
    last_sync_at: datetime | None
    last_sync_status: str | None
    last_sync_error: str | None
    has_secret: bool

    def as_dict(self) -> dict[str, Any]:
        """Представление для API — без секрета, только факт его наличия."""
        return {
            "id": self.id,
            "team_id": self.team_id,
            "kind": self.kind,
            "base_url": self.base_url,
            "jql": self.jql,
            "username": self.username,
            "field_mapping": self.field_mapping,
            "verify_ssl": self.verify_ssl,
            "sync_interval_minutes": self.sync_interval_minutes,
            "last_sync_at": self.last_sync_at.isoformat() if self.last_sync_at else None,
            "last_sync_status": self.last_sync_status,
            "last_sync_error": self.last_sync_error,
            "has_secret": self.has_secret,
        }


def _row_to_source(row: Any) -> TeamSource:
    return TeamSource(
        id=row.id,
        team_id=row.team_id,
        kind=row.kind,
        base_url=row.base_url,
        jql=row.jql,
        username=row.username,
        field_mapping=row.field_mapping or {},
        verify_ssl=row.verify_ssl,
        sync_interval_minutes=row.sync_interval_minutes,
        last_sync_at=row.last_sync_at,
        last_sync_status=row.last_sync_status,
        last_sync_error=row.last_sync_error,
        has_secret=row.secret_encrypted is not None,
    )


_COLUMNS = (
    "id, team_id, kind, base_url, jql, username, field_mapping, verify_ssl, "
    "sync_interval_minutes, last_sync_at, last_sync_status, last_sync_error, "
    "secret_encrypted"
)


def list_sources(engine: Engine, team_id: int | None = None) -> list[TeamSource]:
    """Подключения команды или всех команд."""
    where = "WHERE team_id = :team " if team_id is not None else ""
    with engine.begin() as conn:
        rows = conn.execute(
            text(f"SELECT {_COLUMNS} FROM team_source {where}ORDER BY id"),
            {"team": team_id} if team_id is not None else {},
        ).all()
    return [_row_to_source(r) for r in rows]


def get_source(engine: Engine, source_id: int) -> TeamSource | None:
    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT {_COLUMNS} FROM team_source WHERE id = :id"),
            {"id": source_id},
        ).one_or_none()
    return _row_to_source(row) if row else None


def save_source(
    engine: Engine,
    *,
    team_id: int,
    base_url: str,
    jql: str,
    secret: str | None = None,
    username: str | None = None,
    field_mapping: dict[str, str] | None = None,
    verify_ssl: bool = True,
    sync_interval_minutes: int | None = None,
    kind: str = "jira",
) -> TeamSource:
    """Создать или обновить подключение команды.

    Пустой `secret` при обновлении означает «оставить прежний»: иначе форма,
    где токен не показывается, стирала бы его при каждом сохранении.
    """
    import json

    encrypted = secrets.encrypt(secret) if secret else None

    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO team_source "
                "  (team_id, kind, base_url, jql, secret_encrypted, username, "
                "   field_mapping, verify_ssl, sync_interval_minutes) "
                "VALUES (:team, :kind, :url, :jql, :secret, :user, "
                "        CAST(:mapping AS jsonb), :ssl, :interval) "
                "ON CONFLICT (team_id, base_url) DO UPDATE SET "
                "  jql = EXCLUDED.jql, "
                "  username = EXCLUDED.username, "
                "  field_mapping = EXCLUDED.field_mapping, "
                "  verify_ssl = EXCLUDED.verify_ssl, "
                "  sync_interval_minutes = EXCLUDED.sync_interval_minutes, "
                "  secret_encrypted = COALESCE("
                "      EXCLUDED.secret_encrypted, team_source.secret_encrypted), "
                "  updated_at = now() "
                f"RETURNING {_COLUMNS}"
            ),
            {
                "team": team_id,
                "kind": kind,
                "url": base_url.rstrip("/"),
                "jql": jql,
                "secret": encrypted,
                "user": username,
                "mapping": json.dumps(field_mapping or {}),
                "ssl": verify_ssl,
                "interval": sync_interval_minutes,
            },
        ).one()
    return _row_to_source(row)


def delete_source(engine: Engine, source_id: int) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM team_source WHERE id = :id"), {"id": source_id}
        )
    return result.rowcount > 0


def read_secret(engine: Engine, source_id: int) -> str | None:
    """Расшифровать токен подключения — только для самой синхронизации."""
    with engine.begin() as conn:
        blob = conn.execute(
            text("SELECT secret_encrypted FROM team_source WHERE id = :id"),
            {"id": source_id},
        ).scalar_one_or_none()
    return secrets.decrypt(blob)


def mark_sync(
    engine: Engine,
    source_id: int,
    *,
    status: str,
    error: str | None = None,
    watermark: datetime | None = None,
) -> None:
    """Записать итог синхронизации.

    Ошибка сохраняется рядом с подключением: без неё владелец команды видит
    только устаревшие данные и не понимает, почему они не обновляются.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE team_source SET last_sync_at = now(), last_sync_status = :status, "
                "  last_sync_error = :error, "
                "  watermark = COALESCE(:watermark, watermark) "
                "WHERE id = :id"
            ),
            {"id": source_id, "status": status, "error": error, "watermark": watermark},
        )


def due_for_sync(engine: Engine, now: datetime | None = None) -> list[TeamSource]:
    """Подключения, которым пора обновиться.

    Никогда не синхронизированные считаются просроченными: иначе только что
    настроенное подключение ждало бы целый интервал до первых данных.
    """
    moment = now or datetime.now(UTC)
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                f"SELECT {_COLUMNS} FROM team_source "
                "WHERE sync_interval_minutes IS NOT NULL "
                "  AND secret_encrypted IS NOT NULL "
                "  AND (last_sync_at IS NULL "
                "       OR last_sync_at + make_interval(mins => sync_interval_minutes) <= :now) "
                "ORDER BY last_sync_at NULLS FIRST"
            ),
            {"now": moment},
        ).all()
    return [_row_to_source(r) for r in rows]


def sync_source(engine: Engine, source: TeamSource, *, full: bool = False) -> dict[str, Any]:
    """Выгрузить данные подключения и пересчитать метрики.

    Инкрементально: без `full` берутся только тикеты, изменённые с прошлой
    синхронизации. На большом проекте разница между минутой и получасом.
    """
    from flowlens.collectors.jira import collect
    from flowlens.collectors.jira_setup import build_config
    from flowlens.importer import import_tickets
    from flowlens.pipeline import recompute_all

    token = read_secret(engine, source.id)
    if not token:
        mark_sync(engine, source.id, status="failed", error="Токен недоступен")
        raise ValueError(
            "Токен подключения недоступен: возможно, сменился ключ шифрования. "
            "Введите токен заново."
        )

    with engine.begin() as conn:
        since = conn.execute(
            text("SELECT watermark FROM team_source WHERE id = :id"), {"id": source.id}
        ).scalar_one_or_none()

    config = build_config(
        source_name=f"{source.kind}-team-{source.team_id}",
        base_url=source.base_url,
        jql=source.jql,
        token=token,
        username=source.username,
        verify_ssl=source.verify_ssl,
        mapping=source.field_mapping,
    )

    started = datetime.now(UTC)
    try:
        profile, tickets = collect(config, since=None if full else since)
    except Exception as exc:
        mark_sync(engine, source.id, status="failed", error=str(exc)[:500])
        raise

    stats = import_tickets(engine, profile, tickets, team_name=_team_name(engine, source.team_id))
    recompute_all(engine)
    # watermark сдвигаем на момент начала выгрузки, а не окончания: тикеты,
    # изменённые во время неё, попадут в следующий заход, а не потеряются
    mark_sync(engine, source.id, status="ok", watermark=started)

    return {
        "tickets": stats.tickets,
        "events": stats.events,
        "people": stats.people,
        "incremental": not full and since is not None,
    }


def _team_name(engine: Engine, team_id: int) -> str:
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT name FROM team WHERE id = :id"), {"id": team_id}
        ).scalar_one()


def next_run_at(source: TeamSource) -> datetime | None:
    """Когда подключение обновится в следующий раз."""
    if source.sync_interval_minutes is None:
        return None
    if source.last_sync_at is None:
        return datetime.now(UTC)
    return source.last_sync_at + timedelta(minutes=source.sync_interval_minutes)


__all__ = [
    "TeamSource",
    "delete_source",
    "due_for_sync",
    "get_source",
    "list_sources",
    "mark_sync",
    "next_run_at",
    "read_secret",
    "save_source",
    "sync_source",
]
