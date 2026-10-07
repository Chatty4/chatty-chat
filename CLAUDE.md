# chatty-chat

FastAPI service for Chatty: messages, sync, reactions, pins, read state, attachments and the WebSocket gateway.
Public API at `/api/chat/v1`, health at `/health` and `/health/db`.

Stack: Python 3.14, FastAPI, SQLAlchemy 2.1 (async, asyncpg), Alembic, Redis (`redis.asyncio`), pydantic-settings,
Postgres 17, uvicorn. Later: Kafka (`aiokafka`), MinIO (read-only, attachment downloads).

## Sources of truth

Contracts live in `../chatty-infra/docs`, not in this repo:
- `api-chat.md`: public endpoints, error body, pagination, WebSocket
- `events.md`: channel events we produce (WebSocket, `POST /sync`, Kafka) and the client rules for `seq`
- `api-internal.md`: chatty-core's `/internal` endpoints we call, with `X-Service-Token`
- `core-events.md`: chatty-core's Redis events we consume
- `decisions.md`: agreements with chatty-core (D-01 ... D-08)

Implement exactly what the contract says. If an API, event or decision has to change, update the doc in a
chatty-infra PR first and link it from the chatty-chat PR.

## Commands

Run everything in Docker, from `../chatty-infra` (the `chat` service mounts this repo and runs with `--reload`):

```bash
docker compose up -d --build chat                                   # Postgres, Redis, ... and chatty-chat
docker compose exec chat pytest                                     # tests
docker compose exec chat ruff check . && docker compose exec chat ruff format --check .
docker compose exec chat alembic revision --autogenerate -m "..."   # new migration
docker compose exec chat alembic upgrade head                       # apply migrations
```

Locally (from this repo, with `.venv` active) the same commands work without `docker compose exec chat`,
plus `uvicorn app.main:app --reload --port 8001` to run.

## Project layout

```
app/
  main.py             create_app(), lifespan (Redis client, engine.dispose()), routers, error handlers
  core/               shared, no FastAPI or HTTP here
    config.py         Settings (pydantic-settings, fields in UPPERCASE), get_settings()
    exceptions.py     app exceptions (NotFound, Forbidden, Conflict, ValidationFailed, ...)
    logging.py
    security.py       JWT check with chatty-core's public key (RS256)
  db/
    base.py           DeclarativeBase with the naming convention
    session.py        async engine, SessionLocal, get_session()
  models/             SQLAlchemy tables; __init__.py imports every model (Alembic needs it)
  schemas/            Pydantic request and response models (the contract shapes)
  repositories/       database queries only
  services/           business rules, transactions, events
  clients/            chatty-core internal API (httpx), Redis, later Kafka and MinIO
  api/
    deps.py           dependency wiring: session, Redis, services, current user
    error_handlers.py app exceptions -> HTTP status + contract error body
    router.py         api_router, mounted at /api/chat/v1
    routers/          one module per feature (health.py, messages.py, ...)
alembic/              env.py reads DATABASE_URL from the settings; versions/
tests/
  api/                endpoints, services replaced with app.dependency_overrides
  unit/               services with fake repositories and clients
  integration/        against the real Postgres and Redis (@pytest.mark.integration)
```

Start each layer as one module per feature. Turn it into a package when it grows past roughly 300 lines.

## Layers and dependency rule

Calls go one way only: `api/routers -> services -> repositories / clients -> models / db`.

- **routers**: read the request, call one service method, return a schema. No SQL, no business rules,
  no commits, no `try/except` for app exceptions (the error handlers do that).
- **schemas**: shape only (types, lengths, required fields). Checks that need the database belong in services.
- **services**: plain classes built in `api/deps.py`. They own permissions that depend on data (roles from the
  memberships response), transactions and events. They raise exceptions from `app/core/exceptions.py`, never
  `HTTPException`, and never import FastAPI.
- **repositories**: small, named query methods that take an `AsyncSession` (`get_message(message_id)`). They
  never commit. Database errors that mean something (a unique violation) become app exceptions here.
- **clients**: the only code that talks to other systems. They translate external errors into app exceptions
  (chatty-core unreachable -> `core_unavailable`).
- **models**: tables only: columns, constraints, indexes. No logic.
- **api/deps.py** is the only place that builds objects. Tests replace them with `app.dependency_overrides`.
- `core/` never imports from `api/`, `services/` or `repositories/`.

## Async rules

- Everything that does I/O is `async def`. Never call blocking code from async code: no `requests`,
  `time.sleep`, sync Redis or sync database drivers. Blocking SDKs (the MinIO client) go through
  `asyncio.to_thread`.
- One `AsyncSession` per request (`get_session`). Never use one session from two tasks at the same time
  (no `asyncio.gather` over queries on the same session).
- No lazy loading: accessing a relationship that wasn't loaded raises in async code. Load what you need in the
  repository (`selectinload`, explicit joins).
- Services own the transaction: `async with session.begin():` commits on success and rolls back on any
  exception. Read-only work doesn't need `begin()`.
- Sending a message follows D-03: call chatty-core's `attach` before the transaction, take the next `seq` from
  `channel_counters` inside it (the row lock keeps the order), publish only after the commit, and call `detach`
  if the transaction fails.
- Every external call has a timeout (`httpx` timeout, `asyncio.wait_for`).
- chatty-core's events arrive on the Redis channel `core.events` (D-08). On every (re)subscribe, delete all
  `members:*` and `channel:*` cache keys.

## Conventions

- IDs are UUIDs from chatty-core, stored as `uuid` columns without foreign keys to their tables (D-01).
  Times are UTC, ISO 8601 in JSON.
- Errors always use the contract body: `{"error": {"code", "message", "fields"?}}`. Codes come from
  `api-chat.md`; add new ones there first.
- Messages and events are paginated by `seq` (`before_seq` / `after_seq`); other lists use an opaque cursor.
- Fixed sets of values are `StrEnum`s (see `schemas/health.py`).
- Settings: one `app/core/config.py`. Every difference between environments comes from `.env` or compose.
  No secrets in code; secrets are `SecretStr`. New variables go into `.env.example` in the same commit.
- Local URLs use `127.0.0.1`, never `localhost`. Docker publishes the infra ports on IPv4 only, and Windows
  tries `localhost` as IPv6 first. Inside Docker, use the service names (`chat-db:5432`, `redis:6379`).
- Type hints on all function signatures. Async generators are typed `AsyncGenerator[T]`. Double quotes,
  line length 100 (ruff).
- Names: service methods are verbs (`send_message`, `mark_read`), repository methods describe the query
  (`get_message`, `list_events_after`).

## Tests

- pytest + pytest-asyncio (`asyncio_mode = "auto"`). Tests are `async def`; API tests use the `client` fixture
  from `tests/conftest.py`.
- API tests check status codes and response shapes from the contract, with services faked through
  `app.dependency_overrides`. Unit tests check business rules with fake repositories and clients.
- Tests that need the real database or Redis are marked `@pytest.mark.integration`. Skip them with
  `pytest -m "not integration"`.
- Every bug fix comes with a test that fails without the fix.

## Workflow

- One Jira ticket per branch and PR. Branch: `CHAT-<n>/<short-name>`. Commit messages start with `CHAT-<n>`.
- Use the PR template from the `.github` repo. Before pushing: `ruff check .`, `ruff format --check .`, `pytest`.
- One `requirements.txt` with every package pinned (`pip freeze`), dev tools included.
- Read every autogenerated migration before applying it. Never edit a migration that is already on `main`;
  add a new one.

## Don'ts

- Don't put logic in routers, schemas or models.
- Don't read or write chatty-core's database or call its public API; use only the internal API and its events.
- Don't publish on `core.events`; that channel belongs to chatty-core.
- Don't commit `.env`, keys or tokens. Don't log tokens, passwords or presigned URLs.
- On Windows, compiled packages (SQLAlchemy, asyncpg) may be blocked by Application Control. Run in Docker
  instead of working around it.
