"""blocker reason on intervals

Без причины блокировки её длительность — просто число потерь, из которого
нельзя составить план улучшений. С причиной те же данные превращаются в
Парето: две-три причины обычно дают 80% потерь, и это уже решение.

Revision ID: 003_blocker_reason
Revises: 002_event_unique
"""

from __future__ import annotations

from alembic import op

revision = "003_blocker_reason"
down_revision = "002_event_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE ticket_interval ADD COLUMN IF NOT EXISTS blocker_reason text")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ti_blocker_reason_idx "
        "ON ticket_interval (blocker_reason) WHERE blocker_reason IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ti_blocker_reason_idx")
    op.execute("ALTER TABLE ticket_interval DROP COLUMN IF EXISTS blocker_reason")
