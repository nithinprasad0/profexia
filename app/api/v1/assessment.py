from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.access import is_enrolled
from app.api.v1.deps import require_roles
from app.api.v1.users import get_current_user
from app.core.database import get_db
from app.domain.models import Course, Question, Submission, SubmissionAnswer, Test, User, test_questions
from app.schemas.assessment import (
    AnswerInput, AttemptResponse, QuestionCreate, QuestionResponse, QuestionUpdate,
    ResultAnswer, ResultResponse, SubmitRequest, TestCreate, TestQuestionResponse,
    TestResponse, TestUpdate,
)

router = APIRouter(prefix="/api/v1", tags=["assessment"])

# Submissions arriving this long after the time limit are still graded (network latency).
ATTEMPT_GRACE_SECONDS = 60
QUESTION_BANK_FORBIDDEN = "Questions are only available to learners through a test attempt"


async def _require_learner_enrollment(session: AsyncSession, course_id: int, user: User) -> None:
    """Learners may only see or take assessments of courses they are enrolled in."""
    if user.role.name == "learner" and not await is_enrolled(session, course_id, user.id):
        raise HTTPException(status_code=403, detail="You must be enrolled in this course to access its assessments")


def _question_response(q: Question) -> QuestionResponse:
    return QuestionResponse.model_validate(q)


async def _question_ids(session: AsyncSession, test_id: int) -> list[int]:
    rows = await session.execute(
        select(test_questions.c.question_id)
        .where(test_questions.c.test_id == test_id)
        .order_by(test_questions.c.question_id)
    )
    return list(rows.scalars().all())


async def _test_response(session: AsyncSession, test: Test) -> TestResponse:
    return TestResponse(
        id=test.id, course_id=test.course_id, teacher_id=test.teacher_id,
        title=test.title, description=test.description,
        time_limit_minutes=test.time_limit_minutes, max_marks=test.max_marks,
        is_published=test.is_published, question_ids=await _question_ids(session, test.id),
        created_at=test.created_at, updated_at=test.updated_at,
    )


async def _validate_course_teacher(session: AsyncSession, course_id: int, user: User) -> Course:
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")
    if user.role.name != "admin" and course.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Only the course teacher or admin can manage assessment")
    return course


async def _validate_question_ids(session: AsyncSession, course_id: int, ids: list[int]) -> list[Question]:
    unique_ids = list(dict.fromkeys(ids))
    if not unique_ids:
        return []
    rows = await session.execute(select(Question).where(Question.id.in_(unique_ids), Question.course_id == course_id))
    questions = rows.scalars().all()
    if len(questions) != len(unique_ids):
        raise HTTPException(status_code=400, detail="One or more questions do not belong to this course")
    by_id = {q.id: q for q in questions}
    return [by_id[qid] for qid in unique_ids]


@router.get("/courses/{course_id}/questions", response_model=list[QuestionResponse])
async def list_questions(course_id: int, session: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role.name not in ("teacher", "admin"):
        raise HTTPException(status_code=403, detail=QUESTION_BANK_FORBIDDEN)
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")
    stmt = select(Question).where(Question.course_id == course_id)
    if user.role.name == "teacher":
        stmt = stmt.where(Question.teacher_id == user.id)
    rows = await session.execute(stmt.order_by(Question.id))
    return rows.scalars().all()


@router.post("/courses/{course_id}/questions", response_model=QuestionResponse, status_code=status.HTTP_201_CREATED)
async def create_question(course_id: int, payload: QuestionCreate, session: AsyncSession = Depends(get_db), user: User = Depends(require_roles("teacher", "admin"))):
    course = await _validate_course_teacher(session, course_id, user)
    question = Question(course_id=course.id, teacher_id=course.teacher_id, **payload.model_dump())
    session.add(question)
    await session.commit(); await session.refresh(question)
    return question


@router.get("/questions/{question_id}", response_model=QuestionResponse)
async def get_question(question_id: int, session: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role.name not in ("teacher", "admin"):
        raise HTTPException(status_code=403, detail=QUESTION_BANK_FORBIDDEN)
    question = await session.get(Question, question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    if user.role.name == "teacher" and question.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Question is not owned by this teacher")
    return question


@router.patch("/questions/{question_id}", response_model=QuestionResponse)
async def update_question(question_id: int, payload: QuestionUpdate, session: AsyncSession = Depends(get_db), user: User = Depends(require_roles("teacher", "admin"))):
    question = await session.get(Question, question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    if user.role.name != "admin" and question.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Question is not owned by this teacher")
    data = payload.model_dump(exclude_unset=True)
    options = data.get("options", question.options)
    correct = data.get("correct_option", question.correct_option)
    if correct >= len(options):
        raise HTTPException(status_code=400, detail="correct_option must reference an existing option")
    for key, value in data.items():
        setattr(question, key, value)
    await session.commit(); await session.refresh(question)
    return question


@router.delete("/questions/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_question(question_id: int, session: AsyncSession = Depends(get_db), user: User = Depends(require_roles("teacher", "admin"))):
    question = await session.get(Question, question_id)
    if question is None:
        raise HTTPException(status_code=404, detail="Question not found")
    if user.role.name != "admin" and question.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Question is not owned by this teacher")
    question.is_active = False
    await session.commit()


@router.get("/courses/{course_id}/tests", response_model=list[TestResponse])
async def list_tests(course_id: int, session: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    if await session.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="Course not found")
    await _require_learner_enrollment(session, course_id, user)
    stmt = select(Test).where(Test.course_id == course_id)
    if user.role.name == "learner":
        stmt = stmt.where(Test.is_published.is_(True))
    elif user.role.name == "teacher":
        stmt = stmt.where(Test.teacher_id == user.id)
    rows = await session.execute(stmt.order_by(Test.id.desc()))
    return [await _test_response(session, t) for t in rows.scalars().all()]


@router.post("/courses/{course_id}/tests", response_model=TestResponse, status_code=status.HTTP_201_CREATED)
async def create_test(course_id: int, payload: TestCreate, session: AsyncSession = Depends(get_db), user: User = Depends(require_roles("teacher", "admin"))):
    course = await _validate_course_teacher(session, course_id, user)
    questions = await _validate_question_ids(session, course_id, payload.question_ids)
    if payload.is_published and not questions:
        raise HTTPException(status_code=400, detail="A published test must contain at least one question")
    test = Test(
        course_id=course.id, teacher_id=course.teacher_id, title=payload.title,
        description=payload.description, time_limit_minutes=payload.time_limit_minutes,
        is_published=payload.is_published, max_marks=sum(q.marks for q in questions),
    )
    session.add(test); await session.flush()
    if questions:
        await session.execute(test_questions.insert(), [{"test_id": test.id, "question_id": q.id} for q in questions])
    await session.commit(); await session.refresh(test)
    return await _test_response(session, test)


@router.get("/tests/{test_id}", response_model=TestResponse)
async def get_test(test_id: int, session: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    test = await session.get(Test, test_id)
    if test is None or (user.role.name == "learner" and not test.is_published):
        raise HTTPException(status_code=404, detail="Test not found")
    if user.role.name == "teacher" and test.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Test is not owned by this teacher")
    await _require_learner_enrollment(session, test.course_id, user)
    return await _test_response(session, test)


@router.patch("/tests/{test_id}", response_model=TestResponse)
async def update_test(test_id: int, payload: TestUpdate, session: AsyncSession = Depends(get_db), user: User = Depends(require_roles("teacher", "admin"))):
    test = await session.get(Test, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Test not found")
    if user.role.name != "admin" and test.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Test is not owned by this teacher")
    data = payload.model_dump(exclude_unset=True)
    if "question_ids" in data:
        questions = await _validate_question_ids(session, test.course_id, data.pop("question_ids") or [])
        if data.get("is_published", test.is_published) and not questions:
            raise HTTPException(status_code=400, detail="A published test must contain at least one question")
        await session.execute(delete(test_questions).where(test_questions.c.test_id == test.id))
        if questions:
            await session.execute(test_questions.insert(), [{"test_id": test.id, "question_id": q.id} for q in questions])
        test.max_marks = sum(q.marks for q in questions)
    if "is_published" in data and data["is_published"] and test.max_marks == 0:
        raise HTTPException(status_code=400, detail="A published test must contain at least one question")
    for key, value in data.items():
        setattr(test, key, value)
    await session.commit(); await session.refresh(test)
    return await _test_response(session, test)


@router.delete("/tests/{test_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_test(test_id: int, session: AsyncSession = Depends(get_db), user: User = Depends(require_roles("teacher", "admin"))):
    test = await session.get(Test, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Test not found")
    if user.role.name != "admin" and test.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Test is not owned by this teacher")
    await session.delete(test); await session.commit()


@router.post("/tests/{test_id}/attempts", response_model=AttemptResponse, status_code=status.HTTP_201_CREATED)
async def start_attempt(test_id: int, session: AsyncSession = Depends(get_db), user: User = Depends(require_roles("learner"))):
    test = await session.get(Test, test_id)
    if test is None or not test.is_published:
        raise HTTPException(status_code=404, detail="Test not found")
    # Enrollment is enforced by the backend, so direct API calls cannot bypass it.
    await _require_learner_enrollment(session, test.course_id, user)
    existing = await session.scalar(select(Submission).where(Submission.test_id == test_id, Submission.learner_id == user.id))
    if existing is not None:
        raise HTTPException(status_code=409, detail="An attempt already exists for this test")
    ids = await _question_ids(session, test_id)
    questions = (await session.execute(select(Question).where(Question.id.in_(ids), Question.is_active.is_(True)))).scalars().all() if ids else []
    if len(questions) != len(ids):
        raise HTTPException(status_code=409, detail="Test contains inactive or missing questions")
    submission = Submission(test_id=test_id, learner_id=user.id, status="in_progress")
    session.add(submission); await session.commit(); await session.refresh(submission)
    return AttemptResponse(
        id=submission.id, test_id=test.id, learner_id=user.id, started_at=submission.started_at,
        submitted_at=None, status=submission.status,
        questions=[TestQuestionResponse(id=q.id, text=q.text, options=q.options, marks=q.marks) for q in sorted(questions, key=lambda x: ids.index(x.id))],
    )


@router.get("/attempts/{attempt_id}", response_model=AttemptResponse)
async def get_attempt(attempt_id: int, session: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    attempt = await session.get(Submission, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if user.role.name == "learner" and attempt.learner_id != user.id:
        raise HTTPException(status_code=403, detail="Attempt is not owned by this learner")
    if user.role.name == "teacher":
        test = await session.get(Test, attempt.test_id)
        if test is None or test.teacher_id != user.id:
            raise HTTPException(status_code=403, detail="Attempt is not for this teacher's test")
    ids = await _question_ids(session, attempt.test_id)
    questions = (await session.execute(select(Question).where(Question.id.in_(ids)))).scalars().all() if ids else []
    return AttemptResponse(
        id=attempt.id, test_id=attempt.test_id, learner_id=attempt.learner_id, started_at=attempt.started_at,
        submitted_at=attempt.submitted_at, status=attempt.status,
        questions=[TestQuestionResponse(id=q.id, text=q.text, options=q.options, marks=q.marks) for q in sorted(questions, key=lambda x: ids.index(x.id))],
    )


@router.post("/attempts/{attempt_id}/submit", response_model=ResultResponse)
async def submit_attempt(attempt_id: int, payload: SubmitRequest, session: AsyncSession = Depends(get_db), user: User = Depends(require_roles("learner"))):
    attempt = await session.get(Submission, attempt_id)
    if attempt is None or attempt.learner_id != user.id:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if attempt.status == "submitted":
        raise HTTPException(status_code=409, detail="Attempt has already been submitted")
    test = await session.get(Test, attempt.test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Test not found")
    ids = await _question_ids(session, test.id)
    questions = (await session.execute(select(Question).where(Question.id.in_(ids)))).scalars().all() if ids else []
    question_map = {q.id: q for q in questions}
    # Enforce the time limit server-side. A late submission closes the attempt
    # with no answers counted, so the attempt cannot be held open indefinitely.
    if test.time_limit_minutes:
        started = attempt.started_at if attempt.started_at.tzinfo else attempt.started_at.replace(tzinfo=timezone.utc)
        deadline = started + timedelta(minutes=test.time_limit_minutes, seconds=ATTEMPT_GRACE_SECONDS)
        if datetime.now(timezone.utc) > deadline:
            payload = SubmitRequest(answers=[])
    submitted_ids = {a.question_id for a in payload.answers}
    if not submitted_ids.issubset(set(ids)):
        raise HTTPException(status_code=400, detail="One or more answers do not belong to this test")
    if len(submitted_ids) != len(payload.answers):
        raise HTTPException(status_code=400, detail="Duplicate question answers are not allowed")
    score = 0
    for answer in payload.answers:
        q = question_map[answer.question_id]
        if answer.selected_option is not None and answer.selected_option >= len(q.options):
            raise HTTPException(status_code=400, detail=f"Invalid option for question {q.id}")
        correct = answer.selected_option == q.correct_option
        marks = q.marks if correct else 0
        score += marks
        session.add(SubmissionAnswer(
            submission_id=attempt.id, question_id=q.id,
            selected_option=answer.selected_option, is_correct=correct, marks_awarded=marks,
        ))
    max_score = sum(q.marks for q in questions)
    percentage = round((score / max_score) * 100, 2) if max_score else 0.0
    attempt.status = "submitted"
    attempt.submitted_at = datetime.now(timezone.utc)
    attempt.score = score
    attempt.max_score = max_score
    attempt.percentage = percentage
    attempt.passed = percentage >= 50.0
    await session.commit(); await session.refresh(attempt)
    return await _result_response(session, attempt)


async def _result_response(session: AsyncSession, attempt: Submission) -> ResultResponse:
    rows = await session.execute(select(SubmissionAnswer).where(SubmissionAnswer.submission_id == attempt.id).order_by(SubmissionAnswer.id))
    return ResultResponse(
        id=attempt.id, test_id=attempt.test_id, learner_id=attempt.learner_id,
        submission_id=attempt.id, score=attempt.score or 0, max_score=attempt.max_score or 0,
        percentage=attempt.percentage or 0.0, passed=bool(attempt.passed),
        submitted_at=attempt.submitted_at, answers=[
            ResultAnswer(question_id=a.question_id, selected_option=a.selected_option, correct=a.is_correct, marks_awarded=a.marks_awarded)
            for a in rows.scalars().all()
        ],
    )


@router.get("/results/{result_id}", response_model=ResultResponse)
async def get_result(result_id: int, session: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    attempt = await session.get(Submission, result_id)
    if attempt is None or attempt.status != "submitted":
        raise HTTPException(status_code=404, detail="Result not found")
    if user.role.name == "learner" and attempt.learner_id != user.id:
        raise HTTPException(status_code=403, detail="Result is not owned by this learner")
    if user.role.name == "teacher":
        test = await session.get(Test, attempt.test_id)
        if test is None or test.teacher_id != user.id:
            raise HTTPException(status_code=403, detail="Result is not for this teacher's test")
    return await _result_response(session, attempt)


@router.get("/tests/{test_id}/results", response_model=list[ResultResponse])
async def list_results(test_id: int, session: AsyncSession = Depends(get_db), user: User = Depends(require_roles("teacher", "admin"))):
    test = await session.get(Test, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Test not found")
    if user.role.name != "admin" and test.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Test is not owned by this teacher")
    rows = await session.execute(select(Submission).where(Submission.test_id == test_id, Submission.status == "submitted").order_by(Submission.id.desc()))
    return [await _result_response(session, attempt) for attempt in rows.scalars().all()]
