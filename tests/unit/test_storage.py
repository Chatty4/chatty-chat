from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from app.clients import storage
from app.core.config import get_settings


@pytest.fixture(autouse=True)
def minio_settings(monkeypatch):
    """A public address and a key of its own, and no cached client or settings from elsewhere."""
    monkeypatch.setenv("MINIO_INTERNAL_URL", "http://minio:9000")
    monkeypatch.setenv("MINIO_PUBLIC_URL", "http://files.example.com")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "chatty-chat")
    monkeypatch.setenv("MINIO_SECRET_KEY", "test-secret")
    monkeypatch.setenv("MINIO_BUCKET", "chatty")
    get_settings.cache_clear()
    storage.public_client.cache_clear()
    storage.internal_client.cache_clear()
    yield
    get_settings.cache_clear()
    storage.public_client.cache_clear()
    storage.internal_client.cache_clear()


def test_url_is_signed_for_the_public_address():
    url, _ = storage.presign_attachment("attachments/t1/f6", "report.pdf", "application/pdf")

    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.netloc == "files.example.com"  # the browser's address (D-16)
    assert parts.path == "/chatty/attachments/t1/f6"  # path style
    assert query["X-Amz-Expires"] == [str(storage.DOWNLOAD_URL_SECONDS)]
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Credential"][0].startswith("chatty-chat/")


def test_url_has_the_files_type_and_downloads_it_under_its_name():
    url, _ = storage.presign_attachment("attachments/t1/f6", "report.pdf", "application/pdf")

    query = parse_qs(urlsplit(url).query)
    assert query["response-content-type"] == ["application/pdf"]
    assert query["response-content-disposition"] == [
        "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"
    ]


def test_expires_at_is_five_minutes_ahead():
    before = datetime.now(UTC)

    _, expires_at = storage.presign_attachment("attachments/t1/f6", "a.txt", "text/plain")

    assert (
        before + timedelta(seconds=300) <= expires_at <= datetime.now(UTC) + timedelta(seconds=300)
    )


def test_the_client_is_made_once():
    assert storage.public_client() is storage.public_client()


@pytest.mark.parametrize(
    ("filename", "fallback"),
    [
        ("raport ăîș.pdf", "raport ___.pdf"),
        ('say "hi".txt', "say _hi_.txt"),
        ("a;b.txt", "a_b.txt"),
        ("back\\slash.txt", "back_slash.txt"),
        ("line\nbreak.txt", "line_break.txt"),
        ("日本.png", "__.png"),
    ],
)
def test_names_that_could_break_the_header(filename, fallback):
    header = storage.content_disposition(filename)

    plain, exact = header.removeprefix("attachment; ").split("; ")
    assert plain == f'filename="{fallback}"'
    assert unquote(exact.removeprefix("filename*=UTF-8''")) == filename  # the exact name
    assert all(char.isascii() and char.isprintable() for char in header)


def test_an_empty_name_still_has_a_fallback():
    assert storage.content_disposition("") == "attachment; filename=\"file\"; filename*=UTF-8''"


def test_downloads_use_the_internal_address():
    assert storage.internal_client().meta.endpoint_url == "http://minio:9000"
