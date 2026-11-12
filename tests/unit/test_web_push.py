import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from pywebpush import WebPushException

from app.clients import web_push
from app.clients.web_push import WebPushClient
from app.models import PushSubscription

ENDPOINT = "https://push.example.test/secret-endpoint"


def subscription() -> PushSubscription:
    return PushSubscription(
        id=uuid.uuid4(), user_id=uuid.uuid4(), endpoint=ENDPOINT, p256dh="key", auth="secret"
    )


def push_service(monkeypatch, *, error: Exception | None = None) -> list[dict[str, Any]]:
    """Replaces pywebpush's webpush; returns the calls it got."""
    calls: list[dict[str, Any]] = []

    def fake_webpush(**kwargs: Any) -> None:
        calls.append(kwargs)
        if error is not None:
            raise error

    monkeypatch.setattr(web_push, "webpush", fake_webpush)
    return calls


def answered(status: int) -> WebPushException:
    return WebPushException("Push failed", response=SimpleNamespace(status_code=status))


async def test_sends_with_the_keys_vapid_and_a_ttl(monkeypatch):
    calls = push_service(monkeypatch)

    assert await WebPushClient("private", "mailto:dev@chatty.local").send(subscription(), "{}")

    [call] = calls
    assert call["subscription_info"] == {
        "endpoint": ENDPOINT,
        "keys": {"p256dh": "key", "auth": "secret"},
    }
    assert call["data"] == "{}"
    assert call["vapid_private_key"] == "private"
    assert call["vapid_claims"] == {"sub": "mailto:dev@chatty.local"}
    assert call["ttl"] > 0


async def test_each_send_gets_its_own_claims(monkeypatch):
    calls = push_service(monkeypatch)
    client = WebPushClient("private", "mailto:dev@chatty.local")

    await client.send(subscription(), "{}")
    await client.send(subscription(), "{}")

    assert calls[0]["vapid_claims"] is not calls[1]["vapid_claims"]


@pytest.mark.parametrize("status", [404, 410])
async def test_a_gone_subscription_returns_false(monkeypatch, status):
    push_service(monkeypatch, error=answered(status))

    assert not await WebPushClient("private", "mailto:x").send(subscription(), "{}")


@pytest.mark.parametrize(
    "error",
    [answered(500), WebPushException("no response"), requests.ConnectionError("down")],
)
async def test_other_failures_count_as_sent_and_never_log_the_endpoint(monkeypatch, caplog, error):
    push_service(monkeypatch, error=error)

    assert await WebPushClient("private", "mailto:x").send(subscription(), "{}")

    assert caplog.records
    assert ENDPOINT not in caplog.text
