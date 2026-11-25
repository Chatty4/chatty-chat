import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import AwareDatetime

from app.api.deps import CurrentUser, SearchServiceDep
from app.api.rate_limits import LIMIT_SEARCH, LIMIT_SEARCH_ATTACHMENTS
from app.schemas.search import AttachmentSearchResponse, SearchResponse

router = APIRouter()


@router.get("/search", response_model=SearchResponse, dependencies=[LIMIT_SEARCH])
async def search(
    user: CurrentUser,
    service: SearchServiceDep,
    team_id: uuid.UUID,
    q: Annotated[str, Query(min_length=2, max_length=200)],
    channel_id: Annotated[uuid.UUID | None, Query(alias="in")] = None,
    author_id: Annotated[uuid.UUID | None, Query(alias="from")] = None,
    before: AwareDatetime | None = None,
    after: AwareDatetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> SearchResponse:
    return await service.search(
        user, team_id, q, channel_id, author_id, before, after, cursor, limit
    )


@router.get(
    "/search/attachments",
    response_model=AttachmentSearchResponse,
    dependencies=[LIMIT_SEARCH_ATTACHMENTS],
)
async def search_attachments(
    user: CurrentUser,
    service: SearchServiceDep,
    team_id: uuid.UUID,
    q: Annotated[str, Query(min_length=2, max_length=200)],
    channel_id: Annotated[uuid.UUID | None, Query(alias="in")] = None,
    author_id: Annotated[uuid.UUID | None, Query(alias="from")] = None,
    before: AwareDatetime | None = None,
    after: AwareDatetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> AttachmentSearchResponse:
    return await service.search_attachments(
        user, team_id, q, channel_id, author_id, before, after, cursor, limit
    )
