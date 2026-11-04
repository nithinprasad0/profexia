# API reference

Base path: `/api/v1`. Interactive documentation is served by the API itself at `/docs` (Swagger UI), `/redoc`, and `/openapi.json` — those are always in sync with the code; this page is a summary.

Authentication: `Authorization: Bearer <access_token>` unless marked *public*. Errors use FastAPI's shape: `{"detail": "message"}` or, for validation errors (422), `{"detail": [{"loc": [...], "msg": "..."}]}`.

Role legend: **L** learner, **T** teacher, **A** admin, **any** = any authenticated user.

Rate-limited endpoints return **429** with a `Retry-After` header. Security model: [SECURITY.md](../SECURITY.md).

## System

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| GET | `/` | public | Name and version |
| GET | `/health` | public | `{status: ok\|degraded, database, redis}` — always HTTP 200 |
| GET | `/health/live` | public | Liveness, no dependency checks (Render health check) |
| GET | `/health/ready` | public | 200 when PostgreSQL and Redis are reachable, otherwise 503 |

## Authentication — `/auth`

| Method | Path | Auth | Body | Notes |
| --- | --- | --- | --- | --- |
| POST | `/auth/register` | public | `{email, username, password (8-72 bytes), role?: "learner"\|"teacher"}` | 201 + token pair. `role: "admin"` → 403. Duplicate → 409. 10/hour per IP |
| POST | `/auth/login` | public | `{email, password}` | 200 + token pair; 401 on bad credentials; 403 if inactive; 429 after 5 failures per account (15 min) or 30 attempts per IP |
| POST | `/auth/refresh` | public | `{refresh_token}` | Rotates atomically: each refresh token works exactly once. 60/15 min per IP |
| POST | `/auth/logout` | public | `{refresh_token}` | 204; revokes the refresh token |
| GET | `/users/me` | any | – | Current user |

Token response:

```json
{
  "access_token": "eyJ…",
  "refresh_token": "…",
  "token_type": "bearer",
  "expires_in": 1800,
  "user": {"id": 1, "email": "a@b.com", "username": "alice", "is_active": true, "role": "learner"}
}
```

If Redis is unreachable, token-issuing endpoints return **503**.

## Learning

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| GET | `/skills` | public | List skills |
| POST | `/skills` | A | Create skill |
| GET | `/teachers` | public | List active teachers |
| GET | `/courses` | public | Course catalogue |
| POST | `/courses` | T, A | Create course `{title, description?, skill_ids?}` |
| GET | `/courses/{course_id}` | public | Course details |
| PATCH | `/courses/{course_id}` | T (owner), A | Update course |
| POST | `/courses/{course_id}/enroll` | L | Enroll (409 if already enrolled) |
| GET | `/enrollments/me` | L | Own enrollments |
| PATCH | `/enrollments/{enrollment_id}/progress` | L (owner) | `{progress_percent: 0-100}`; 100 marks completed |

## Assessment

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| GET | `/courses/{course_id}/questions` | T, A | Question bank (T: own questions). Learners → 403: they only see questions inside an attempt |
| POST | `/courses/{course_id}/questions` | T (owner), A | Create question |
| GET | `/questions/{question_id}` | T (owner), A | Read (includes explanation). Learners → 403 |
| PATCH / DELETE | `/questions/{question_id}` | T (owner), A | Update / delete |
| GET | `/courses/{course_id}/tests` | L (enrolled), T, A | L: published tests of enrolled courses (else 403); T: own tests; A: all |
| POST | `/courses/{course_id}/tests` | T (owner), A | Create test |
| GET | `/tests/{test_id}` | L (enrolled, published), T (owner), A | |
| PATCH / DELETE | `/tests/{test_id}` | T (owner), A | Published tests need at least one question |
| POST | `/tests/{test_id}/attempts` | L (enrolled) | Start attempt on a published test (one per learner). Not enrolled → 403 |
| GET | `/attempts/{attempt_id}` | owner L, test's T, A | Attempt with questions (no answers) |
| POST | `/attempts/{attempt_id}/submit` | L (owner) | `{answers: [{question_id, selected_option}]}` → result. After the time limit (+60 s) the attempt closes with score 0. Results never include answer keys or explanations |
| GET | `/results/{result_id}` | owner L, test's T, A | Result |
| GET | `/tests/{test_id}/results` | T (owner), A | All results for a test |

## Real-time

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| GET | `/chat/{room_id}/messages` | room member | Last 100 messages |
| POST | `/chat/{room_id}/messages` | room member | Send message (also broadcast on the room socket) |
| WS | `/ws/chat/{room_id}?token=…` | room member | Send `{"content": "..."}`; receive `{"event": "message", "data": {...}}`. Membership re-checked per message |

Chat `room_id` must be `course:<course_id>` (teacher + enrolled learners + admins), `session:<session_id>` (session participants) or `dm:<lower_user_id>:<higher_user_id>` (exactly those two users). Malformed → 400, not a member → 403 (also for non-existent rooms).

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| GET | `/notifications?unread_only=false` | any | Own notifications |
| POST | `/notifications` | A | Create notification (pushed to recipient socket) |
| PATCH | `/notifications/{id}/read` | recipient | Mark read |
| POST | `/notifications/read-all` | any | Mark all own as read |
| WS | `/ws/notifications?token=…` | any | Receives `{"event": "notification", ...}` |
| GET | `/learning-sessions?course_id=` | any | L: enrolled courses; T: hosted; A: all |
| POST | `/learning-sessions` | T (course owner), A | Create session |
| GET / PATCH | `/learning-sessions/{session_id}` | participants / host, A | |
| WS | `/ws/learning-sessions/{session_id}?token=…` | participants | Signalling relay: `{"event", "data"}` broadcast to other participants. Ended/cancelled sessions rejected |

WebSocket URLs: `wss://<api-host>/api/v1/ws/...` in production (`ws://` locally). Invalid/missing tokens close the socket with code **1008**.

## External providers

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| POST | `/payments/orders` | any | Create Razorpay order `{amount (paise), currency="INR", receipt?, notes?}` |
| POST | `/payments/webhooks/razorpay` | Razorpay signature | Verifies `X-Razorpay-Signature`; updates order status |
| POST | `/code-executions` | any | Submit `{language_id, source_code, stdin?}` to Judge0 (with CPU/memory limits). 30/hour per user |
| GET | `/code-executions/{id}` | owner, A | Result (refreshes from Judge0 while queued) |
| POST | `/learning-sessions/{session_id}/livekit-token` | host, enrolled L, A | `{room_name?, ttl_minutes=60}` → `{livekit_url, room_name, token, ...}`. Room is always `profexia-session-<id>` (other `room_name` → 403); closed session → 409; only host/admin can publish |

Unconfigured providers return **503** `"<Provider> is not configured"`; provider failures return **502** with a generic message (details are logged by type only).
