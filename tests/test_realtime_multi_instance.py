"""Simulate two API instances sharing Redis pub/sub for WebSocket fan-out."""

import asyncio
import uuid

import pytest

from app.core.config import settings
from app.infrastructure.realtime import ConnectionManager

pytestmark = pytest.mark.usefixtures("redis_required")


class FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def accept(self):
        pass

    async def send_json(self, payload):
        self.sent.append(payload)


async def _instances(channel: str):
    a = ConnectionManager(backend="redis", redis_url=settings.redis_url, channel=channel)
    b = ConnectionManager(backend="redis", redis_url=settings.redis_url, channel=channel)
    await a.start()
    await b.start()
    assert await a.wait_until_subscribed() and await b.wait_until_subscribed()
    return a, b


async def _settle():
    for _ in range(40):
        await asyncio.sleep(0.05)


def test_event_reaches_client_on_other_instance_exactly_once():
    async def scenario():
        a, b = await _instances(f"profexia:test:{uuid.uuid4().hex}")
        try:
            on_a, on_b, sender = FakeSocket(), FakeSocket(), FakeSocket()
            await a.connect("course:1", on_a)
            await a.connect("course:1", sender)
            await b.connect("course:1", on_b)
            await a.broadcast("course:1", {"event": "message", "n": 1}, exclude=sender)
            await _settle()
            return on_a.sent, on_b.sent, sender.sent
        finally:
            await a.stop()
            await b.stop()

    on_a, on_b, sender = asyncio.run(scenario())
    assert on_a == [{"event": "message", "n": 1}]   # local delivery, not duplicated by the echo
    assert on_b == [{"event": "message", "n": 1}]   # delivered via Redis to the other instance
    assert sender == []                              # exclude still honoured


def test_events_are_scoped_to_their_room_across_instances():
    async def scenario():
        a, b = await _instances(f"profexia:test:{uuid.uuid4().hex}")
        try:
            other_room, user_room = FakeSocket(), FakeSocket()
            await b.connect("course:2", other_room)
            await b.connect("user:7", user_room)
            await a.broadcast("user:7", {"event": "notification"})
            await _settle()
            return other_room.sent, user_room.sent
        finally:
            await a.stop()
            await b.stop()

    other_room, user_room = asyncio.run(scenario())
    assert other_room == [] and user_room == [{"event": "notification"}]


def test_instance_without_redis_still_delivers_locally():
    async def scenario():
        m = ConnectionManager(backend="redis", redis_url="redis://127.0.0.1:1/0", channel="x")
        await m.start()
        try:
            s = FakeSocket()
            await m.connect("course:1", s)
            await m.broadcast("course:1", {"event": "local"})
            return s.sent
        finally:
            await m.stop()

    assert asyncio.run(scenario()) == [{"event": "local"}]


def test_malformed_envelopes_are_ignored():
    async def scenario():
        m = ConnectionManager(backend="memory")
        s = FakeSocket()
        await m.connect("course:1", s)
        for raw in ("not json", "{}", '{"room": "course:1"}', "[1,2]"):
            await m._handle_remote(raw)
        return s.sent

    assert asyncio.run(scenario()) == []
