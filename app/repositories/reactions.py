import uuid
from datetime import datetime

from sqlalchemy import delete, func, select
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

    async def remove_reaction(
        self, message_id: uuid.UUID, user_id: uuid.UUID, emoji: str
    ) -> datetime | None:
        """
        Delete the reaction. Returns the database time (the event's created_at), or None if the
        user didn't have it. Two removes at once: the second one deletes nothing.
        """
        stmt = (
            delete(Reaction)
            .where(
                Reaction.message_id == message_id,
                Reaction.user_id == user_id,
                Reaction.emoji == emoji,
            )
            .returning(func.now())
        )
        return await self._session.scalar(stmt)

    async def delete_message_reactions(self, message_id: uuid.UUID) -> None:
        """Remove every reaction of a deleted message. message.deleted covers them: no events."""
        await self._session.execute(delete(Reaction).where(Reaction.message_id == message_id))

    async def list_reactions(
        self, message_ids: list[uuid.UUID], user_id: uuid.UUID
    ) -> dict[uuid.UUID, list[tuple[str, int, bool]]]:
        """
        (emoji, count, me) per emoji of each message, in the order the emoji were first used.
        One query for all of them. Messages without reactions are not in the result.
        """
        if not message_ids:
            return {}

        stmt = (
            select(
                Reaction.message_id,
                Reaction.emoji,
                func.count(),
                func.bool_or(Reaction.user_id == user_id),
            )
            .where(Reaction.message_id.in_(message_ids))
            .group_by(Reaction.message_id, Reaction.emoji)
            .order_by(func.min(Reaction.created_at))
        )

        reactions: dict[uuid.UUID, list[tuple[str, int, bool]]] = {}
        for message_id, emoji, count, me in await self._session.execute(stmt):
            reactions.setdefault(message_id, []).append((emoji, count, me))
        return reactions
