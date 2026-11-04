# Profexia — Phase 3.3

Phase 3.3 adds the first finished Learning API contract on top of Phase 3.2 authentication.

## Included
- Skills: list and admin create
- Teacher discovery: active teachers with profile data
- Courses: list, detail, teacher/admin create, teacher/admin update
- Course-to-skill associations
- Learner enrollment with duplicate protection
- Enrollment listing for the current learner
- Progress tracking from 0–100%
- Automatic enrollment status: `active` / `completed`
- Role enforcement: learner, teacher, admin
- Alembic migration `0002_learning_progress`
- Docker startup migration + role seed retained
- Existing Phase 3.2 authentication routes retained

## API contract
- GET/POST `/api/v1/skills`
- GET `/api/v1/teachers`
- GET/POST `/api/v1/courses`
- GET/PATCH `/api/v1/courses/{course_id}`
- POST `/api/v1/courses/{course_id}/enroll`
- GET `/api/v1/enrollments/me`
- PATCH `/api/v1/enrollments/{enrollment_id}/progress`

## Verification
From the project directory:

```powershell
docker compose up -d --build
docker compose exec api sh -c "PYTHONPATH=/app pytest -q"
docker compose exec api alembic current
```

Expected test result: all tests pass.

## Important
Do not run `docker compose down -v` unless you intentionally want to delete PostgreSQL data.
