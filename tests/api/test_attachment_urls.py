import uuid
from datetime import UTC, datetime

import pytest

from app.api.deps import get_core_client, get_message_service
from app.clients import storage
from app.models import Message
from app.schemas.core import CoreChannel
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
EXPIRES_AT = datetime(2026, 10, 5, 14, 7, 11, tzinfo=UTC)


def url(message_id: uuid.UUID) -> str:
    return f"/api/chat/v1/messages/{message_id}/attachments"


def use(app, room: CoreChannel, message: Message) -> MessageFakes:
    core = FakeCore(memberships=memberships(USER_ID), channel=room)
    fakes = MessageFakes(core, FakeMessages(stored=[message]))
    app.dependency_overrides[get_core_client] = lambda: core
    app.dependency_overrides[get_message_service] = lambda: fakes.service
    return fakes


@pytest.fixture(autouse=True)
def signed(monkeypatch):
    def presign(object_key: str, filename: str, mime: str) -> tuple[str, datetime]:
        return f"https://signed.example/{object_key}", EXPIRES_AT

    monkeypatch.setattr(storage, "presign_attachment", presign)


@pytest.fixture
def auth(make_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(USER_ID))}"}


async def test_message_with_a_file_is_200_with_its_url(app, client, auth):
    room = channel(USER_ID, OTHER_ID)
    message = saved_message(room.channel_id, OTHER_ID, uuid.uuid4())
    fakes = use(app, room, message)
    report = attached_file()
    await fakes.messages.insert_attachments(message.id, [report])

    response = await client.get(url(message.id), headers=auth)

    assert response.status_code == 200
    assert response.json() == {
        "attachments": [
            {
                "file_id": str(report.file_id),
                "filename": "report.pdf",
                "mime": "application/pdf",
                "size": 482133,
                "url": f"https://signed.example/{report.object_key}",
                "expires_at": "2026-10-05T14:07:11Z",
            }
        ]
    }


async def test_message_without_files_is_200_with_none(app, client, auth):
    room = channel(USER_ID)
    message = saved_message(room.channel_id, USER_ID, uuid.uuid4())
    use(app, room, message)

    response = await client.get(url(message.id), headers=auth)

    assert response.status_code == 200
    assert response.json() == {"attachments": []}


async def test_deleted_message_is_404(app, client, auth):
    room = channel(USER_ID)
    message = saved_message(room.channel_id, USER_ID, uuid.uuid4())
    message.deleted_at = datetime.now(UTC)
    use(app, room, message)

    response = await client.get(url(message.id), headers=auth)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "message_not_found"


async def test_no_token_is_401(app, client):
    room = channel(USER_ID)
    message = saved_message(room.channel_id, USER_ID, uuid.uuid4())
    use(app, room, message)

    response = await client.get(url(message.id))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
