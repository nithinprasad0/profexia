# Environment variable reference

Templates: [`/.env.example`](../.env.example) (backend) and [`/frontend/.env.example`](../frontend/.env.example) (frontend). Both contain placeholders only. Real values go in git-ignored `.env` files locally and in the provider dashboards in production.

## Backend (Render / docker-compose / `.env`)

| Variable | Required in prod | Default | Description |
| --- | --- | --- | --- |
| `ENVIRONMENT` | yes | `development` | `production` enables strict startup validation and removes localhost CORS origins |
| `DATABASE_URL` | yes | docker-compose Postgres | Any `postgres://`, `postgresql://` or `postgresql+asyncpg://` URL. `sslmode` is translated for asyncpg; `channel_binding` is dropped |
| `REDIS_URL` | yes | `redis://redis:6379/0` | Use `rediss://` for TLS (Upstash) |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | no | `5` | Connect/read timeout so requests fail fast (503) when Redis is down |
| `JWT_SECRET_KEY` | yes | `change-me` (rejected in prod) | HS256 signing key, ≥ 32 random chars. Rotating it invalidates all access tokens (clients silently refresh) |
| `JWT_ALGORITHM` | no | `HS256` | |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | no | `30` | Access-token lifetime |
| `REFRESH_TOKEN_EXPIRE_DAYS` | no | `7` | Refresh-token TTL in Redis (tokens rotate on each use) |
| `CORS_ORIGINS` | yes | empty | Comma-separated exact `https://` origins of the frontend |
| `CORS_ORIGIN_REGEX` | no | empty | Optional regex for extra origins (Vercel previews) |
| `RUN_MIGRATIONS_ON_STARTUP` | no | `true` | Entrypoint runs `alembic upgrade head` + role seed before serving |
| `PORT` | set by platform | `8000` | Port uvicorn binds on `0.0.0.0` (Render injects it) |
| `WS_MAX_SIZE_BYTES` | no | `1048576` | Maximum WebSocket frame size accepted by uvicorn |
| `TRUSTED_PROXY_COUNT` | yes on Render (`1`) | `0` | Proxies appending to `X-Forwarded-For`; the N-th entry from the right is the client IP for rate limiting |
| `REALTIME_BACKEND` | no | `redis` | `redis` = pub/sub fan-out across instances; `memory` = single instance only |
| `REALTIME_CHANNEL` | no | `profexia:realtime` | Redis pub/sub channel shared by all instances of one environment |
| `UVICORN_EXTRA_ARGS` | no | empty | Extra uvicorn flags, e.g. `--workers 2` (see WebSocket note in DEPLOYMENT.md) |
| `RAZORPAY_KEY_ID` | for payments | empty | `rzp_test_…` (test mode) or `rzp_live_…` (live mode) |
| `RAZORPAY_KEY_SECRET` | for payments | empty | |
| `RAZORPAY_WEBHOOK_SECRET` | for webhooks | empty | Same value as in the Razorpay webhook settings |
| `RAZORPAY_BASE_URL` | no | `https://api.razorpay.com/v1` | |
| `JUDGE0_BASE_URL` | for code execution | empty | RapidAPI or self-hosted Judge0 URL |
| `JUDGE0_API_KEY` | depends | empty | Sent in the `JUDGE0_AUTH_HEADER` header |
| `JUDGE0_AUTH_HEADER` | no | `X-RapidAPI-Key` | `X-Auth-Token` for self-hosted Judge0 |
| `JUDGE0_WAIT` | no | `true` | Wait for the result synchronously |
| `JUDGE0_TIMEOUT_SECONDS` | no | `20` | HTTP timeout for Judge0 calls |
| `JUDGE0_CPU_TIME_LIMIT` | no | `5` | Seconds per submission (0 = server default) |
| `JUDGE0_WALL_TIME_LIMIT` | no | `10` | Seconds per submission (0 = server default) |
| `JUDGE0_MEMORY_LIMIT_KB` | no | `128000` | KB per submission (0 = server default) |
| `LIVEKIT_URL` | for video | empty | `wss://<project>.livekit.cloud` |
| `LIVEKIT_API_KEY` | for video | empty | |
| `LIVEKIT_API_SECRET` | for video | empty | |

## Rate limiting

Redis fixed windows. A value of `0` disables that limit; `RATE_LIMIT_ENABLED=false` disables all of them. Exceeding a limit returns 429 with `Retry-After`. See [SECURITY.md](../SECURITY.md#7-rate-limiting).

| Variable | Default | Meaning |
| --- | --- | --- |
| `RATE_LIMIT_ENABLED` | `true` | Master switch |
| `RATE_LIMIT_LOGIN_FAILURES_PER_ACCOUNT` | `5` | Failed logins per email before a temporary block (success resets) |
| `RATE_LIMIT_LOGIN_FAILURE_WINDOW_SECONDS` | `900` | Block/window length |
| `RATE_LIMIT_LOGIN_PER_IP` | `30` | Login attempts per client IP |
| `RATE_LIMIT_LOGIN_IP_WINDOW_SECONDS` | `900` | |
| `RATE_LIMIT_REGISTER_PER_IP` | `10` | Registrations per client IP |
| `RATE_LIMIT_REGISTER_WINDOW_SECONDS` | `3600` | |
| `RATE_LIMIT_REFRESH_PER_IP` | `60` | Refresh-token exchanges per client IP |
| `RATE_LIMIT_REFRESH_WINDOW_SECONDS` | `900` | |
| `RATE_LIMIT_CODE_EXECUTIONS_PER_USER` | `30` | Judge0 submissions per user |
| `RATE_LIMIT_CODE_EXECUTION_WINDOW_SECONDS` | `3600` | |

## Frontend (Vercel / `frontend/.env`)

Vite exposes only `VITE_*` variables, and inlines them **at build time**. Never put secrets here — everything ends up in the public JavaScript bundle.

| Variable | Required in prod | Default | Description |
| --- | --- | --- | --- |
| `VITE_API_URL` | yes | dev: `http://localhost:8000`; prod: same origin | Backend origin, with or without `/api/v1` (both resolve to `…/api/v1`) |
| `VITE_WS_URL` | no | derived from `VITE_API_URL` (`https`→`wss`) | WebSocket base override |
| `VITE_SECURE_COOKIES` | no | `false` | Reserved for a future cookie-based auth strategy; currently unused |

## Checklist by environment

| | Local (compose) | CI | Production |
| --- | --- | --- | --- |
| `ENVIRONMENT` | development | development | **production** |
| `DATABASE_URL` | compose default | service container | Neon |
| `REDIS_URL` | compose default | service container | Upstash `rediss://` |
| `JWT_SECRET_KEY` | dev placeholder | CI placeholder | Render-generated |
| `CORS_ORIGINS` | localhost:5173 | – | Vercel origin(s) |
| `VITE_API_URL` | `http://localhost:8000` | `https://api.example.com` | Render URL |
