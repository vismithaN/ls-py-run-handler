"""Add versioned batch locations for optimized run storage.

Revision ID: 9d8cf2b2b8b4
Revises: 26a9efc758a0
"""

from typing import Sequence, Union

from alembic import op

revision: str = "9d8cf2b2b8b4"
down_revision: Union[str, None] = "26a9efc758a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add compact, versioned locations while retaining version-1 rows."""

    op.execute(
        """
        ALTER TABLE runs
            ADD COLUMN batch_id UUID,
            ADD COLUMN object_key TEXT,
            ADD COLUMN byte_start BIGINT,
            ADD COLUMN byte_length BIGINT,
            ADD COLUMN format_version SMALLINT NOT NULL DEFAULT 1,
            ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            ADD CONSTRAINT runs_v2_location_check CHECK (
                format_version = 1 OR (
                    format_version = 2
                    AND batch_id IS NOT NULL
                    AND object_key IS NOT NULL
                    AND byte_start IS NOT NULL
                    AND byte_start >= 0
                    AND byte_length IS NOT NULL
                    AND byte_length > 0
                )
            );
        """
    )


def downgrade() -> None:
    """Remove version-2 storage location columns."""

    op.execute(
        """
        ALTER TABLE runs
            DROP CONSTRAINT runs_v2_location_check,
            DROP COLUMN created_at,
            DROP COLUMN format_version,
            DROP COLUMN byte_length,
            DROP COLUMN byte_start,
            DROP COLUMN object_key,
            DROP COLUMN batch_id;
        """
    )
