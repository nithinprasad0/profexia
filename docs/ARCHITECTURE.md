# Architecture

## Overview

Profexia is a single-page React application talking to a FastAPI backend over a versioned REST API (`/api/v1`) and WebSockets (`/api/v1/ws/*`). PostgreSQL is the system of record; Redis stores short-lived authentication state. Third-party providers handle payments (Razorpay), sandboxed code execution (Judge0) and real-time video (LiveKit).

```mermaid
flowchart TB
    subgraph Frontend [frontend/ — React 19 + Vite]
        Pages[pages/*] --> Guards[routes/guards.tsx<br/>ProtectedRoute / RoleRoute]
        Pages --> Auth[auth/AuthProvider]
        Auth --> Client[api/client.ts<br/>fetch + 401 refresh + retry]
        Pages --> Resources[api/resources.ts]
        Resources --> Client
        Pages --> WS[api/websocket.ts]
        Client --> Cfg[api/config.ts<br/>VITE_API_URL → API_URL / WS_URL]
        WS --> Cfg
    end

    subgraph Backend [app/ — FastAPI]
        MW[CORS middleware] --> Routers
        Routers[api/v1/*<br/>auth · users · learning · assessment · realtime · providers]
        Routers --> Deps[deps.py<br/>get_current_user · require_roles]
        Routers --> Schemas[schemas/* Pydantic]
        Routers --> Models[domain/models.py SQLAlchemy]
        Routers --> RT[infrastructure/realtime.py<br/>in-process room manager]
        Routers --> Prov[infrastructure/providers/*<br/>Razorpay · Judge0 · LiveKit]
        Core[core/<br/>config · database · redis · security]
    end

    Client -- "HTTPS + Bearer JWT" --> MW
    WS -- "WSS ?token=JWT" --> Routers
    Models --> PG[(PostgreSQL)]
    Core --> RD[(Redis)]
```

## Backend layers

| Layer | Location | Responsibility |
| --- | --- | --- |
| HTTP/WebSocket | `app/api/v1/` | Routing, request validation, authorization checks |
| Schemas | `app/schemas/` | Pydantic request/response contracts |
| Domain | `app/domain/models.py` | SQLAlchemy 2 models (users, roles, profiles, skills, courses, enrollments, questions, tests, submissions, chat, notifications, learning sessions, payments, code executions, LiveKit audit) |
| Infrastructure | `app/infrastructure/` | WebSocket connection manager, provider HTTP clients, seed/admin scripts |
| Core | `app/core/` | Settings (env), async engine + sessions, Redis client, password hashing, JWT |
| Migrations | `alembic/versions/` | `0001_core_domain` → `0002_learning_progress` → `0003_assessment` → `0004_realtime` → `0005_external_providers` (head) |

## Authentication flow

```mermaid
sequenceDiagram
    participant B as Browser
    participant A as API
    participant R as Redis
    B->>A: POST /auth/login {email, password}
    A->>A: bcrypt verify
    A->>R: SET profexia:refresh:<sha256(token)> = user_id (TTL 7d)
    A-->>B: {access_token (JWT, 30 min), refresh_token}
    B->>A: GET /users/me  Authorization: Bearer <access>
    A-->>B: 200 user
    Note over B,A: access token expires
    B->>A: GET /courses → 401
    B->>A: POST /auth/refresh {refresh_token}
    A->>R: GET + DEL old key (rotation)
    A->>R: SET new key
    A-->>B: new token pair
    B->>A: retry GET /courses → 200
```

- Passwords: bcrypt with per-password salt.
- Access tokens: HS256 JWT with `sub`, `role`, `type=access`, `iat`, `exp`.
- Refresh tokens: 384-bit random strings; only their SHA-256 hash is stored in Redis; single use (atomic `GETDEL` on every refresh); deleted on logout.
- Login, registration, refresh and code execution are rate limited in Redis (429 + `Retry-After`).
- WebSockets authenticate with `?token=<access token>` and close with code 1008 when invalid.
- Roles are checked server-side through `require_roles(...)` and per-resource ownership checks.
- Public registration may choose `learner` or `teacher`; `admin` is rejected (403) and must be granted with `python -m app.infrastructure.create_admin`.

## Frontend

- `api/config.ts` resolves `VITE_API_URL` (with or without `/api/v1`) to one canonical base and derives `ws://`/`wss://` from it. Production builds never fall back to `localhost`.
- `api/client.ts` attaches the bearer token, refreshes once on 401 (de-duplicated across concurrent requests) and retries the original request.
- Tokens are kept in `sessionStorage` (per tab, cleared when the tab closes).
- `routes/guards.tsx` redirects unauthenticated users to `/login` and non-teachers away from `/app/teach`; the API enforces the same rules independently.

## Deployment topology

```mermaid
flowchart LR
    U[User] -->|HTTPS| V[Vercel CDN<br/>frontend/dist + SPA rewrite]
    U -->|HTTPS / WSS| R[Render<br/>Docker: uvicorn --proxy-headers on $PORT]
    R -->|TLS| N[(Neon PostgreSQL)]
    R -->|rediss://| UP[(Upstash Redis)]
    R --> P[Razorpay / Judge0]
    U -->|WSS| L[LiveKit Cloud]
    GH[GitHub] -->|CI: tests, migrations, build| GH
    GH -->|checks pass → deploy| R
    GH -->|deploy| V
```

Startup sequence on Render (`docker-entrypoint.sh`): `alembic upgrade head` → idempotent role seed → `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers`. The FastAPI lifespan validates production settings, logs redacted connection info, and disposes the DB engine on shutdown.

## Known architectural limits

- Each instance keeps its own WebSocket connections in memory; events are shared across instances through Redis pub/sub (`app/infrastructure/realtime.py`). Delivery is at-most-once.
- Authorization helpers shared by REST and WebSocket endpoints live in `app/api/v1/access.py`; rate limiting in `app/core/rate_limit.py`. See [SECURITY.md](../SECURITY.md).
