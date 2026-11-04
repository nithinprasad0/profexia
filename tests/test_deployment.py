"""Regression tests for production-readiness behaviour.

None of these need PostgreSQL, Redis, or provider credentials.
"""

import pytest

from app.core.config import DEV_JWT_SECRET, Settings, normalize_database_url, redact_url
from app.core.database import engine_options
from app.infrastructure.providers.judge0 import Judge0Client

DEV_ORIGIN = "http://localhost:5173"


# --- CORS preflight (previously returned 405 and broke browser login) ---------

@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/v1/auth/login", "POST"),
        ("/api/v1/auth/register", "POST"),
        ("/api/v1/auth/refresh", "POST"),
        ("/api/v1/users/me", "GET"),
        ("/api/v1/courses", "POST"),
        ("/api/v1/enrollments/1/progress", "PATCH"),
    ],
)
def test_cors_preflight_allowed_origin(client, path, method):
    response = client.options(
        path,
        headers={
            "Origin": DEV_ORIGIN,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == DEV_ORIGIN
    allowed_headers = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed_headers
    assert "content-type" in allowed_headers
    assert method in response.headers["access-control-allow-methods"]


def test_cors_preflight_rejects_unknown_origin(client):
    response = client.options(
        "/api/v1/auth/login",
        headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_cors_headers_on_simple_request(client):
    response = client.get("/", headers={"Origin": DEV_ORIGIN})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == DEV_ORIGIN


# --- Health endpoints ---------------------------------------------------------

def test_health_live(client):
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_ready_reports_dependencies(client):
    response = client.get("/health/ready")
    assert response.status_code in {200, 503}
    assert set(response.json()) == {"status", "database", "redis"}


# --- Auth security ------------------------------------------------------------

def test_public_registration_cannot_create_admin(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "a@example.com", "username": "attacker", "password": "password123", "role": "admin"},
    )
    assert response.status_code == 403


def test_protected_route_requires_token(client):
    assert client.get("/api/v1/users/me").status_code == 401
    assert client.get("/api/v1/users/me", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401


# --- Database URL handling ----------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "postgresql+asyncpg://profexia:profexia@postgres:5432/profexia",
            "postgresql+asyncpg://profexia:profexia@postgres:5432/profexia",
        ),
        (
            "postgres://u:p@ep-cool-1.eu-central-1.aws.neon.tech/neondb?sslmode=require",
            "postgresql+asyncpg://u:p@ep-cool-1.eu-central-1.aws.neon.tech/neondb?ssl=require",
        ),
        (
            "postgresql://u:p@ep-x-pooler.aws.neon.tech/db?sslmode=require&channel_binding=require",
            "postgresql+asyncpg://u:p@ep-x-pooler.aws.neon.tech/db?ssl=require",
        ),
        ("postgresql://u:p@localhost/db?sslmode=disable", "postgresql+asyncpg://u:p@localhost/db"),
        ("sqlite+aiosqlite:///x.db", "sqlite+aiosqlite:///x.db"),
    ],
)
def test_normalize_database_url(raw, expected):
    assert normalize_database_url(raw) == expected


def test_pooler_host_disables_prepared_statement_cache():
    opts = engine_options("postgresql+asyncpg://u:p@ep-x-pooler.aws.neon.tech/db?ssl=require")
    assert opts["connect_args"]["statement_cache_size"] == 0
    assert "connect_args" not in engine_options("postgresql+asyncpg://u:p@postgres:5432/db")


def test_redact_url_hides_credentials():
    redacted = redact_url("rediss://default:supersecret@eu1-x.upstash.io:6379")
    assert "supersecret" not in redacted
    assert "eu1-x.upstash.io:6379" in redacted


def test_log_filter_redacts_websocket_tokens():
    import logging

    from app.main import RedactTokensFilter

    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1,
        '%s - "WebSocket %s" [accepted]', ("1.2.3.4:5", "/api/v1/ws/notifications?token=eyJhbGci.secret.sig"), None,
    )
    RedactTokensFilter().filter(record)
    message = record.getMessage()
    assert "eyJhbGci" not in message
    assert "token=[REDACTED]" in message


# --- Production configuration guard -----------------------------------------

@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch):
    """Settings() reads real env vars (e.g. inside docker-compose); isolate these tests."""
    for key in ("ENVIRONMENT", "DATABASE_URL", "REDIS_URL", "JWT_SECRET_KEY", "CORS_ORIGINS", "CORS_ORIGIN_REGEX"):
        monkeypatch.delenv(key, raising=False)


def test_production_rejects_compose_dev_secret():
    s = Settings(_env_file=None, environment="production",
                 jwt_secret_key="profexia-local-dev-only-not-for-production")
    assert any("JWT_SECRET_KEY" in p for p in s.validate_production_settings())


def test_production_rejects_insecure_defaults():
    problems = Settings(_env_file=None, environment="production").validate_production_settings()
    joined = " ".join(problems)
    assert "JWT_SECRET_KEY" in joined
    assert "CORS_ORIGINS" in joined
    assert "DATABASE_URL" in joined
    assert "REDIS_URL" in joined


def test_production_accepts_valid_configuration():
    s = Settings(
        _env_file=None,
        environment="production",
        jwt_secret_key="x" * 64,
        cors_origins="https://profexia.vercel.app",
        database_url="postgres://u:p@ep-1.neon.tech/db?sslmode=require",
        redis_url="rediss://default:pw@eu1.upstash.io:6379",
    )
    assert s.validate_production_settings() == []
    assert s.cors_origin_list == ["https://profexia.vercel.app"]  # no localhost in production


def test_production_rejects_wildcard_and_http_origins():
    s = Settings(_env_file=None, environment="production", jwt_secret_key="x" * 64, cors_origins="*,http://a.com")
    joined = " ".join(s.validate_production_settings())
    assert "'*'" in joined and "https://" in joined


def test_development_includes_local_origins():
    s = Settings(_env_file=None, environment="development", jwt_secret_key=DEV_JWT_SECRET)
    assert DEV_ORIGIN in s.cors_origin_list
    assert s.validate_production_settings() == []


# --- Judge0 limits ------------------------------------------------------------

def test_judge0_sends_resource_limits():
    limits = Judge0Client._limits()
    assert limits["cpu_time_limit"] > 0
    assert limits["memory_limit"] > 0
