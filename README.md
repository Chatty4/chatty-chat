# chatty-chat

FastAPI service for Chatty: messages, sync, reactions, pins, read state, attachments and the WebSocket gateway.

- Public API: `/api/chat/v1` (contract: `chatty-infra/docs/api-chat.md`)
- Events on the WebSocket and Kafka: `chatty-infra/docs/events.md`
- Calls chatty-core's internal API (contract: `chatty-infra/docs/api-internal.md`) and listens to its Redis events (`chatty-infra/docs/core-events.md`)
- Health checks: `GET /health` (database and Redis) and `GET /health/db` (database only)

Stack: Python 3.14, FastAPI, SQLAlchemy 2.1 (async, asyncpg), Alembic, Redis, pydantic-settings, Postgres 17, uvicorn.

## Two ways to run it

| | Docker (recommended) | Local |
|---|---|---|
| You need | Docker | Docker (for the infra) and Python 3.14 |
| The app runs | in the `chat` container | on your PC, in `.venv` |
| Run commands from | `chatty-infra` | `chatty-chat` |
| Database and Redis | `chat-db:5432`, `redis:6379` (set by compose) | `127.0.0.1` + the ports from `chatty-infra/.env` |
| Code changes | applied right away (code is mounted, `--reload`) | applied right away (`--reload`) |

Use Docker if Windows blocks compiled packages (`An Application Control policy has blocked this file`).

## 1. Setup

**Both:** create your `.env`.

```bash
cp .env.example .env
```

**Docker:** nothing else. `.env` is optional; compose sets `DATABASE_URL`, `REDIS_URL` and `CORE_SERVICE_TOKEN` itself.

**Local:** create the virtual environment and point `.env` at the infra.

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1          # PowerShell; Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt
```

In `.env`, use `127.0.0.1` (not `localhost`) and the ports from `chatty-infra/.env`, for example
`DATABASE_URL=postgresql+asyncpg://chat_user:chat_pass@127.0.0.1:5433/chat_db`.
Docker publishes the infra ports on IPv4 only.

## 2. Start

**Docker** (from `chatty-infra`): starts chatty-chat with `chat-db` and `redis`.

```bash
docker compose up -d --build chat
```

**Local:** start the infra, then the app (from `chatty-chat`).

```bash
docker compose -f ../chatty-infra/docker-compose.yml up -d
uvicorn app.main:app --reload --port 8001
```

## 3. Check

| URL | Expected |
|---|---|
| http://127.0.0.1:8001/health | `{"status": "ok", "db": "ok", "redis": "ok"}` |
| http://127.0.0.1:8001/health/db | `{"status": "ok", "database": "chat_db", "error": null}` |
| http://127.0.0.1:8001/docs | API docs |

A `503` means the database or Redis is not reachable: check that the infra containers are running and the URLs
in `.env`.

## Commands

| Task | Docker (from `chatty-infra`) | Local (from `chatty-chat`) |
|---|---|---|
| Logs | `docker compose logs -f chat` | in the uvicorn terminal |
| Tests | `docker compose exec chat pytest` | `pytest` |
| Tests without the real DB and Redis | `docker compose exec chat pytest -m "not integration"` | `pytest -m "not integration"` |
| Lint | `docker compose exec chat ruff check .` | `ruff check .` |
| Format | `docker compose exec chat ruff format .` | `ruff format .` |
| New migration | `docker compose exec chat alembic revision --autogenerate -m "..."` | `alembic revision --autogenerate -m "..."` |
| Apply migrations | `docker compose exec chat alembic upgrade head` | `alembic upgrade head` |
| Stop | `docker compose stop chat` | `Ctrl+C` |

**Docker:** rebuild with `docker compose up -d --build chat` only when `requirements.txt` or the `Dockerfile`
changes. Code changes don't need a rebuild.

## Migrations

`alembic/env.py` reads the database URL from the settings, so `alembic.ini` has no URL. New models must be imported
in `app/models/__init__.py`, or autogenerate won't see them. Always read a generated migration before you apply it.

## Tests

API and unit tests replace the database and Redis with fakes, so they run anywhere. Tests marked `integration`
use the real Postgres and Redis containers.

## Docker image

The image on its own, without compose and without `--reload`:

```bash
docker build -t chatty-chat .
docker run --rm -p 8001:8001 --network chatty_default \
  -e DATABASE_URL=postgresql+asyncpg://chat_user:chat_pass@chat-db:5432/chat_db \
  -e REDIS_URL=redis://redis:6379/0 \
  -e CORE_SERVICE_TOKEN=change-me \
  chatty-chat
```

The container joins the infra network (`chatty_default`), so it reaches Postgres as `chat-db` and Redis as `redis`.

## Contributing

Read `CLAUDE.md` for the architecture, async rules and conventions. One Jira ticket per branch and PR
(`CHAT-<n>/<short-name>`), and `ruff check`, `ruff format --check` and `pytest` must pass before you push.
