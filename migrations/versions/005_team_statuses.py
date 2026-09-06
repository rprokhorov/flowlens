"""per-team workflow statuses

Классификация статусов была общей на источник, то есть на всю Jira. Но `qa`
у одной команды — code review и активная работа, а у другой — очередь на ручное
тестирование. С общей настройкой вторая команда молча получала бы чужую
классификацию: метрики считались бы без ошибок, но описывали бы не её процесс.

Существующие статусы привязываются к первой команде — на момент миграции она
единственная, и это сохраняет текущие расчёты без изменений.

Revision ID: 005_team_statuses
Revises: 004_sle
"""

from __future__ import annotations

from alembic import op

revision = "005_team_statuses"
down_revision = "004_sle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE workflow_status ADD COLUMN IF NOT EXISTS team_id bigint")
    op.execute(
        "ALTER TABLE workflow_status ADD CONSTRAINT workflow_status_team_fk "
        "FOREIGN KEY (team_id) REFERENCES team(id) ON DELETE CASCADE"
    )
    # существующие статусы отдаём первой команде: до этой миграции она была одна
    op.execute(
        "UPDATE workflow_status SET team_id = (SELECT min(id) FROM team) "
        "WHERE team_id IS NULL"
    )

    op.execute(
        "ALTER TABLE workflow_status DROP CONSTRAINT IF EXISTS "
        "workflow_status_source_id_external_name_key"
    )
    # Два частичных индекса вместо одного по выражению: ON CONFLICT требует
    # дословного совпадения выражения с индексом, а COALESCE(bigint, int)
    # и COALESCE(bigint, bigint) для Postgres — разные выражения.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS workflow_status_team_idx "
        "ON workflow_status (source_id, team_id, external_name) "
        "WHERE team_id IS NOT NULL"
    )
    # NULL в team_id означает «настройка по умолчанию для источника»
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS workflow_status_default_idx "
        "ON workflow_status (source_id, external_name) WHERE team_id IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS workflow_status_team_idx")
    op.execute("DROP INDEX IF EXISTS workflow_status_default_idx")
    op.execute(
        "ALTER TABLE workflow_status DROP CONSTRAINT IF EXISTS workflow_status_team_fk"
    )
    # при откате оставляем по одной строке на статус, иначе UNIQUE не создастся
    op.execute(
        "DELETE FROM workflow_status a USING workflow_status b "
        "WHERE a.id > b.id AND a.source_id = b.source_id "
        "  AND a.external_name = b.external_name"
    )
    op.execute("ALTER TABLE workflow_status DROP COLUMN IF EXISTS team_id")
    op.execute(
        "ALTER TABLE workflow_status ADD CONSTRAINT "
        "workflow_status_source_id_external_name_key UNIQUE (source_id, external_name)"
    )
