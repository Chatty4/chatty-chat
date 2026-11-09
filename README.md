# chatty-chat

FastAPI service for Chatty: messages, history and sync, reactions, pins, read state, presence and the WebSocket
gateway today; attachments next.
Python 3.14 · FastAPI · SQLAlchemy 2.1 (async) · Postgres 17 · Redis · uvicorn.

Contracts live in `chatty-infra/docs`: `api-chat.md` (public API and WebSocket), `events.md` (channel events),
`api-internal.md` (the chatty-core calls), `core-events.md` (chatty-core's Redis events).

## Quick start (about 10 min)

Run from `chatty-chat` in PowerShell:

1. Start the infra and chatty-core, without the `chat` container, which would take port 8001:
   ```bash
   docker compose -f ../chatty-infra/docker-compose.yml up -d chat-db redis core
   ```
2. Install:
   ```bash
   python -m venv .venv; .venv\Scripts\Activate.ps1; pip install -r requirements.txt
   ```
3. Create `.env`, then set `CORE_SERVICE_TOKEN` to the value in chatty-core's `.env`, and the port in
   `DATABASE_URL` to `CHAT_DB_PORT` from `chatty-infra/.env`:
   ```bash
   cp .env.example .env
   ```
4. Get chatty-core's public key with chatty-infra's setup (needs Docker; safe to run again):
   ```bash
   ..\chatty-infra\setup.bat
   ```
   Its "JWT keys" step copies `jwt_public.pem` to `keys/`. chatty-chat only checks tokens; it never signs them.
5. Create the tables:
   ```bash
   alembic upgrade head
   ```
6. Run:
   ```bash
   uvicorn app.main:app --reload --port 8001 --ws-max-size 4096
   ```

Done when `curl http://127.0.0.1:8001/health` returns `{"status": "ok", "db": "ok", "redis": "ok", "tasks": "ok"}`.

## Endpoints that work

All under `/api/chat/v1`. 🔒 = needs `Authorization: Bearer <access_token>` from chatty-core.
Exact requests, responses, events and errors: `chatty-infra/docs/api-chat.md` and `events.md`.

**Messages** (all 🔒)

| Endpoint | What it does |
|---|---|
| `POST channels/{channel_id}/messages` | Send a message; a retry with the same `client_msg_id` returns the original (200) |
| `GET channels/{channel_id}/messages` | History, newest first, paged with `before_seq`; deleted messages keep their place |
| `PUT messages/{message_id}` | Edit my message |
| `DELETE messages/{message_id}` | Delete a message: the author, the channel owner or a team owner/admin; only the author in DMs |

**Reactions and pins** (all 🔒)

| Endpoint | What it does |
|---|---|
| `PUT` / `DELETE messages/{message_id}/reactions/{emoji}` | Add or remove my reaction; doing it twice changes nothing |
| `POST` / `DELETE channels/{channel_id}/pins/{message_id}` | Pin or unpin a message (at most 100 pins per channel) |
| `GET channels/{channel_id}/pins` | The channel's pins, newest first, with the messages |

**Read state, sync and presence** (all 🔒)

| Endpoint | What it does |
|---|---|
| `POST channels/{channel_id}/read` | Move my read position forward; my other tabs get the new counts |
| `GET unread?team_id=` | Unread messages and mentions in each of my channels of a team, each counted up to 100 |
| `POST sync` | Missed events of up to 500 channels after a reconnect |
| `GET channels/{channel_id}/events?after_seq=` | One channel's events after a seq; `409 gap_too_large` once they're cleaned up |
| `GET teams/{team_id}/presence` | Who is online in a team |

**WebSocket**: `GET ws?token=<access_token>` (the token goes in the URL, not a header)

| | |
|---|---|
| The client sends | `heartbeat` (every 20 s), `typing`, `watch` and `unwatch` |
| The server sends | `event`, `typing`, `presence`, `unread`, `channel.updated` and `error` |
| Closes with | `4001` token expired · `4002` no heartbeat for 60 s · `4003` user deactivated · `1001` too slow or the gateway lost Redis · `1011` chatty-core unavailable |

Not built yet: attachments (sending `file_ids` returns `400 invalid_file`), and Kafka (`channel_events` is already
the outbox, but nothing publishes it).

## Daily commands

| Task | Command |
|---|---|
| Run in Docker instead | `docker compose -f ../chatty-infra/docker-compose.yml up -d --build chat` |
| Logs in Docker | `docker compose -f ../chatty-infra/docker-compose.yml logs -f chat` |
| Test | `pytest` (`-m "not integration"` skips the tests that need the real Postgres and Redis) |
| Lint and format | `ruff check . && ruff format .` |
| New migration | `alembic revision --autogenerate -m "..."`, read it, then `alembic upgrade head` |
| Delete old events | `python -m app.jobs.clean_up_events` (older than 30 days) |

`clean_up_events` deletes each channel's events older than 30 days, so `/sync` can still tell that they're gone and
answer `gap_too_large`. It has no flags. In Docker, the `chat` container applies pending migrations when it starts,
then runs this job once a day.

## When something breaks

| You see | Fix |
|---|---|
| `Field required` for a setting at start | A variable is missing: compare `.env` with `.env.example` |
| `500 internal_error` on every request, `FileNotFoundError ... jwt_public.pem` in the log | The public key is missing: run `..\chatty-infra\setup.bat` once |
| `401` with a token that worked before | chatty-core's keys changed: run `..\chatty-infra\setup.bat` to copy the new public key, then log in again |
| `503 core_unavailable`, or the WebSocket closes with `1011` | chatty-core isn't running, or `CORE_SERVICE_TOKEN` differs from chatty-core's (the log says "chatty-core answered 401") |
| `/health` is `503` with `"db"` or `"redis": "error"` | Use `127.0.0.1` in `.env`, never `localhost` (IPv6 stall on Windows), and the ports from `chatty-infra/.env` |
| `/health` is `503` with `"tasks": "error"` | The gateway or the `core.events` listener crashed; the log says "... stopped". Restart chat |
| Port 8001 is already in use | The `chat` container is running: `docker compose -f ../chatty-infra/docker-compose.yml stop chat` |
| `An Application Control policy has blocked this file` | Windows blocks compiled packages: run in Docker instead |

## Rules

- Never commit `.env` or `keys/`.
- One Jira ticket per branch: `CHAT-<n>/<short-name>`.
- `ruff check`, `ruff format --check` and `pytest` pass before you push.
- New models are imported in `app/models/__init__.py`, or Alembic won't see them. CI runs `alembic check`, so models
  and migrations must match.
- Architecture, async rules and conventions: `CLAUDE.md`.
