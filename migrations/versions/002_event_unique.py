"""fix event uniqueness with null field

NULL != NULL в SQL, поэтому события без поля (created, comment) обходили
ограничение UNIQUE (ticket_id, source_event_id, field) и дублировались
при повторном импорте. Используем уникальный индекс с COALESCE.

Revision ID: 002_event_unique
Revises: 001_core
"""

from __future__ import annotations

from alembic import op

revision = "002_event_unique"
down_revision = "001_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE ticket_event DROP CONSTRAINT IF EXISTS "
        "ticket_event_ticket_id_source_event_id_field_key"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ticket_event_natural_key_idx "
        "ON ticket_event (ticket_id, source_event_id, COALESCE(field, ''))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ticket_event_natural_key_idx")
    op.execute(
        "ALTER TABLE ticket_event ADD CONSTRAINT "
        "ticket_event_ticket_id_source_event_id_field_key "
        "UNIQUE (ticket_id, source_event_id, field)"
    )
