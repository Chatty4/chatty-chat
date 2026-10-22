import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import Unauthorized
from app.core.security import decode_access_token


def test_valid_token_returns_the_user_id_and_exp(make_token):
    user_id = uuid.uuid4()
    exp = datetime.now(UTC) + timedelta(minutes=5)

    token = decode_access_token(make_token(sub=str(user_id), exp=exp))

    assert token.user_id == user_id
    assert token.expires_at == int(exp.timestamp())


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": datetime.now(UTC) - timedelta(minutes=1)},
        {"sub": None},
        {"exp": None},
        {"sub": "not-a-uuid"},
    ],
    ids=["expired", "no sub", "no exp", "sub not a uuid"],
)
def test_bad_claims_are_rejected(make_token, claims):
    with pytest.raises(Unauthorized):
        decode_access_token(make_token(**claims))


def test_token_signed_with_another_key_is_rejected(make_token, other_private_key):
    with pytest.raises(Unauthorized):
        decode_access_token(make_token(key=other_private_key))


def test_garbage_is_rejected(make_token):
    with pytest.raises(Unauthorized):
        decode_access_token("not-a-jwt")
