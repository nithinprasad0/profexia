import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import text

from app.api.v1.auth import router as auth_router
from app.api.v1.users import router as users_router
from app.api.v1.learning import router as learning_router
from app.api.v1.assessment import router as assessment_router
from app.api.v1.realtime import router as realtime_router
from app.api.v1.providers import router as providers_router
from app.core.config import redact_url, settings
from app.core.database import engine
from app.core.redis import get_redis
from app.infrastructure.realtime import manager as realtime_manager

logger = logging.getLogger("profexia")
logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")


class RedactTokensFilter(logging.Filter):
    """Strip credentials from log lines.

    WebSockets authenticate with `?token=<JWT>`, and uvicorn logs request paths
    including the query string, which would leak access tokens into platform logs.
    """

    _pattern = re.compile(r"((?:access_|refresh_)?token=)[^&\s\"']+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        redacted = self._pattern.sub(r"\1[REDACTED]", message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


for _name in ("uvicorn.access", "uvicorn.error", "uvicorn"):
    logging.getLogger(_name).addFilter(RedactTokensFilter())

API_VERSION = "0.5.0"


@asynccontextmanager
async def lifespan(_: FastAPI):
    problems = settings.validate_production_settings()
    if problems:
        for problem in problems:
            logger.error("Configuration error: %s", problem)
        raise RuntimeError("Refusing to start: invalid production configuration")
    # Only redacted connection info is ever logged.
    logger.info(
        "Starting Profexia API (environment=%s, database=%s, redis=%s, cors_origins=%s)",
        settings.environment,
        redact_url(settings.database_url),
        redact_url(settings.redis_url),
        settings.cors_origin_list,
    )
    await realtime_manager.start()
    logger.info("Real-time backend: %s", "redis pub/sub" if realtime_manager.uses_redis else "in-memory (single instance)")
    yield
    logger.info("Shutting down Profexia API")
    await realtime_manager.stop()
    await engine.dispose()


app = FastAPI(
    title="Profexia API",
    version=API_VERSION,
    description="Profexia API — authentication, learning, assessment, real-time, and core domain.",
    lifespan=lifespan,
)

# CORS must be registered so browser preflight (OPTIONS) requests are answered
# instead of falling through to the routers (which returned 405 before).
# Bearer tokens are sent in the Authorization header, not cookies, so
# credentialed (cookie) CORS is disabled: browsers will not attach cookies to
# cross-origin API calls. Enable it only if cookie-based auth is introduced.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=settings.cors_origin_regex or None,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Requested-With"],
    max_age=600,
)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    if settings.is_production:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


app.include_router(auth_router)
app.include_router(users_router)
app.include_router(learning_router)
app.include_router(assessment_router)
app.include_router(realtime_router)
app.include_router(providers_router)


@app.exception_handler(RedisConnectionError)
@app.exception_handler(RedisTimeoutError)
async def redis_unavailable_handler(_: Request, exc: Exception):
    logger.warning("Redis unavailable: %s", type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={"detail": "A required service is temporarily unavailable. Please retry shortly."},
    )


@app.get("/", tags=["system"])
async def root():
    return {"name": "Profexia API", "version": API_VERSION}


async def _dependency_status() -> tuple[str, str]:
    database_status = "disconnected"
    redis_status = "disconnected"
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            database_status = "connected"
    except Exception:
        pass
    try:
        redis = get_redis()
        try:
            await redis.ping()
            redis_status = "connected"
        finally:
            await redis.aclose()
    except Exception:
        pass
    return database_status, redis_status


@app.get("/health", tags=["system"])
async def health():
    """Detailed status. Always HTTP 200 so a slow dependency does not restart the service."""
    database_status, redis_status = await _dependency_status()
    ok = database_status == "connected" and redis_status == "connected"
    return {"status": "ok" if ok else "degraded", "database": database_status, "redis": redis_status}


@app.get("/health/live", tags=["system"])
async def health_live():
    """Liveness probe: the process is up and serving HTTP. No dependency checks."""
    return {"status": "ok"}


@app.get("/health/ready", tags=["system"])
async def health_ready():
    """Readiness probe: 503 unless both PostgreSQL and Redis are reachable."""
    database_status, redis_status = await _dependency_status()
    ok = database_status == "connected" and redis_status == "connected"
    body = {"status": "ok" if ok else "degraded", "database": database_status, "redis": redis_status}
    return JSONResponse(status_code=200 if ok else 503, content=body)
