# Profexia — Phase 4.1 Frontend Foundation

This frontend is designed against the completed Phase 3.6 API contract.

## Stack

- React
- TypeScript
- Vite
- React Router
- Native Fetch API
- Native WebSocket API

## Start

```bash
npm install
copy .env.example .env
npm run dev
```

Linux/macOS:

```bash
cp .env.example .env
npm install
npm run dev
```

Default API:

```text
http://localhost:8000/api/v1
```

## Validation

```bash
npm run typecheck
npm run build
```

## Authentication

The API client supports:

- register
- login
- refresh token
- logout
- `/users/me`
- automatic 401 refresh
- retry of the original request
- clearing tokens when refresh fails

Tokens are stored in `sessionStorage` for this foundation phase. Move to a production cookie/BFF strategy before production deployment if the security architecture requires it.

## WebSockets

The client supports the Phase 3.5 endpoints:

- `/ws/chat/{room_id}`
- `/ws/notifications`
- `/ws/learning-sessions/{session_id}`

The token is passed using the backend's current `?token=` WebSocket contract.

## Resource clients

Typed resource clients are prepared for:

- learning
- assessment
- realtime
- Razorpay
- Judge0
- LiveKit

Phase 4.2 should build the complete authentication UX on top of this foundation.