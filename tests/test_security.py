"""Backend security tests against real PostgreSQL + Redis (test database only).

Every test verifies that the *API* rejects the unauthorized action; nothing here
relies on frontend visibility. Skipped automatically when the services are not
reachable (see conftest.py).
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.main import app
from tests.helpers import (
    PASSWORD, create_course, create_published_test, create_session, enroll, make_admin, register, unique,
)

pytestmark = pytest.mark.usefixtures("integration")
WS_POLICY = 1008


def ws_rejected(client: TestClient, url: str) -> int:
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url) as ws:
            ws.receive_json()
    return exc.value.code


# =============================================================================
# A. Chat room authorization
# =============================================================================

@pytest.fixture
def course_room(client):
    teacher = register(client, "teacher")
    member = register(client)
    outsider = register(client)
    course_id = create_course(client, teacher)
    enroll(client, member, course_id)
    return teacher, member, outsider, course_id, f"course:{course_id}"


def test_chat_members_can_read_and_post(client, course_room):
    teacher, member, _, _, room = course_room
    r = client.post(f"/api/v1/chat/{room}/messages", headers=member.h, json={"content": "hello class"})
    assert r.status_code == 201 and r.json()["sender_id"] == member.id
    r = client.get(f"/api/v1/chat/{room}/messages", headers=teacher.h)
    assert r.status_code == 200 and [m["content"] for m in r.json()] == ["hello class"]


def test_chat_outsider_cannot_read_or_post(client, course_room):
    _, member, outsider, _, room = course_room
    client.post(f"/api/v1/chat/{room}/messages", headers=member.h, json={"content": "private"})
    r = client.get(f"/api/v1/chat/{room}/messages", headers=outsider.h)
    assert r.status_code == 403 and "private" not in r.text
    assert client.post(f"/api/v1/chat/{room}/messages", headers=outsider.h, json={"content": "x"}).status_code == 403


def test_chat_other_teacher_cannot_access_course_room(client, course_room):
    _, _, _, _, room = course_room
    other_teacher = register(client, "teacher")
    assert client.get(f"/api/v1/chat/{room}/messages", headers=other_teacher.h).status_code == 403


def test_chat_requires_authentication(client, course_room):
    room = course_room[4]
    assert client.get(f"/api/v1/chat/{room}/messages").status_code == 401
    assert client.get(f"/api/v1/chat/{room}/messages", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_chat_guessing_room_ids_reveals_nothing(client):
    outsider = register(client)
    # A non-existent course and an existing-but-forbidden one look identical.
    assert client.get("/api/v1/chat/course:999999999/messages", headers=outsider.h).status_code == 403
    # Free-form legacy room names are no longer accepted.
    for room in ("general", "room-1", "course:1:extra"):
        assert client.get(f"/api/v1/chat/{room}/messages", headers=outsider.h).status_code == 400


def test_chat_direct_messages_only_for_the_two_participants(client):
    a, b, c = register(client), register(client), register(client)
    lo, hi = sorted([a, b], key=lambda x: x.id)
    room = f"dm:{lo.id}:{hi.id}"
    assert client.post(f"/api/v1/chat/{room}/messages", headers=a.h, json={"content": "hi b"}).status_code == 201
    assert client.get(f"/api/v1/chat/{room}/messages", headers=b.h).status_code == 200
    assert client.get(f"/api/v1/chat/{room}/messages", headers=c.h).status_code == 403
    # Non-canonical order and self-DMs are rejected.
    assert client.get(f"/api/v1/chat/dm:{hi.id}:{lo.id}/messages", headers=a.h).status_code == 400
    assert client.get(f"/api/v1/chat/dm:{a.id}:{a.id}/messages", headers=a.h).status_code == 400


def test_chat_websocket_member_connects_and_receives(client, course_room):
    _, member, _, _, room = course_room
    with client.websocket_connect(f"/api/v1/ws/chat/{room}?token={member.token}") as ws:
        assert ws.receive_json()["event"] == "connected"
        ws.send_json({"content": "via socket"})
        event = ws.receive_json()
        assert event["event"] == "message" and event["data"]["sender_id"] == member.id


def test_chat_websocket_rejects_outsider_and_bad_tokens(client, course_room):
    _, _, outsider, _, room = course_room
    assert ws_rejected(client, f"/api/v1/ws/chat/{room}?token={outsider.token}") == WS_POLICY
    assert ws_rejected(client, f"/api/v1/ws/chat/{room}") == WS_POLICY
    assert ws_rejected(client, f"/api/v1/ws/chat/{room}?token=invalid") == WS_POLICY
    assert ws_rejected(client, f"/api/v1/ws/chat/general?token={outsider.token}") == WS_POLICY


def test_chat_websocket_rechecks_membership_per_message(client):
    teacher, learner = register(client, "teacher"), register(client)
    course_id = create_course(client, teacher)
    enrollment_id = enroll(client, learner, course_id)
    room = f"course:{course_id}"
    with client.websocket_connect(f"/api/v1/ws/chat/{room}?token={learner.token}") as ws:
        assert ws.receive_json()["event"] == "connected"
        # Access is revoked while connected (enrollment withdrawn in the database).
        _set_enrollment_status(enrollment_id, "withdrawn")
        ws.send_json({"content": "after revocation"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == WS_POLICY
    assert client.get(f"/api/v1/chat/{room}/messages", headers=teacher.h).json() == []


def test_session_chat_room_follows_session_access(client):
    teacher, learner, outsider = register(client, "teacher"), register(client), register(client)
    course_id = create_course(client, teacher)
    enroll(client, learner, course_id)
    session_id = create_session(client, teacher, course_id)
    room = f"session:{session_id}"
    assert client.get(f"/api/v1/chat/{room}/messages", headers=learner.h).status_code == 200
    assert client.get(f"/api/v1/chat/{room}/messages", headers=outsider.h).status_code == 403


# =============================================================================
# B + C. Assessment enrollment enforcement and answer protection
# =============================================================================

@pytest.fixture
def assessment(client):
    teacher = register(client, "teacher")
    course_id = create_course(client, teacher)
    test_id, qids = create_published_test(client, teacher, course_id)
    learner = register(client)
    enroll(client, learner, course_id)
    return teacher, learner, course_id, test_id, qids


def _no_answer_leak(response):
    text = response.text
    assert "SECRET-EXPLANATION" not in text
    assert "correct_option" not in text
    assert "explanation" not in text


def test_unenrolled_learner_cannot_see_or_start_tests(client, assessment):
    _, _, course_id, test_id, _ = assessment
    stranger = register(client)
    assert client.get(f"/api/v1/courses/{course_id}/tests", headers=stranger.h).status_code == 403
    assert client.get(f"/api/v1/tests/{test_id}", headers=stranger.h).status_code == 403
    assert client.post(f"/api/v1/tests/{test_id}/attempts", headers=stranger.h).status_code == 403


def test_teacher_and_admin_cannot_start_attempts(client, assessment):
    teacher, _, _, test_id, _ = assessment
    assert client.post(f"/api/v1/tests/{test_id}/attempts", headers=teacher.h).status_code == 403
    admin = make_admin(client)
    assert client.post(f"/api/v1/tests/{test_id}/attempts", headers=admin.h).status_code == 403


def test_answers_hidden_before_during_and_after_attempt(client, assessment):
    _, learner, course_id, test_id, qids = assessment
    # Before: the question bank is staff-only.
    assert client.get(f"/api/v1/courses/{course_id}/questions", headers=learner.h).status_code == 403
    assert client.get(f"/api/v1/questions/{qids[0]}", headers=learner.h).status_code == 403
    r = client.get(f"/api/v1/tests/{test_id}", headers=learner.h)
    assert r.status_code == 200
    _no_answer_leak(r)
    # During: the attempt contains questions and options only.
    r = client.post(f"/api/v1/tests/{test_id}/attempts", headers=learner.h)
    assert r.status_code == 201
    _no_answer_leak(r)
    attempt_id = r.json()["id"]
    _no_answer_leak(client.get(f"/api/v1/attempts/{attempt_id}", headers=learner.h))
    assert client.get(f"/api/v1/questions/{qids[0]}", headers=learner.h).status_code == 403
    # After: the result reports per-question correctness, never the key or explanation.
    r = client.post(f"/api/v1/attempts/{attempt_id}/submit", headers=learner.h,
                    json={"answers": [{"question_id": qids[0], "selected_option": 0},
                                      {"question_id": qids[1], "selected_option": 1}]})
    assert r.status_code == 200 and r.json()["score"] == 1
    _no_answer_leak(r)
    _no_answer_leak(client.get(f"/api/v1/results/{attempt_id}", headers=learner.h))
    assert client.get(f"/api/v1/courses/{course_id}/questions", headers=learner.h).status_code == 403


def test_teacher_owner_and_admin_keep_question_bank_access(client, assessment):
    teacher, _, course_id, _, qids = assessment
    r = client.get(f"/api/v1/courses/{course_id}/questions", headers=teacher.h)
    assert r.status_code == 200 and "SECRET-EXPLANATION-0" in r.text
    admin = make_admin(client)
    assert client.get(f"/api/v1/questions/{qids[0]}", headers=admin.h).status_code == 200
    other_teacher = register(client, "teacher")
    assert client.get(f"/api/v1/questions/{qids[0]}", headers=other_teacher.h).status_code == 403


def test_learners_cannot_access_each_others_attempts_or_results(client, assessment):
    teacher, learner, course_id, test_id, qids = assessment
    other = register(client)
    enroll(client, other, course_id)
    attempt_id = client.post(f"/api/v1/tests/{test_id}/attempts", headers=learner.h).json()["id"]
    assert client.get(f"/api/v1/attempts/{attempt_id}", headers=other.h).status_code == 403
    r = client.post(f"/api/v1/attempts/{attempt_id}/submit", headers=other.h, json={"answers": []})
    assert r.status_code == 404
    client.post(f"/api/v1/attempts/{attempt_id}/submit", headers=learner.h,
                json={"answers": [{"question_id": qids[0], "selected_option": 0}]})
    assert client.get(f"/api/v1/results/{attempt_id}", headers=other.h).status_code == 403
    assert client.get(f"/api/v1/tests/{test_id}/results", headers=learner.h).status_code == 403
    other_teacher = register(client, "teacher")
    assert client.get(f"/api/v1/results/{attempt_id}", headers=other_teacher.h).status_code == 403
    assert client.get(f"/api/v1/tests/{test_id}/results", headers=other_teacher.h).status_code == 403
    assert client.get(f"/api/v1/tests/{test_id}/results", headers=teacher.h).status_code == 200


def test_time_limit_is_enforced_server_side(client):
    teacher, learner = register(client, "teacher"), register(client)
    course_id = create_course(client, teacher)
    test_id, qids = create_published_test(client, teacher, course_id, time_limit=1)
    enroll(client, learner, course_id)
    attempt_id = client.post(f"/api/v1/tests/{test_id}/attempts", headers=learner.h).json()["id"]
    _set_attempt_started(attempt_id, datetime.now(timezone.utc) - timedelta(minutes=5))
    r = client.post(f"/api/v1/attempts/{attempt_id}/submit", headers=learner.h,
                    json={"answers": [{"question_id": q, "selected_option": 0} for q in qids]})
    assert r.status_code == 200
    assert r.json()["score"] == 0 and r.json()["answers"] == []
    # The attempt is closed and cannot be resubmitted.
    assert client.post(f"/api/v1/attempts/{attempt_id}/submit", headers=learner.h, json={"answers": []}).status_code == 409


# =============================================================================
# D. LiveKit authorization (no external calls: tokens are signed locally)
# =============================================================================

@pytest.fixture
def livekit(monkeypatch, client):
    monkeypatch.setattr(settings, "livekit_api_key", "test-key")
    monkeypatch.setattr(settings, "livekit_api_secret", "test-livekit-secret")
    monkeypatch.setattr(settings, "livekit_url", "wss://example.livekit.cloud")
    teacher, learner = register(client, "teacher"), register(client)
    course_id = create_course(client, teacher)
    enroll(client, learner, course_id)
    session_id = create_session(client, teacher, course_id)
    return teacher, learner, course_id, session_id


def _grant(response):
    return jwt.decode(response.json()["token"], "test-livekit-secret", algorithms=["HS256"])["video"]


def test_livekit_room_is_bound_to_session(client, livekit):
    _, learner, _, session_id = livekit
    url = f"/api/v1/learning-sessions/{session_id}/livekit-token"
    r = client.post(url, headers=learner.h, json={})
    assert r.status_code == 200
    assert r.json()["room_name"] == f"profexia-session-{session_id}" == _grant(r)["room"]
    assert client.post(url, headers=learner.h, json={"room_name": f"profexia-session-{session_id}"}).status_code == 200
    for room in ("any-room", f"profexia-session-{session_id + 1}", "teachers-lounge"):
        assert client.post(url, headers=learner.h, json={"room_name": room}).status_code == 403


def test_livekit_publish_permissions(client, livekit):
    teacher, learner, _, session_id = livekit
    url = f"/api/v1/learning-sessions/{session_id}/livekit-token"
    assert _grant(client.post(url, headers=teacher.h, json={}))["canPublish"] is True
    assert _grant(client.post(url, headers=learner.h, json={}))["canPublish"] is False
    admin = make_admin(client)
    assert _grant(client.post(url, headers=admin.h, json={}))["canPublish"] is True


def test_livekit_rejects_non_participants(client, livekit):
    _, _, _, session_id = livekit
    url = f"/api/v1/learning-sessions/{session_id}/livekit-token"
    assert client.post(url, json={}).status_code == 401
    assert client.post(url, headers=register(client).h, json={}).status_code == 403
    assert client.post(url, headers=register(client, "teacher").h, json={}).status_code == 403
    assert client.post("/api/v1/learning-sessions/999999999/livekit-token",
                       headers=register(client).h, json={}).status_code == 404


def test_livekit_rejects_closed_sessions(client, livekit):
    teacher, learner, _, session_id = livekit
    assert client.patch(f"/api/v1/learning-sessions/{session_id}", headers=teacher.h,
                        json={"status": "ended"}).status_code == 200
    url = f"/api/v1/learning-sessions/{session_id}/livekit-token"
    assert client.post(url, headers=learner.h, json={}).status_code == 409
    assert ws_rejected(client, f"/api/v1/ws/learning-sessions/{session_id}?token={learner.token}") == WS_POLICY


# =============================================================================
# E. Rate limiting + refresh-token security
# =============================================================================

def test_login_account_lockout_and_expiry(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_login_failures_per_account", 3)
    monkeypatch.setattr(settings, "rate_limit_login_failure_window_seconds", 2)
    user = register(client)
    bad = {"email": user.email, "password": "wrong-password"}
    for _ in range(3):
        assert client.post("/api/v1/auth/login", json=bad).status_code == 401
    r = client.post("/api/v1/auth/login", json=bad)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    # Even the correct password is refused while locked.
    good = {"email": user.email, "password": PASSWORD}
    assert client.post("/api/v1/auth/login", json=good).status_code == 429
    # Other accounts are unaffected.
    other = register(client)
    assert client.post("/api/v1/auth/login", json={"email": other.email, "password": PASSWORD}).status_code == 200
    time.sleep(2.5)
    assert client.post("/api/v1/auth/login", json=good).status_code == 200


def test_successful_login_resets_failure_counter(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_login_failures_per_account", 3)
    user = register(client)
    for _ in range(2):
        client.post("/api/v1/auth/login", json={"email": user.email, "password": "nope-nope"})
    assert client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}).status_code == 200
    for _ in range(2):
        assert client.post("/api/v1/auth/login", json={"email": user.email, "password": "nope-nope"}).status_code == 401


def test_login_per_ip_limit(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_login_per_ip", 3)
    ip = f"203.0.113.{int(time.time() * 1000) % 250}-{unique('')}"
    with TestClient(app, client=(ip, 5000)) as c:
        for i in range(3):
            assert c.post("/api/v1/auth/login", json={"email": f"x{i}@example.com", "password": "pw"}).status_code == 401
        assert c.post("/api/v1/auth/login", json={"email": "y@example.com", "password": "pw"}).status_code == 429
    with TestClient(app, client=(f"{ip}-other", 5000)) as c:
        assert c.post("/api/v1/auth/login", json={"email": "y@example.com", "password": "pw"}).status_code == 401


def test_register_and_refresh_per_ip_limits(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_register_per_ip", 2)
    monkeypatch.setattr(settings, "rate_limit_refresh_per_ip", 2)
    with TestClient(app, client=(unique("198.51.100."), 5000)) as c:
        tokens = [register(c).refresh_token for _ in range(2)]
        r = c.post("/api/v1/auth/register", json={
            "email": f"{unique('r')}@example.com", "username": unique("r"), "password": PASSWORD})
        assert r.status_code == 429
        assert c.post("/api/v1/auth/refresh", json={"refresh_token": tokens[0]}).status_code == 200
        assert c.post("/api/v1/auth/refresh", json={"refresh_token": tokens[1]}).status_code == 200
        assert c.post("/api/v1/auth/refresh", json={"refresh_token": "anything"}).status_code == 429


def test_auth_returns_503_not_500_when_redis_is_down(client, monkeypatch):
    import redis.asyncio as redis

    from app.api.v1 import auth as auth_module
    from app.core import rate_limit

    user = register(client)
    broken = lambda: redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2, socket_timeout=0.2)  # noqa: E731
    monkeypatch.setattr(rate_limit, "get_redis", broken)
    monkeypatch.setattr(auth_module, "get_redis", broken)
    r = client.post("/api/v1/auth/login", json={"email": user.email, "password": PASSWORD})
    assert r.status_code == 503  # limiter failed open; token storage unavailable -> 503


def test_refresh_token_is_single_use_even_concurrently(client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    user = register(client)
    barrier = Barrier(5)

    def attempt(_):
        barrier.wait()  # release all requests at once; they run concurrently on the app's loop
        return client.post("/api/v1/auth/refresh", json={"refresh_token": user.refresh_token}).status_code

    with ThreadPoolExecutor(max_workers=5) as pool:
        codes = list(pool.map(attempt, range(5)))
    assert codes.count(200) == 1 and codes.count(401) == 4, codes


def test_tokens_are_not_interchangeable(client):
    user = register(client)
    # Refresh token used as a bearer token, access token used as a refresh token.
    assert client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {user.refresh_token}"}).status_code == 401
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": user.token}).status_code == 401
    # Logout revokes the refresh token.
    assert client.post("/api/v1/auth/logout", json={"refresh_token": user.refresh_token}).status_code == 204
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": user.refresh_token}).status_code == 401


def test_rate_limit_keys_do_not_contain_identifiers(client, monkeypatch):
    import redis as sync_redis

    user = register(client)
    client.post("/api/v1/auth/login", json={"email": user.email, "password": "definitely-wrong"})
    r = sync_redis.from_url(settings.redis_url, decode_responses=True)
    keys = list(r.scan_iter("profexia:rl:*"))
    r.close()
    assert keys, "expected rate-limit keys"
    local_part = user.email.split("@")[0]
    assert not any(local_part in k or "definitely-wrong" in k for k in keys)


# =============================================================================
# Cross-user / role boundaries (IDOR)
# =============================================================================

def test_role_and_object_boundaries(client):
    teacher, other_teacher, learner, other_learner = (
        register(client, "teacher"), register(client, "teacher"), register(client), register(client))
    course_id = create_course(client, teacher)
    enrollment_id = enroll(client, learner, course_id)
    # Course ownership
    assert client.patch(f"/api/v1/courses/{course_id}", headers=other_teacher.h, json={"title": "pwned"}).status_code == 403
    assert client.post("/api/v1/courses", headers=learner.h, json={"title": "x"}).status_code == 403
    # Admin-only operations
    assert client.post("/api/v1/skills", headers=teacher.h, json={"name": unique("s")}).status_code == 403
    assert client.post("/api/v1/notifications", headers=teacher.h, json={
        "recipient_id": learner.id, "type": "x", "title": "x", "message": "x"}).status_code == 403
    # Enrollment ownership
    assert client.patch(f"/api/v1/enrollments/{enrollment_id}/progress", headers=other_learner.h,
                        json={"progress_percent": 99}).status_code == 404
    # Learning sessions: other teachers cannot create/modify/see
    session_id = create_session(client, teacher, course_id)
    assert client.post("/api/v1/learning-sessions", headers=other_teacher.h,
                       json={"course_id": course_id, "title": "x"}).status_code == 403
    assert client.patch(f"/api/v1/learning-sessions/{session_id}", headers=other_teacher.h,
                        json={"title": "x"}).status_code == 403
    assert client.get(f"/api/v1/learning-sessions/{session_id}", headers=other_learner.h).status_code == 404
    assert ws_rejected(client, f"/api/v1/ws/learning-sessions/{session_id}?token={other_learner.token}") == WS_POLICY


def test_notifications_are_private(client):
    admin, alice, bob = make_admin(client), register(client), register(client)
    r = client.post("/api/v1/notifications", headers=admin.h, json={
        "recipient_id": alice.id, "type": "info", "title": "t", "message": "for alice"})
    assert r.status_code == 201
    nid = r.json()["id"]
    assert client.patch(f"/api/v1/notifications/{nid}/read", headers=bob.h).status_code == 404
    assert "for alice" not in client.get("/api/v1/notifications", headers=bob.h).text
    assert ws_rejected(client, "/api/v1/ws/notifications?token=bad") == WS_POLICY


def test_payment_webhook_rejects_unsigned_and_forged(client, monkeypatch):
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "whsec-test")
    body = json.dumps({"event": "payment.captured", "payload": {}}).encode()
    url = "/api/v1/payments/webhooks/razorpay"
    assert client.post(url, content=body).status_code == 400
    assert client.post(url, content=body, headers={"X-Razorpay-Signature": "forged"}).status_code == 400


# =============================================================================
# DB helpers (test database only)
# =============================================================================

def _run_sql(stmt):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    async def go():
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(stmt)
        await engine.dispose()

    asyncio.run(go())


def _set_enrollment_status(enrollment_id: int, status: str):
    from sqlalchemy import update

    from app.domain.models import Enrollment
    _run_sql(update(Enrollment).where(Enrollment.id == enrollment_id).values(status=status))


def _set_attempt_started(attempt_id: int, started: datetime):
    from sqlalchemy import update

    from app.domain.models import Submission
    _run_sql(update(Submission).where(Submission.id == attempt_id).values(started_at=started))
