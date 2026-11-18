import uuid
from datetime import UTC, datetime

from app.models import Attachment
from app.services.message_service import to_message_response
from tests.fakes import saved_message


def stored_file(message_id: uuid.UUID, position: int, filename: str) -> Attachment:
    file_id = uuid.uuid4()
    return Attachment(
        file_id=file_id,
        message_id=message_id,
        position=position,
        object_key=f"attachments/{uuid.uuid4()}/{file_id}",
        filename=filename,
        mime="application/pdf",
        size=482133,
    )


def test_files_in_the_order_given():
    message = saved_message(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    files = [stored_file(message.id, 0, "report.pdf"), stored_file(message.id, 1, "notes.pdf")]

    response = to_message_response(message, attachments=files)

    assert response.file_ids == [files[0].file_id, files[1].file_id]
    assert [item.filename for item in response.attachments] == ["report.pdf", "notes.pdf"]
    assert response.attachments[0].model_dump() == {
        "file_id": files[0].file_id,
        "filename": "report.pdf",
        "mime": "application/pdf",
        "size": 482133,
    }


def test_no_files():
    message = saved_message(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    response = to_message_response(message)

    assert response.file_ids == [] and response.attachments == []


def test_deleted_message_has_no_files():
    message = saved_message(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    message.deleted_at = datetime.now(UTC)

    response = to_message_response(message, attachments=[stored_file(message.id, 0, "a.pdf")])

    assert response.file_ids == [] and response.attachments == []


def test_object_key_is_never_returned():
    message = saved_message(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    response = to_message_response(message, attachments=[stored_file(message.id, 0, "a.pdf")])

    assert "object_key" not in response.model_dump_json()
