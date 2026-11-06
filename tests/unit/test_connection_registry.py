import uuid

import pytest

from app.services.connection_registry import ConnectionRegistry


@pytest.fixture
def registry() -> ConnectionRegistry[object]:
    return ConnectionRegistry()


def test_first_join_returns_true_and_later_joins_false(registry):
    channel_id = uuid.uuid4()
    first, second = object(), object()

    assert registry.channels.join(first, channel_id) is True
    assert registry.channels.join(second, channel_id) is False
    assert registry.channels.members(channel_id) == {first, second}


def test_joining_the_same_channel_twice_keeps_one_entry(registry):
    channel_id = uuid.uuid4()
    socket = object()

    registry.channels.join(socket, channel_id)

    assert registry.channels.join(socket, channel_id) is False
    assert registry.channels.members(channel_id) == {socket}


def test_leave_returns_true_only_for_the_last_socket(registry):
    channel_id = uuid.uuid4()
    first, second = object(), object()
    registry.channels.join(first, channel_id)
    registry.channels.join(second, channel_id)

    assert registry.channels.leave(first, channel_id) is False
    assert registry.channels.leave(second, channel_id) is True
    assert registry.channels.members(channel_id) == set()
    assert registry.channels.ids() == []


def test_leave_of_a_socket_not_in_the_channel_returns_false(registry):
    channel_id = uuid.uuid4()
    member, stranger = object(), object()
    registry.channels.join(member, channel_id)

    assert registry.channels.leave(stranger, channel_id) is False
    assert registry.channels.leave(member, uuid.uuid4()) is False
    assert registry.channels.members(channel_id) == {member}


def test_leave_all_returns_only_the_channels_that_became_empty(registry):
    shared, alone = uuid.uuid4(), uuid.uuid4()
    socket, other = object(), object()
    registry.channels.join(socket, shared)
    registry.channels.join(socket, alone)
    registry.channels.join(other, shared)

    assert registry.channels.leave_all(socket) == [alone]
    assert registry.channels.members(shared) == {other}
    assert registry.channels.ids() == [shared]


def test_leave_all_of_an_unknown_socket_returns_nothing(registry):
    assert registry.channels.leave_all(object()) == []


def test_leave_all_skips_channels_the_socket_already_left(registry):
    left, still_in = uuid.uuid4(), uuid.uuid4()
    socket = object()
    registry.channels.join(socket, left)
    registry.channels.join(socket, still_in)
    registry.channels.leave(socket, left)

    assert registry.channels.leave_all(socket) == [still_in]
    assert registry.channels.ids() == []


def test_in_channel_of_an_unknown_channel_is_empty(registry):
    assert registry.channels.members(uuid.uuid4()) == set()


def test_channels_and_users_are_tracked_separately(registry):
    user_id, channel_id = uuid.uuid4(), uuid.uuid4()
    socket = object()
    registry.users.join(socket, user_id)
    registry.channels.join(socket, channel_id)

    registry.users.leave_all(socket)

    # users.leave_all() only touches users; the gateway calls channels.leave_all() too
    assert registry.users.members(user_id) == set()
    assert registry.channels.members(channel_id) == {socket}


def test_of_lists_the_groups_of_a_socket(registry):
    socket, first, second = object(), uuid.uuid4(), uuid.uuid4()
    registry.teams.join(socket, first)
    registry.teams.join(socket, second)
    registry.teams.leave(socket, first)

    assert registry.teams.of(socket) == [second]
    assert registry.teams.of(object()) == []


def test_channels_and_teams_are_separate(registry):
    socket, group_id = object(), uuid.uuid4()
    registry.channels.join(socket, group_id)

    assert registry.teams.join(socket, group_id) is True  # the first socket of the team
    assert registry.channels.leave_all(socket) == [group_id]
    assert registry.teams.members(group_id) == {socket}


def test_users_join_and_leave_all_say_when_a_user_has_the_first_and_last_socket(registry):
    user_id = uuid.uuid4()
    first, second = object(), object()

    assert registry.users.join(first, user_id) is True
    assert registry.users.join(second, user_id) is False
    assert registry.users.ids() == [user_id]
    assert registry.users.leave_all(first) == []
    assert registry.users.leave_all(second) == [user_id]
    assert registry.users.ids() == []


def test_leave_all_of_an_unknown_user_socket_is_not_the_last(registry):
    user_id = uuid.uuid4()
    registry.users.join(object(), user_id)

    assert registry.users.leave_all(object()) == []
    assert registry.users.ids() == [user_id]


def test_all_sockets_lists_every_joined_socket_until_it_leaves_all(registry):
    first, second = object(), object()
    registry.users.join(first, uuid.uuid4())
    registry.users.join(second, uuid.uuid4())

    registry.users.leave_all(first)

    assert registry.users.all_sockets() == [second]
