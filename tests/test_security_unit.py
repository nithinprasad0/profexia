"""Security unit tests that need no PostgreSQL/Redis."""

import asyncio
import time
from types import SimpleNamespace

import jwt
import pytest

from app.api.v1.access import CHAT_ROOM_PATTERN
from app.core import rate_limit
from app.core.config import settings
from app.core.security import create_access_token, decode_access_token
from app.infrastructure.providers.livekit import LiveKitClient
from app.infrastructure.realtime import ConnectionManager
from app.schemas.auth import RegisterRequest


# --- JWT validation -------------------------------------------------------------

def _forge(**overrides):
    now = int(time.time())
    payload = {"sub": "1", "role": "admin", "type": "access", "iat": now, "exp": now + 60, **overrides}
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm="HS256")


def test_valid_access_token_decodes():
    assert decode_access_token(create_access_token(5, "learner"))["sub"] == "5"


@pytest.mark.parametrize("token", [
    _forge(type="refresh"),                 # wrong token type
    _forge(type=None),                      # missing type
    _forge(exp=None),                       # no expiry
    _forge(sub=None),                       # no subject
    _forge(exp=int(time.time()) - 10),      # expired
])
def test_invalid_access_tokens_rejected(token):
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(token)


def test_wrong_signature_and_alg_none_rejected():
    now = int(time.time())
    claims = {"sub": "1", "type": "access", "iat": now, "exp": now + 60}
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(jwt.encode(claims, "attacker-secret", algorithm="HS256"))
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(jwt.encode(claims, None, algorithm="none"))


def test_forged_token_rejected_by_api(client):
    bad = jwt.encode({"sub": "1", "type": "access", "iat": 0, "exp": 9999999999}, "attacker", algorithm="HS256")
    assert client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {bad}"}).status_code == 401


# --- Passwords -------------------------------------------------------------------

def test_password_over_72_bytes_rejected():
    with pytest.raises(ValueError):
        RegisterRequest(email="a@example.com", username="abc", password="é" * 40)  # 80 bytes
    RegisterRequest(email="a@example.com", username="abc", password="x" * 72)


# --- Chat room id format -----------------------------------------------------------

@pytest.mark.parametrize("room,ok", [
    ("course:12", True), ("session:3", True), ("dm:1:2", True),
    ("general", False), ("course:", False), ("course:1;DROP", False), ("dm:1", False),
    ("course:1/../2", False), ("COURSE:1", False), ("", False),
])
def test_chat_room_pattern(room, ok):
    assert (CHAT_ROOM_PATTERN.fullmatch(room) is not None) is ok


# --- Rate limiter helpers ------------------------------------------------------------

def _req(peer: str, xff: str | None = None):
    headers = {"x-forwarded-for": xff} if xff else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_client_ip_uses_peer_without_trusted_proxies(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_count", 0)
    assert rate_limit.client_ip(_req("10.0.0.5", "6.6.6.6")) == "10.0.0.5"


def test_client_ip_ignores_spoofed_leftmost_entries(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_count", 1)
    # Client sent "X-Forwarded-For: 6.6.6.6"; the platform proxy appended the real IP.
    assert rate_limit.client_ip(_req("10.0.0.5", "6.6.6.6, 198.51.100.7")) == "198.51.100.7"


def test_rate_limit_keys_are_hashed():
    key = rate_limit.limit_key("login:fail", "Victim@Example.com")
    assert "victim" not in key.lower() and "example" not in key.lower()
    assert key == rate_limit.limit_key("login:fail", "victim@example.com")  # case-insensitive


def test_rate_limiter_fails_open_when_redis_is_down(monkeypatch):
    import redis.asyncio as redis

    monkeypatch.setattr(rate_limit, "get_redis", lambda: redis.from_url(
        "redis://127.0.0.1:1/0", socket_connect_timeout=0.2, socket_timeout=0.2))
    result = asyncio.run(rate_limit.hit("profexia:rl:test:x", 1, 60))
    assert result.allowed
    assert asyncio.run(rate_limit.peek("profexia:rl:test:x", 1)).allowed


def test_too_many_requests_sets_retry_after():
    exc = rate_limit.too_many_requests(12.2)
    assert exc.status_code == 429 and exc.headers["Retry-After"] == "13"


# --- LiveKit grant shape -------------------------------------------------------------

def test_livekit_learner_grant_cannot_publish_or_admin(monkeypatch):
    monkeypatch.setattr(settings, "livekit_api_key", "k")
    monkeypatch.setattr(settings, "livekit_api_secret", "s")
    monkeypatch.setattr(settings, "livekit_url", "wss://x.livekit.cloud")
    token = LiveKitClient().create_access_token("7", "profexia-session-1", False, True, 30)
    video = jwt.decode(token, "s", algorithms=["HS256"])["video"]
    assert video["room"] == "profexia-session-1"
    assert video["canPublish"] is False
    assert video["roomAdmin"] is False and video["roomCreate"] is False and video["roomList"] is False


# --- Real-time manager (in-memory) -------------------------------------------------------

class FakeSocket:
    def __init__(self):
        self.sent = []

    async def accept(self):
        pass

    async def send_json(self, payload):
        self.sent.append(payload)


def test_memory_backend_delivers_locally_and_respects_exclude():
    async def scenario():
        m = ConnectionManager(backend="memory")
        a, b, other = FakeSocket(), FakeSocket(), FakeSocket()
        await m.connect("course:1", a)
        await m.connect("course:1", b)
        await m.connect("course:2", other)
        await m.broadcast("course:1", {"event": "x"}, exclude=a)
        return a, b, other

    a, b, other = asyncio.run(scenario())
    assert a.sent == [] and b.sent == [{"event": "x"}] and other.sent == []
