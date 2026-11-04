"""End-to-end smoke test against a running Profexia API.

Usage:
    python scripts/smoke_test.py http://localhost:8000 [--origin http://localhost:5173]
    python scripts/smoke_test.py https://profexia-api.onrender.com --origin https://profexia.vercel.app

Exercises: health, CORS preflight, registration (learner + teacher), admin
self-registration rejection, login, /users/me, refresh-token rotation, logout,
course creation (teacher), role enforcement, course discovery, enrollment,
progress update, and the notifications WebSocket handshake.

Creates uniquely named throwaway users and one course. Never deletes data.
Depends only on httpx and websockets (both installed via requirements.txt).
"""

import argparse
import asyncio
import json
import secrets
import sys
import uuid

import httpx

results: list[tuple[str, bool]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition)))
    status = "PASS" if condition else "FAIL"
    suffix = f" - {detail[:300]}" if detail and not condition else ""
    print(f"[{status}] {name}{suffix}")


async def ws_check(base: str, token: str) -> None:
    try:
        import websockets  # installed by uvicorn[standard]
    except ImportError:
        check("websocket handshake", False, "websockets package not installed")
        return
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    try:
        async with websockets.connect(f"{ws_base}/api/v1/ws/notifications?token={token}", open_timeout=10) as ws:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            check("websocket notifications handshake", message.get("event") == "connected", str(message))
    except Exception as exc:  # noqa: BLE001
        check("websocket notifications handshake", False, repr(exc))
    try:
        async with websockets.connect(f"{ws_base}/api/v1/ws/notifications?token=invalid", open_timeout=10) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
            check("websocket rejects invalid token", False, "connection was accepted")
    except Exception:  # noqa: BLE001
        check("websocket rejects invalid token", True)


async def ws_chat_check(base: str, member_token: str, outsider_token: str, room: str) -> None:
    import websockets

    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    try:
        async with websockets.connect(f"{ws_base}/api/v1/ws/chat/{room}?token={member_token}", open_timeout=10) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            await ws.send(json.dumps({"content": "socket hello"}))
            echo = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            check("chat websocket member send/receive",
                  first.get("event") == "connected" and echo.get("event") == "message", f"{first} {echo}")
    except Exception as exc:  # noqa: BLE001
        check("chat websocket member send/receive", False, repr(exc))
    try:
        async with websockets.connect(f"{ws_base}/api/v1/ws/chat/{room}?token={outsider_token}", open_timeout=10) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
            check("chat websocket rejects outsider", False, "connection was accepted")
    except Exception:  # noqa: BLE001
        check("chat websocket rejects outsider", True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--origin", default="http://localhost:5173")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    api = f"{base}/api/v1"
    tag = uuid.uuid4().hex[:8]
    # Random per run: smoke accounts created in production must not share a
    # password that is published in this repository.
    password = "Smoke-" + secrets.token_urlsafe(18)
    learner_email = f"learner-{tag}@example.com"

    with httpx.Client(timeout=30) as c:
        r = c.get(f"{base}/health")
        check("GET /health reports ok", r.status_code == 200 and r.json().get("status") == "ok", r.text)
        check("GET /health/live", c.get(f"{base}/health/live").status_code == 200)
        check("GET /health/ready", c.get(f"{base}/health/ready").status_code == 200)

        r = c.options(f"{api}/auth/login", headers={
            "Origin": args.origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        })
        check(
            "CORS preflight /auth/login",
            r.status_code == 200 and r.headers.get("access-control-allow-origin") == args.origin,
            f"{r.status_code} {dict(r.headers)}",
        )

        r = c.post(f"{api}/auth/register", json={
            "email": f"admin-{tag}@example.com", "username": f"admin{tag}", "password": password, "role": "admin"})
        check("register as admin is forbidden (403)", r.status_code == 403, r.text)

        r = c.post(f"{api}/auth/register", json={
            "email": f"teacher-{tag}@example.com", "username": f"teacher{tag}", "password": password, "role": "teacher"})
        check("register teacher", r.status_code == 201, r.text)
        teacher = r.json() if r.status_code == 201 else {}

        r = c.post(f"{api}/auth/register", json={"email": learner_email, "username": f"learner{tag}", "password": password})
        check("register learner (default role)", r.status_code == 201 and r.json()["user"]["role"] == "learner", r.text)

        r = c.post(f"{api}/auth/login", json={"email": learner_email, "password": "wrong-password"})
        check("login rejects bad password (401)", r.status_code == 401, r.text)

        r = c.post(f"{api}/auth/login", json={"email": learner_email, "password": password})
        check("login learner", r.status_code == 200, r.text)
        learner = r.json() if r.status_code == 200 else {}
        th = {"Authorization": f"Bearer {teacher.get('access_token', '')}"}

        r = c.get(f"{api}/users/me", headers={"Authorization": f"Bearer {learner.get('access_token', '')}"})
        check("GET /users/me", r.status_code == 200 and r.json().get("email") == learner_email, r.text)
        check("GET /users/me without token (401)", c.get(f"{api}/users/me").status_code == 401)

        r = c.post(f"{api}/auth/refresh", json={"refresh_token": learner.get("refresh_token", "")})
        check("refresh token", r.status_code == 200, r.text)
        rotated = r.json() if r.status_code == 200 else {}
        r = c.post(f"{api}/auth/refresh", json={"refresh_token": learner.get("refresh_token", "")})
        check("old refresh token rejected after rotation (401)", r.status_code == 401, r.text)
        lh = {"Authorization": f"Bearer {rotated.get('access_token', '')}"}

        r = c.post(f"{api}/courses", headers=lh, json={"title": "Not allowed"})
        check("learner cannot create course (403)", r.status_code == 403, r.text)

        r = c.post(f"{api}/courses", headers=th, json={"title": f"Smoke Course {tag}", "description": "smoke test"})
        check("teacher creates course", r.status_code == 201, r.text)
        course_id = r.json().get("id") if r.status_code == 201 else 0

        r = c.get(f"{api}/courses", headers=lh)
        check("course discovery lists new course",
              r.status_code == 200 and any(x.get("id") == course_id for x in r.json()), r.text[:300])
        check("course details", c.get(f"{api}/courses/{course_id}", headers=lh).status_code == 200)
        check("GET /skills", c.get(f"{api}/skills", headers=lh).status_code == 200)
        check("GET /teachers", c.get(f"{api}/teachers", headers=lh).status_code == 200)

        r = c.post(f"{api}/courses/{course_id}/enroll", headers=th)
        check("teacher cannot enroll (403)", r.status_code == 403, r.text)
        r = c.post(f"{api}/courses/{course_id}/enroll", headers=lh)
        check("learner enrolls", r.status_code == 201, r.text)
        enrollment_id = r.json().get("id") if r.status_code == 201 else 0
        r = c.patch(f"{api}/enrollments/{enrollment_id}/progress", headers=lh, json={"progress_percent": 40})
        check("progress update", r.status_code == 200, r.text)
        r = c.get(f"{api}/enrollments/me", headers=lh)
        check("GET /enrollments/me", r.status_code == 200 and len(r.json()) >= 1, r.text)

        # --- Assessment (Phase 5: enrollment + answer protection) ---------------
        r = c.post(f"{api}/courses/{course_id}/questions", headers=th, json={
            "text": "2 + 2 = ?", "options": ["3", "4"], "correct_option": 1,
            "explanation": "SMOKE-EXPLANATION", "marks": 1})
        check("teacher creates question", r.status_code == 201, r.text)
        qid = r.json().get("id") if r.status_code == 201 else 0
        r = c.post(f"{api}/courses/{course_id}/tests", headers=th, json={
            "title": "Smoke test", "question_ids": [qid], "is_published": True, "time_limit_minutes": 30})
        check("teacher publishes test", r.status_code == 201, r.text)
        test_id = r.json().get("id") if r.status_code == 201 else 0
        check("learner cannot read question bank (403)",
              c.get(f"{api}/courses/{course_id}/questions", headers=lh).status_code == 403)

        r = c.post(f"{api}/auth/register", json={
            "email": f"outsider-{tag}@example.com", "username": f"outsider{tag}", "password": password})
        oh = {"Authorization": f"Bearer {r.json().get('access_token', '')}"} if r.status_code == 201 else {}
        check("unenrolled learner cannot start attempt (403)",
              c.post(f"{api}/tests/{test_id}/attempts", headers=oh).status_code == 403)

        r = c.post(f"{api}/tests/{test_id}/attempts", headers=lh)
        check("enrolled learner starts attempt without answer key",
              r.status_code == 201 and "SMOKE-EXPLANATION" not in r.text and "correct_option" not in r.text, r.text)
        attempt_id = r.json().get("id") if r.status_code == 201 else 0
        r = c.post(f"{api}/attempts/{attempt_id}/submit", headers=lh,
                   json={"answers": [{"question_id": qid, "selected_option": 1}]})
        check("learner submits and is scored", r.status_code == 200 and r.json().get("score") == 1
              and "SMOKE-EXPLANATION" not in r.text, r.text)
        check("teacher sees results", c.get(f"{api}/tests/{test_id}/results", headers=th).status_code == 200)
        check("other learner cannot read result (403)", c.get(f"{api}/results/{attempt_id}", headers=oh).status_code == 403)

        # --- Chat (Phase 5: room membership) -----------------------------------
        room = f"course:{course_id}"
        r = c.post(f"{api}/chat/{room}/messages", headers=lh, json={"content": "hello from smoke"})
        check("course member posts chat message", r.status_code == 201, r.text)
        r = c.get(f"{api}/chat/{room}/messages", headers=th)
        check("course teacher reads chat", r.status_code == 200 and any(m["content"] == "hello from smoke" for m in r.json()), r.text)
        check("outsider cannot read course chat (403)", c.get(f"{api}/chat/{room}/messages", headers=oh).status_code == 403)
        check("free-form room ids rejected (400)", c.get(f"{api}/chat/general/messages", headers=lh).status_code == 400)

        # --- Learning session + LiveKit ------------------------------------------
        r = c.post(f"{api}/learning-sessions", headers=th, json={"course_id": course_id, "title": "Smoke session"})
        check("teacher creates learning session", r.status_code == 201, r.text)
        session_id = r.json().get("id") if r.status_code == 201 else 0
        check("learner lists sessions", c.get(f"{api}/learning-sessions", headers=lh).status_code == 200)
        r = c.post(f"{api}/learning-sessions/{session_id}/livekit-token", headers=oh, json={})
        check("outsider cannot get LiveKit token (403)", r.status_code == 403, r.text)
        r = c.post(f"{api}/learning-sessions/{session_id}/livekit-token", headers=lh, json={"room_name": "other-room"})
        check("arbitrary LiveKit room rejected (403)", r.status_code == 403, r.text)
        r = c.post(f"{api}/learning-sessions/{session_id}/livekit-token", headers=lh, json={})
        check("participant LiveKit token: 200 if configured, else 503", r.status_code in (200, 503), r.text)

        asyncio.run(ws_check(base, rotated.get("access_token", "")))
        asyncio.run(ws_chat_check(base, rotated.get("access_token", ""), oh.get("Authorization", "")[7:], room))

        r = c.post(f"{api}/auth/logout", json={"refresh_token": rotated.get("refresh_token", "")})
        check("logout (204)", r.status_code == 204, r.text)
        r = c.post(f"{api}/auth/refresh", json={"refresh_token": rotated.get("refresh_token", "")})
        check("refresh after logout rejected (401)", r.status_code == 401, r.text)

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
