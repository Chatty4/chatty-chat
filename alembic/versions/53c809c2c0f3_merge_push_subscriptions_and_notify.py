"""merge push subscriptions and notify

Revision ID: 53c809c2c0f3
Revises: 0cbda8d6f1e2, 64b0a6c86546
Create Date: 2026-11-12 11:42:40.240352

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "53c809c2c0f3"
down_revision: str | Sequence[str] | None = ("0cbda8d6f1e2", "64b0a6c86546")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
