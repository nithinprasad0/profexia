"""Redis-backed fixed-window rate limiting.

Keys never contain raw identifiers: emails, IPs and user ids are SHA-256 hashed,
and passwords/tokens are never part of a key. Counters expire with their window,
so blocks lift automatically.

If Redis is unreachable the limiter *fails open* (logs a warning and allows the
request). Authentication itself still needs Redis for refresh tokens and will
return 503 in that case, so failing open does not create an unauthenticated
path; it only avoids turning a Redis blip into a total lockout.
"""

import hashlib
import logging
import math
from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.redis import get_redis

logger = logging.getLogger("profexia.ratelimit")
KEY_PREFIX = "profexia:rl"


@dataclass(frozen=True)
class LimitStatus:
    allowed: bool
    count: int
    retry_after: int


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:40]


def limit_key(scope: str, identifier: str) -> str:
    return f"{KEY_PREFIX}:{scope}:{_hash(identifier)}"


def client_ip(request: Request) -> str:
    """Best-effort client IP.

    With TRUSTED_PROXY_COUNT=N, the N-th entry from the *right* of
    X-Forwarded-For is used: entries to its left can be forged by the client,
    entries appended by our own proxies cannot.
    """
    hops = settings.trusted_proxy_count
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return request.client.host if request.client else "unknown"


async def hit(key: str, limit: int, window_seconds: int) -> LimitStatus:
    """Count one event against `key`; allowed while count <= limit."""
    if not settings.rate_limit_enabled or limit <= 0:
        return LimitStatus(True, 0, 0)
    redis = get_redis()
    try:
        async with redis.pipeline(transaction=True) as pipe:
            # SET NX EX starts the window only once; works on Redis >= 2.6.12 (Upstash too).
            pipe.set(key, 0, ex=window_seconds, nx=True)
            pipe.incr(key)
            pipe.ttl(key)
            _, count, ttl = await pipe.execute()
    except RedisError as exc:
        logger.warning("Rate limiter unavailable (%s); allowing request", type(exc).__name__)
        return LimitStatus(True, 0, 0)
    finally:
        await redis.aclose()
    retry_after = ttl if ttl and ttl > 0 else window_seconds
    return LimitStatus(count <= limit, int(count), int(retry_after))


async def peek(key: str, limit: int) -> LimitStatus:
    """Check a counter without incrementing it."""
    if not settings.rate_limit_enabled or limit <= 0:
        return LimitStatus(True, 0, 0)
    redis = get_redis()
    try:
        raw = await redis.get(key)
        ttl = await redis.ttl(key) if raw is not None else 0
    except RedisError as exc:
        logger.warning("Rate limiter unavailable (%s); allowing request", type(exc).__name__)
        return LimitStatus(True, 0, 0)
    finally:
        await redis.aclose()
    count = int(raw or 0)
    return LimitStatus(count < limit, count, int(ttl) if ttl and ttl > 0 else 0)


async def reset(key: str) -> None:
    redis = get_redis()
    try:
        await redis.delete(key)
    except RedisError:
        pass
    finally:
        await redis.aclose()


def too_many_requests(retry_after: int, detail: str = "Too many requests. Please try again later.") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
    )


async def enforce(scope: str, identifier: str, limit: int, window_seconds: int, detail: str | None = None) -> None:
    result = await hit(limit_key(scope, identifier), limit, window_seconds)
    if not result.allowed:
        raise too_many_requests(result.retry_after, detail or "Too many requests. Please try again later.")
