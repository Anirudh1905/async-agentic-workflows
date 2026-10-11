# Local dev loop: `tilt up`, then open http://localhost:10350.
# Optional — `make up` plus starting the processes by hand works without Tilt.

# Pin the project name: Tilt ignores the compose file's `name:` and would otherwise
# start a second stack named after the directory, clashing with `make up`.
docker_compose("docker-compose.yml", project_name="async-agentic-workflows")

# Compose services, grouped by what they belong to in the dashboard.
for svc in ["temporal-schema", "temporal", "temporal-init"]:
    dc_resource(svc, labels=["temporal"])
dc_resource("temporal-ui", labels=["temporal"], links=[link("http://127.0.0.1:8233", "Temporal UI")])

for svc in ["kafka", "kafka-init"]:
    dc_resource(svc, labels=["kafka"])
dc_resource("kafka-ui", labels=["kafka"], links=[link("http://127.0.0.1:8081", "Kafka UI")])

for svc in ["postgres", "redis"]:
    dc_resource(svc, labels=["data"])
dc_resource("pgweb", labels=["data"], links=[link("http://127.0.0.1:8083", "Postgres UI")])

dc_resource("webhook-tester", labels=["tools"], links=[link("http://127.0.0.1:8082/s/a9e7c0de-0000-4000-8000-000000000001", "Webhook inbox")])

# App processes on the host. Tilt restarts them when anything under src/ changes,
# and only starts them once Temporal is serving and the namespace exists.
local_resource(
    "worker",
    serve_cmd="uv run worker",
    deps=["src"],
    resource_deps=["temporal-init"],
    labels=["app"],
)

local_resource(
    "api",
    serve_cmd="uv run uvicorn agentic_workflows.api.main:app --port 8000",
    deps=["src"],
    resource_deps=["temporal-init"],
    readiness_probe=probe(http_get=http_get_action(port=8000, path="/healthz")),
    links=[link("http://127.0.0.1:8000/docs", "API docs")],
    labels=["app"],
)

# RUN_FINISHED consumers, one consumer group each so both see every event.
for kind in ["persistence", "notifications"]:
    local_resource(
        "consumer-" + kind,
        serve_cmd="uv run consumer " + kind,
        deps=["src"],
        resource_deps=["kafka-init", "postgres"],
        labels=["app"],
    )

# Testing-only chat UI; click ▶ in the dashboard to start it.
local_resource(
    "streamlit",
    serve_cmd="uv run --group ui streamlit run ui/app.py --server.headless true --server.port 8501",
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
    links=[link("http://127.0.0.1:8501", "Streamlit")],
    labels=["tools"],
)

# One-click actions (manual resources act as buttons).
local_resource(
    "send-test-run",
    cmd="uv run python scripts/send_test_run.py",
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
    resource_deps=["api", "worker"],
    labels=["tools"],
)

local_resource(
    "smoke",
    cmd="make smoke",
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
    labels=["tools"],
)
