# Profexia Frontend — Phase 4.3 Learning UI

Phase 4.3 builds the learning experience against the finished backend API contract.

## Included

- Course catalog and search
- Skill filtering
- Teacher discovery
- Course details
- Enrollment
- Enrollment progress tracking
- Teacher/admin course creation
- Teacher/admin course editing for owned courses
- Protected learning routes
- Responsive learning workspace
- Existing Phase 4.1 API client and Phase 4.2 authentication preserved

## Routes

- `/app` — Learning hub
- `/app/courses/:courseId` — Course detail
- `/app/teach` — Teacher/admin learning workspace

## API contract used

- `GET /api/v1/courses`
- `GET /api/v1/courses/{course_id}`
- `POST /api/v1/courses`
- `PATCH /api/v1/courses/{course_id}`
- `POST /api/v1/courses/{course_id}/enroll`
- `GET /api/v1/enrollments/me`
- `PATCH /api/v1/enrollments/{enrollment_id}/progress`
- `GET /api/v1/skills`
- `GET /api/v1/teachers`

## Verification

Run:

```powershell
npm install
npm run typecheck
npm run build
npm run dev
```
