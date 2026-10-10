.PHONY: up down reset ps logs smoke

up:     ## start all services and wait until healthy
	docker compose up -d --wait

down:   ## stop services, keep data
	docker compose down

reset:  ## stop services and delete all data volumes
	docker compose down -v

ps:
	docker compose ps -a

logs:
	docker compose logs -f --tail=100

smoke:  ## check every service is reachable from the host
	uv run --env-file .env python scripts/smoke.py
