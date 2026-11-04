"""Helpers for integration tests: create isolated, uniquely named fixtures via the API."""

import uuid

from fastapi.testclient import TestClient

PASSWORD = "Sec-test-pass-123"


def unique(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


class Actor:
    def __init__(self, auth: dict):
        self.auth = auth
        self.id: int = auth["user"]["id"]
        self.email: str = auth["user"]["email"]
        self.role: str = auth["user"]["role"]
        self.token: str = auth["access_token"]
        self.refresh_token: str = auth["refresh_token"]

    @property
    def h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}


def register(client: TestClient, role: str = "learner") -> Actor:
    name = unique(role[:1])
    r = client.post("/api/v1/auth/register", json={
        "email": f"{name}@example.com", "username": name, "password": PASSWORD, "role": role})
    assert r.status_code == 201, r.text
    return Actor(r.json())


def make_admin(client: TestClient) -> Actor:
    """Register a learner, promote it in the DB (test database only), and log in again."""
    import asyncio

    from sqlalchemy import select, update
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import settings
    from app.domain.models import Role, User

    async def promote(email: str) -> None:
        # Separate short-lived engine: the app's engine belongs to the TestClient loop.
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        async with engine.begin() as conn:
            role_id = await conn.scalar(select(Role.id).where(Role.name == "admin"))
            await conn.execute(update(User).where(User.email == email).values(role_id=role_id))
        await engine.dispose()

    actor = register(client)
    asyncio.run(promote(actor.email))
    r = client.post("/api/v1/auth/login", json={"email": actor.email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return Actor(r.json())


def create_course(client: TestClient, teacher: Actor) -> int:
    r = client.post("/api/v1/courses", headers=teacher.h, json={"title": unique("course-")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def enroll(client: TestClient, learner: Actor, course_id: int) -> int:
    r = client.post(f"/api/v1/courses/{course_id}/enroll", headers=learner.h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def create_published_test(client: TestClient, teacher: Actor, course_id: int, time_limit: int | None = None) -> tuple[int, list[int]]:
    qids = []
    for i in range(2):
        r = client.post(f"/api/v1/courses/{course_id}/questions", headers=teacher.h, json={
            "text": f"Q{i}?", "options": ["right", "wrong"], "correct_option": 0,
            "explanation": f"SECRET-EXPLANATION-{i}", "marks": 1})
        assert r.status_code == 201, r.text
        qids.append(r.json()["id"])
    body = {"title": unique("test-"), "question_ids": qids, "is_published": True}
    if time_limit:
        body["time_limit_minutes"] = time_limit
    r = client.post(f"/api/v1/courses/{course_id}/tests", headers=teacher.h, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"], qids


def create_session(client: TestClient, teacher: Actor, course_id: int) -> int:
    r = client.post("/api/v1/learning-sessions", headers=teacher.h, json={"course_id": course_id, "title": "Live"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def route_paths(app_or_router, prefix: str = "") -> set[str]:
    """All HTTP and WebSocket route paths, including routes inside included routers.

    FastAPI >= 0.16x keeps included routers as lazy `_IncludedRouter` entries in
    `app.routes` (exposing `original_router`); older versions flattened them.
    This walks both shapes so route-inventory tests work on either.
    """
    paths: set[str] = set()
    for route in getattr(app_or_router, "routes", []):
        nested = getattr(route, "original_router", None)
        if nested is not None:
            context = getattr(route, "include_context", None)
            paths |= route_paths(nested, prefix + (getattr(context, "prefix", "") or ""))
        elif getattr(route, "path", None):
            paths.add(prefix + route.path)
    return paths
