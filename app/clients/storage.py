from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING
from urllib.parse import quote

import boto3
from botocore.config import Config

from app.core.config import get_settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

DOWNLOAD_URL_SECONDS = 300
_REGION = "us-east-1"


@lru_cache(maxsize=1)
def _client(endpoint_url: str) -> S3Client:
    settings = get_settings()
    # A session of its own: boto3's shared default session isn't safe to create from two threads.
    return boto3.session.Session().client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=settings.MINIO_ACCESS_KEY,
        aws_secret_access_key=settings.MINIO_SECRET_KEY.get_secret_value(),
        region_name=_REGION,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=30,
        ),
    )


@lru_cache(maxsize=1)
def public_client() -> S3Client:
    """Only signs URLs, for the address the browser reaches."""
    return _client(get_settings().MINIO_PUBLIC_URL)


@lru_cache(maxsize=1)
def internal_client() -> S3Client:
    """Downloads attachments for the labeler, at MinIO's address inside the network."""
    return _client(get_settings().MINIO_INTERNAL_URL)


def read_attachment(object_key: str) -> bytes:
    """The file's bytes. Blocking (boto3): call it through asyncio.to_thread."""
    response = internal_client().get_object(Bucket=get_settings().MINIO_BUCKET, Key=object_key)
    return response["Body"].read()


def presign_attachment(object_key: str, filename: str, mime: str) -> tuple[str, datetime]:
    """A GET URL that downloads the file under its name, and when it expires (D-16)."""
    expires_at = datetime.now(UTC) + timedelta(seconds=DOWNLOAD_URL_SECONDS)
    url = public_client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": get_settings().MINIO_BUCKET,
            "Key": object_key,
            "ResponseContentType": mime,
            "ResponseContentDisposition": content_disposition(filename),
        },
        ExpiresIn=DOWNLOAD_URL_SECONDS,
    )
    return url, expires_at


def content_disposition(filename: str) -> str:
    """attachment, with the name twice (RFC 6266): an ASCII fallback and the exact UTF-8 name, so
    quotes, semicolons and other letters can't break the header."""
    fallback = "".join(
        char if char.isascii() and char.isprintable() and char not in '"\\;' else "_"
        for char in filename
    )
    exact = quote(filename, safe="")
    return f"attachment; filename=\"{fallback or 'file'}\"; filename*=UTF-8''{exact}"
