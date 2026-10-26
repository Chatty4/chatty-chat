import uuid

from app.services.message_service import extract_mentions

ANA, BOB, OUTSIDER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
MEMBERS = {ANA, BOB}


def test_keeps_members_once_in_order():
    assert extract_mentions(f"<@{BOB}> and <@{ANA}>, again <@{BOB}>", MEMBERS) == [BOB, ANA]


def test_drops_users_who_are_not_members():
    assert extract_mentions(f"<@{OUTSIDER}> <@{ANA}>", MEMBERS) == [ANA]


def test_ignores_text_that_is_not_a_mention():
    body = f"@{ANA} <{ANA}> <@{'-' * 36}> <@not-a-uuid>"
    assert extract_mentions(body, MEMBERS) == []
