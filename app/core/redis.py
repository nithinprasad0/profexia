import redis.asyncio as redis

from app.core.config import settings


def get_redis():
    """Return a Redis client for the configured REDIS_URL.

    Hosted providers such as Upstash require TLS: use a ``rediss://`` URL and
    redis-py enables TLS automatically. Timeouts keep requests from hanging
    when Redis is unreachable; callers close the client with ``aclose()``.
    """
    return redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        health_check_interval=30,
    )
