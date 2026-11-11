"""notify chat_events on insert

Revision ID: 64b0a6c86546
Revises: e90e67279dcf
Create Date: 2026-11-11 14:29:22.950392

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "64b0a6c86546"
down_revision: str | Sequence[str] | None = "e90e67279dcf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Wakes the outbox relay. Sent on commit, so the relay never wakes for a row it can't see."""
    op.execute(
        """
        CREATE FUNCTION notify_chat_events() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('chat_events', '');
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER channel_events_notify AFTER INSERT ON channel_events
        FOR EACH STATEMENT EXECUTE FUNCTION notify_chat_events()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER channel_events_notify ON channel_events")
    op.execute("DROP FUNCTION notify_chat_events()")
