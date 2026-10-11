.PHONY: help up down reset ps logs smoke psql redis-cli dev worker api consumer-persistence consumer-notifications ui test test-run
.DEFAULT_GOAL := help

PROMPT ?= What is the latest Python release? Search it.

help:   ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

# --- compose stack ---------------------------------------------------------------

up:     ## start all services and wait until healthy
	docker compose up -d --wait

down:   ## stop services, keep data
	docker compose down

reset:  ## stop services and delete all data volumes
	docker compose down -v

ps:     ## service status
	docker compose ps -a

logs:   ## follow logs (all services, or SVC=kafka)
	docker compose logs -f --tail=100 $(SVC)

smoke:  ## check every service is reachable from the host
	uv run --env-file .env python scripts/smoke.py

psql:   ## SQL shell on the agentic db (web UI: http://127.0.0.1:8083)
	docker compose exec postgres psql -U postgres -d agentic

redis-cli:  ## Redis shell (rate-limit buckets, active-run sets)
	docker compose exec redis redis-cli

# --- app processes (one terminal each, or `make dev` for all of them) -----------

dev:    ## stack + app processes under Tilt, dashboard http://localhost:10350
	tilt up

worker: ## Temporal worker: AgentRunWorkflow + agent activities
	uv run worker

api:    ## FastAPI on :8000 (docs at /docs)
	uv run uvicorn agentic_workflows.api.main:app --port 8000

consumer-persistence:   ## RUN_FINISHED -> Postgres runs
	uv run consumer persistence

consumer-notifications: ## RUN_FINISHED -> webhook
	uv run consumer notifications

ui:     ## testing-only Streamlit chat on :8501
	uv run --group ui streamlit run ui/app.py

# --- testing ---------------------------------------------------------------------

test:   ## all tests; Kafka/Postgres/Redis ones skip unless `make up` is running
	uv run pytest

test-run:  ## log in as the dev user and start one run (PROMPT="...")
	uv run python scripts/send_test_run.py "$(PROMPT)"
