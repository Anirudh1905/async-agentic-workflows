import asyncio
import logging
import uuid

import pytest

from agentic_workflows.logs import setup_logging


@pytest.fixture
def restore_root():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    root.handlers[:], root.level = handlers, level


def test_setup_logging_is_idempotent_and_tags_the_service(restore_root):
    setup_logging("worker")
    setup_logging("worker")
    [handler] = restore_root.handlers  # re-running replaces, doesn't stack
    record = logging.LogRecord("agentic_workflows.x", logging.INFO, __file__, 1, "run started run_id=%s", ("r1",), None)
    assert "INFO  [worker] agentic_workflows.x: run started run_id=r1" in handler.format(record)


def test_noisy_libraries_are_quieted_at_info(restore_root):
    setup_logging("api")
    assert logging.getLogger("aiokafka").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("agentic_workflows").getEffectiveLevel() == logging.INFO


def test_auth_logs_never_contain_passwords_tokens_or_failed_emails(caplog):
    # Runs against the compose Postgres, like test_auth.py.
    from tests.test_auth import _reachable, client, register_and_login

    if not _reachable():
        pytest.skip("compose Postgres not running (make up)")
    address, password = f"test-{uuid.uuid4().hex[:10]}@example.com", "super-secret-pass"

    async def go():
        async with client() as (http, pool):
            token = await register_and_login(http, address, password)
            await http.post("/auth/login", data={"username": address, "password": "wrong-password"})
            await pool.execute("DELETE FROM users WHERE email = $1", address)
            return token

    with caplog.at_level(logging.INFO, logger="agentic_workflows"):
        token = asyncio.run(go())

    text = caplog.text
    assert "user registered" in text and "login ok" in text and "login failed" in text
    for secret in (password, "wrong-password", token, address):
        assert secret not in text
