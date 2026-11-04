# Profexia frontend

React 19 + TypeScript + Vite 7 + React Router 7 single-page app for Profexia.

## Develop

```bash
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
npm install
npm run dev                 # http://localhost:5173
```

The backend must be running at `http://localhost:8000` (see the root README — `docker compose up -d`).

## Scripts

| Command | What it does |
| --- | --- |
| `npm run dev` | Vite dev server on port 5173 |
| `npm run typecheck` | Type-checks app (`tsconfig.app.json`) and Vite config (`tsconfig.node.json`) |
| `npm run build` | Type-checks then builds to `dist/` |
| `npm run preview` | Serves `dist/` on port 4173 |

## Configuration

| Variable | Example | Notes |
| --- | --- | --- |
| `VITE_API_URL` | `https://profexia-api.onrender.com` | With or without `/api/v1`. Dev default `http://localhost:8000`. Production builds never fall back to localhost |
| `VITE_WS_URL` | `wss://profexia-api.onrender.com` | Optional; derived from `VITE_API_URL` (`https` → `wss`) |

Values are inlined at build time — rebuild/redeploy after changing them. Do not put secrets in `VITE_*` variables.

## Structure

```text
src/
├── api/
│   ├── config.ts       # API_URL / WS_URL resolution
│   ├── client.ts       # fetch wrapper, token store, 401 → refresh → retry, error formatting
│   ├── resources.ts    # typed clients: learning, assessment, realtime, providers
│   └── websocket.ts    # authenticated WebSocket helper
├── auth/AuthProvider.tsx
├── routes/guards.tsx   # ProtectedRoute, RoleRoute
├── pages/              # Login, Register, LearningDashboard (learn/teach), CourseDetail
└── types/api.ts
```

## Routes

| Path | Access |
| --- | --- |
| `/login`, `/register` | public |
| `/app` | signed in — learning hub (catalogue, search, skills, teachers, enrollments) |
| `/app/courses/:courseId` | signed in — course details, enroll, progress, edit (owner) |
| `/app/teach` | teacher/admin — teaching workspace |

## Deploy (Vercel)

Root Directory `frontend`, framework Vite, env `VITE_API_URL`. `vercel.json` rewrites all non-asset routes to `index.html`, so deep links and page refreshes work. Full guide: [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md#5-vercel-frontend).

Phase notes: [4.1 foundation](../docs/phases/README_PHASE_4_1.md), [4.3 learning UI](../docs/phases/README_PHASE_4_3.md).
