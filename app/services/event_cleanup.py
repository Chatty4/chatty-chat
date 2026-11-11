from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import ChannelEventRepository

EVENT_RETENTION = timedelta(days=30)  # events.md, api-chat.md


async def clean_up_events(session: AsyncSession, now: datetime) -> int:
    async with session.begin():
        return await ChannelEventRepository(session).delete_events_before(now - EVENT_RETENTION)
