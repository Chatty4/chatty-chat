import uuid

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ReadState


class ReadStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def advance_read_position(
        self, user_id: uuid.UUID, channel_id: uuid.UUID, seq: int
    ) -> None:
        """Move the user's read position to seq. It only moves forward."""
        stmt = insert(ReadState).values(user_id=user_id, channel_id=channel_id, last_read_seq=seq)
        stmt = stmt.on_conflict_do_update(
            index_elements=[ReadState.user_id, ReadState.channel_id],
            set_={
                "last_read_seq": func.greatest(
                    ReadState.last_read_seq, stmt.excluded.last_read_seq
                ),
                "updated_at": func.now(),
            },
        )

        await self._session.execute(stmt)
