"""service level expectations

Пока перцентиль пересчитывается на лету, обещание всегда равно факту, и
сказать «мы не уложились» технически нельзя. Зафиксированный SLE делает
обещание проверяемым: сравнивать факт становится с чем.

Revision ID: 004_sle
Revises: 003_blocker_reason
"""

from __future__ import annotations

from alembic import op

revision = "004_sle"
down_revision = "003_blocker_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS service_level_expectation (
            id            bigserial PRIMARY KEY,
            team_id       bigint REFERENCES team(id),
            issue_type    text,                    -- NULL = любой тип
            priority      text,                    -- NULL = любой приоритет
            percentile    int    NOT NULL DEFAULT 85,
            target_business_s bigint NOT NULL,
            -- на какой выборке зафиксировали: без этого нельзя понять,
            -- обещание устарело или система действительно испортилась
            sample_size   int    NOT NULL,
            fixed_at      timestamptz NOT NULL DEFAULT now(),
            fixed_by      bigint REFERENCES person(id),
            note          text,
            retired_at    timestamptz,             -- NULL = действует
            CHECK (percentile BETWEEN 1 AND 99),
            CHECK (target_business_s > 0)
        )
        """
    )
    # действующее обещание на класс — ровно одно
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS sle_active_class_idx
            ON service_level_expectation (
                COALESCE(team_id, 0),
                COALESCE(issue_type, ''),
                COALESCE(priority, ''),
                percentile
            )
            WHERE retired_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS sle_active_class_idx")
    op.execute("DROP TABLE IF EXISTS service_level_expectation")
