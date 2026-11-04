"""WebSocket room manager with optional Redis pub/sub fan-out.

Every API instance keeps the sockets connected *to it* in memory. When an event
is broadcast:

1. it is delivered immediately to matching sockets on this instance, and
2. with REALTIME_BACKEND=redis it is also published on one Redis channel; every
   other instance receives it and delivers it to *its* local sockets.

Instances ignore their own published events (they already delivered them in
step 1), so each client receives an event exactly once. If Redis is down,
step 1 still works, so a single instance keeps functioning; cross-instance
delivery resumes automatically when Redis is back (the listener reconnects with
backoff). Delivery is best-effort / at-most-once: there is no replay of events
published while an instance was disconnected. Clients should re-fetch history
(e.g. GET /chat/{room}/messages) after reconnecting.
"""

import asyncio
import json
import logging
import uuid
from collections import defaultdict

import redis.asyncio as redis
from fastapi import WebSocket

from app.core.config import settings

logger = logging.getLogger("profexia.realtime")


class ConnectionManager:
    def __init__(self, backend: str | None = None, redis_url: str | None = None, channel: str | None = None):
        self.instance_id = uuid.uuid4().hex
        self._rooms: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._backend = (backend or settings.realtime_backend).lower()
        self._redis_url = redis_url or settings.redis_url
        self._channel = channel or settings.realtime_channel
        self._publisher: redis.Redis | None = None
        self._listener: asyncio.Task | None = None
        self._subscribed = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle
    @property
    def uses_redis(self) -> bool:
        return self._backend == "redis"

    def _client(self) -> redis.Redis:
        # No socket_timeout: a pub/sub connection is idle most of the time.
        return redis.from_url(
            self._redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_socket_timeout_seconds,
            health_check_interval=30,
        )

    async def start(self) -> None:
        if not self.uses_redis or self._listener is not None:
            return
        self._subscribed = asyncio.Event()
        self._publisher = self._client()
        self._listener = asyncio.create_task(self._listen(), name="realtime-redis-listener")

    async def stop(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            try:
                await self._listener
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._listener = None
        if self._publisher is not None:
            try:
                await self._publisher.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._publisher = None

    async def wait_until_subscribed(self, timeout: float = 5.0) -> bool:
        try:
            await asyncio.wait_for(self._subscribed.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _listen(self) -> None:
        backoff = 1.0
        while True:
            client = self._client()
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.subscribe(self._channel)
                self._subscribed.set()
                backoff = 1.0
                logger.info("Real-time fan-out subscribed to Redis channel")
                while True:
                    message = await pubsub.get_message(timeout=1.0)
                    if message is None or message.get("type") != "message":
                        continue
                    await self._handle_remote(message.get("data"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._subscribed.clear()
                logger.warning("Real-time Redis listener error (%s); retrying in %.0fs", type(exc).__name__, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                try:
                    await pubsub.aclose()
                    await client.aclose()
                except Exception:  # noqa: BLE001
                    pass

    async def _handle_remote(self, raw: str | None) -> None:
        try:
            envelope = json.loads(raw or "")
            if envelope.get("origin") == self.instance_id:
                return  # already delivered locally
            room, payload = envelope["room"], envelope["payload"]
        except (ValueError, KeyError, TypeError, AttributeError):
            logger.warning("Ignoring malformed real-time envelope")
            return
        await self._deliver_local(str(room), payload)

    # ---------------------------------------------------------------- operations
    async def connect(self, room: str, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self._rooms[room].add(websocket)

    async def disconnect(self, room: str, websocket: WebSocket):
        async with self._lock:
            peers = self._rooms.get(room)
            if peers:
                peers.discard(websocket)
                if not peers:
                    self._rooms.pop(room, None)

    async def _deliver_local(self, room: str, payload: dict, exclude: WebSocket | None = None):
        async with self._lock:
            peers = list(self._rooms.get(room, set()))
        dead = []
        for websocket in peers:
            if websocket is exclude:
                continue
            try:
                await websocket.send_json(payload)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            await self.disconnect(room, websocket)

    async def broadcast(self, room: str, payload: dict, exclude: WebSocket | None = None):
        await self._deliver_local(room, payload, exclude)
        if self.uses_redis and self._publisher is not None:
            envelope = json.dumps({"origin": self.instance_id, "room": room, "payload": payload}, default=str)
            try:
                await self._publisher.publish(self._channel, envelope)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Real-time publish failed (%s); delivered locally only", type(exc).__name__)


manager = ConnectionManager()
