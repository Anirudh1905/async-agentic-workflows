"""Login API against the compose Postgres: register, login, token checks, and that
/runs is locked and per-user. Skips when Postgres isn't up (`make up`)."""

import asyncio
import socket
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest

from agentic_workflows import db
from agentic_workflows.api.deps import get_db
from agentic_workflows.api.main import app
from agentic_workflows.config import get_settings

s = get_settings()
_pg_port = int(s.database_url.rsplit(":", 1)[1].split("/")[0])


def _reachable() -> bool:
    try:
        socket.create_connection(("127.0.0.1", _pg_port), timeout=1).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="compose Postgres not running (make up)")


@asynccontextmanager
async def client():
    pool = await db.connect(s.database_url)
    app.dependency_overrides[get_db] = lambda: pool
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, pool
    finally:
        app.dependency_overrides.clear()
        await pool.close()


def email() -> str:
    return f"test-{uuid.uuid4().hex[:10]}@example.com"


async def register_and_login(http, address: str, password: str = "correct horse") -> str:
    r = await http.post("/auth/register", json={"email": address, "password": password})
    assert r.status_code == 201, r.text
    r = await http.post("/auth/login", data={"username": address, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_register_login_and_me():
    async def go():
        async with client() as (http, pool):
            address = email()
            token = await register_and_login(http, address)
            me = (await http.get("/auth/me", headers=bearer(token))).json()
            assert me["email"] == address

            # Emails are case-insensitive: same address in caps is a duplicate, and logs in.
            r = await http.post("/auth/register", json={"email": address.upper(), "password": "something else"})
            assert r.status_code == 409
            r = await http.post("/auth/login", data={"username": address.upper(), "password": "correct horse"})
            assert r.status_code == 200

            # The stored password is a hash, not the password.
            stored = await pool.fetchval("SELECT password_hash FROM users WHERE email = $1", address)
            assert stored.startswith("$argon2id$") and "correct horse" not in stored
            await pool.execute("DELETE FROM users WHERE email = $1", address)

    asyncio.run(go())


def test_login_rejects_bad_credentials_the_same_way():
    async def go():
        async with client() as (http, pool):
            address = email()
            await register_and_login(http, address)
            wrong_password = await http.post("/auth/login", data={"username": address, "password": "nope nope"})
            unknown_user = await http.post("/auth/login", data={"username": email(), "password": "nope nope"})
            assert wrong_password.status_code == unknown_user.status_code == 401
            assert wrong_password.json() == unknown_user.json()  # no hint which one was wrong
            await pool.execute("DELETE FROM users WHERE email = $1", address)

    asyncio.run(go())


def test_register_validates_input():
    async def go():
        async with client() as (http, _):
            assert (await http.post("/auth/register", json={"email": "not-an-email", "password": "long enough"})).status_code == 422
            assert (await http.post("/auth/register", json={"email": email(), "password": "short"})).status_code == 422

    asyncio.run(go())


def test_runs_require_a_valid_token():
    async def go():
        async with client() as (http, pool):
            address = email()
            token = await register_and_login(http, address)
            user_id = (await http.get("/auth/me", headers=bearer(token))).json()["id"]

            expired = jwt.encode(
                {"sub": user_id, "exp": datetime.now(UTC) - timedelta(minutes=1)}, s.jwt_secret, algorithm="HS256"
            )
            forged = jwt.encode({"sub": user_id, "exp": datetime.now(UTC) + timedelta(minutes=5)}, "a-different-secret-that-is-also-32-bytes-long", algorithm="HS256")

            for headers in ({}, bearer("garbage"), bearer(expired), bearer(forged)):
                assert (await http.post("/runs", json={"prompt": "hi"}, headers=headers)).status_code == 401
                assert (await http.get("/runs", headers=headers)).status_code == 401
                assert (await http.get(f"/runs/{uuid.uuid4()}", headers=headers)).status_code == 401

            # A valid token for a deleted user stops working too.
            await pool.execute("DELETE FROM users WHERE email = $1", address)
            assert (await http.get("/auth/me", headers=bearer(token))).status_code == 401

    asyncio.run(go())


def test_list_runs_returns_only_your_own():
    async def go():
        async with client() as (http, pool):
            alice, bob = email(), email()
            alice_token, bob_token = await register_and_login(http, alice), await register_and_login(http, bob)
            alice_id = (await http.get("/auth/me", headers=bearer(alice_token))).json()["id"]
            bob_id = (await http.get("/auth/me", headers=bearer(bob_token))).json()["id"]

            # Rows as the persistence consumer would write them.
            now = datetime.now(UTC)
            runs = {f"test-{uuid.uuid4()}": alice_id, f"test-{uuid.uuid4()}": alice_id, f"test-{uuid.uuid4()}": bob_id}
            for i, (run_id, owner) in enumerate(runs.items()):
                await pool.execute(
                    "INSERT INTO runs (run_id, user_id, status, prompt, answer, started_at, finished_at)"
                    " VALUES ($1, $2, 'completed', 'p', 'a', $3, $4)",
                    run_id, owner, now, now + timedelta(seconds=i),
                )
            try:
                mine = (await http.get("/runs", headers=bearer(alice_token))).json()
                alice_runs = [r for r, o in runs.items() if o == alice_id]
                assert [r["run_id"] for r in mine] == list(reversed(alice_runs))  # newest first
                assert [r["run_id"] for r in (await http.get("/runs", headers=bearer(bob_token))).json()] == [
                    r for r, o in runs.items() if o == bob_id
                ]
            finally:
                await pool.execute("DELETE FROM runs WHERE run_id = ANY($1::text[])", list(runs))
                await pool.execute("DELETE FROM users WHERE email = ANY($1::text[])", [alice, bob])

    asyncio.run(go())
