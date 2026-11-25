"""MessageRepository.insert_attachments and list_attachments against the real Postgres."""

import uuid

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import Attachment, Message
from app.models.message import SEARCH_CONFIG
from app.repositories import AttachmentRepository, MessageRepository
from app.repositories.attachments import MAX_ATTEMPTS
from tests.fakes import attached_file

pytestmark = pytest.mark.integration

CHANNEL_ID = uuid.uuid4()


@pytest.fixture
async def sessions():
    engine = create_async_engine(get_settings().DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield sessions
    async with sessions.begin() as session:
        # the attachments go with their messages (ON DELETE CASCADE)
        await session.execute(delete(Message).where(Message.channel_id == CHANNEL_ID))
    await engine.dispose()


async def new_message(sessions, seq: int) -> Message:
    async with sessions.begin() as session:
        return await MessageRepository(session).insert_message(
            channel_id=CHANNEL_ID,
            author_id=uuid.uuid4(),
            seq=seq,
            client_msg_id=uuid.uuid4(),
            body="",
            mentions=[],
        )


async def test_position_is_the_order_of_the_list(sessions):
    message = await new_message(sessions, 1)
    files = [attached_file("report.pdf"), attached_file("chart.png", "image/png")]

    async with sessions.begin() as session:
        rows = await MessageRepository(session).insert_attachments(message.id, files)

    assert [row.position for row in rows] == [0, 1]
    assert [row.file_id for row in rows] == [file.file_id for file in files]
    assert rows[1].object_key == files[1].object_key
    assert rows[1].mime == "image/png"


async def test_list_attachments_in_position_order(sessions):
    first, second = await new_message(sessions, 1), await new_message(sessions, 2)
    files = [attached_file(f"{name}.txt", "text/plain") for name in "abc"]
    async with sessions.begin() as session:
        repository = MessageRepository(session)
        await repository.insert_attachments(first.id, files)
        await repository.insert_attachments(second.id, [attached_file()])

    async with sessions() as session:
        found = await MessageRepository(session).list_attachments([first.id, second.id])

    assert [row.filename for row in found[first.id]] == ["a.txt", "b.txt", "c.txt"]
    assert len(found[second.id]) == 1


async def test_messages_without_files_are_missing(sessions):
    with_files, without = await new_message(sessions, 1), await new_message(sessions, 2)
    async with sessions.begin() as session:
        await MessageRepository(session).insert_attachments(with_files.id, [attached_file()])

    async with sessions() as session:
        found = await MessageRepository(session).list_attachments([with_files.id, without.id])

    assert list(found) == [with_files.id]


async def test_empty_list(sessions):
    async with sessions() as session:
        assert await MessageRepository(session).list_attachments([]) == {}


async def test_deleting_the_message_deletes_its_attachments(sessions):
    message = await new_message(sessions, 1)
    async with sessions.begin() as session:
        await MessageRepository(session).insert_attachments(message.id, [attached_file()])

    async with sessions.begin() as session:
        await session.execute(delete(Message).where(Message.id == message.id))

    async with sessions() as session:
        assert await MessageRepository(session).list_attachments([message.id]) == {}


async def test_tsv_matches_filename_parts_labels_and_content(sessions):
    message = await new_message(sessions, 1)
    async with sessions.begin() as session:
        [row] = await MessageRepository(session).insert_attachments(
            message.id, [attached_file("report_q3.pdf")]
        )
        row.labels, row.content = "red sunset", "quarterly revenue in Bucharest"

    async with sessions() as session:
        for word in ["report", "red", "bucharest"]:
            hit = await session.scalar(
                select(Attachment.file_id).where(
                    Attachment.file_id == row.file_id,
                    Attachment.tsv.bool_op("@@")(func.plainto_tsquery(SEARCH_CONFIG, word)),
                )
            )
            assert hit == row.file_id, word


async def only_unlabeled(session, file_id: uuid.UUID) -> None:
    """Hides every other unlabeled attachment from the claim. The caller never commits."""
    await session.execute(
        update(Attachment)
        .where(Attachment.labeled_at.is_(None), Attachment.file_id != file_id)
        .values(labeled_at=func.now())
    )


async def test_claim_counts_the_attempt_and_saved_files_are_not_claimed(sessions):
    message = await new_message(sessions, 1)
    async with sessions.begin() as session:
        [row] = await MessageRepository(session).insert_attachments(message.id, [attached_file()])

    async with sessions() as session:  # never committed: rolled back when it closes
        await only_unlabeled(session, row.file_id)
        repository = AttachmentRepository(session)

        claimed = await repository.claim_unlabeled()
        assert claimed is not None and claimed.file_id == row.file_id
        assert claimed.label_attempts == 1

        await repository.save_labels(row.file_id, "orange cat", None)
        assert await repository.claim_unlabeled() is None


async def test_claim_gives_up_after_max_attempts(sessions):
    message = await new_message(sessions, 1)
    async with sessions.begin() as session:
        [row] = await MessageRepository(session).insert_attachments(message.id, [attached_file()])

    async with sessions() as session:
        await only_unlabeled(session, row.file_id)
        repository = AttachmentRepository(session)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            claimed = await repository.claim_unlabeled()
            assert claimed is not None and claimed.label_attempts == attempt

        assert await repository.claim_unlabeled() is None
