# Phase 2: Infrastructure in Docker Compose

**Goal:** `docker compose up` starts every backing service the later phases need, with health checks.

```mermaid
flowchart TB
    subgraph compose["docker compose"]
        PG[(Postgres :5432<br/>temporal db + app db)]
        TS[Temporal server :7233]
        TUI[Temporal UI :8080]
        K[(Kafka KRaft :9092)]
        KUI[Kafka UI :8081]
        R[(Redis :6379)]
        TS --> PG
        TUI --> TS
        KUI --> K
    end
    APP[app: API + worker + consumers<br/>runs on host for now] --> TS & K & R & PG
```

## Services

| Service | Image | Port | Used in |
|---|---|---|---|
| Postgres | `postgres` | 5432 | Temporal persistence, run records (phase 4), users (phase 5) |
| Temporal server | `temporalio/auto-setup` | 7233 | Phase 3 |
| Temporal UI | `temporalio/ui` | 8080 | Phase 3 |
| Kafka (KRaft, no ZooKeeper) | `apache/kafka` | 9092 | Phase 4 |
| Kafka UI (optional) | `provectuslabs/kafka-ui` | 8081 | Phase 4 |
| Redis | `redis` | 6379 | Phase 6 |

## Tasks

- [ ] `docker-compose.yml` with named volumes and a health check on every service
- [ ] A separate database in Postgres for the app versus Temporal (init script)
- [ ] Add all connection URLs to `.env.example`
- [ ] `make up` / `make down` / `make logs` helpers
- [ ] Smoke check: script that pings each service

## Done when

`docker compose up -d && docker compose ps` shows every service as healthy.
