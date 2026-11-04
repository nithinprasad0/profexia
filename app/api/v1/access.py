"""Server-side authorization helpers shared by REST and WebSocket endpoints.

Every check derives membership from database records (courses, enrollments,
learning sessions, users). Nothing the client sends (room ids, user ids, room
names) is trusted on its own.
"""

import re

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Course, Enrollment, LearningSession, User

ACTIVE_ENROLLMENT_STATUSES = ("active", "completed")
CLOSED_SESSION_STATUSES = ("ended", "cancelled")

CHAT_ROOM_PATTERN = re.compile(r"^(?:course:(?P<course>\d{1,10})|session:(?P<session>\d{1,10})|dm:(?P<a>\d{1,10}):(?P<b>\d{1,10}))$")
CHAT_ROOM_HELP = "Invalid chat room id. Use course:<course_id>, session:<session_id> or dm:<lower_user_id>:<higher_user_id>"


class ChatRoomInvalid(Exception):
    pass


class ChatAccessDenied(Exception):
    pass


async def is_enrolled(session: AsyncSession, course_id: int, learner_id: int) -> bool:
    enrollment = await session.scalar(
        select(Enrollment.id).where(
            Enrollment.course_id == course_id,
            Enrollment.learner_id == learner_id,
            Enrollment.status.in_(ACTIVE_ENROLLMENT_STATUSES),
        )
    )
    return enrollment is not None


async def can_access_course_members_area(session: AsyncSession, course: Course, user: User) -> bool:
    """Course teacher, admins, or learners actively enrolled in the course."""
    if user.role.name == "admin" or course.teacher_id == user.id:
        return True
    if user.role.name == "learner":
        return await is_enrolled(session, course.id, user.id)
    return False


async def can_access_learning_session(session: AsyncSession, obj: LearningSession, user: User) -> bool:
    if user.role.name == "admin" or obj.host_id == user.id:
        return True
    if user.role.name == "learner":
        return await is_enrolled(session, obj.course_id, user.id)
    return False


async def authorize_chat_room(session: AsyncSession, room_id: str, user: User) -> None:
    """Raise ChatRoomInvalid / ChatAccessDenied unless `user` belongs to `room_id`.

    Unknown or non-existent rooms are reported exactly like forbidden ones, so
    guessing room ids reveals nothing about which courses/sessions/users exist.
    """
    match = CHAT_ROOM_PATTERN.fullmatch(room_id or "")
    if match is None:
        raise ChatRoomInvalid()

    if match["course"]:
        course = await session.get(Course, int(match["course"]))
        if course is None or not await can_access_course_members_area(session, course, user):
            raise ChatAccessDenied()
        return

    if match["session"]:
        obj = await session.get(LearningSession, int(match["session"]))
        if obj is None or not await can_access_learning_session(session, obj, user):
            raise ChatAccessDenied()
        return

    a, b = int(match["a"]), int(match["b"])
    if not a < b:  # canonical form prevents duplicate rooms for the same pair
        raise ChatRoomInvalid()
    if user.id not in (a, b):
        raise ChatAccessDenied()
    other_id = b if user.id == a else a
    other = await session.scalar(select(User.id).where(User.id == other_id, User.is_active.is_(True)))
    if other is None:
        raise ChatAccessDenied()


async def require_chat_room(session: AsyncSession, room_id: str, user: User) -> None:
    """HTTP variant of authorize_chat_room."""
    try:
        await authorize_chat_room(session, room_id, user)
    except ChatRoomInvalid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=CHAT_ROOM_HELP)
    except ChatAccessDenied:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have access to this chat room")
