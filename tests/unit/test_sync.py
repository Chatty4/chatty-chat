import asyncio
import uuid

import pytest

from app.core.codes import ErrorCode
from app.core.exceptions import AppError, ServiceUnavailable
from app.schemas.core import CoreChannel, TeamMembership, TeamRole
from app.schemas.sync import ChannelSyncError, ChannelSyncResult
from app.services.sync_service import MAX_PARALLEL_CHECKS
from tests.fakes import FakeCore, SyncFakes, channel, memberships, saved_event

USER_ID, OTHER_ID = uuid.uuid4(), uuid.uuid4()
A, B = uuid.uuid4(), uuid.uuid4()  # public channels of the user's team
TEAM = TeamMembership(team_id=uuid.uuid4(), role=TeamRole.MEMBER, public_channel_ids=[A, B])
USER = memberships(USER_ID, teams=[TEAM])


def fakes_for(
    last_seqs: dict[uuid.UUID, int], *events: tuple[uuid.UUID, int], room: CoreChannel | None = None
) -> SyncFakes:
    return SyncFakes(FakeCore(channel=room), [saved_event(c, s) for c, s in events], last_seqs)


def result(response, channel_id: uuid.UUID) -> ChannelSyncResult:
    found = response.channels[channel_id]
    assert isinstance(found, ChannelSyncResult)
    return found


async def test_sync_returns_the_missed_events_in_order():
    fakes = fakes_for({A: 5}, *((A, seq) for seq in range(1, 6)))

    response = await fakes.service.sync(USER, {A: 2})

    found = result(response, A)
    assert [event.seq for event in found.events] == [3, 4, 5]
    assert (found.last_seq, found.gap_too_large) == (5, False)


async def test_sync_of_an_up_to_date_channel_is_empty():
    fakes = fakes_for({A: 5}, *((A, seq) for seq in range(1, 6)))

    found = result(await fakes.service.sync(USER, {A: 5}), A)

    assert (found.events, found.gap_too_large) == ([], False)


async def test_more_than_500_missed_is_gap_too_large():
    fakes = fakes_for({A: 600})

    found = result(await fakes.service.sync(USER, {A: 99}), A)

    assert (found.events, found.last_seq, found.gap_too_large) == ([], 600, True)


@pytest.mark.parametrize("stored", [[4, 5], []], ids=["some cleaned up", "all cleaned up"])
async def test_cleaned_up_events_are_gap_too_large(stored):
    fakes = fakes_for({A: 5}, *((A, seq) for seq in stored))

    found = result(await fakes.service.sync(USER, {A: 1}), A)

    assert (found.events, found.gap_too_large) == ([], True)


@pytest.mark.parametrize(
    ("room", "code"),
    [
        (channel(OTHER_ID, kind="private"), ErrorCode.CANNOT_READ_CHANNEL),
        (None, ErrorCode.CHANNEL_NOT_FOUND),
    ],
    ids=["private, not a member", "no channel"],
)
async def test_a_channel_the_user_cannot_read_gets_its_own_error(room, code):
    other = uuid.uuid4()  # not in the user's team, so chatty-core is asked
    fakes = fakes_for({A: 2}, (A, 1), (A, 2), room=room)

    response = await fakes.service.sync(USER, {A: 1, other: 0})

    assert response.channels[other] == ChannelSyncError(error=code)
    assert [event.seq for event in result(response, A).events] == [2]


class _BarrierCore(FakeCore):
    """get_channel returns only once `parties` calls are waiting, so sequential checks hang."""

    def __init__(self, parties: int) -> None:
        super().__init__()
        self._barrier = asyncio.Barrier(parties)

    async def get_channel(self, channel_id: uuid.UUID) -> CoreChannel | None:
        await self._barrier.wait()
        return None


async def test_unknown_channels_are_checked_at_the_same_time():
    unknown = [uuid.uuid4() for _ in range(3)]
    fakes = SyncFakes(_BarrierCore(len(unknown)), [saved_event(A, 1)], {A: 1})

    async with asyncio.timeout(1):
        response = await fakes.service.sync(USER, {A: 0, **dict.fromkeys(unknown, 0)})

    for channel_id in unknown:
        assert response.channels[channel_id] == ChannelSyncError(error=ErrorCode.CHANNEL_NOT_FOUND)
    assert [event.seq for event in result(response, A).events] == [1]


class _CountingCore(FakeCore):
    """get_channel takes a moment, so overlapping calls really overlap; `most` is the peak."""

    def __init__(self) -> None:
        super().__init__()
        self.running = 0
        self.most = 0

    async def get_channel(self, channel_id: uuid.UUID) -> CoreChannel | None:
        self.running += 1
        self.most = max(self.most, self.running)
        await asyncio.sleep(0.01)
        self.running -= 1
        return None


async def test_at_most_20_channels_are_checked_at_once():
    core = _CountingCore()
    unknown = [uuid.uuid4() for _ in range(100)]
    fakes = SyncFakes(core, [], {})

    response = await fakes.service.sync(USER, dict.fromkeys(unknown, 0))

    assert core.most == MAX_PARALLEL_CHECKS == 20
    assert all(
        response.channels[channel_id] == ChannelSyncError(error=ErrorCode.CHANNEL_NOT_FOUND)
        for channel_id in unknown
    )


async def test_core_unavailable_fails_the_whole_sync():
    fakes = SyncFakes(FakeCore(error=ServiceUnavailable()), [], {A: 0})

    with pytest.raises(ServiceUnavailable):
        await fakes.service.sync(USER, {A: 0, uuid.uuid4(): 0})


async def test_events_page_with_has_more():
    fakes = fakes_for({A: 5}, *((A, seq) for seq in range(1, 6)))

    first = await fakes.service.list_channel_events(USER, A, 1, 2)
    second = await fakes.service.list_channel_events(USER, A, first.events[-1].seq, 3)

    assert ([e.seq for e in first.events], first.has_more) == ([2, 3], True)
    assert ([e.seq for e in second.events], second.has_more) == ([4, 5], False)
    assert first.last_seq == second.last_seq == 5


async def test_cleaned_up_events_are_409_gap_too_large():
    fakes = fakes_for({A: 5}, (A, 4), (A, 5))

    with pytest.raises(AppError) as error:
        await fakes.service.list_channel_events(USER, A, 1, 500)

    assert (error.value.status_code, error.value.code) == (409, ErrorCode.GAP_TOO_LARGE)


async def test_events_of_a_channel_the_user_cannot_read_is_403_before_any_query():
    fakes = fakes_for({}, room=channel(OTHER_ID, kind="private"))

    with pytest.raises(AppError) as error:
        await fakes.service.list_channel_events(USER, uuid.uuid4(), 0, 500)

    assert (error.value.status_code, error.value.code) == (403, ErrorCode.CANNOT_READ_CHANNEL)
    assert fakes.log == []  # no transaction was opened
