# Deployment guide

Target architecture:

| Piece | Provider | What you configure |
| --- | --- | --- |
| Source + CI | GitHub + GitHub Actions | push the repo; CI runs automatically |
| PostgreSQL | Neon | copy the connection string → `DATABASE_URL` |
| Redis | Upstash | copy the TLS URL → `REDIS_URL` |
| API | Render (Docker web service) | `render.yaml` Blueprint + secrets |
| Frontend | Vercel | root directory `frontend`, `VITE_API_URL` |

Nothing here assumes accounts or credentials already exist. Nothing in the repository contains real credentials; every secret is entered in a provider dashboard.

Security model and hardening details: [SECURITY.md](../SECURITY.md).

## Who does what

| Status | Task | Who |
| --- | --- | --- |
| ✅ Done locally | Repository audit (ignore rules, no secrets/artifacts), dependency vulnerability fixes, CI workflow, Render Blueprint, Vercel config, docs | Claude |
| ✅ Done locally | CI jobs simulated from a clean export in Linux containers: backend (101 tests, migrations, smoke 46/46), frontend (typecheck, build, bundle checks), security (gitleaks, pip-audit, npm audit), Docker (non-root, no secrets in image) | Claude |
| ✅ Done locally | Production rehearsal: `ENVIRONMENT=production`, `PORT=10000`, TLS-only PostgreSQL with a Neon-style `?sslmode=require&channel_binding=require` URL, TLS+password Redis (`rediss://`), two API instances, CORS, HSTS, proxy-trust probe | Claude |
| 🔑 Needs your GitHub account | Create the repository, push, enable branch protection | You |
| 🔑 Needs your Neon account | Create project/database, copy the connection string | You |
| 🔑 Needs your Upstash account | Create Redis database, copy the `rediss://` URL | You |
| 🔑 Needs your Render account | Create the Blueprint, paste the three secrets, deploy, run the [client-IP diagnostic](#rate-limiting-and-client-ips) | You |
| 🔑 Needs your Vercel account | Import the repo (root `frontend`), set `VITE_API_URL`, deploy | You |
| ✋ Needs your credentials / approval | Commit and push (not done), Razorpay/Judge0/LiveKit keys, promoting the first admin, any paid plan upgrades | You |
| ⏳ Not yet verifiable | Anything on the real hosted services (Render build, Neon, Upstash, Vercel routing). Run the [production verification checklist](#production-verification-checklist) after deploying | You (Claude can help with the results) |


## 0. Local Docker deployment

```bash
cp .env.example .env            # optional: compose already sets working local values
docker compose up -d --build    # postgres:17 + redis:8 + API (migrations + role seed on start)
curl http://localhost:8000/health
docker compose exec api pytest -q                       # unit + security tests (uses profexia_test DB, Redis db 15)
python scripts/smoke_test.py http://localhost:8000      # end-to-end (creates 3 throwaway users)
cd frontend && cp .env.example .env && npm ci && npm run dev   # http://localhost:5173
```

Locally all requests reach the API from the Docker gateway IP, so `docker-compose.yml` relaxes the **per-IP** rate limits to 1000 (the per-account login lockout keeps its default of 5). Production uses the stricter defaults.

Stop with `docker compose down`. **Never** use `docker compose down -v` unless you mean to delete the local database and Redis volumes. The test suite never writes to the development database: it creates/migrates a separate `<db>_test` database and refuses to run otherwise.

Recommended production order (it avoids a chicken-and-egg problem between the API URL and the frontend URL):

1. [Publish to GitHub](#1-publish-to-github)
2. [Neon PostgreSQL](#2-neon-postgresql)
3. [Upstash Redis](#3-upstash-redis)
4. [Render backend](#4-render-backend)
5. [Vercel frontend](#5-vercel-frontend)
6. [Connect CORS and verify](#6-connect-cors-and-verify)
7. [External providers](#7-external-providers-optional) (optional)

---

## 1. Publish to GitHub

From the repository root (`profexia/`):

```bash
git init -b main
git add .
git status --short                                   # review: no .env, *.zip, node_modules, .venv, dist
git ls-files --cached | grep -Ei '(^|/)\.env$|\.zip$|node_modules|\.venv/|/dist/'   # must print nothing
git commit -m "Initial commit: Profexia"
```

Create an **empty** repository on GitHub (no README/.gitignore), then:

```bash
git remote add origin https://github.com/<you>/profexia.git
git push -u origin main
```

Before the first push, double-check that no secrets are staged:

```bash
git ls-files | grep -Ei '(^|/)\.env$|\.pem$|\.key$|\.zip$'   # must print nothing
```

The **CI** workflow (`.github/workflows/ci.yml`) runs on every push and pull request:

- **backend** – installs dependencies, compiles/imports the app, runs `pytest`, checks there is a single Alembic head, runs `alembic upgrade head` twice against a fresh PostgreSQL 17 service (idempotency), seeds roles, starts the API and runs `scripts/smoke_test.py` against it (auth, CORS, courses, enrollment, WebSocket).
- **frontend** – `npm ci`, `npm run typecheck`, `npm run build` with a production `VITE_API_URL`, and fails if the bundle contains `localhost:8000`.
- **docker** – builds the backend image and validates `docker-compose.yml`.

CI never deploys. Render is configured with `autoDeployTrigger: checksPass`, so it only deploys commits whose checks passed. On Vercel, enable the same behaviour by requiring the CI checks in a GitHub branch protection rule for `main` (Settings → Branches) and merging through pull requests.

---

### Branch protection (GitHub)

Prevents untested code reaching `main` (Vercel deploys every push to `main`; Render waits for CI).

1. GitHub → **nithinprasad0/profexia → Settings → Branches** → **Add branch ruleset** (or **Add classic branch protection rule**).
2. Ruleset name `main`, **Enforcement status: Active**, **Target branches → Add target → Include default branch**.
3. Enable:
   - **Restrict deletions** and **Block force pushes**
   - **Require a pull request before merging** (required approvals: `0` if you work alone; GitHub does not let you approve your own PR)
   - **Require status checks to pass** → **Require branches to be up to date before merging** → **Add checks**, then add all four (they appear after CI has run at least once; names must match exactly):
     - `Backend (tests, migrations, smoke)`
     - `Frontend (typecheck, build)`
     - `Security (secrets, dependencies)`
     - `Docker image builds`
4. Leave **Bypass list** empty (or add yourself only for emergencies). **Create**.
5. From now on: work on a branch → push → open a PR → CI runs and Vercel posts a preview URL → merge when green. Render deploys `main` only after the checks pass.

## 2. Neon PostgreSQL

1. Sign up at https://neon.tech and **create a project**. Choose a region close to your Render region (e.g. *AWS Asia Pacific (Singapore)* to match `region: singapore` in `render.yaml`). Neon creates a default database (`neondb`) and owner role.
2. On the project dashboard click **Connect**. Select the branch (`main`), the database and role, and **turn connection pooling off** (direct connection). Copy the connection string. It looks like:

   ```text
   postgresql://neondb_owner:<password>@ep-xxxx-xxxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require
   ```

3. Use that value for `DATABASE_URL` as-is. The backend normalises it automatically (`app/core/config.py`):
   - `postgres://` / `postgresql://` → `postgresql+asyncpg://`
   - `sslmode=require` → `ssl=require` (asyncpg syntax)
   - `channel_binding=…` is dropped (not supported by asyncpg)

4. **Direct vs pooled:** prefer the **direct** connection string (host without `-pooler`). If you use the pooled host (`…-pooler…`), the backend detects it and disables asyncpg's prepared-statement cache, which PgBouncer transaction mode requires.

### Migrations against Neon

Migrations are **additive only** (`alembic upgrade head`). Nothing in this project drops tables or resets the database automatically.

- **Automatic (default):** the container entrypoint runs `alembic upgrade head` and the idempotent role seed on every start (`RUN_MIGRATIONS_ON_STARTUP=true`). Alembic records the applied revision in `alembic_version`, so re-runs are no-ops.
- **Manual (from your machine):**

  ```bash
  # bash
  export DATABASE_URL='postgresql://…neon.tech/neondb?sslmode=require'
  alembic upgrade head
  python -m app.infrastructure.seed
  alembic current        # expect: 0005_external_providers (head)
  ```

  ```powershell
  # PowerShell
  $env:DATABASE_URL = 'postgresql://…neon.tech/neondb?sslmode=require'
  alembic upgrade head
  python -m app.infrastructure.seed
  alembic current
  ```

- **As a release step (paid Render plans):** set `RUN_MIGRATIONS_ON_STARTUP=false` and add a Pre-Deploy Command `alembic upgrade head && python -m app.infrastructure.seed` in the Render dashboard.

Back up before risky changes: Neon supports branches/point-in-time restore — create a branch before running new migrations in production.

**Free-tier notes (check https://neon.tech/pricing for current limits):** small storage quota, compute that suspends when idle (the first query after idle takes a moment while it resumes; the backend's `pool_pre_ping` and `pool_recycle` handle dropped connections), and limited compute hours. Suitable for demos/portfolios, not for guaranteed production uptime.

---

## 3. Upstash Redis

Redis is **required** in production. It stores:

| Use | Keys / channel | If Redis is down |
| --- | --- | --- |
| Refresh tokens (single use, TTL) | `profexia:refresh:<sha256>` | login/refresh/register return **503** |
| Rate-limit counters | `profexia:rl:<scope>:<sha256>` | limiter fails open (logged) |
| Real-time fan-out between instances | pub/sub channel `REALTIME_CHANNEL` | each instance still serves its own sockets; cross-instance delivery resumes automatically |

Requirements: Redis >= 6.2 (uses `GETDEL`), pub/sub enabled, TLS (`rediss://`) for hosted providers. Use a dedicated logical database or instance per environment. `/health` reports `"redis": "disconnected"` when it cannot be reached.

1. Sign up at https://upstash.com → **Redis** → **Create Database**. Pick the region closest to Render (e.g. `ap-southeast-1`). TLS is always on.
2. Open the database → **Connect** → choose the plain **Redis** client URL (not the REST API). Reveal the password and copy the URL with **`rediss://`** (double *s*):

   ```text
   rediss://default:<password>@<name>-<id>.upstash.io:6379
   ```

3. Set it as `REDIS_URL`. redis-py enables TLS automatically for `rediss://`. A plain `redis://` URL will fail against Upstash.

**Free-tier notes (check https://upstash.com/pricing):** limits on commands per month and data size. Approximate usage: each login is about 5-7 commands (rate limits + token), a refresh about 4, each real-time event 1 `PUBLISH`, and each API instance's pub/sub listener sends a keep-alive `PING` every 30 s (about 86k commands/month per instance). If you run a single instance and want to save quota, set `REALTIME_BACKEND=memory`. Render's health check uses `/health/live`, which does **not** touch Redis.

---

## 4. Render backend

### Option A — Blueprint (recommended)

1. https://dashboard.render.com → **New → Blueprint** → connect your GitHub repo.
2. Render reads `render.yaml`:
   - `runtime: docker`, `dockerfilePath: ./Dockerfile`, `dockerContext: .` (repository root), `region: singapore` (cannot be changed later), `plan: free`
   - start command = Dockerfile `ENTRYPOINT` → `docker-entrypoint.sh` (migrations → seed → `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers`)
   - `healthCheckPath: /health/live`
   - `autoDeployTrigger: checksPass`
   - `JWT_SECRET_KEY` is generated by Render (`generateValue: true`)
3. Render prompts once for the three `sync: false` secrets:

   | Variable | Value |
   | --- | --- |
   | `DATABASE_URL` | Neon connection string (paste as-is, including `?sslmode=require…`) |
   | `REDIS_URL` | Upstash `rediss://default:<password>@<host>.upstash.io:6379` |
   | `CORS_ORIGINS` | your Vercel URL, e.g. `https://profexia.vercel.app` (decide the Vercel project name now; edit later if it differs) |

   Already set by the Blueprint: `ENVIRONMENT=production`, `JWT_SECRET_KEY` (generated), `TRUSTED_PROXY_COUNT=1`, `REALTIME_BACKEND=redis`, token lifetimes, `RUN_MIGRATIONS_ON_STARTUP=true`.
   Optional, add later under **Environment**: `CORS_ORIGIN_REGEX` (Vercel previews), `RAZORPAY_KEY_ID/SECRET/WEBHOOK_SECRET`, `JUDGE0_BASE_URL/API_KEY`, `LIVEKIT_URL/API_KEY/API_SECRET`. Unset providers simply return 503.

4. Deploy. The service URL looks like `https://profexia-api.onrender.com` (Render may add a suffix if the name is taken).

### Option B — manual web service

New → Web Service → your repo → Language **Docker**, Root Directory empty, Dockerfile path `./Dockerfile`, Health Check Path `/health/live`, then add the variables from [ENVIRONMENT.md](ENVIRONMENT.md) with `ENVIRONMENT=production`.

### Production safety checks

With `ENVIRONMENT=production` the API **refuses to start** (and logs why, without printing secrets) if:

- `JWT_SECRET_KEY` is the default or shorter than 32 characters,
- `CORS_ORIGINS` is empty, contains `*`, or contains an `http://` origin,
- `DATABASE_URL` / `REDIS_URL` still point at the docker-compose hosts.

Connection URLs are only ever logged with credentials redacted (`***:***@host`).

### Creating an admin

Public registration cannot create admins. Register normally in the app, then promote the account:

```bash
# from your machine, pointed at the production database
DATABASE_URL='postgresql://…neon.tech/neondb?sslmode=require' \
  python -m app.infrastructure.create_admin --email you@example.com
```

(On paid Render plans you can also run the same command from the service's **Shell** tab.)

### Render limitations and costs

- **Free** instances sleep after ~15 minutes without traffic; the next request has a cold start of up to about a minute, and open WebSocket connections are dropped when the instance sleeps. Free instances have a monthly usage cap. Use a paid instance (e.g. *Starter*) for anything users depend on. Check https://render.com/pricing.
- Multiple instances are supported for real-time features when `REALTIME_BACKEND=redis` (default): events published on one instance reach WebSocket clients on all instances. See [WebSocket deployment](#websocket-deployment).
- Pre-deploy commands and Shell access require a paid plan; this setup works without them.

---

## 5. Vercel frontend

1. https://vercel.com/new → import the GitHub repo.
2. **Root Directory:** `frontend` (important — the repository root is the backend).
3. Framework preset: **Vite** (also pinned in `frontend/vercel.json`): install `npm ci`, build `npm run build`, output `dist`.
4. **Environment Variables** (Production, and Preview if you use previews):

   | Variable | Value |
   | --- | --- |
   | `VITE_API_URL` | `https://profexia-api.onrender.com` (your Render URL; with or without `/api/v1`) |
   | `VITE_WS_URL` | optional; derived automatically as `wss://…/api/v1` |

5. Deploy. Vite inlines `VITE_*` at **build time** — after changing them, trigger a redeploy.

`frontend/vercel.json` provides:

- SPA fallback: every non-asset path is rewritten to `/index.html`, so direct navigation and refresh on `/app`, `/app/courses/12`, `/login`, etc. work.
- Long-term caching for hashed `/assets/*` files and basic security headers.

**Vercel limitations:** the Hobby plan is for personal, non-commercial use; commercial use needs Pro. See https://vercel.com/pricing.

---

## 6. Connect CORS and verify

### CORS

`CORS_ORIGINS` on Render must contain the **exact** origin the browser shows — scheme + host, no path, no trailing slash:

```text
CORS_ORIGINS=https://profexia.vercel.app,https://www.your-domain.com
```

Vercel **preview** deployments use changing URLs. To allow them, set a tight regex rather than a wildcard:

```text
CORS_ORIGIN_REGEX=^https://profexia-[a-z0-9-]+-<your-vercel-team>\.vercel\.app$
```

Development origins (`http://localhost:5173`, `http://127.0.0.1:5173`) are only allowed when `ENVIRONMENT` is not `production`.

### Verify

```bash
curl https://profexia-api.onrender.com/health
# {"status":"ok","database":"connected","redis":"connected"}

curl -i -X OPTIONS https://profexia-api.onrender.com/api/v1/auth/login \
  -H "Origin: https://profexia.vercel.app" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type"
# HTTP/2 200 + access-control-allow-origin: https://profexia.vercel.app

# Full end-to-end check (creates throwaway users and one course):
python scripts/smoke_test.py https://profexia-api.onrender.com --origin https://profexia.vercel.app
```

Then in the browser: register → land on `/app` → refresh the page (still signed in) → open a course → enroll → sign out.

---

### Rate limiting and client IPs

Login, registration, refresh and code execution are rate limited (thresholds in [ENVIRONMENT.md](ENVIRONMENT.md#rate-limiting)). The per-account login lock does not depend on IPs. The **per-IP** limits need the real client IP, which depends on the proxies in front of the API:

| Setting | Meaning |
| --- | --- |
| `CLIENT_IP_HEADER` | Read the IP from a header the edge sets itself (e.g. `cf-connecting-ip`). Safe only if the edge **always overwrites** it |
| `TRUSTED_PROXY_COUNT=N` | Use the N-th `X-Forwarded-For` entry from the right (entries further left are client-controlled) |
| neither | Use the TCP peer address (local/docker) |

Render serves `onrender.com` through Cloudflare, and reports suggest `X-Forwarded-For` arrives as `<client-sent>, <client>, <cloudflare-edge>`. If so, `TRUSTED_PROXY_COUNT=1` resolves to a **Cloudflare edge address shared by many users**. Do not guess: measure it with the diagnostic below.

**Measure (privacy-safe):**

1. Render → Environment → add `PROXY_DIAGNOSTICS=true` (Render redeploys).
2. Call the diagnostic normally and with forged headers (documentation addresses, RFC 5737). The response describes structure only: hop counts, address *kinds*, positions and true/false comparisons. It never contains an IP address.

   ```bash
   API=https://profexia-api.onrender.com
   curl -s $API/health/proxy | python -m json.tool
   curl -s $API/health/proxy -H "X-Forwarded-For: 203.0.113.7" \
     -H "CF-Connecting-IP: 203.0.113.8" -H "True-Client-IP: 203.0.113.9" | python -m json.tool
   ```

3. Read the result:

   | Observation | Conclusion |
   | --- | --- |
   | `ip_headers.cf-connecting-ip.kind` is `public` in **both** calls (never `test-net`) | Cloudflare overwrites it; `CLIENT_IP_HEADER=cf-connecting-ip` is safe and preferred |
   | `trusted_proxy_count_candidates.N.equals_cf_connecting_ip` is `true` for exactly one N, in both calls | `TRUSTED_PROXY_COUNT=N` would also work (fallback) |
   | `current_config.resolved_equals_cf_connecting_ip` is `false` | The current setting does **not** resolve to the client: change it |
   | a forged header shows `kind: test-net` | That header is client-controlled: never trust it |

4. Apply the conclusion in Render → Environment (e.g. `CLIENT_IP_HEADER=cf-connecting-ip`, `TRUSTED_PROXY_COUNT=<N>`), set `PROXY_DIAGNOSTICS=false`, and re-run step 2: the endpoint must return 404 and, before you disable it, `resolved_equals_cf_connecting_ip` must be `true`.

The old "forge X-Forwarded-For 35 times" probe is **not** sufficient: if the rightmost entry is a Cloudflare edge address, the key stays constant and the probe looks correct while all users behind that edge share one bucket.

### WebSocket deployment

- Clients connect to `wss://<api-host>/api/v1/ws/...` (the frontend derives `wss://` from an `https://` `VITE_API_URL`). Render passes WebSocket upgrades through without extra configuration.
- Authentication is `?token=<access token>`; tokens expire after `ACCESS_TOKEN_EXPIRE_MINUTES`, so clients must reconnect with a refreshed token.
- Idle connections may be closed by the platform; clients should reconnect with backoff and re-fetch history (`GET /api/v1/chat/{room}/messages`), because delivery is at-most-once.
- Frame size limit: `WS_MAX_SIZE_BYTES` (default 1 MiB).
- **Single vs multi-instance:**

  | Setting | Instances | Behaviour |
  | --- | --- | --- |
  | `REALTIME_BACKEND=redis` (default) | 1..N | events fan out through Redis pub/sub to every instance |
  | `REALTIME_BACKEND=memory` | **1 only** | no Redis traffic for real-time; with more instances, clients only receive events from their own instance |

  All instances must share the same `REDIS_URL` and `REALTIME_CHANNEL`. Uvicorn workers in one container (`UVICORN_EXTRA_ARGS=--workers 2`) count as separate instances and work the same way. Sticky sessions are not required.
- Render free instances sleep after about 15 minutes idle, which drops all WebSockets.

### Secret management

- Secrets live only in provider dashboards (Render and Vercel env vars) or a local git-ignored `.env`. Never in the repository, Docker images (`.dockerignore` excludes `.env*`), or `VITE_*` variables (those are public).
- `JWT_SECRET_KEY` is generated by Render (`generateValue`). Rotating it invalidates every access token; refresh tokens keep working, so users are silently re-authenticated.
- Rotate provider keys (Razorpay, Judge0, LiveKit) in the provider dashboard, then update the Render env vars and redeploy.
- The app logs connection URLs with credentials redacted and redacts `token=` query parameters. Do not add request-body logging.

### Health checks and troubleshooting

| Endpoint | Use |
| --- | --- |
| `GET /health/live` | liveness (Render health check); no dependency calls |
| `GET /health/ready` | 200 only when PostgreSQL and Redis are reachable, else 503 |
| `GET /health` | always 200; reports `database` / `redis` status |

| Symptom | Likely cause / fix |
| --- | --- |
| Service exits with `Refusing to start: invalid production configuration` | The log lists the problems: weak `JWT_SECRET_KEY`, missing or `http://` `CORS_ORIGINS`, local DB/Redis URLs |
| Browser shows a CORS error / preflight fails | `CORS_ORIGINS` must exactly match the page origin (scheme + host, no trailing slash); redeploy after editing |
| Login/refresh return 503 | Redis unreachable: `REDIS_URL` must use `rediss://` for Upstash; `/health` shows `redis: disconnected` |
| Users get 429 unexpectedly | Many users behind one IP (office/NAT), or a wrong client-IP setting making users share an edge/proxy address; run the [client-IP diagnostic](#rate-limiting-and-client-ips) |
| `prepared statement ... does not exist` | Neon `-pooler` host: the app disables the statement cache for hosts containing `-pooler`; otherwise use the direct host |
| Chat returns 400 | Room ids must be `course:<id>`, `session:<id>` or `dm:<low>:<high>` |
| WebSocket closes immediately with code 1008 | Missing/expired token, or the user is not a member of that room/session |
| Real-time events missing across instances | `REALTIME_BACKEND` is not `redis`, instances use different `REDIS_URL`/`REALTIME_CHANNEL`, or Redis is down (look for `Real-time Redis listener error` in the logs) |
| Migrations fail on start | Check `DATABASE_URL`; run `alembic upgrade head` manually to see the error. Migrations never drop data |

---

## 7. External providers (optional)

The API works without these; their endpoints return **503 "… is not configured"** until credentials are set.

### Razorpay

1. Dashboard → **Account & Settings → API Keys**. Use **Test mode** keys (`rzp_test_…`) until you have completed Razorpay activation; switch to **Live** keys (`rzp_live_…`) for real payments. Test and live mode are selected purely by which keys you configure.
2. Set `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`.
3. **Webhooks** → Add new webhook:
   - URL: `https://<render-url>/api/v1/payments/webhooks/razorpay` (Render serves HTTPS)
   - Secret: a random string → also set as `RAZORPAY_WEBHOOK_SECRET`
   - Events: `payment.captured`, `order.paid` (and `payment.failed` if you want failures recorded)
4. The webhook verifies `X-Razorpay-Signature` (HMAC-SHA256 over the raw body, constant-time compare) and rejects unsigned/invalid requests with 400.

Not yet verified against live Razorpay — see *Remaining issues* in the project report.

### Judge0

- Hosted (RapidAPI): `JUDGE0_BASE_URL=https://judge0-ce.p.rapidapi.com`, `JUDGE0_API_KEY=<RapidAPI key>`, `JUDGE0_AUTH_HEADER=X-RapidAPI-Key`. RapidAPI may additionally require an `X-RapidAPI-Host` header depending on the plan; self-hosting avoids that.
- Self-hosted: `JUDGE0_BASE_URL=https://judge0.your-domain`, `JUDGE0_AUTH_HEADER=X-Auth-Token`, `JUDGE0_API_KEY=<AUTHN token>`.
- Limits sent with every submission: `JUDGE0_CPU_TIME_LIMIT` (s, default 5), `JUDGE0_WALL_TIME_LIMIT` (s, default 10), `JUDGE0_MEMORY_LIMIT_KB` (default 128000); HTTP timeout `JUDGE0_TIMEOUT_SECONDS` (default 20). The Judge0 server's own maximums still apply.

### LiveKit

1. https://cloud.livekit.io → create a project → **Settings → Keys** → create an API key.
2. Set `LIVEKIT_URL=wss://<project>.livekit.cloud`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`.
3. `POST /api/v1/learning-sessions/{id}/livekit-token` returns `{livekit_url, room_name, token}`; the browser connects directly to LiveKit over WSS. The room is always `profexia-session-<id>`, derived on the server. Only the session host and admins can publish; learners get subscribe-only tokens. Ended or cancelled sessions are refused (409). Tokens are HS256 JWTs with `iss`=API key, `sub`=user id and a `video` grant without room-admin rights. The backend makes no LiveKit server API calls.

---

## Production deployment checklist

- [ ] `git ls-files` shows no `.env`, keys or zip archives
- [ ] CI is green on `main`
- [ ] Neon project created; `DATABASE_URL` set on Render
- [ ] Upstash database created; `REDIS_URL` uses `rediss://`
- [ ] Render service live; logs show `Applying Alembic migrations` and `Starting API`
- [ ] `alembic current` against Neon shows `0005_external_providers (head)`
- [ ] `ENVIRONMENT=production`, `JWT_SECRET_KEY` generated (≥ 32 chars)
- [ ] `CORS_ORIGINS` = exact Vercel/custom-domain origin(s), all `https://`
- [ ] Vercel root directory `frontend`; `VITE_API_URL` = Render URL; redeployed after setting it
- [ ] `GET /health` → `"status":"ok"`
- [ ] Browser: register, login, refresh on `/app/courses/:id`, enroll, logout all work
- [ ] `scripts/smoke_test.py` passes against production
- [ ] Admin account promoted with `create_admin`
- [ ] (If used) Razorpay webhook URL + secret configured; test payment in test mode
- [ ] (If used) Judge0 and LiveKit credentials set and exercised once
- [ ] Client IP verified with the diagnostic in [Rate limiting and client IPs](#rate-limiting-and-client-ips); `PROXY_DIAGNOSTICS` back to `false`
- [ ] `REALTIME_BACKEND=redis` if running more than one instance
- [ ] [Branch protection](#branch-protection-github) on `main` requires the four CI checks
- [ ] Plan tiers reviewed (free tiers sleep / have quotas)

---

## Production verification checklist

Run after the first deploy (and after major changes). Replace `API=https://<your-api>.onrender.com` and `WEB=https://<your-app>.vercel.app`. Nothing below is verified until you run it against the real services.

```bash
API=https://<your-api>.onrender.com
WEB=https://<your-app>.vercel.app
```

| # | Check | How | Expected |
| --- | --- | --- | --- |
| 1 | Public frontend loads | Open `$WEB` | Login page renders, no console errors |
| 2 | API health | `curl $API/health` and `curl -i $API/health/ready` | `{"status":"ok","database":"connected","redis":"connected"}`, 200 |
| 3 | Registration | Register a learner and a teacher in the UI | Lands on `/app`; admin is not offered |
| 4 | Login / logout | Sign out, sign in | Session cleared on logout; login returns to `/app` |
| 5 | Token refresh | In DevTools: `sessionStorage.setItem('profexia_access_token','x')`, reload | Page still loads; one `POST /auth/refresh` → 200 |
| 6 | Role restrictions | As learner open `$WEB/app/teach`; `curl -X POST $API/api/v1/courses -H "Authorization: Bearer <learner token>" -H "Content-Type: application/json" -d '{"title":"x"}'` | Redirected to `/app`; API 403 |
| 7 | Course creation | As teacher, Teach → Create course | Course appears in catalogue |
| 8 | Course discovery | As learner, search/filter the catalogue | New course listed |
| 9 | Enrollment | Enroll from the catalogue or course page | Progress panel appears |
| 10 | Progress persistence | Save 60%, reload | 60% shown |
| 11 | Assessment authorization | The smoke test's assessment checks: unenrolled learner → 403, question bank → 403, no answer key in attempts/results | All PASS |
| 12 | Chat authorization | Smoke test: member posts/reads `course:<id>`, outsider 403, `general` → 400 | All PASS |
| 13 | WebSocket connectivity | Smoke test WebSocket checks (uses `wss://`) | handshake PASS, invalid token rejected |
| 14 | Multi-instance real-time | Only if you scale to ≥ 2 instances: connect a chat socket, send via REST, repeat a few times | Every message received once (instances share Redis pub/sub) |
| 15 | CORS | `curl -s -o /dev/null -w "%{http_code}" -X OPTIONS $API/api/v1/auth/login -H "Origin: $WEB" -H "Access-Control-Request-Method: POST"` then with `-H "Origin: https://evil.example.com"` | 200, then 400 |
| 16 | Direct navigation / refresh | Open `$WEB/app/courses/1` directly and press F5; open `$WEB/does/not/exist` | App loads (no Vercel 404); unknown route redirects to `/app` |
| 17 | Rate limiting | Six wrong passwords for a throwaway email in the UI; run the [proxy probe](#rate-limiting-and-client-ips) | 6th attempt shows "Too many failed login attempts"; probe turns 401 → 429 after 30 |
| 18 | Production logs & secrets | Render → Logs: look for the startup line | `environment=production`, URLs shown as `***:***@host`, `token=[REDACTED]`, no passwords/keys anywhere; `curl -sI $API/health/live` shows `strict-transport-security` |

End-to-end API check (covers 2, 3, 4, 5, 6, 9, 11, 12, 13, 15; creates 4 throwaway accounts with a random password, never deletes data):

```bash
python scripts/smoke_test.py "$API" --origin "$WEB"
```

Note: the smoke test registers 4 accounts from your IP; the production limit is 10 registrations/hour per IP, so run it at most twice per hour.
