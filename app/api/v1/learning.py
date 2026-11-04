from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.deps import require_roles
from app.core.database import get_db
from app.domain.models import Course, Enrollment, Profile, Role, Skill, User, course_skills
from app.schemas.learning import (
    CourseCreate, CourseResponse, CourseUpdate, EnrollmentResponse,
    ProgressUpdate, SkillCreate, SkillResponse, TeacherResponse,
)
from app.api.v1.users import get_current_user

router = APIRouter(prefix="/api/v1", tags=["learning"])

async def _course_response(session: AsyncSession, course: Course) -> CourseResponse:
    teacher = await session.scalar(select(User).where(User.id == course.teacher_id))
    skill_rows = await session.execute(
        select(Skill).join(course_skills, Skill.id == course_skills.c.skill_id)
        .where(course_skills.c.course_id == course.id).order_by(Skill.name)
    )
    return CourseResponse(
        id=course.id, title=course.title, description=course.description,
        teacher_id=course.teacher_id, teacher_username=teacher.username,
        skills=[SkillResponse.model_validate(s) for s in skill_rows.scalars().all()],
    )

@router.get("/skills", response_model=list[SkillResponse])
async def list_skills(session: AsyncSession = Depends(get_db)):
    result = await session.execute(select(Skill).order_by(Skill.name))
    return result.scalars().all()

@router.post("/skills", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
async def create_skill(payload: SkillCreate, session: AsyncSession = Depends(get_db), user: User = Depends(require_roles("admin"))):
    existing = await session.scalar(select(Skill).where(Skill.name == payload.name.strip()))
    if existing:
        raise HTTPException(status_code=409, detail="Skill already exists")
    skill = Skill(name=payload.name.strip(), description=payload.description)
    session.add(skill)
    await session.commit(); await session.refresh(skill)
    return skill

@router.get("/teachers", response_model=list[TeacherResponse])
async def list_teachers(session: AsyncSession = Depends(get_db)):
    result = await session.execute(
        select(User, Profile).join(Role, User.role_id == Role.id)
        .outerjoin(Profile, Profile.user_id == User.id)
        .where(Role.name == "teacher", User.is_active.is_(True)).order_by(User.username)
    )
    return [TeacherResponse(id=u.id, username=u.username, display_name=p.display_name if p else None, bio=p.bio if p else None) for u,p in result.all()]

@router.get("/courses", response_model=list[CourseResponse])
async def list_courses(session: AsyncSession = Depends(get_db)):
    courses = (await session.execute(select(Course).order_by(Course.id.desc()))).scalars().all()
    return [await _course_response(session, c) for c in courses]

@router.post("/courses", response_model=CourseResponse, status_code=status.HTTP_201_CREATED)
async def create_course(payload: CourseCreate, session: AsyncSession = Depends(get_db), user: User = Depends(require_roles("teacher", "admin"))):
    skills = []
    if payload.skill_ids:
        skills = (await session.execute(select(Skill).where(Skill.id.in_(payload.skill_ids)))).scalars().all()
        if len(skills) != len(set(payload.skill_ids)):
            raise HTTPException(status_code=400, detail="One or more skills do not exist")
    course = Course(title=payload.title, description=payload.description, teacher_id=user.id)
    session.add(course); await session.flush()
    if skills:
        await session.execute(course_skills.insert(), [{"course_id": course.id, "skill_id": s.id} for s in skills])
    await session.commit(); await session.refresh(course)
    return await _course_response(session, course)

@router.get("/courses/{course_id}", response_model=CourseResponse)
async def get_course(course_id: int, session: AsyncSession = Depends(get_db)):
    course = await session.get(Course, course_id)
    if not course: raise HTTPException(status_code=404, detail="Course not found")
    return await _course_response(session, course)

@router.patch("/courses/{course_id}", response_model=CourseResponse)
async def update_course(course_id: int, payload: CourseUpdate, session: AsyncSession = Depends(get_db), user: User = Depends(require_roles("teacher", "admin"))):
    course = await session.get(Course, course_id)
    if not course: raise HTTPException(status_code=404, detail="Course not found")
    if user.role.name != "admin" and course.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Only the course teacher or admin can modify this course")
    if payload.title is not None: course.title = payload.title
    if payload.description is not None: course.description = payload.description
    if payload.skill_ids is not None:
        skills = (await session.execute(select(Skill).where(Skill.id.in_(payload.skill_ids)))).scalars().all() if payload.skill_ids else []
        if len(skills) != len(set(payload.skill_ids)):
            raise HTTPException(status_code=400, detail="One or more skills do not exist")
        await session.execute(delete(course_skills).where(course_skills.c.course_id == course.id))
        if skills: await session.execute(course_skills.insert(), [{"course_id": course.id, "skill_id": s.id} for s in skills])
    await session.commit(); await session.refresh(course)
    return await _course_response(session, course)

@router.post("/courses/{course_id}/enroll", response_model=EnrollmentResponse, status_code=status.HTTP_201_CREATED)
async def enroll(course_id: int, session: AsyncSession = Depends(get_db), user: User = Depends(require_roles("learner"))):
    if not await session.get(Course, course_id): raise HTTPException(status_code=404, detail="Course not found")
    existing = await session.scalar(select(Enrollment).where(Enrollment.course_id == course_id, Enrollment.learner_id == user.id))
    if existing: raise HTTPException(status_code=409, detail="Already enrolled")
    enrollment = Enrollment(course_id=course_id, learner_id=user.id, progress_percent=0, status="active")
    session.add(enrollment); await session.commit(); await session.refresh(enrollment)
    return enrollment

@router.get("/enrollments/me", response_model=list[EnrollmentResponse])
async def my_enrollments(session: AsyncSession = Depends(get_db), user: User = Depends(require_roles("learner"))):
    result = await session.execute(select(Enrollment).where(Enrollment.learner_id == user.id).order_by(Enrollment.id.desc()))
    return result.scalars().all()

@router.patch("/enrollments/{enrollment_id}/progress", response_model=EnrollmentResponse)
async def update_progress(enrollment_id: int, payload: ProgressUpdate, session: AsyncSession = Depends(get_db), user: User = Depends(require_roles("learner"))):
    enrollment = await session.get(Enrollment, enrollment_id)
    if not enrollment or enrollment.learner_id != user.id: raise HTTPException(status_code=404, detail="Enrollment not found")
    enrollment.progress_percent = payload.progress_percent
    enrollment.status = "completed" if payload.progress_percent == 100 else "active"
    await session.commit(); await session.refresh(enrollment)
    return enrollment
