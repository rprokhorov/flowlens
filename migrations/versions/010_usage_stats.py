"""aggregated usage counters

Чтобы понимать, чем в продукте пользуются, нужен счётчик обращений. Он
намеренно агрегатный: сутки, пользователь, раздел, количество. Из такой
таблицы видно, какие разделы живут, а какие никто не открывает, но нельзя
восстановить, что человек делал в конкретный момент.

Подробный журнал дал бы больше для разбора и превратил бы инструмент анализа
потока в инструмент наблюдения за сотрудниками. Для ответа на вопрос «чем
пользуются» достаточно счётчиков.

Revision ID: 010_usage_stats
Revises: 009_auth
"""

from __future__ import annotations

from alembic import op

revision = "010_usage_stats"
down_revision = "009_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_daily (
            day      date   NOT NULL,
            user_id  bigint REFERENCES app_user(id) ON DELETE CASCADE,
            -- раздел интерфейса, а не путь запроса: путей десятки,
            -- и они меняются, а разделы — то, о чём думает человек
            section  text   NOT NULL,
            hits     int    NOT NULL DEFAULT 0,
            PRIMARY KEY (day, user_id, section)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS usage_daily_day_idx ON usage_daily (day DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS usage_daily")
