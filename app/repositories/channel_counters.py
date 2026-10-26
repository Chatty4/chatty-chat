import uuid

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChannelCounter


class ChannelCounterRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def next_seq(self, channel_id: uuid.UUID) -> int:
        """
        The channel's next seq. The upsert locks the counter row until the transaction ends,
        so seqs of one channel are handed out in order. It also creates the row for the channel's
        first message.
        """
        stmt = (
            insert(ChannelCounter)
            .values(channel_id=channel_id, last_seq=1)
            .on_conflict_do_update(
                index_elements=[ChannelCounter.channel_id],
                set_={"last_seq": ChannelCounter.last_seq + 1},
            )
            .returning(ChannelCounter.last_seq)
        )

        return (await self._session.execute(stmt)).scalar_one()
