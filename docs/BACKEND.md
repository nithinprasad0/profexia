# Backend (FastAPI)

The backend lives at the repository root (`app/`, `alembic/`, `tests/`, `Dockerfile`, `docker-compose.yml`) so the Render service and Docker Compose can build from `.`.

## Run with Docker Compose (recommended)

```bash
docker compose up -d --build          # postgres + redis + api
docker compose logs -f api            # migrations, seed, uvicorn
docker compose exec api pytest -q     # tests
docker compose exec api alembic current
docker compose down                   # stop (keeps data volumes)
```

`docker compose down -v` **deletes** the local database and Redis volumes — avoid it unless you want a reset.

The API container bind-mounts the repository (`.:/app`), so code changes are picked up on restart (`docker compose restart api`). For auto-reload, set `UVICORN_EXTRA_ARGS=--reload` in the `api` service environment.

## Run without Docker

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                   # set DATABASE_URL / REDIS_URL to your local services
alembic upgrade head
python -m app.infrastructure.seed
uvicorn app.main:app --reload --port 8000
```

## Tests

```bash
pytest -q                                         # unit + contract tests, no services needed
python scripts/smoke_test.py http://localhost:8000   # end-to-end against a running API (needs Postgres + Redis)
```

`tests/test_deployment.py` covers CORS preflight (the former 405), health endpoints, admin self-registration rejection, database URL normalisation for Neon, Redis URL redaction, and production configuration validation.

## Migrations

```bash
alembic upgrade head          # apply
alembic current               # show applied revision
alembic history               # list revisions
alembic revision -m "msg" --autogenerate   # new migration (review it before committing!)
```

`alembic/env.py` reads `DATABASE_URL` (normalised by `app/core/config.py`); the URL in `alembic.ini` is only a fallback. Current head: `0005_external_providers`.

Rules for new migrations: additive changes first (new nullable columns/tables), backfill, then tighten constraints in a later migration. Never drop columns or tables in the same release that stops using them.

## Creating an admin

```bash
python -m app.infrastructure.create_admin --email you@example.com
# docker: docker compose exec api python -m app.infrastructure.create_admin --email you@example.com
```

The user must already exist (register through the app first).

## Configuration

See [ENVIRONMENT.md](ENVIRONMENT.md). With `ENVIRONMENT=production` the app refuses to start with insecure defaults.

## Phase history

Detailed notes for each phase are in [docs/phases](phases/): 3.2 authentication, 3.3 learning, 3.4 assessment, 3.5 real-time, 3.6 external providers.
