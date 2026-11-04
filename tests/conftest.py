"""Test configuration.

Unit tests need no external services. Integration/security tests (marked with
the `integration` fixture) need PostgreSQL and Redis; they run automatically when
both are reachable (docker compose, CI) and are skipped otherwise.

Isolation from real data:
- PostgreSQL: tests use a *separate* database named `<db>_test` on the same
  server (created and migrated with `alembic upgrade head` if missing). The
  development/production database is never used and nothing is ever dropped.
- Redis: tests use logical database 15.
- Every test creates its own uniquely named users/courses.

IMPORTANT: environment variables are configured here *before* the app is
imported, because settings and the database engine are created at import time.
"""

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_URL = "postgresql+asyncpg://profexia:profexia@postgres:5432/profexia"
DEFAULT_REDIS_URL = "redis://redis:6379/0"


def _reachable(url: str, default_port: int) -> bool:
    parts = urlsplit(url)
    if not parts.hostname:
        return False
    try:
        with socket.create_connection((parts.hostname, parts.port or default_port), timeout=0.5):
            return True
    except OSError:
        return False


def _with_path(url: str, path: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, path, p.query, p.fragment))


async def _ensure_database(admin_url: str, name: str) -> None:
    import asyncpg

    p = urlsplit(admin_url)
    conn = await asyncpg.connect(
        user=p.username, password=p.password, host=p.hostname, port=p.port or 5432, database="postgres"
    )
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


def _configure_environment() -> tuple[bool, bool]:
    # NOTE: must not import anything from `app` here: importing app.core.config
    # instantiates Settings, which would freeze the environment before the
    # test-database / test-Redis overrides below are applied.
    if os.environ.get("PROFEXIA_INTEGRATION", "auto") == "0":
        os.environ.setdefault("REALTIME_BACKEND", "memory")
        return False, False

    base_db = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    base_redis = os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)

    redis_ok = _reachable(base_redis, 6379)
    if redis_ok:
        os.environ["REDIS_URL"] = _with_path(base_redis, "/15")
        os.environ.setdefault("REALTIME_BACKEND", "redis")
    else:
        # Avoid every TestClient lifespan waiting on an unreachable Redis.
        os.environ.setdefault("REALTIME_BACKEND", "memory")

    db_ok = False
    if _reachable(base_db, 5432):
        db_name = urlsplit(base_db).path.lstrip("/") or "profexia"
        test_name = db_name if db_name.endswith("_test") else f"{db_name}_test"
        test_url = _with_path(base_db, f"/{test_name}")
        try:
            asyncio.run(_ensure_database(base_db, test_name))
            env = {**os.environ, "DATABASE_URL": test_url, "PYTHONPATH": str(ROOT)}
            subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env,
                           check=True, capture_output=True)
            subprocess.run([sys.executable, "-m", "app.infrastructure.seed"], cwd=ROOT, env=env,
                           check=True, capture_output=True)
            os.environ["DATABASE_URL"] = test_url
            db_ok = True
        except Exception as exc:  # noqa: BLE001
            print(f"[conftest] integration database unavailable: {type(exc).__name__}", file=sys.stderr)

    # A real-length signing key so PyJWT does not warn (never used outside tests).
    os.environ.setdefault("JWT_SECRET_KEY", "test-only-jwt-signing-key-0123456789abcdefghij")

    # Generous limits for ordinary tests; rate-limit tests tighten them explicitly.
    for key in ("RATE_LIMIT_LOGIN_PER_IP", "RATE_LIMIT_REGISTER_PER_IP", "RATE_LIMIT_REFRESH_PER_IP",
                "RATE_LIMIT_CODE_EXECUTIONS_PER_USER"):
        os.environ.setdefault(key, "100000")
    return db_ok and redis_ok, redis_ok


INTEGRATION_AVAILABLE, REDIS_AVAILABLE = _configure_environment()

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import settings as _settings  # noqa: E402
from app.main import app  # noqa: E402

# Hard safety check: integration tests must never run against a non-test database.
if INTEGRATION_AVAILABLE:
    assert urlsplit(_settings.database_url).path.endswith("_test"), "refusing to run against a non-test database"
    assert urlsplit(_settings.redis_url).path == "/15", "refusing to run against a non-test Redis database"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def integration():
    if not INTEGRATION_AVAILABLE:
        pytest.skip("PostgreSQL/Redis not reachable (run inside docker compose or CI)")


@pytest.fixture
def redis_required():
    if not REDIS_AVAILABLE:
        pytest.skip("Redis not reachable")
