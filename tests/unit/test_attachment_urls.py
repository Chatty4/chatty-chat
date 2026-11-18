import uuid
from datetime import UTC, datetime

import pytest

from app.clients import storage
from app.core.codes import ErrorCode
from app.core.exceptions import NotFound
from tests.fakes import (
    FakeCore,
    FakeMessages,
    MessageFakes,
    attached_file,
    channel,
    memberships,
    saved_message,
)

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()
USER = memberships(USER_ID)
EXPIRES_AT = datetime(2026, 10, 5, 14, 7, 11, tzinfo=UTC)


@pytest.fixture(autouse=True)
def signed(monkeypatch) -> list[tuple[str, str, str]]:
    """presign_attachment without settings: it records what it signed and returns a fake URL."""
    calls: list[tuple[str, str, str]] = []

    def presign(object_key: str, filename: str, mime: str) -> tuple[str, datetime]:
        calls.append((object_key, filename, mime))
        return f"https://signed.example/{object_key}", EXPIRES_AT

    monkeypatch.setattr(storage, "presign_attachment", presign)
    return calls


async def fakes_with(room, *names: str):
    message = saved_message(room.channel_id, OTHER_ID, uuid.uuid4())
    fakes = MessageFakes(FakeCore(channel=room), FakeMessages(stored=[message]))
    files = [attached_file(name) for name in names]
    await fakes.messages.insert_attachments(message.id, files)
    return message, fakes, files


async def test_a_url_per_file_in_the_sent_order(signed):
    room = channel(USER_ID, OTHER_ID)
    message, fakes, files = await fakes_with(room, "report.pdf", "chart.png")

    result = await fakes.service.attachment_urls(USER, message.id)

    assert [item.file_id for item in result.attachments] == [file.file_id for file in files]
    first = result.attachments[0]
    assert (first.filename, first.mime, first.size) == ("report.pdf", "application/pdf", 482133)
    assert first.url == f"https://signed.example/{files[0].object_key}"
    assert first.expires_at == EXPIRES_AT
    assert signed == [(file.object_key, file.filename, file.mime) for file in files]
    assert "object_key" not in result.model_dump_json()


async def test_a_message_without_files_has_none(signed):
    room = channel(USER_ID, OTHER_ID)
    message, fakes, _ = await fakes_with(room)

    result = await fakes.service.attachment_urls(USER, message.id)

    assert result.attachments == []
    assert signed == []


async def test_a_deleted_message_is_404(signed):
    room = channel(USER_ID, OTHER_ID)
    message, fakes, _ = await fakes_with(room, "report.pdf")
    message.deleted_at = datetime.now(UTC)

    with pytest.raises(NotFound) as error:
        await fakes.service.attachment_urls(USER, message.id)

    assert error.value.code == ErrorCode.MESSAGE_NOT_FOUND
    assert signed == []


async def test_a_missing_message_is_404():
    fakes = MessageFakes(FakeCore(channel=channel(USER_ID)))

    with pytest.raises(NotFound) as error:
        await fakes.service.attachment_urls(USER, uuid.uuid4())

    assert error.value.code == ErrorCode.MESSAGE_NOT_FOUND


async def test_a_channel_the_user_cant_read_is_404(signed):
    private = channel(OTHER_ID, kind="private")  # the user isn't in it
    message, fakes, _ = await fakes_with(private, "report.pdf")

    with pytest.raises(NotFound) as error:
        await fakes.service.attachment_urls(USER, message.id)

    assert error.value.code == ErrorCode.MESSAGE_NOT_FOUND  # nobody learns the message exists
    assert signed == []
