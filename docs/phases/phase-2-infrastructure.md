# Phase 2: Infrastructure in Docker Compose

**Goal:** `make up` starts every backing service the later phases need, and waits until all of them are healthy.

```mermaid
flowchart TB
    subgraph compose["docker compose (project: async-agentic-workflows)"]
        PG[("postgres :5433<br/>dbs: agentic, temporal, temporal_visibility")]
        TSC["temporal-schema (one-shot)<br/>create / migrate schemas"]
        TS["temporal :7233<br/>server 1.32"]
        TI["temporal-init (one-shot)<br/>register default namespace"]
        TUI["temporal-ui :8233"]
        K[("kafka :9092<br/>KRaft, single node")]
        KI["kafka-init (one-shot)<br/>creates RUN_FINISHED, RUN_FINISHED.DLQ"]
        KUI["kafka-ui :8081"]
        R[("redis :6380<br/>AOF on")]
        WH["webhook-tester :8082<br/>(added in phase 4)"]
        PGW["pgweb :8083<br/>Postgres UI (added in phase 6)"]
        TSC --> PG
        TS --> PG
        TI --> TS
        TUI --> TS
        KI --> K
        KUI --> K
        PGW --> PG
    end
    APP["app processes on the host<br/>API · worker · consumers"] -->|127.0.0.1| TS & K & R & PG
```

## Services

| Service | Image | Host port | Used in |
| --- | --- | --- | --- |
| `postgres` | `postgres:17-alpine` | 5433 | Temporal persistence; `agentic` db for run records (phase 4) and users (phase 5) |
| `temporal-schema` | `temporalio/admin-tools:1.32.1` | — | creates or migrates the Temporal schemas, then exits |
| `temporal` | `temporalio/server:1.32.1` | 7233 | Phase 3 |
| `temporal-init` | `temporalio/admin-tools:1.32.1` | — | waits for the server, registers `default`, then exits |
| `temporal-ui` | `temporalio/ui:2.55.0` | [8233](http://127.0.0.1:8233) | Phase 3 |
| `kafka` | `apache/kafka:4.2.2` | 9092 | Phase 4 |
| `kafka-init` | `apache/kafka:4.2.2` | — | creates topics, then exits |
| `kafka-ui` | `kafbat/kafka-ui:v1.5.0` | [8081](http://127.0.0.1:8081) | Phase 4 |
| `redis` | `redis:8.8-alpine` | 6380 | Phase 6 |
| `webhook-tester` | `tarampampam/webhook-tester:2.3.0` | [8082](http://127.0.0.1:8082) | Phase 4: dummy notification receiver (added in Phase 4) |
| `pgweb` | `sosedoff/pgweb:0.17.0` | [8083](http://127.0.0.1:8083) | Postgres UI: browse tables, run SQL (added in Phase 6) |

All ports bind to `127.0.0.1` only. You can override each host port in `.env` (see `.env.example`). The defaults avoid ports that are often already taken locally (5432, 6379, 8080).

## Design notes

- **Temporal 1.32 on Postgres runs as three services.** The `temporalio/server` image ships with no config and no schema setup. (The old all-in-one `auto-setup` image is deprecated and stops at 1.29. Its server lacks APIs the current UI calls, like `ListWorkers`, which made the Workers page fail with 501.)
  - `temporal-schema` runs `infra/temporal/setup-schema.sh` (`temporal-sql-tool`). It creates `temporal` and `temporal_visibility` and applies any pending migrations. It's safe to re-run, and it upgrades older schemas in place.
  - `temporal` starts from `infra/temporal/config.yaml`, vendored from `temporalio/temporal` v1.32.1 `config/docker.yaml`. That file is a template filled in from environment variables like `DB` and `POSTGRES_SEEDS`. **Bump it whenever the image tag changes.** Server settings that can change at runtime live in `infra/temporal/dynamicconfig.yaml`.
  - `temporal-init` runs `infra/temporal/init.sh`. It waits until `cluster health` reports SERVING, then registers the `default` namespace (72h retention) if it's missing.
  - The server image has no CLI, so its health check only tests that port 7233 is open. The real gRPC health check is in `temporal-init`, and the UI, worker and API wait for it to finish.
- **App database:** `infra/postgres/init/01-app-db.sh` creates `agentic`. It runs only against an empty volume, so after changing it run `make reset`.
- **Postgres UI:** `pgweb` at http://127.0.0.1:8083 browses tables and runs SQL. It connects to the `agentic` database through `PGWEB_DATABASE_URL`, built from the same `POSTGRES_*` / `APP_DB` variables as `postgres`, and its database dropdown also reaches `temporal` and `temporal_visibility`. It has no login, which is fine because it binds to `127.0.0.1` only; don't expose it. A desktop client or `psql` works too: `postgresql://postgres:postgres@127.0.0.1:5433/agentic`.
- **Kafka has two listeners:**
  - `INTERNAL://kafka:29092` for other containers
  - `EXTERNAL://127.0.0.1:9092` for apps on the host

  The external address is advertised as `127.0.0.1` rather than `localhost`. Otherwise clients try `::1` first and log connection errors.
- **Topics are created explicitly** by `kafka-init`, whose topic names are hardcoded. If you override `RUN_FINISHED_TOPIC` or `RUN_FINISHED_DLQ_TOPIC`, change `kafka-init` to match. Topics (`KAFKA_AUTO_CREATE_TOPICS_ENABLE=false`), 3 partitions each. A typo in a topic name fails loudly instead of quietly creating a new topic.
- **Health checks:**
  - Temporal: port check on the server, then `temporal operator cluster health` in `temporal-init`
  - Kafka: `kafka-broker-api-versions.sh`
  - Postgres: `pg_isready`
  - Redis: `redis-cli ping`

  `make up` (`docker compose up -d --wait`) returns only when all of them pass.

## Commands

```bash
make up      # start and wait for healthy
make smoke   # check every service from the host
make ps      # status
make logs    # follow logs (SVC=kafka for one service)
make psql    # SQL shell on the agentic db
make redis-cli
make down    # stop, keep data
make reset   # stop and delete volumes
```

## Done when

- [x] `docker-compose.yml` with named volumes, and a health check on Postgres, Temporal, Kafka and Redis. The UIs (Temporal UI, Kafka UI, pgweb) and webhook-tester have none; nothing waits on them.
- [x] Separate app database from Temporal's (init script)
- [x] All connection URLs in `.env.example`
- [x] `make up` / `down` / `reset` / `logs` / `smoke` (later phases add app, test and shell targets; `make` lists them)
- [x] `make smoke` passes; a real Kafka client on the host sees both topics
