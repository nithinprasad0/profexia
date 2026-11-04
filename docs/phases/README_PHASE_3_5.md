# Profexia — Phase 3.5: Real-time

This replacement package builds on the verified Phase 3.4 assessment baseline.

## Included

- Persistent chat messages by room.
- WebSocket chat delivery.
- Persistent user notifications.
- Notification read/unread APIs.
- Notification WebSocket stream.
- Teacher/admin-managed learning sessions.
- Enrolled learner session access.
- Learning-session WebSocket signaling channel.
- Alembic migration `0004_realtime`.
- Route/OpenAPI regression tests.
- Docker Compose + Alembic entrypoint preserved.
- `PYTHONPATH=/app` configured in Compose for reliable pytest/import behavior.

## WebSocket contract

Authenticate WebSockets with the Phase 3 JWT access token:

- `GET /api/v1/ws/chat/{room_id}?token=<access_token>`
- `GET /api/v1/ws/notifications?token=<access_token>`
- `GET /api/v1/ws/learning-sessions/{session_id}?token=<access_token>`

Chat client sends:

```json
{"content":"Hello"}
```

Chat server broadcasts:

```json
{"event":"message","data":{...}}
```

Learning-session clients may exchange signaling payloads:

```json
{"event":"offer","data":{"sdp":"..."}}
{"event":"answer","data":{"sdp":"..."}}
{"event":"ice-candidate","data":{"candidate":"..."}}
```

The signaling channel is provider-neutral; LiveKit integration belongs to the external-provider phase.

## Apply

```powershell
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose exec api python -m app.infrastructure.seed
docker compose exec api sh -c "PYTHONPATH=/app pytest -q"
docker compose exec api alembic current
```

Expected migration head:

```text
0004_realtime
```

If the API is stopped:

```powershell
docker compose up -d api
```

Then rerun the migration command.

## Phase boundary

Phase 3.5 intentionally does not add Razorpay, Judge0, LiveKit SDK integration, or frontend implementation. Those are kept behind the API contract so the React/TypeScript frontend can be implemented against stable endpoints.
