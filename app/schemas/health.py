from enum import StrEnum

from pydantic import BaseModel


class CheckStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class HealthStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"


class HealthResponse(BaseModel):
    status: HealthStatus
    db: CheckStatus
    redis: CheckStatus


class DatabaseHealthResponse(BaseModel):
    status: CheckStatus
    database: str | None = None
    error: str | None = None
