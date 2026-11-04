# Profexia — Phase 3.6: External Providers

Phase 3.6 adds provider adapters and API contracts for:

- Razorpay: payment order creation and signed webhook verification
- Judge0: code execution submission and result refresh
- LiveKit: learning-session access-token generation
- Provider integration records in PostgreSQL
- Configuration through environment variables
- Provider errors mapped to safe API responses
- Tests that do not require real provider credentials

## Architecture

The API layer calls small provider clients under `app/infrastructure/providers/`.
Provider-specific HTTP/JWT details stay outside the domain models and routes as much as practical.

### Razorpay
`POST /api/v1/payments/orders`
`POST /api/v1/payments/webhooks/razorpay`

Amounts are sent in the provider's smallest currency unit (for INR, paise).

### Judge0
`POST /api/v1/code-executions`
`GET /api/v1/code-executions/{execution_id}`

The service stores the local execution record and the Judge0 submission token.

### LiveKit
`POST /api/v1/learning-sessions/{session_id}/livekit-token`

Learners must be enrolled in the course. Teachers/admins can publish; learners can subscribe.

## Environment

Set these in `.env` for real integrations:

```text
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
RAZORPAY_WEBHOOK_SECRET=
RAZORPAY_BASE_URL=https://api.razorpay.com/v1

JUDGE0_BASE_URL=
JUDGE0_API_KEY=
JUDGE0_AUTH_HEADER=X-RapidAPI-Key
JUDGE0_WAIT=true

LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=
LIVEKIT_URL=
```

Do not commit provider secrets.

## Verification

```powershell
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose exec api python -m app.infrastructure.seed
docker compose exec api sh -c "PYTHONPATH=/app pytest -q"
docker compose exec api alembic current
```

Expected migration head:

```text
0005_external_providers (head)
```

Real provider calls require valid credentials and reachable provider endpoints. The automated tests are intentionally credential-free.


## Fixed build
The `PaymentOrder` SQLAlchemy model maps the database column `metadata` to the Python attribute `extra_metadata`, avoiding SQLAlchemy Declarative API's reserved `metadata` attribute name.


## Phase 3.6 v2 fixes
- Registered the external-provider router in `app/main.py`, so provider endpoints appear in runtime routes and OpenAPI.
- Fixed SQLAlchemy reserved `metadata` access by using the model attribute `extra_metadata` while preserving the API response field name `metadata`.
