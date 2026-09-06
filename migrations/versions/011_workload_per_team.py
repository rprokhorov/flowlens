"""workload is per team, not just per person and day

Нагрузка людей копилась с ключом (person_id, day) и сохранялась с team_id
последнего обработанного тикета. С одной командой это было незаметно;
со второй вся нагрузка попадала в одну команду, и вкладка «Нагрузка»
у остальных оказывалась пустой.

Человек может работать в нескольких командах — например, платформенный
разработчик, помогающий продуктовой команде. Его день в каждой из них
должен считаться отдельно, иначе строки перетирают друг друга.

Revision ID: 011_workload_per_team
Revises: 010_usage_stats
"""

from __future__ import annotations

from alembic import op

revision = "011_workload_per_team"
down_revision = "010_usage_stats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Данные пересчитываемы из ticket_interval, поэтому таблицу проще
    # очистить, чем мигрировать: следующий recompute заполнит её заново
    # и уже правильно.
    op.execute("TRUNCATE person_workload_daily")
    op.execute(
        "ALTER TABLE person_workload_daily DROP CONSTRAINT IF EXISTS "
        "person_workload_daily_pkey"
    )
    op.execute("ALTER TABLE person_workload_daily ALTER COLUMN team_id SET NOT NULL")
    op.execute(
        "ALTER TABLE person_workload_daily ADD PRIMARY KEY (person_id, day, team_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS workload_team_day_idx "
        "ON person_workload_daily (team_id, day)"
    )


def downgrade() -> None:
    op.execute("TRUNCATE person_workload_daily")
    op.execute("DROP INDEX IF EXISTS workload_team_day_idx")
    op.execute(
        "ALTER TABLE person_workload_daily DROP CONSTRAINT IF EXISTS "
        "person_workload_daily_pkey"
    )
    op.execute("ALTER TABLE person_workload_daily ALTER COLUMN team_id DROP NOT NULL")
    op.execute("ALTER TABLE person_workload_daily ADD PRIMARY KEY (person_id, day)")
