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

    CORE_SERVICE_TOKEN: SecretStr
    JWT_ALGORITHM: Literal["RS256"] = "RS256"
    JWT_PUBLIC_KEY_PATH: Path = Path("keys/jwt_public.pem")

    DEBUG: bool = False

    def read_jwt_public_key(self) -> str:
        return self.JWT_PUBLIC_KEY_PATH.read_text(encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
