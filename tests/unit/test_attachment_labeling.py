import uuid
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment
from app.repositories import AttachmentRepository
from app.services import attachment_labeling
from app.services.attachment_labeling import label_next
from tests.fakes import FakeSession


class FakeAttachments:
    def __init__(self, *rows: Attachment) -> None:
        self.rows, self.saved = list(rows), {}

    async def claim_unlabeled(self) -> Attachment | None:
        return self.rows.pop(0) if self.rows else None

    async def save_labels(self, file_id, labels, content) -> None:
        self.saved[file_id] = (labels, content)


def attachment(mime: str) -> Attachment:
    return Attachment(file_id=uuid.uuid4(), object_key="attachments/t1/f1", mime=mime)


async def run(attachments: FakeAttachments, log: list[str]) -> bool:
    return await label_next(
        cast(AsyncSession, FakeSession(log)), cast(AttachmentRepository, attachments)
    )


@pytest.fixture
def downloads(monkeypatch) -> list[str]:
    """No MinIO and no models: records each download and returns fixed labels."""
    keys: list[str] = []

    def fake(object_key: str, mime: str):
        keys.append(object_key)
        return "red", "hello"

    monkeypatch.setattr(attachment_labeling, "_download_and_label", fake)
    return keys


async def test_nothing_left(downloads):
    assert await run(FakeAttachments(), log := []) is False
    assert log == ["commit"]


async def test_saves_labels_in_a_second_transaction(downloads):
    row, log = attachment("image/png"), []
    attachments = FakeAttachments(row)

    assert await run(attachments, log) is True

    assert attachments.saved == {row.file_id: ("red", "hello")}
    assert log == ["commit", "commit"]  # the claim is committed before labeling


async def test_other_types_are_saved_without_downloading(downloads):
    row = attachment("video/mp4")
    attachments = FakeAttachments(row)

    await run(attachments, [])

    assert downloads == []
    assert attachments.saved == {row.file_id: (None, None)}


async def test_a_failure_saves_nothing(monkeypatch):
    def broken(object_key: str, mime: str):
        raise ValueError("broken file")

    monkeypatch.setattr(attachment_labeling, "_download_and_label", broken)
    row = attachment("application/pdf")
    attachments, log = FakeAttachments(row), []

    assert await run(attachments, log) is True  # handled: the loop goes on to the next file

    assert attachments.saved == {}
    assert log == ["commit"]  # only the claim: the attempt stays counted
