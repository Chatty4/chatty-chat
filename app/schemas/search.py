from pydantic import BaseModel

from app.schemas.messages import AttachmentResponse, MessageResponse


class SearchResult(BaseModel):
    message: MessageResponse
    highlight: str


class SearchResponse(BaseModel):
    results: list[SearchResult]
    next_cursor: str | None


class AttachmentSearchResult(BaseModel):
    attachment: AttachmentResponse
    message: MessageResponse
    highlight: str


class AttachmentSearchResponse(BaseModel):
    results: list[AttachmentSearchResult]
    next_cursor: str | None
