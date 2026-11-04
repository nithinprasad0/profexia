"""Application settings.

All values are read from environment variables (or a local, git-ignored `.env`
file). Nothing in this module may contain a real secret: defaults are safe for
local development only, and `validate_production_settings()` refuses to start a
production instance that still uses them.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "change-me"
DEV_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
]


def normalize_database_url(url: str) -> str:
    """Return a SQLAlchemy async (asyncpg) URL for any common Postgres URL form.

    Managed providers (Neon, Render, Heroku-style) hand out URLs such as
    ``postgres://user:pass@host/db?sslmode=require&channel_binding=require``.
    asyncpg does not understand ``sslmode``/``channel_binding`` query params and
    SQLAlchemy needs the ``postgresql+asyncpg`` scheme, so translate them here.
    Local URLs that are already correct pass through unchanged.
    """
    url = url.strip()
    if not url:
        return url
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in {"postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"}:
        scheme = "postgresql+asyncpg"
    if scheme != "postgresql+asyncpg":
        return url

    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "sslmode":
            # asyncpg uses `ssl`; "require" encrypts without CA verification,
            # which matches libpq's sslmode=require semantics.
            if value and value != "disable":
                query.append(("ssl", "require" if value in {"require", "prefer", "allow"} else value))
        elif key in {"channel_binding", "options"}:
            # Not supported by asyncpg's DSN parser; safe to drop.
            continue
        else:
            query.append((key, value))
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def redact_url(url: str) -> str:
    """Hide credentials in a connection URL so it can be logged safely."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if parts.password is None and parts.username is None:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"***:***@{host}", parts.path, "", ""))


class Settings(BaseSettings):
    # "development" or "production". Production enables strict validation.
    environment: str = "development"

    database_url: str = "postgresql+asyncpg://profexia:profexia@postgres:5432/profexia"
    redis_url: str = "redis://redis:6379/0"
    redis_socket_timeout_seconds: float = 5.0

    jwt_secret_key: str = DEV_JWT_SECRET
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    jwt_algorithm: str = "HS256"

    # CORS. Comma-separated list of exact origins, e.g.
    # "https://profexia.vercel.app,https://www.profexia.com".
    # In development the local Vite origins are always allowed in addition.
    cors_origins: str = ""
    # Optional regex for extra origins (e.g. Vercel preview deployments):
    # ^https://profexia-[a-z0-9-]+-yourteam\.vercel\.app$
    cors_origin_regex: str = ""

    # Run `alembic upgrade head` + role seed from the container entrypoint.
    run_migrations_on_startup: bool = True

    # --- Rate limiting (Redis fixed windows; set a limit to 0 to disable it) ---
    rate_limit_enabled: bool = True
    # Failed logins per account (email) before it is temporarily blocked.
    rate_limit_login_failures_per_account: int = 5
    rate_limit_login_failure_window_seconds: int = 900
    # Login attempts (success or failure) per client IP.
    rate_limit_login_per_ip: int = 30
    rate_limit_login_ip_window_seconds: int = 900
    # Registrations per client IP.
    rate_limit_register_per_ip: int = 10
    rate_limit_register_window_seconds: int = 3600
    # Refresh-token exchanges per client IP.
    rate_limit_refresh_per_ip: int = 60
    rate_limit_refresh_window_seconds: int = 900
    # Judge0 code executions per user (paid provider quota protection).
    rate_limit_code_executions_per_user: int = 30
    rate_limit_code_execution_window_seconds: int = 3600
    # Number of reverse proxies in front of the API that append to X-Forwarded-For.
    # 0 = use the socket peer address. Render (measured): 3, together with
    # CLIENT_IP_HEADER=cf-connecting-ip (see docs/DEPLOYMENT.md).
    trusted_proxy_count: int = 0
    # Optional: read the client IP from a header set by the edge proxy itself
    # (e.g. "cf-connecting-ip" behind Cloudflare). Takes precedence over
    # TRUSTED_PROXY_COUNT when the header holds a valid IP. Only set this when the
    # edge is guaranteed to overwrite the header (see docs/DEPLOYMENT.md).
    client_ip_header: str = ""
    # Temporary: expose GET /health/proxy, a privacy-safe description of the
    # forwarding headers (structure only, never IP addresses). Keep off normally.
    proxy_diagnostics: bool = False

    # --- Real-time fan-out ---
    # "redis": publish WebSocket events through Redis pub/sub so clients connected to
    # different API instances receive them. "memory": single-instance only.
    realtime_backend: str = "redis"
    realtime_channel: str = "profexia:realtime"

    # External provider configuration. Keep secrets out of source control.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    razorpay_base_url: str = "https://api.razorpay.com/v1"

    judge0_base_url: str = ""
    judge0_api_key: str = ""
    judge0_auth_header: str = "X-RapidAPI-Key"
    judge0_wait: bool = True
    judge0_timeout_seconds: float = 20.0
    # Per-submission sandbox limits sent to Judge0 (0 = use Judge0 server default).
    judge0_cpu_time_limit: float = 5.0
    judge0_wall_time_limit: float = 10.0
    judge0_memory_limit_kb: int = 128000

    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    livekit_url: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @field_validator("environment")
    @classmethod
    def _normalize_environment(cls, value: str) -> str:
        return value.strip().lower() or "development"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        origins = [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]
        if not self.is_production:
            origins += [o for o in DEV_CORS_ORIGINS if o not in origins]
        return origins

    def validate_production_settings(self) -> list[str]:
        """Return a list of fatal configuration problems for production."""
        if not self.is_production:
            return []
        problems = []
        secret = self.jwt_secret_key.lower()
        known_placeholder = (
            self.jwt_secret_key == DEV_JWT_SECRET
            or any(marker in secret for marker in ("change-me", "replace-with", "not-for-production", "dev-only"))
        )
        if known_placeholder or len(self.jwt_secret_key) < 32:
            problems.append("JWT_SECRET_KEY must be set to a random value of at least 32 characters")
        if not self.cors_origin_list and not self.cors_origin_regex:
            problems.append("CORS_ORIGINS must list the production frontend origin(s)")
        if "*" in self.cors_origin_list:
            problems.append("CORS_ORIGINS must not contain '*' in production")
        if any(o.startswith("http://") for o in self.cors_origin_list):
            problems.append("CORS_ORIGINS must use https:// origins in production")
        if "@postgres:5432" in self.database_url:
            problems.append("DATABASE_URL still points at the local docker-compose database")
        if self.redis_url.startswith("redis://redis:"):
            problems.append("REDIS_URL still points at the local docker-compose Redis")
        return problems


settings = Settings()
