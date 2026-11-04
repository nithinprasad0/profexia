from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core import rate_limit
from app.core.redis import get_redis
from app.core.security import (
    create_access_token,
    create_refresh_token,
    DUMMY_PASSWORD_HASH,
    hash_password,
    refresh_token_key,
    refresh_ttl_seconds,
    verify_password,
)
from app.domain.models import Profile, Role, User
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    UserResponse,
)

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])

SELF_REGISTRATION_ROLES = {"learner", "teacher"}


async def _get_role(session: AsyncSession, name: str) -> Role:
    role = await session.scalar(select(Role).where(Role.name == name))
    if role is None:
        raise HTTPException(status_code=500, detail="Required role is not configured")
    return role


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        username=user.username,
        is_active=user.is_active,
        role=user.role.name,
    )


async def _issue_tokens(user: User):
    access = create_access_token(user_id=user.id, role=user.role.name)
    refresh = create_refresh_token()
    redis = get_redis()
    try:
        await redis.set(
            refresh_token_key(refresh),
            str(user.id),
            ex=refresh_ttl_seconds(),
        )
    finally:
        await redis.aclose()
    return access, refresh


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, request: Request, session: AsyncSession = Depends(get_db)):
    await rate_limit.enforce(
        "register:ip", rate_limit.client_ip(request),
        settings.rate_limit_register_per_ip, settings.rate_limit_register_window_seconds,
        "Too many registration attempts. Please try again later.",
    )
    # Security: public self-registration must never grant admin privileges.
    # Admins are created out-of-band with `python -m app.infrastructure.create_admin`.
    if payload.role not in SELF_REGISTRATION_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin accounts cannot be created through public registration",
        )

    existing = await session.scalar(
        select(User).where((User.email == payload.email) | (User.username == payload.username))
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email or username is already registered",
        )

    role = await _get_role(session, payload.role)
    user = User(
        email=payload.email,
        username=payload.username,
        password_hash=hash_password(payload.password),
        role_id=role.id,
        role=role,
    )
    user.profile = Profile(display_name=payload.username)

    session.add(user)
    await session.commit()
    await session.refresh(user)

    access, refresh = await _issue_tokens(user)

    return AuthResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_expire_minutes * 60,
        user=_user_response(user),
    )


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginRequest, request: Request, session: AsyncSession = Depends(get_db)):
    # 1) Per-IP attempt budget (slows credential stuffing across many accounts).
    await rate_limit.enforce(
        "login:ip", rate_limit.client_ip(request),
        settings.rate_limit_login_per_ip, settings.rate_limit_login_ip_window_seconds,
        "Too many login attempts. Please try again later.",
    )
    # 2) Per-account failure budget (stops password guessing on one account,
    #    independent of the client IP). Keyed by a hash of the email.
    account_key = rate_limit.limit_key("login:fail", str(payload.email))
    account = await rate_limit.peek(account_key, settings.rate_limit_login_failures_per_account)
    if not account.allowed:
        raise rate_limit.too_many_requests(
            account.retry_after or settings.rate_limit_login_failure_window_seconds,
            "Too many failed login attempts for this account. Please try again later.",
        )

    user = await session.scalar(
        select(User)
        .options(selectinload(User.role))
        .where(User.email == payload.email)
    )

    # Always run bcrypt so response time does not reveal whether the email exists.
    password_ok = verify_password(payload.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
    if user is None or not password_ok:
        await rate_limit.hit(
            account_key,
            settings.rate_limit_login_failures_per_account,
            settings.rate_limit_login_failure_window_seconds,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    await rate_limit.reset(account_key)

    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is inactive")

    access, refresh = await _issue_tokens(user)

    return AuthResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_expire_minutes * 60,
        user=_user_response(user),
    )


@router.post("/refresh", response_model=AuthResponse)
async def refresh(payload: RefreshRequest, request: Request, session: AsyncSession = Depends(get_db)):
    await rate_limit.enforce(
        "refresh:ip", rate_limit.client_ip(request),
        settings.rate_limit_refresh_per_ip, settings.rate_limit_refresh_window_seconds,
    )
    redis = get_redis()
    key = refresh_token_key(payload.refresh_token)

    try:
        # Rotate refresh tokens atomically: GETDEL guarantees a token can be
        # exchanged exactly once, even under concurrent requests.
        user_id = await redis.getdel(key)
    finally:
        await redis.aclose()

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user = await session.scalar(
        select(User)
        .options(selectinload(User.role))
        .where(User.id == int(user_id))
    )

    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User account is unavailable")

    access, new_refresh = await _issue_tokens(user)

    return AuthResponse(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=settings.access_token_expire_minutes * 60,
        user=_user_response(user),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: LogoutRequest):
    redis = get_redis()
    try:
        await redis.delete(refresh_token_key(payload.refresh_token))
    finally:
        await redis.aclose()
