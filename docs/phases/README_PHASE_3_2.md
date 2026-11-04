# Profexia — Phase 3.2 Fixed Replacement

This package contains the complete Phase 3.2 FastAPI authentication foundation with:

- FastAPI
- PostgreSQL 17
- Redis 8
- SQLAlchemy async
- Alembic migrations
- JWT access tokens
- Redis-backed refresh tokens
- Refresh-token rotation
- Password hashing with bcrypt
- Learner / teacher / admin roles
- `/api/v1/auth/register`
- `/api/v1/auth/login`
- `/api/v1/auth/refresh`
- `/api/v1/auth/logout`
- `/api/v1/users/me`
- Docker Compose
- pytest
- deterministic `PYTHONPATH=/app`
- `email-validator` dependency
- fixed Alembic configuration
- fixed API router composition

## Clean installation

From the extracted project directory:

```powershell
docker compose up -d --build
docker compose ps
docker compose exec api pytest -q
```

## Database verification

```powershell
docker compose exec postgres psql -U profexia -d profexia -c "\dt"
docker compose exec postgres psql -U profexia -d profexia -c "SELECT id,name FROM roles ORDER BY id;"
```

Expected roles:

```text
1 | learner
2 | teacher
3 | admin
```

## API route verification

```powershell
docker compose exec api python -c "from app.main import app; print([(getattr(r,'path',None),getattr(r,'methods',None)) for r in app.routes if hasattr(r,'path')])"
```

Expected authentication routes:

```text
/api/v1/auth/register
/api/v1/auth/login
/api/v1/auth/refresh
/api/v1/auth/logout
/api/v1/users/me
```

## Important

The API router intentionally extends the child routers' concrete `APIRoute` objects instead of calling `include_router()` inside `app/api/v1/router.py`. This avoids `_IncludedRouter` objects appearing in `router.routes` in the current FastAPI/Starlette environment and makes the Phase 3.2 route tests deterministic.

Do not run `docker compose down -v` unless you intentionally want to destroy the PostgreSQL data volume.
