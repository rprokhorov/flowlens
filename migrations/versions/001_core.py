"""core schema

Revision ID: 001_core
Revises:
Create Date: 2026-09-01
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "001_core"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA_SQL = Path(__file__).resolve().parents[2] / "schema" / "001_core.sql"

DROP_ORDER = [
    "sync_run",
    "intervention",
    "person_workload_daily",
    "ticket_metrics",
    "ticket_interval",
    "ticket_timeline_fact",
    "ticket_comment",
    "ticket_declared_date",
    "ticket_event",
    "ticket_link",
    "ticket",
    "service_class",
    "workflow_status",
    "person_absence",
    "person_team",
    "person_alias",
    "person",
    "team",
    "calendar",
    "source",
]


def upgrade() -> None:
    op.execute(SCHEMA_SQL.read_text())


def downgrade() -> None:
    for table in DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP TYPE IF EXISTS event_type")
    op.execute("DROP TYPE IF EXISTS canonical_phase")
