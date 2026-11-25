from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str
    REDIS_URL: str = "redis://127.0.0.1:6379/0"

    CORE_INTERNAL_URL: str = "http://127.0.0.1:8000"
    KAFKA_BOOTSTRAP_SERVERS: str = "127.0.0.1:9092"
    KAFKA_TOPIC: str = "chat.events"

    # MinIO, with chatty-chat's read-only key (D-02, D-16): the public URL signs downloads for the
    # browser, the internal one is where the labeler reads attachments
    # (http://minio:9000 in Docker).
    MINIO_INTERNAL_URL: str = "http://127.0.0.1:9000"
    MINIO_PUBLIC_URL: str = "http://127.0.0.1:9000"
    MINIO_ACCESS_KEY: str = "chatty-chat"
    MINIO_SECRET_KEY: SecretStr = SecretStr("")
    MINIO_BUCKET: str = "chatty"

    # Web Push (D-13). Only the push worker uses them; it won't start without the key.
    VAPID_PRIVATE_KEY: SecretStr = SecretStr("")
    VAPID_SUBJECT: str = "mailto:dev@chatty.local"

    CORE_SERVICE_TOKEN: SecretStr
    JWT_ALGORITHM: Literal["RS256"] = "RS256"
    JWT_PUBLIC_KEY_PATH: Path = Path("keys/jwt_public.pem")

    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Logs every SQL statement with its parameters (message bodies too). Local debugging only.
    SQL_ECHO: bool = False

    def read_jwt_public_key(self) -> str:
        return self.JWT_PUBLIC_KEY_PATH.read_text(encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore : filled from .env
