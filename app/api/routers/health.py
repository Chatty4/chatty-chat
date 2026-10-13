from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import get_health_service
from app.schemas.health import HealthResponse, HealthStatus
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
