"""Redis token-bucket rate limiting.

Each (bucket, key) pair is a Redis hash {tokens, ts}. One Lua script
(token_bucket.lua) refills the bucket for the time elapsed since `ts`, takes a token
if one is there, and returns the decision, all atomically in one round trip, so any
number of API replicas share the same limits. It reads Redis's clock (TIME), so
replicas never disagree on time.
"""

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

import redis.asyncio as redis
from fastapi import HTTPException, Response
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

# Loaded once at import; RateLimiter registers it and calls it via EVALSHA.
TOKEN_BUCKET_LUA = (Path(__file__).parent / "token_bucket.lua").read_text()

_PERIODS = {"s": 1, "sec": 1, "min": 60, "h": 3600, "hour": 3600}


@dataclass(frozen=True)
class Limit:
    capacity: int  # burst size
    per_second: float  # refill rate

    @classmethod
    def parse(cls, spec: str) -> "Limit":
        """'5/min' -> 5 at once, refilling 5 per minute."""
        m = re.fullmatch(r"\s*(\d+)\s*/\s*([a-z]+)\s*", spec)
        if not m or m.group(2) not in _PERIODS or int(m.group(1)) < 1:
            raise ValueError(f"bad rate limit {spec!r}; expected e.g. '5/min', '10/s', '100/h'")
        n = int(m.group(1))
        return cls(capacity=n, per_second=n / _PERIODS[m.group(2)])


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int  # whole seconds until a token is available; 0 if allowed

    def headers(self) -> dict[str, str]:
        h = {"X-RateLimit-Limit": str(self.limit), "X-RateLimit-Remaining": str(self.remaining)}
        if not self.allowed:
            h["Retry-After"] = str(self.retry_after)
        return h


class RateLimiter:
    def __init__(self, client: redis.Redis, limits: dict[str, Limit], prefix: str = "ratelimit"):
        self._client = client
        self._script = client.register_script(TOKEN_BUCKET_LUA)
        self.limits = limits
        self._prefix = prefix

    async def check(self, bucket: str, key: str) -> Decision | None:
        """Take one token from `bucket` for `key`. Returns None if Redis is
        unreachable: the caller lets the request through (fail open)."""
        limit = self.limits[bucket]
        try:
            allowed, remaining, retry_ms = await self._script(
                keys=[f"{self._prefix}:{bucket}:{key}"], args=[limit.capacity, limit.per_second]
            )
        except (RedisError, OSError) as e:
            log.warning("rate limiter unavailable, allowing request bucket=%s: %s", bucket, e)
            return None
        return Decision(
            allowed=bool(allowed),
            limit=limit.capacity,
            remaining=int(remaining),
            retry_after=math.ceil(int(retry_ms) / 1000),
        )


    async def enforce(self, response: Response, bucket: str, key: str) -> None:
        """Route-level check. Adds X-RateLimit-* headers, or raises 429 with Retry-After."""
        decision = await self.check(bucket, key)
        if decision is None:  # Redis down: fail open
            return
        if not decision.allowed:
            log.warning("rate limited bucket=%s key=%s retry_after=%ds", bucket, key, decision.retry_after)
            raise HTTPException(429, "rate limit exceeded, slow down", headers=decision.headers())
        response.headers.update(decision.headers())
