import uuid
from datetime import UTC, datetime

from app.models import Attachment, Message
from app.services.message_service import REPLY_PREVIEW_CHARS, to_message_response
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


def reply_to(replied: Message) -> Message:
    reply = saved_message(replied.channel_id, uuid.uuid4(), uuid.uuid4(), seq=replied.seq + 1)
    reply.reply_to_id = replied.id
    return reply


def test_a_message_that_is_not_a_reply_has_no_quote():
    message = saved_message(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    assert to_message_response(message).reply_to is None


def test_a_reply_quotes_the_start_of_the_replied_message():
    replied = saved_message(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), seq=4)
    replied.body = "x" * 300

    quote = to_message_response(reply_to(replied), replied={replied.id: replied}).reply_to

    assert quote is not None
    assert (quote.id, quote.seq, quote.author_id) == (replied.id, 4, replied.author_id)
    assert (quote.body, quote.deleted) == ("x" * REPLY_PREVIEW_CHARS, False)


def test_the_quote_of_a_deleted_message_has_no_text():
    replied = saved_message(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    replied.body, replied.deleted_at = "", datetime.now(UTC)  # as soft delete leaves it

    quote = to_message_response(reply_to(replied), replied={replied.id: replied}).reply_to

    assert quote is not None
    assert (quote.body, quote.deleted, quote.author_id) == ("", True, replied.author_id)


def test_a_deleted_reply_has_no_quote():
    replied = saved_message(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    reply = reply_to(replied)
    reply.deleted_at = datetime.now(UTC)

    assert to_message_response(reply, replied={replied.id: replied}).reply_to is None
