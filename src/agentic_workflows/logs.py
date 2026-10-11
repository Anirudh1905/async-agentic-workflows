"""One logging setup for every process (API, worker, consumers, CLI).

Each entry point calls `setup_logging("<service>")` once. Lines look like

    2026-10-11 19:51:18 INFO  [worker] agentic_workflows.agent.tools: web_search done query='kafka' results=5 in 1.2s

Modules log with `logging.getLogger(__name__)`. Key facts go in the message as
key=value pairs (run_id=..., user_id=...) so they're easy to grep in Tilt.
Never log secrets: passwords, tokens, API keys, full prompts or answers at INFO.
"""

import logging
import sys

from temporalio import activity, workflow

from agentic_workflows.config import get_settings

# Chatty at INFO; only their warnings are useful unless LOG_LEVEL=DEBUG.
_NOISY = ("httpx", "httpcore", "openai", "aiokafka", "primp", "ddgs", "asyncio", "temporalio.worker._activity")


def setup_logging(service: str) -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(f"%(asctime)s %(levelname)-5s [{service}] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]  # idempotent: replace, don't stack, on re-import / reload
    root.setLevel(level)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.DEBUG if level <= logging.DEBUG else logging.WARNING)

    # uvicorn installs its own handlers; route its loggers through ours so the API
    # looks like every other process.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _DropHealthChecks) for f in access.filters):
        access.addFilter(_DropHealthChecks())  # Tilt's readiness probe hits it every few seconds

    # Temporal's loggers append a long dict of workflow/activity info to every line,
    # including Temporal's internal run id (not ours). step_logger() and the
    # workflow's own messages carry the useful parts instead.
    activity.logger.activity_info_on_message = False
    workflow.logger.workflow_info_on_message = False


class _DropHealthChecks(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


class _StepAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[{self.extra['tag']}] {msg}", kwargs


def step_logger(logger: logging.Logger) -> logging.Logger | logging.LoggerAdapter:
    """Inside a Temporal activity, prefix lines with `[run_id=... <activity> attempt=N]`
    (the workflow id is our run_id). Outside one (CLI, Streamlit), return `logger` as is."""
    if not activity.in_activity():
        return logger
    info = activity.info()
    return _StepAdapter(logger, {"tag": f"run_id={info.workflow_id} {info.activity_type} attempt={info.attempt}"})
