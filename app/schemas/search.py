from pydantic import BaseModel

from app.schemas.messages import MessageResponse


class SearchResult(BaseModel):
    message: MessageResponse
    highlight: str


class SearchResponse(BaseModel):
    results: list[SearchResult]
    next_cursor: str | None
