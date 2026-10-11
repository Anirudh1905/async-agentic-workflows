"""Token-bucket rate limiting: the Lua script against the compose Redis, and the
429 behaviour through the real API routes. Redis tests skip when it isn't up."""

import asyncio
import socket
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
import redis.asyncio as redis

from agentic_workflows.api.deps import get_db, get_limiter
from agentic_workflows.api.limits.ratelimit import Limit, RateLimiter
from agentic_workflows.api.main import app
from agentic_workflows.api.routes.auth import get_current_user
from agentic_workflows.api.schemas import User
from agentic_workflows.config import get_settings

REDIS_URL = get_settings().redis_url
_port = int(REDIS_URL.rsplit(":", 1)[1].split("/")[0])


def _reachable() -> bool:
    try:
        socket.create_connection(("127.0.0.1", _port), timeout=1).close()
        return True
    except OSError:
        return False


needs_redis = pytest.mark.skipif(not _reachable(), reason="compose Redis not running (make up)")


def test_limit_parse():
    assert Limit.parse("5/min") == Limit(capacity=5, per_second=5 / 60)
    assert Limit.parse(" 10 / s ") == Limit(capacity=10, per_second=10)
    assert Limit.parse("100/h") == Limit(capacity=100, per_second=100 / 3600)
    for bad in ("5", "0/min", "5/day", "five/min"):
        with pytest.raises(ValueError):
            Limit.parse(bad)


@asynccontextmanager
async def limiter(**limits: str):
    client = redis.from_url(REDIS_URL)
    # A unique prefix per test, so buckets never leak between tests or runs.
    lim = RateLimiter(client, {k: Limit.parse(v) for k, v in limits.items()}, prefix=f"test-{uuid.uuid4().hex}")
    try:
        yield lim, client
    finally:
        await client.aclose()


@needs_redis
def test_burst_then_429_then_refill():
    async def go():
        async with limiter(b="3/s") as (lim, _):
            burst = [await lim.check("b", "k") for _ in range(4)]
            await asyncio.sleep(0.4)  # 3/s refills one token every ~0.33s
            after = await lim.check("b", "k")
            return burst, after

    burst, after = asyncio.run(go())
    assert [d.allowed for d in burst] == [True, True, True, False]
    assert [d.remaining for d in burst] == [2, 1, 0, 0]
    assert burst[3].retry_after == 1  # ~0.33s, rounded up to whole seconds
    assert after.allowed


@needs_redis
def test_keys_and_buckets_are_independent():
    async def go():
        async with limiter(a="1/min", b="1/min") as (lim, _):
            return [
                (await lim.check("a", "alice")).allowed,
                (await lim.check("a", "alice")).allowed,  # alice's bucket a is empty
                (await lim.check("a", "bob")).allowed,  # bob has his own
                (await lim.check("b", "alice")).allowed,  # and so does bucket b
            ]

    assert asyncio.run(go()) == [True, False, True, True]


@needs_redis
def test_idle_buckets_expire():
    async def go():
        async with limiter(b="2/s") as (lim, client):
            await lim.check("b", "k")
            [key] = [k async for k in client.scan_iter(match=f"{lim._prefix}:b:*")]
            return await client.pttl(key)

    # Full refill takes 1s, so the key is set to expire a little after that.
    assert 0 < asyncio.run(go()) <= 2000


def test_fails_open_when_redis_is_down():
    async def go():
        client = redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)  # nothing listens here
        try:
            return await RateLimiter(client, {"b": Limit.parse("1/min")}).check("b", "k")
        finally:
            await client.aclose()

    assert asyncio.run(go()) is None  # None = let the request through


# --- through the API -----------------------------------------------------------------


class _EmptyDb:
    """No users, no runs: enough for the routes to answer without Postgres."""

    async def fetch(self, *args):
        return []

    async def fetchrow(self, *args):
        return None


@asynccontextmanager
async def api(lim, user: User | None = None):
    app.dependency_overrides[get_limiter] = lambda: lim
    app.dependency_overrides[get_db] = lambda: _EmptyDb()
    if user:
        app.dependency_overrides[get_current_user] = lambda: user
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.clear()


@needs_redis
def test_login_is_limited_per_ip_with_retry_after():
    async def go():
        async with limiter(auth="2/min") as (lim, _), api(lim) as http:
            # The bucket is taken before credentials are checked, so bad logins count too.
            return [await http.post("/auth/login", data={"username": "x@example.com", "password": "nope-nope"}) for _ in range(3)]

    first, second, third = asyncio.run(go())
    assert first.status_code == second.status_code == 401  # wrong password, but let through
    assert third.status_code == 429
    assert third.headers["Retry-After"] == "30"  # 2/min refills one token every 30s
    assert third.headers["X-RateLimit-Limit"] == "2" and third.headers["X-RateLimit-Remaining"] == "0"


@needs_redis
def test_runs_are_limited_per_user_and_report_remaining():
    alice, bob = User(id="alice", email="a@example.com"), User(id="bob", email="b@example.com")

    async def go():
        async with limiter(runs_read="2/min") as (lim, _):
            async with api(lim, alice) as http:
                alice_calls = [await http.get("/runs") for _ in range(3)]
            async with api(lim, bob) as http:
                bob_call = await http.get("/runs")
            return alice_calls, bob_call

    alice_calls, bob_call = asyncio.run(go())
    assert [r.status_code for r in alice_calls] == [200, 200, 429]
    assert [r.headers.get("X-RateLimit-Remaining") for r in alice_calls[:2]] == ["1", "0"]
    assert bob_call.status_code == 200  # alice using up her budget doesn't touch bob's
