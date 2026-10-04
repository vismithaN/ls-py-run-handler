"""Add a reference to each complete run in S3.

Revision ID: 81b1d309a02a
Revises: 26a9efc758a0
"""

from alembic import op

revision = "81b1d309a02a"
down_revision = "26a9efc758a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable so existing rows can continue using their field references.
    op.execute("ALTER TABLE runs ADD COLUMN run_ref TEXT")


def downgrade() -> None:
    # Dropping the only storage pointer would make new runs unreadable.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM runs
                WHERE run_ref IS NOT NULL
                  AND (inputs IS NULL OR outputs IS NULL OR metadata IS NULL)
            ) THEN
                RAISE EXCEPTION
                    'Backfill field references before dropping run_ref';
            END IF;
        END $$;
    """
    )
    op.execute("ALTER TABLE runs DROP COLUMN run_ref")
