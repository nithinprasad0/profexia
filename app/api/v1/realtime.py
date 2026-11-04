from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.access import (
    CLOSED_SESSION_STATUSES, ChatAccessDenied, ChatRoomInvalid, authorize_chat_room,
    can_access_learning_session, require_chat_room,
)
from app.api.v1.deps import require_roles
from app.api.v1.users import get_current_user
from app.core.database import SessionLocal, get_db
from app.core.security import decode_access_token
from app.domain.models import ChatMessage, Course, Enrollment, LearningSession, Notification, User

WS_POLICY_VIOLATION = 1008
from app.infrastructure.realtime import manager
from app.schemas.realtime import (
    ChatMessageCreate, ChatMessageResponse, LearningSessionCreate,
    LearningSessionResponse, LearningSessionUpdate, NotificationCreate,
    NotificationResponse,
)

router = APIRouter(prefix="/api/v1", tags=["realtime"])


async def _ws_user(token: str | None) -> User | None:
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        if payload.get("type") != "access":
            return None
        user_id = int(payload["sub"])
    except (ValueError, KeyError, TypeError, jwt.InvalidTokenError):
        return None
    async with SessionLocal() as session:
        return await session.scalar(
            select(User).options(selectinload(User.role)).where(User.id == user_id, User.is_active.is_(True))
        )


@router.get("/chat/{room_id}/messages", response_model=list[ChatMessageResponse])
async def chat_history(
    room_id: str,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await require_chat_room(session, room_id, user)
    rows = await session.execute(
        select(ChatMessage).where(ChatMessage.room_id == room_id).order_by(ChatMessage.id.desc()).limit(100)
    )
    return list(reversed(rows.scalars().all()))


@router.post("/chat/{room_id}/messages", response_model=ChatMessageResponse, status_code=status.HTTP_201_CREATED)
async def create_chat_message(
    room_id: str,
    payload: ChatMessageCreate,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await require_chat_room(session, room_id, user)
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="content must contain 1-4000 characters")
    # sender_id always comes from the authenticated token, never from the client.
    message = ChatMessage(room_id=room_id, sender_id=user.id, content=content)
    session.add(message)
    await session.commit()
    await session.refresh(message)
    data = ChatMessageResponse.model_validate(message).model_dump(mode="json")
    await manager.broadcast(room_id, {"event": "message", "data": data})
    return message


@router.websocket("/ws/chat/{room_id}")
async def chat_socket(websocket: WebSocket, room_id: str, token: str | None = Query(default=None)):
    user = await _ws_user(token)
    if user is None:
        await websocket.close(code=WS_POLICY_VIOLATION)
        return
    # Membership is verified before the socket is accepted...
    async with SessionLocal() as session:
        try:
            await authorize_chat_room(session, room_id, user)
        except (ChatRoomInvalid, ChatAccessDenied):
            await websocket.close(code=WS_POLICY_VIOLATION)
            return
    await manager.connect(room_id, websocket)
    try:
        await websocket.send_json({"event": "connected", "room_id": room_id, "user_id": user.id})
        while True:
            payload = await websocket.receive_json()
            content = str(payload.get("content", "")).strip() if isinstance(payload, dict) else ""
            if not content or len(content) > 4000:
                await websocket.send_json({"event": "error", "detail": "content must contain 1-4000 characters"})
                continue
            async with SessionLocal() as session:
                # ...and re-checked on every message, so revoked access (e.g. an
                # ended enrollment) takes effect without reconnecting.
                try:
                    await authorize_chat_room(session, room_id, user)
                except (ChatRoomInvalid, ChatAccessDenied):
                    await websocket.close(code=WS_POLICY_VIOLATION)
                    return
                message = ChatMessage(room_id=room_id, sender_id=user.id, content=content)
                session.add(message)
                await session.commit()
                await session.refresh(message)
                data = ChatMessageResponse.model_validate(message).model_dump(mode="json")
            await manager.broadcast(room_id, {"event": "message", "data": data})
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(room_id, websocket)


@router.get("/notifications", response_model=list[NotificationResponse])
async def list_notifications(
    unread_only: bool = False,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = select(Notification).where(Notification.recipient_id == user.id)
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    rows = await session.execute(stmt.order_by(Notification.id.desc()).limit(100))
    return rows.scalars().all()


@router.post("/notifications", response_model=NotificationResponse, status_code=status.HTTP_201_CREATED)
async def create_notification(
    payload: NotificationCreate,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    recipient = await session.get(User, payload.recipient_id)
    if recipient is None:
        raise HTTPException(status_code=404, detail="Recipient not found")
    notification = Notification(**payload.model_dump())
    session.add(notification)
    await session.commit()
    await session.refresh(notification)
    await manager.broadcast(
        f"user:{notification.recipient_id}",
        {"event": "notification", "data": NotificationResponse.model_validate(notification).model_dump(mode="json")},
    )
    return notification


@router.patch("/notifications/{notification_id}/read", response_model=NotificationResponse)
async def mark_notification_read(
    notification_id: int,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    notification = await session.get(Notification, notification_id)
    if notification is None or notification.recipient_id != user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.is_read = True
    await session.commit()
    await session.refresh(notification)
    return notification


@router.post("/notifications/read-all")
async def mark_all_notifications_read(
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await session.execute(
        update(Notification).where(
            Notification.recipient_id == user.id, Notification.is_read.is_(False)
        ).values(is_read=True)
    )
    await session.commit()
    return {"updated": True}


@router.websocket("/ws/notifications")
async def notification_socket(websocket: WebSocket, token: str | None = Query(default=None)):
    user = await _ws_user(token)
    if user is None:
        await websocket.close(code=WS_POLICY_VIOLATION)
        return
    room = f"user:{user.id}"
    await manager.connect(room, websocket)
    try:
        await websocket.send_json({"event": "connected", "user_id": user.id})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(room, websocket)


async def _session_response(session_obj: LearningSession) -> LearningSessionResponse:
    return LearningSessionResponse.model_validate(session_obj)


async def _can_access_session(session: AsyncSession, session_obj: LearningSession, user: User) -> bool:
    return await can_access_learning_session(session, session_obj, user)


@router.get("/learning-sessions", response_model=list[LearningSessionResponse])
async def list_learning_sessions(
    course_id: int | None = None,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = select(LearningSession)
    if course_id is not None:
        stmt = stmt.where(LearningSession.course_id == course_id)
    if user.role.name == "learner":
        enrolled = select(Enrollment.course_id).where(
            Enrollment.learner_id == user.id, Enrollment.status.in_(["active", "completed"])
        )
        stmt = stmt.where(LearningSession.course_id.in_(enrolled))
    elif user.role.name == "teacher":
        stmt = stmt.where(LearningSession.host_id == user.id)
    rows = await session.execute(stmt.order_by(LearningSession.id.desc()))
    return rows.scalars().all()


@router.post("/learning-sessions", response_model=LearningSessionResponse, status_code=status.HTTP_201_CREATED)
async def create_learning_session(
    payload: LearningSessionCreate,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles("teacher", "admin")),
):
    course = await session.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Course not found")
    if user.role.name != "admin" and course.teacher_id != user.id:
        raise HTTPException(status_code=403, detail="Only the course teacher or admin can create sessions")
    obj = LearningSession(host_id=course.teacher_id, **payload.model_dump())
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.get("/learning-sessions/{session_id}", response_model=LearningSessionResponse)
async def get_learning_session(
    session_id: int,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    obj = await session.get(LearningSession, session_id)
    if obj is None or not await _can_access_session(session, obj, user):
        raise HTTPException(status_code=404, detail="Learning session not found")
    return obj


@router.patch("/learning-sessions/{session_id}", response_model=LearningSessionResponse)
async def update_learning_session(
    session_id: int,
    payload: LearningSessionUpdate,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles("teacher", "admin")),
):
    obj = await session.get(LearningSession, session_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Learning session not found")
    if user.role.name != "admin" and obj.host_id != user.id:
        raise HTTPException(status_code=403, detail="Only the session host or admin can modify it")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, key, value)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.websocket("/ws/learning-sessions/{session_id}")
async def learning_session_socket(
    websocket: WebSocket,
    session_id: int,
    token: str | None = Query(default=None),
):
    user = await _ws_user(token)
    if user is None:
        await websocket.close(code=WS_POLICY_VIOLATION)
        return
    async with SessionLocal() as db:
        obj = await db.get(LearningSession, session_id)
        if (
            obj is None
            or obj.status in CLOSED_SESSION_STATUSES
            or not await _can_access_session(db, obj, user)
        ):
            await websocket.close(code=WS_POLICY_VIOLATION)
            return
    room = f"session:{session_id}"
    await manager.connect(room, websocket)
    try:
        await websocket.send_json({"event": "connected", "session_id": session_id, "user_id": user.id})
        while True:
            payload = await websocket.receive_json()
            if not isinstance(payload, dict):
                continue
            event = str(payload.get("event", "signal"))[:50]
            data = payload.get("data", {})
            await manager.broadcast(
                room,
                {"event": event, "session_id": session_id, "user_id": user.id, "data": data},
                exclude=websocket,
            )
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(room, websocket)
