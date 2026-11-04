# Security

This document describes how Profexia protects accounts and data, what was hardened in Phase 5, and which risks remain. Every rule below is enforced by the **backend**; the frontend only hides controls a user cannot use.

To report a vulnerability, contact the maintainer privately (do not open a public issue).

---

## 1. Security architecture

```text
Browser (React SPA, Vercel)
  │  HTTPS: Authorization: Bearer <JWT>         WSS: ?token=<JWT>
  ▼
FastAPI (Render, Docker, non-root)
  ├─ CORS allow-list (exact https origins in production, no credentials)
  ├─ Security headers (nosniff, DENY framing, no-referrer, no-store, HSTS in prod)
  ├─ Rate limiter (Redis fixed windows, hashed keys)
  ├─ Auth dependency: get_current_user → JWT validation → active user
  ├─ Role dependency: require_roles(...)
  ├─ Object checks: app/api/v1/access.py + per-route ownership checks
  └─ Log filter: tokens redacted, connection URLs redacted
        │
        ├─ PostgreSQL (TLS on Neon) – ORM only, parameterised queries
        └─ Redis (TLS on Upstash) – refresh tokens, rate limits, real-time pub/sub
```

## 2. Authentication

| Control | Implementation |
| --- | --- |
| Password hashing | bcrypt with per-password salt (`app/core/security.py`). Passwords > 72 bytes are rejected (bcrypt would silently truncate) |
| Login timing | bcrypt runs even for unknown emails (dummy hash) so response time does not reveal registered emails |
| Access token | HS256 JWT, 30 min (`ACCESS_TOKEN_EXPIRE_MINUTES`). Algorithm pinned; `exp`, `iat`, `sub` required; `type` must be `access`; `alg: none`, wrong signatures and expired tokens are rejected |
| Refresh token | 384-bit random value; only its SHA-256 is stored in Redis with a TTL (`REFRESH_TOKEN_EXPIRE_DAYS`). **Single use**: exchanged atomically with `GETDEL`, so concurrent replays get exactly one success |
| Logout | Deletes the refresh token. The current access token stays valid until it expires (≤ 30 min) — see risks |
| Signing secret | `JWT_SECRET_KEY`; production refuses to start if it is missing, < 32 chars, or a known placeholder |
| Registration | Learner or teacher only. `role=admin` → 403. Admins are promoted out-of-band: `python -m app.infrastructure.create_admin --email …` |
| Token storage (browser) | `sessionStorage` (per tab). Vulnerable to XSS like any JS-readable storage — see risks |

## 3. Authorization model and role permissions

Roles: **learner**, **teacher**, **admin**. Ownership is always derived from database records, never from IDs supplied by the client (e.g. a chat message's `sender_id` and a LiveKit token's identity come from the token).

| Resource | Learner | Teacher | Admin |
| --- | --- | --- | --- |
| Course catalogue, teachers, skills (read) | public | public | public |
| Create course | ✗ | ✓ | ✓ |
| Edit course | ✗ | own | any |
| Enroll / progress | own | ✗ | ✗ |
| Question bank (incl. answer key, explanations) | ✗ **(403)** | own | any |
| Tests: list / view | **enrolled courses**, published only | own | any |
| Start / submit attempt | **enrolled**, own attempt | ✗ | ✗ |
| View attempt / result | own | own tests | any |
| All results of a test | ✗ | own tests | any |
| Chat room | member (see §4) | member | course/session rooms |
| Notifications | own | own | create for anyone |
| Learning sessions | enrolled courses | host (own courses) | any |
| LiveKit token | enrolled, subscribe-only | host, publish | publish |
| Skills (create) | ✗ | ✗ | ✓ |
| Code execution results | own | own | any |

Object-level (IDOR) protections are covered by `tests/test_security.py::test_role_and_object_boundaries` and the per-area tests below.

## 4. Chat and WebSocket security

**Room model.** Chat rooms are no longer free-form strings. A room id must be one of:

| Room id | Members |
| --- | --- |
| `course:<course_id>` | course teacher, learners with an active/completed enrollment, admins |
| `session:<session_id>` | session host, learners enrolled in the session's course, admins |
| `dm:<lower_user_id>:<higher_user_id>` | exactly those two (active) users — admins are **not** members of DMs |

- `GET/POST /api/v1/chat/{room}/messages`: 401 without a valid token, 400 for a malformed room id, **403** when the caller is not a member. A non-existent course/session returns the same 403 as a forbidden one, so guessing ids reveals nothing.
- `WS /api/v1/ws/chat/{room}?token=…`: token and membership are checked **before** the socket is accepted (close code **1008** otherwise), and membership is **re-checked on every message**, so revoked access (e.g. a withdrawn enrollment) takes effect immediately.
- `WS /api/v1/ws/learning-sessions/{id}`: participants only; ended/cancelled sessions are rejected.
- `WS /api/v1/ws/notifications`: each user only joins their own `user:<id>` room.
- WebSocket frames are capped at 1 MiB (`WS_MAX_SIZE_BYTES`), signalling event names at 50 chars, chat messages at 4000 chars.
- Access tokens in WebSocket URLs are redacted from uvicorn logs (`token=[REDACTED]`).

**Contract change:** messages previously stored under free-form room ids (e.g. `general`) are kept in the database but are no longer readable through the API.

## 5. Assessment security

- Learners must have an **active or completed enrollment** in the course to list tests, view a test, or start an attempt (403 otherwise). This is enforced in the API, not the UI.
- The **question bank** (`/courses/{id}/questions`, `/questions/{id}`) is teacher/admin only. Learners receive questions exclusively through an attempt.
- **Answer keys and explanations are never sent to learners** — not before, during, or after an attempt. Attempts contain `id/text/options/marks`; results contain per-question `correct` and `marks_awarded`, never `correct_option` or `explanation`. (Release condition: there is currently no "reveal answers" feature, so the condition is *never for learners*. Adding one would need a per-test release setting.)
- One attempt per learner per test (DB unique constraint). Other learners get 403/404 for someone else's attempt or result.
- **Time limits** are enforced server-side: a submission arriving more than 60 s after `started_at + time_limit_minutes` closes the attempt with no answers counted (score 0).
- Teachers manage only their own courses' questions/tests/results; admins manage all.

## 6. LiveKit authorization

`POST /api/v1/learning-sessions/{session_id}/livekit-token`

- Requires authentication; the caller must be the session host, an admin, or a learner enrolled in the session's course (403 otherwise; unknown session 404).
- The **room name is derived on the server**: `profexia-session-<session_id>`. `room_name` in the request is optional; any other value → 403. Tokens can therefore only grant the room belonging to that session record.
- Ended or cancelled sessions → 409.
- `canPublish` is granted only to the session host and admins. Learners always receive **subscribe-only** tokens and cannot request more. No token ever grants `roomAdmin`, `roomCreate` or `roomList`.
- Token TTL 5–240 minutes; every issued token is audited in `livekit_token_audits`.
- Tests sign tokens locally with a fake secret; no LiveKit API calls are made.

## 7. Rate limiting

Redis fixed windows (`app/core/rate_limit.py`). Keys are `profexia:rl:<scope>:<sha256(identifier)>` — no raw emails, IPs, passwords or tokens. Exceeding a limit returns **429** with `Retry-After`.

| Scope | Default | Env vars |
| --- | --- | --- |
| Failed logins per account (email) | 5 / 15 min; success resets | `RATE_LIMIT_LOGIN_FAILURES_PER_ACCOUNT`, `RATE_LIMIT_LOGIN_FAILURE_WINDOW_SECONDS` |
| Login attempts per IP | 30 / 15 min | `RATE_LIMIT_LOGIN_PER_IP`, `RATE_LIMIT_LOGIN_IP_WINDOW_SECONDS` |
| Registrations per IP | 10 / hour | `RATE_LIMIT_REGISTER_PER_IP`, `RATE_LIMIT_REGISTER_WINDOW_SECONDS` |
| Refresh exchanges per IP | 60 / 15 min | `RATE_LIMIT_REFRESH_PER_IP`, `RATE_LIMIT_REFRESH_WINDOW_SECONDS` |
| Judge0 executions per user | 30 / hour | `RATE_LIMIT_CODE_EXECUTIONS_PER_USER`, `RATE_LIMIT_CODE_EXECUTION_WINDOW_SECONDS` |

- Set a limit to `0` (or `RATE_LIMIT_ENABLED=false`) to disable it.
- The per-account limit is authoritative and IP-independent. Per-IP limits depend on correct client-IP detection: `TRUSTED_PROXY_COUNT=N` uses the N-th `X-Forwarded-For` entry from the right (entries to its left are client-controlled). Use `1` on Render, `0` locally.
- **Redis failure:** the limiter fails open (logs a warning) rather than locking everyone out. Login/refresh still need Redis for refresh tokens and return **503** while it is down, so failing open does not create an unauthenticated path.
- Trade-off: the per-account lock can be triggered by someone who knows a user's email (temporary denial of service for 15 min). The defaults keep that window short.

## 8. Other controls

- **CORS:** production allows only the exact `https://` origins in `CORS_ORIGINS` (+ optional `CORS_ORIGIN_REGEX`); `*` and `http://` are rejected at startup; credentials are disabled because auth uses bearer headers.
- **Secrets:** only via environment variables; `.env` is git-ignored; `.env.example` holds placeholders; connection URLs are logged redacted; provider errors are logged by exception type only.
- **SQL:** SQLAlchemy ORM / bound parameters only; the single raw statement is `SELECT 1`. No string-built SQL.
- **Input validation:** Pydantic models with length/range limits on every write endpoint; Razorpay `notes` bounded to 15 × 256 chars.
- **Payment webhook:** HMAC-SHA256 over the raw body with `RAZORPAY_WEBHOOK_SECRET`, constant-time comparison; unsigned/forged → 400; unconfigured → 503.
- **File uploads:** none implemented.
- **Container:** runs as an unprivileged user (uid 10001); the image excludes `.env*`, tests, docs and the frontend.
- **Supply chain / CI:** every push runs gitleaks over the full Git history, `pip-audit` on `requirements.txt` and `npm audit --audit-level=high`, and fails if an `.env`/key file is tracked or a token appears in API logs. Dependencies are pinned; Phase 6 upgraded FastAPI 0.116→0.162, Starlette 0.47→1.10, PyJWT 2.10→2.15, pytest 8→9 and Vite 7.3.6→7.3.7 to clear all known advisories at the time of the audit (2026-11-04).

## 9. Multi-instance real-time

WebSocket events are fanned out through Redis pub/sub (`REALTIME_BACKEND=redis`, channel `REALTIME_CHANNEL`). Each instance delivers to its own sockets immediately and publishes the event; other instances deliver it to theirs; the origin instance ignores its own echo, so clients receive each event once. Verified with two API containers sharing Redis (`tests/test_realtime_multi_instance.py` and a manual two-container run). Delivery is best-effort (at-most-once, no replay): clients should re-fetch history after reconnecting.

## 10. Known limitations and remaining risks

| Severity | Risk | Mitigation / decision needed |
| --- | --- | --- |
| Medium | Tokens in `sessionStorage` can be stolen by an XSS bug | React escapes output; no `dangerouslySetInnerHTML`. Moving to HttpOnly cookies + CSRF protection is an architectural change (BFF or same-site API domain) |
| Medium | Access tokens are not revocable before expiry (logout, role change, deactivation take up to 30 min for open tokens; `/users/me` checks `is_active` on every request) | Shorten `ACCESS_TOKEN_EXPIRE_MINUTES`, or add a Redis deny-list / token version column |
| Medium | WebSocket tokens travel in the URL query string (may appear in proxy logs outside our control) | Logs we control are redacted. Alternative: first-message auth or short-lived WS tickets (contract change) |
| Medium | Payment orders accept a client-supplied amount and are not linked to any product/entitlement | Harmless today (payments grant nothing). Before selling courses: add prices server-side and grant access only from verified webhooks |
| Low | Per-account lockout can be abused to lock out a known email for 15 min | Short window; consider CAPTCHA/step-up after N failures |
| Low | Registration responds 409 for an existing email (enumeration) | Common UX trade-off; rate-limited per IP |
| Low | Webhook replay is possible (Razorpay signs bodies without timestamps) | Re-applying the same status is idempotent |
| Low | `/docs` and `/openapi.json` are public in production | Schema contains no secrets; hide behind auth if desired |
| Low | `alembic check` reports pre-existing drift: the DB has *stricter* unique constraints (e.g. `enrollments(course_id, learner_id)`) than the models declare | Do not autogenerate a migration from it (it would drop constraints); align the models in a future change |
| Info | Real-time delivery is at-most-once | Re-fetch on reconnect |
