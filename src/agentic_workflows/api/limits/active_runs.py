"""Per-user cap on runs in flight at once.

The rate limit caps how often a user starts runs; this caps how many are running
at the same time, since one run can take many minutes. Each user's admitted runs
are a Redis sorted set {run_id: admitted_at}, and active_runs.lua admits a new one
only while the set has room, atomically.

Nothing removes a run from the set when it finishes. Temporal is the source of
truth instead: when a user is at the cap, each held run is checked with describe()
and the finished ones are dropped before trying again. So a slot can't leak, even
for a run that was terminated and never published RUN_FINISHED.
"""

import asyncio
import logging
from pathlib import Path

import redis.asyncio as redis
from fastapi import HTTPException
from redis.exceptions import RedisError
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

log = logging.getLogger(__name__)

ACTIVE_RUNS_LUA = (Path(__file__).parent / "active_runs.lua").read_text()

_KEY_TTL_S = 24 * 3600


class ActiveRunLimiter:
    def __init__(
        self,
        client: redis.Redis,
        temporal: Client,
        max_active: int,
        prefix: str = "active_runs",
        start_grace_s: float = 30,
    ):
        self._client = client
        self._script = client.register_script(ACTIVE_RUNS_LUA)
        self._temporal = temporal
        self.max_active = max_active
        self._prefix = prefix
        # A run is admitted just before its workflow is started, so for a moment
        # Temporal doesn't know it yet. Don't drop it as stale during that window.
        self._start_grace_s = start_grace_s

    def _key(self, user_id: str) -> str:
        return f"{self._prefix}:{user_id}"

    async def enforce(self, user_id: str, run_id: str) -> None:
        """Take a slot for `run_id`, or raise 429. Redis down: fail open."""
        try:
            admitted, active = await self._admit(user_id, run_id)
            if not admitted and await self._drop_finished(user_id):
                admitted, active = await self._admit(user_id, run_id)
        except (RedisError, OSError) as e:
            log.warning("active-run limiter unavailable, allowing run user_id=%s: %s", user_id, e)
            return
        if not admitted:
            log.warning("active run cap hit user_id=%s active=%d max=%d", user_id, active, self.max_active)
            raise HTTPException(429, f"too many active runs ({active}/{self.max_active}); wait for one to finish")

    async def release(self, user_id: str, run_id: str) -> None:
        """Give the slot back, for a run whose workflow never started."""
        try:
            await self._client.zrem(self._key(user_id), run_id)
        except (RedisError, OSError) as e:
            # Not fatal: the next check at the cap finds no workflow and drops it.
            log.warning("could not release active-run slot run_id=%s: %s", run_id, e)

    async def _admit(self, user_id: str, run_id: str) -> tuple[bool, int]:
        admitted, active = await self._script(keys=[self._key(user_id)], args=[self.max_active, run_id, _KEY_TTL_S])
        return bool(admitted), int(active)

    async def _drop_finished(self, user_id: str) -> int:
        """Remove the user's runs that Temporal says are no longer running. Returns how many."""
        key = self._key(user_id)
        held = await self._client.zrange(key, 0, -1, withscores=True)
        secs, micros = await self._client.time()
        now = secs + micros / 1_000_000
        checks = await asyncio.gather(*(self._finished(rid.decode(), now - admitted_at) for rid, admitted_at in held))
        done = [rid for (rid, _), finished in zip(held, checks) if finished]
        if done:
            await self._client.zrem(key, *done)
            log.info("freed active-run slots user_id=%s runs=%s", user_id, [r.decode() for r in done])
        return len(done)

    async def _finished(self, run_id: str, age_s: float) -> bool:
        try:
            desc = await self._temporal.get_workflow_handle(run_id).describe()
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                # Never started (start failed and the release was lost), unless it's just being started now.
                return age_s > self._start_grace_s
            # Can't tell; keep the slot rather than risk going over the cap.
            log.warning("could not check run run_id=%s: %s", run_id, e)
            return False
        return desc.status != WorkflowExecutionStatus.RUNNING
