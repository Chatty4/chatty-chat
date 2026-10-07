from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import get_health_service
from app.schemas.health import CheckStatus, DatabaseHealthResponse, HealthResponse, HealthStatus
from app.services.health_service import HealthService

router = APIRouter(prefix="/health", tags=["health"])

HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]


@router.get("", response_model=HealthResponse)
async def health(response: Response, service: HealthServiceDep) -> HealthResponse:
    """Check the database and Redis. Returns 503 if one of them is down."""
    result = await service.check()
    if result.status is not HealthStatus.OK:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result


@router.get("/db", response_model=DatabaseHealthResponse)
async def health_db(response: Response, service: HealthServiceDep) -> DatabaseHealthResponse:
    """Check only the database and show which one the app is connected to. Returns 503 if down."""
    result = await service.check_database()
    if result.status is CheckStatus.ERROR:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
