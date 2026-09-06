"""status classification belongs to the team, not to the source

Уникальность статусов была по (source_id, team_id, external_name), поэтому
команда, которая тянет данные из двух источников — например, из Jira и разово
из CSV, — получала два набора статусов с одинаковыми именами. В интерфейсе они
дублировались, а правка одной строки оставляла вторую нетронутой: половина
интервалов считалась бы по старой классификации.

Классификация — это ответ команды на вопрос «что у нас считается работой»,
и он не зависит от того, откуда приехали данные. Уникальность становится
по (team_id, external_name).

Revision ID: 007_status_per_team
Revises: 006_team_unique
"""

from __future__ import annotations

from alembic import op

revision = "007_status_per_team"
down_revision = "006_team_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Оставляем самую раннюю строку на пару (команда, имя) — она принадлежит
    # первому источнику и уже используется существующими интервалами.
    op.execute(
        """
        CREATE TEMP TABLE status_dedup AS
        SELECT w.id AS from_id, k.keep_id AS to_id
        FROM workflow_status w
        JOIN (
            SELECT COALESCE(team_id, 0) AS team, external_name, min(id) AS keep_id
            FROM workflow_status GROUP BY COALESCE(team_id, 0), external_name
        ) k ON k.team = COALESCE(w.team_id, 0) AND k.external_name = w.external_name
        WHERE w.id <> k.keep_id
        """
    )
    op.execute(
        "UPDATE ticket_interval SET status_id = d.to_id "
        "FROM status_dedup d WHERE status_id = d.from_id"
    )
    op.execute(
        "UPDATE ticket_interval SET blocked_from_status_id = d.to_id "
        "FROM status_dedup d WHERE blocked_from_status_id = d.from_id"
    )
    for column in ("old_status_id", "new_status_id"):
        op.execute(
            f"UPDATE ticket_event SET {column} = d.to_id "
            f"FROM status_dedup d WHERE {column} = d.from_id"
        )
    op.execute(
        "UPDATE ticket SET current_status_id = d.to_id "
        "FROM status_dedup d WHERE current_status_id = d.from_id"
    )
    op.execute("DELETE FROM workflow_status w USING status_dedup d WHERE w.id = d.from_id")

    op.execute("DROP INDEX IF EXISTS workflow_status_team_idx")
    op.execute("DROP INDEX IF EXISTS workflow_status_default_idx")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS workflow_status_team_name_idx "
        "ON workflow_status (team_id, external_name) WHERE team_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS workflow_status_default_idx "
        "ON workflow_status (source_id, external_name) WHERE team_id IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS workflow_status_team_name_idx")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS workflow_status_team_idx "
        "ON workflow_status (source_id, team_id, external_name) WHERE team_id IS NOT NULL"
    )
