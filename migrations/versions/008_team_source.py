"""stored per-team source connections

Подключение к Jira настраивалось на каждый запуск: токен жил только в памяти
процесса. Для сервиса, где владелец команды настраивает свою команду сам
и данные обновляются по расписанию, подключение надо хранить.

Токен хранится зашифрованным (Fernet, ключ из окружения). В базе его нет
в открытом виде даже у того, у кого есть доступ к базе — а дамп базы
не даёт доступа к Jira.

Revision ID: 008_team_source
Revises: 007_status_per_team
"""

from __future__ import annotations

from alembic import op

revision = "008_team_source"
down_revision = "007_status_per_team"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS team_source (
            id            bigserial PRIMARY KEY,
            team_id       bigint NOT NULL REFERENCES team(id) ON DELETE CASCADE,
            source_id     bigint REFERENCES source(id),
            kind          text   NOT NULL DEFAULT 'jira',
            base_url      text   NOT NULL,
            jql           text   NOT NULL,
            -- зашифрованный секрет; в открытом виде не хранится нигде
            secret_encrypted bytea,
            username      text,
            -- какое поле Jira отвечает за какую дату: {"work_start": "customfield_10014"}
            field_mapping jsonb  NOT NULL DEFAULT '{}'::jsonb,
            verify_ssl    boolean NOT NULL DEFAULT true,
            -- расписание: раз в сколько минут обновлять; NULL = только вручную
            sync_interval_minutes int,
            last_sync_at  timestamptz,
            last_sync_status text,
            last_sync_error  text,
            watermark     timestamptz,
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now(),
            CHECK (sync_interval_minutes IS NULL OR sync_interval_minutes >= 5)
        )
        """
    )
    # одно подключение на команду и адрес: две разные Jira допустимы,
    # два подключения к одной и той же — почти всегда ошибка настройки
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS team_source_unique_idx "
        "ON team_source (team_id, base_url)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS team_source_due_idx "
        "ON team_source (sync_interval_minutes, last_sync_at) "
        "WHERE sync_interval_minutes IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS team_source")
