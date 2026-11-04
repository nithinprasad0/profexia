# Profexia — Phase 3.4 Assessment

This package is a complete replacement for the Phase 3.3 codebase and adds the assessment layer without changing the existing authentication or learning route architecture.

## Implemented

- Questions
- Tests and test-question assignment
- Learner attempts/submissions
- Automatic objective-question evaluation
- Results and question-wise marks
- Teacher/admin RBAC for assessment management
- Learner-only test attempts
- One finalized attempt per learner/test
- Alembic migration `0003_assessment`
- OpenAPI route coverage tests

## API contract

### Questions

- `GET /api/v1/courses/{course_id}/questions`
- `POST /api/v1/courses/{course_id}/questions`
- `GET /api/v1/questions/{question_id}`
- `PATCH /api/v1/questions/{question_id}`
- `DELETE /api/v1/questions/{question_id}`

### Tests

- `GET /api/v1/courses/{course_id}/tests`
- `POST /api/v1/courses/{course_id}/tests`
- `GET /api/v1/tests/{test_id}`
- `PATCH /api/v1/tests/{test_id}`
- `DELETE /api/v1/tests/{test_id}`

### Attempts and results

- `POST /api/v1/tests/{test_id}/attempts`
- `GET /api/v1/attempts/{attempt_id}`
- `POST /api/v1/attempts/{attempt_id}/submit`
- `GET /api/v1/results/{result_id}`
- `GET /api/v1/tests/{test_id}/results`

## Migration

Phase 3.4 adds:

- `questions`
- `tests`
- `test_questions`
- `submissions`
- `submission_answers`

The migration is `0003_assessment` and depends on `0002_learning_progress`.

## Docker verification

From the extracted project directory:

```powershell
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose exec api python -m app.infrastructure.seed
docker compose exec api sh -c "PYTHONPATH=/app pytest -q"
docker compose exec api alembic current
```

Expected Alembic head:

```text
0003_assessment (head)
```

Expected tests in this package:

```text
8 passed
```

The existing Phase 3.1 authentication and Phase 3.3 learning APIs remain part of the package.
