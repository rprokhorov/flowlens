"""fix team uniqueness with null parent

NULL != NULL в SQL, поэтому UNIQUE (name, parent_team_id) не ловил команды
верхнего уровня: у них parent_team_id всегда NULL, и каждый импорт создавал
новую команду с тем же именем. Задачи расползались между дубликатами, а метрики
считались по части данных — без единого сообщения об ошибке.

Та же ошибка уже была с событиями (002_event_unique) и лечится так же —
уникальным индексом с COALESCE.

Revision ID: 006_team_unique
Revises: 005_team_statuses
"""

from __future__ import annotations

from alembic import op

revision = "006_team_unique"
down_revision = "005_team_statuses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Слияние дубликатов: всё переносится на команду с наименьшим id.
    # Порядок важен — сначала ссылки, потом сами команды.
    op.execute(
        """
        CREATE TEMP TABLE team_merge AS
        SELECT t.id AS from_id, k.keep_id AS to_id
        FROM team t
        JOIN (
            SELECT name, COALESCE(parent_team_id, 0) AS parent, min(id) AS keep_id
            FROM team GROUP BY name, COALESCE(parent_team_id, 0)
        ) k ON k.name = t.name AND k.parent = COALESCE(t.parent_team_id, 0)
        WHERE t.id <> k.keep_id
        """
    )
    for table in ("ticket", "person_workload_daily", "intervention"):
        op.execute(
            f"UPDATE {table} SET team_id = m.to_id FROM team_merge m WHERE team_id = m.from_id"
        )
    op.execute(
        "UPDATE service_level_expectation SET team_id = m.to_id "
        "FROM team_merge m WHERE team_id = m.from_id"
    )
    # Статусы дублей нельзя просто удалить: на них ссылаются интервалы,
    # события и сами тикеты. Сначала переводим ссылки на одноимённый статус
    # команды-приёмника, и только потом убираем осиротевшие строки.
    op.execute(
        """
        CREATE TEMP TABLE status_merge AS
        SELECT old.id AS from_id, new.id AS to_id
        FROM workflow_status old
        JOIN team_merge m ON m.from_id = old.team_id
        JOIN workflow_status new
          ON new.team_id = m.to_id
         AND new.source_id = old.source_id
         AND new.external_name = old.external_name
        """
    )
    op.execute(
        "UPDATE ticket_interval SET status_id = s.to_id "
        "FROM status_merge s WHERE status_id = s.from_id"
    )
    op.execute(
        "UPDATE ticket_interval SET blocked_from_status_id = s.to_id "
        "FROM status_merge s WHERE blocked_from_status_id = s.from_id"
    )
    for column in ("old_status_id", "new_status_id"):
        op.execute(
            f"UPDATE ticket_event SET {column} = s.to_id "
            f"FROM status_merge s WHERE {column} = s.from_id"
        )
    op.execute(
        "UPDATE ticket SET current_status_id = s.to_id "
        "FROM status_merge s WHERE current_status_id = s.from_id"
    )
    # статусы дубля, у которых нет пары у приёмника, переезжают вместе с ним
    op.execute(
        "UPDATE workflow_status w SET team_id = m.to_id FROM team_merge m "
        "WHERE w.team_id = m.from_id "
        "  AND NOT EXISTS (SELECT 1 FROM status_merge s WHERE s.from_id = w.id)"
    )
    op.execute(
        "DELETE FROM workflow_status w USING status_merge s WHERE w.id = s.from_id"
    )
    op.execute("UPDATE person_team SET team_id = m.to_id FROM team_merge m WHERE team_id = m.from_id")
    op.execute("UPDATE team SET parent_team_id = m.to_id FROM team_merge m WHERE parent_team_id = m.from_id")
    op.execute("DELETE FROM team t USING team_merge m WHERE t.id = m.from_id")

    op.execute("ALTER TABLE team DROP CONSTRAINT IF EXISTS team_name_parent_team_id_key")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS team_natural_key_idx "
        "ON team (name, COALESCE(parent_team_id, 0))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS team_natural_key_idx")
    op.execute(
        "ALTER TABLE team ADD CONSTRAINT team_name_parent_team_id_key "
        "UNIQUE (name, parent_team_id)"
    )
