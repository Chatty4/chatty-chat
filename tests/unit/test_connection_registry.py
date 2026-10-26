import uuid

import pytest

from app.services.connection_registry import ConnectionRegistry


@pytest.fixture
def registry() -> ConnectionRegistry[object]:
    return ConnectionRegistry()


def test_first_join_returns_true_and_later_joins_false(registry):
    channel_id = uuid.uuid4()
    first, second = object(), object()

    assert registry.join(first, channel_id) is True
    assert registry.join(second, channel_id) is False
    assert registry.in_channel(channel_id) == {first, second}


def test_joining_the_same_channel_twice_keeps_one_entry(registry):
    channel_id = uuid.uuid4()
    socket = object()

    registry.join(socket, channel_id)

    assert registry.join(socket, channel_id) is False
    assert registry.in_channel(channel_id) == {socket}


def test_leave_returns_true_only_for_the_last_socket(registry):
    channel_id = uuid.uuid4()
    first, second = object(), object()
    registry.join(first, channel_id)
    registry.join(second, channel_id)

    assert registry.leave(first, channel_id) is False
    assert registry.leave(second, channel_id) is True
    assert registry.in_channel(channel_id) == set()
    assert registry.channel_ids() == []


def test_leave_of_a_socket_not_in_the_channel_returns_false(registry):
    channel_id = uuid.uuid4()
    member, stranger = object(), object()
    registry.join(member, channel_id)

    assert registry.leave(stranger, channel_id) is False
    assert registry.leave(member, uuid.uuid4()) is False
    assert registry.in_channel(channel_id) == {member}


def test_leave_all_returns_only_the_channels_that_became_empty(registry):
    shared, alone = uuid.uuid4(), uuid.uuid4()
    socket, other = object(), object()
    registry.join(socket, shared)
    registry.join(socket, alone)
    registry.join(other, shared)

    assert registry.leave_all(socket) == [alone]
    assert registry.in_channel(shared) == {other}
    assert registry.channel_ids() == [shared]


def test_leave_all_of_an_unknown_socket_returns_nothing(registry):
    assert registry.leave_all(object()) == []


def test_leave_all_skips_channels_the_socket_already_left(registry):
    left, still_in = uuid.uuid4(), uuid.uuid4()
    socket = object()
    registry.join(socket, left)
    registry.join(socket, still_in)
    registry.leave(socket, left)

    assert registry.leave_all(socket) == [still_in]
    assert registry.channel_ids() == []


def test_in_channel_of_an_unknown_channel_is_empty(registry):
    assert registry.in_channel(uuid.uuid4()) == set()


def test_channels_and_users_are_tracked_separately(registry):
    user_id, channel_id = uuid.uuid4(), uuid.uuid4()
    socket = object()
    registry.add(user_id, socket)
    registry.join(socket, channel_id)

    registry.remove(user_id, socket)

    # remove() is only the user index; the gateway calls leave_all() for the channels
    assert registry.sockets(user_id) == set()
    assert registry.in_channel(channel_id) == {socket}
