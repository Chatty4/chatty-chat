import base64
import binascii
import hashlib
import html
import json
import re
import uuid
from datetime import datetime

from app.core.codes import ErrorCode
from app.core.exceptions import BadRequest
from app.repositories import MessageRepository, PinRepository, ReactionRepository
from app.repositories.messages import HIGHLIGHT_START, HIGHLIGHT_STOP
from app.schemas.core import Memberships
from app.schemas.search import SearchResponse, SearchResult
from app.services.access_service import AccessService
from app.services.message_service import to_message_response

Cursor = tuple[float, datetime, uuid.UUID]

MENTION = re.compile(r"<@[0-9a-fA-F\x02\x03-]+>")


def to_highlight(raw: str) -> str:
    """Escape the user's text first, then turn our markers into <mark>."""
    # A word can match a piece of a mention's UUID; never mark inside a mention.
    raw = MENTION.sub(
        lambda m: m.group().replace(HIGHLIGHT_START, "").replace(HIGHLIGHT_STOP, ""), raw
    )
    return html.escape(raw).replace(HIGHLIGHT_START, "<mark>").replace(HIGHLIGHT_STOP, "</mark>")


def _filters_hash(**filters: object) -> str:
    """The same filters on every page; a cursor from another search is invalid_cursor."""
    text = json.dumps(filters, default=str, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def encode_cursor(key: Cursor, filters: str) -> str:
    """Opaque and URL-safe: the web app sends it back as it is (api-core.md, pagination)."""
    rank, created_at, message_id = key
    data = {"k": [rank, created_at.isoformat(), str(message_id)], "f": filters}
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode()


def decode_cursor(cursor: str, filters: str) -> Cursor:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor))
        rank, created_at, message_id = data["k"]
        if data["f"] != filters:
            raise ValueError("cursor made for another search")
        return float(rank), datetime.fromisoformat(created_at), uuid.UUID(message_id)
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise BadRequest(ErrorCode.INVALID_CURSOR) from exc


def readable_channel_ids(user: Memberships, team_id: uuid.UUID) -> list[uuid.UUID]:
    """Channels the user is in, plus the team's public channels (api-chat.md, access)."""
    ids = {c.channel_id for c in user.channels if c.team_id == team_id}
    for team in user.teams:
        if team.team_id == team_id:
            ids.update(team.public_channel_ids)
    return list(ids)


class SearchService:
    def __init__(
        self,
        messages: MessageRepository,
        reactions: ReactionRepository,
        pins: PinRepository,
        access: AccessService,
    ) -> None:
        self._messages = messages
        self._reactions = reactions
        self._pins = pins
        self._access = access

    async def search(
        self,
        user: Memberships,
        team_id: uuid.UUID,
        q: str,
        channel_id: uuid.UUID | None,
        author_id: uuid.UUID | None,
        before: datetime | None,
        after: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> SearchResponse:
        """Full-text search in the channels of one team the user can read. Best match first."""
        self._access.check_in_team(user, team_id)

        filters = _filters_hash(
            team_id=team_id,
            q=q,
            channel_id=channel_id,
            author_id=author_id,
            before=before,
            after=after,
        )
        key = decode_cursor(cursor, filters) if cursor else None

        channel_ids = readable_channel_ids(user, team_id)
        if channel_id is not None:
            # a channel the user can't read gives no results, not an error that says it exists
            channel_ids = [channel_id] if channel_id in channel_ids else []

        # one extra row tells whether a next page exists
        rows = await self._messages.search_messages(
            channel_ids=channel_ids,
            query=q,
            author_id=author_id,
            before=before,
            after=after,
            cursor=key,
            limit=limit + 1,
        )
        page = rows[:limit]

        message_ids = [row.Message.id for row in page]
        reactions = await self._reactions.list_reactions(message_ids, user.user_id)
        pinned = await self._pins.pinned_message_ids(message_ids)

        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = encode_cursor(
                (last.rank, last.Message.created_at, last.Message.id), filters
            )

        return SearchResponse(
            results=[
                SearchResult(
                    message=to_message_response(
                        row.Message, reactions.get(row.Message.id), row.Message.id in pinned
                    ),
                    highlight=to_highlight(row.highlight),
                )
                for row in page
            ],
            next_cursor=next_cursor,
        )
