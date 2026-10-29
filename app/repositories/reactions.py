import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Reaction


class ReactionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_reaction(
        self, message_id: uuid.UUID, user_id: uuid.UUID, emoji: str
    ) -> datetime | None:
        """Insert the reaction. Returns its created_at, or None if the user already had it."""
        stmt = (
            insert(Reaction)
            .values(message_id=message_id, user_id=user_id, emoji=emoji)
            .on_conflict_do_nothing(
                index_elements=[Reaction.message_id, Reaction.user_id, Reaction.emoji]
            )
            .returning(Reaction.created_at)
        )
        return await self._session.scalar(stmt)

    async def list_reactions(
        self, message_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[tuple[str, int, bool]]:
        """(emoji, count, me) per emoji, in the order the emoji were first used."""
        stmt = (
            select(Reaction.emoji, func.count(), func.bool_or(Reaction.user_id == user_id))
            .where(Reaction.message_id == message_id)
            .group_by(Reaction.emoji)
            .order_by(func.min(Reaction.created_at))
        )
        return [(emoji, count, me) for emoji, count, me in await self._session.execute(stmt)]
