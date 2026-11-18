"""The signed attachment URLs against the real MinIO, with chatty-chat's own key (D-02, D-16).

That key can only read, so these tests can't put an object first. A URL for a missing object
still shows whether MinIO accepted the request: 404 NoSuchKey means the signature (with its
response headers) and the policy passed, 403 means they didn't.
"""

import asyncio
import uuid
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from app.clients import storage
from app.core.config import get_settings

pytestmark = pytest.mark.integration

# In the chat container the public address (127.0.0.1:9000) is the container itself; MinIO is
# the compose service. In CI the public address is MinIO. The URL is signed for the public host
# either way, and MinIO checks the signature against Host, so the request keeps that Host.
COMPOSE_MINIO = "http://minio:9000"


@pytest.fixture(autouse=True)
def fresh_client():
    get_settings.cache_clear()
    storage.public_client.cache_clear()
    yield
    storage.public_client.cache_clear()


async def fetch(url: str) -> httpx.Response:
    """GET a presigned URL like the browser does."""
    signed = urlsplit(url)
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            return await client.get(url)
        except httpx.ConnectError:
            internal = urlsplit(COMPOSE_MINIO)
            moved = signed._replace(scheme=internal.scheme, netloc=internal.netloc).geturl()
            return await client.get(moved, headers={"Host": signed.netloc})


def error_code(response: httpx.Response) -> str:
    """The <Code> of MinIO's XML error."""
    return response.text.split("<Code>", 1)[1].split("</Code>", 1)[0]


def missing_key() -> str:
    return f"attachments/{uuid.uuid4()}/{uuid.uuid4()}"


async def test_minio_accepts_the_signed_url():
    url, _ = storage.presign_attachment(missing_key(), "raport ăîș.pdf", "application/pdf")

    response = await fetch(url)

    # not 403: the signature, the signed response headers and the read policy all passed
    assert response.status_code == 404
    assert error_code(response) == "NoSuchKey"


async def test_a_changed_response_header_breaks_the_signature():
    url, _ = storage.presign_attachment(missing_key(), "report.pdf", "application/pdf")
    parts = urlsplit(url)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    query["response-content-disposition"] = "inline"  # try to open it as a page instead
    changed = parts._replace(query=urlencode(query)).geturl()

    response = await fetch(changed)

    assert response.status_code == 403
    assert error_code(response) == "SignatureDoesNotMatch"


async def test_an_expired_url_is_refused():
    url = storage.public_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": get_settings().MINIO_BUCKET, "Key": missing_key()},
        ExpiresIn=1,
    )
    await asyncio.sleep(2)

    response = await fetch(url)

    assert response.status_code == 403
    assert error_code(response) == "AccessDenied"  # "Request has expired"
