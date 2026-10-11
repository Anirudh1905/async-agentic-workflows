# Phase 4: Kafka producer and consumers

**Goal:** a finished run publishes `RUN_FINISHED`, and independent consumers react to it: one records the run in Postgres, one sends a notification.

```mermaid
flowchart LR
    subgraph worker["Temporal worker"]
        WF[AgentRunWorkflow] --> ACT1[run_agent_activity]
        ACT1 -- "answer / out of retries" --> ACT2["publish_run_finished<br/>(retries until Kafka accepts)"]
    end
    ACT2 -- "key = run_id<br/>acks=all, idempotent" --> TOPIC[("RUN_FINISHED<br/>3 partitions")]
    TOPIC --> P["consumer persistence<br/>group: persistence"]
    TOPIC --> N["consumer notifications<br/>group: notifications"]
    P -- "UPSERT on run_id" --> RUNS[("Postgres<br/>runs")]
    N -- "claim event_id" --> SENT[("Postgres<br/>notifications_sent")]
    N --> OUT["log line + optional<br/>NOTIFY_WEBHOOK_URL POST"]
    P & N -. "3 failed attempts<br/>or bad payload" .-> DLQ[("RUN_FINISHED.DLQ")]
```

## What's built

```
src/agentic_workflows/
  events/producer.py      make_producer(), EventActivities.publish_run_finished
  consumers/
    runner.py             consume loop: parse → handle with retries → DLQ → commit
    handlers.py           persistence_handler, notification_handler
    main.py               `uv run consumer persistence|notifications`
  db.py                   asyncpg pool + idempotent schema (runs, notifications_sent)
  workflows/models.py     + RunFinished event
```

### Event: `RUN_FINISHED`

| Field                           | Notes                                                                                   |
| ------------------------------- | --------------------------------------------------------------------------------------- |
| `event_id`                    | `"{run_id}:RUN_FINISHED"`. Deterministic, one per run, so consumers can dedupe on it. |
| `run_id`, `prompt`          |                                                                                         |
| `status`                      | `completed` or `failed`                                                             |
| `answer` / `error`          | One of the two is set                                                                   |
| `started_at`, `finished_at` | ISO-8601 strings, taken from`workflow.now()` (deterministic)                          |
| `user_id`                     | `null` until Phase 5                                                                  |

JSON in the value, `run_id` as the key, no headers. The topic already says what the event is; add an `event_type` header only if one topic ever carries several event types. One dataclass (`RunFinished`) serves as the Temporal activity input, the wire format and the consumer's parse target (via a pydantic `TypeAdapter`), so there's no second schema to drift.

### Producer: inside the workflow

- **Every run publishes, including failed ones.** After the agent activity, the workflow calls `publish_run_finished`. If the agent runs out of retries, the workflow first publishes a `failed` event, then fails. Downstream hears about every run either way.
- **No retry limit on publishing** (`maximum_interval=30s`). If Kafka is down, the run waits in the `publishing` step, shown by the status query, rather than finishing without telling anyone downstream.
- **One producer per worker.** It's created when the worker starts and shared through a class-based activity (`EventActivities`), with `acks=all` and `enable_idempotence=True`.
- **Keyed by `run_id`**, so all events for a run land on the same partition, in order.

### Consumers

- **Separate consumer groups** (`persistence`, `notifications`), so each one sees every event and they scale independently.
- **At-least-once delivery.** Auto-commit is off; the offset is committed only after the handler succeeds or the message is parked in the DLQ. A crash means replay, so the handlers are idempotent:
  - **Persistence:** `INSERT ... ON CONFLICT (run_id) DO UPDATE`. A replay rewrites the same row.
  - **Notifications:** claim `event_id` in `notifications_sent` (`ON CONFLICT DO NOTHING RETURNING`), then send. If the claim already exists, skip. If sending fails, release the claim so the retry or a replay can send it. Sending is a log line, plus a POST of the event JSON to `NOTIFY_WEBHOOK_URL` if that's set.
- **Retries and DLQ.** Each message gets 3 attempts with backoff (1s, 2s). Then it goes to `RUN_FINISHED.DLQ` with headers `error`, `consumer` and `source` (`topic[partition]@offset`), and the offset is committed so one bad message can't block a partition. Unparseable payloads skip the retries and go straight to the DLQ.
- **Schema:** each consumer runs `CREATE TABLE IF NOT EXISTS` on startup, under a Postgres advisory lock because both consumers start at the same moment. Phase 5 adds users, which is a good point to switch to a migration tool.

## Kafka settings

```mermaid
flowchart TB
    PROD["Temporal worker · producer<br/>acks=all · enable_idempotence=True"]
    PROD -- "send_and_wait(key=run_id)<br/>hash(key) → one partition" --> T

    subgraph T["topic RUN_FINISHED · 3 partitions"]
        direction LR
        P0[(p0)] ~~~ P1[(p1)] ~~~ P2[(p2)]
    end

    T -- "all partitions" --> CP["consumer persistence<br/>group_id=persistence<br/>offsets: p0 · p1 · p2"]
    T -- "all partitions" --> CN["consumer notifications<br/>group_id=notifications<br/>offsets: p0 · p1 · p2"]

    CP -. "3 fails / bad payload" .-> D[("topic RUN_FINISHED.DLQ<br/>same key + bytes, error headers")]
    CN -. "3 fails / bad payload" .-> D
```

All app processes run on the host and connect through the `EXTERNAL` listener (`127.0.0.1:9092`). After each message, a consumer commits its offset (one per group per partition) to Kafka's internal `__consumer_offsets` topic.

**One send, two readers.** The worker sends each event once. Kafka stores it on one partition, chosen by hashing the `run_id` key. Each consumer group tracks its own committed offset per partition, so `persistence` and `notifications` both read every event independently. Running a second copy of a consumer with the same `group_id` would split the 3 partitions between the copies instead of duplicating work.

### Broker (`docker-compose.yml` → `kafka`)

| Setting                                                                       | Value                                                                    | Why                                                                                                                    |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| `KAFKA_PROCESS_ROLES`                                                       | `broker,controller`                                                    | Single-node KRaft: no ZooKeeper                                                                                        |
| `KAFKA_LISTENERS`                                                           | `INTERNAL://:29092`, `EXTERNAL://:9092`, `CONTROLLER://:9093`      | One listener for containers, one for the host, one for KRaft                                                           |
| `KAFKA_ADVERTISED_LISTENERS`                                                | `INTERNAL://kafka:29092`, `EXTERNAL://127.0.0.1:${KAFKA_PORT:-9092}` | Clients reconnect to the advertised address.`127.0.0.1` (not `localhost`) stops clients from trying `::1` first. |
| `KAFKA_INTER_BROKER_LISTENER_NAME`                                          | `INTERNAL`                                                             |                                                                                                                        |
| `KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR`, `KAFKA_TRANSACTION_STATE_LOG_*` | `1`                                                                    | Only one broker, so internal topics can't be replicated                                                                |
| `KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS`                                    | `0`                                                                    | Consumers get their partitions right away at startup                                                                   |
| `KAFKA_AUTO_CREATE_TOPICS_ENABLE`                                           | `false`                                                                | A misspelled topic fails loudly instead of creating a new one                                                          |

**Topics** are created by the `kafka-init` one-shot: `RUN_FINISHED` and `RUN_FINISHED.DLQ`, each with `--partitions 3 --replication-factor 1`.

### App settings (`config.py`, overridable in `.env`)

| Env var                     | Default                                                   | Used by                                      |
| --------------------------- | --------------------------------------------------------- | -------------------------------------------- |
| `KAFKA_BOOTSTRAP_SERVERS` | `127.0.0.1:9092`                                        | producer, both consumers, DLQ producer       |
| `RUN_FINISHED_TOPIC`      | `RUN_FINISHED`                                          | producer topic, consumer subscription        |
| `RUN_FINISHED_DLQ_TOPIC`  | `RUN_FINISHED.DLQ`                                      | where consumers park failed messages         |
| `DATABASE_URL`            | `postgresql://postgres:postgres@127.0.0.1:5433/agentic` | both consumers                               |
| `NOTIFY_WEBHOOK_URL`      | unset                                                     | notification consumer (unset means log only) |

### Producer args (`events/producer.py`)

`AIOKafkaProducer(...)`, created once per worker in `make_producer()`:

| Arg                    | Value                       | Effect                                                                                                                                                                                |
| ---------------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bootstrap_servers`  | `KAFKA_BOOTSTRAP_SERVERS` | First broker to contact. Kafka then hands back the advertised listener.                                                                                                               |
| `acks`               | `"all"`                   | `send_and_wait` returns only once all in-sync replicas have the write. With a single broker that's the leader, but the code stays correct with more brokers.                        |
| `enable_idempotence` | `True`                    | The broker drops duplicates from the producer's own internal retries. It does**not** dedupe a second `publish_run_finished` attempt from Temporal; the consumers handle that. |

`send_and_wait(...)` per event:

| Arg         | Value                                 | Effect                                                                                             |
| ----------- | ------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `topic`   | `RUN_FINISHED_TOPIC`                |                                                                                                    |
| `key`     | `run_id.encode()`                   | The default partitioner hashes the key, so all events for a run go to the same partition, in order |
| `value`   | JSON of`RunFinished`                |                                                                                                    |

Not set, so aiokafka's defaults apply: compression (none), `linger_ms` (0, sends right away), `max_request_size` (1 MB). Answers easily fit under 1 MB.

### Consumer args (`consumers/runner.py`)

`AIOKafkaConsumer(...)`, one per consumer process:

| Arg                    | Value                                                   | Effect                                                                                                                                                                                                                                                               |
| ---------------------- | ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `topic`              | `RUN_FINISHED_TOPIC`                                  | Subscribes to all 3 partitions; the group coordinator assigns them                                                                                                                                                                                                   |
| `bootstrap_servers`  | `KAFKA_BOOTSTRAP_SERVERS`                             |                                                                                                                                                                                                                                                                      |
| `group_id`           | `persistence` or `notifications` (the CLI argument) | Separate groups mean separate offsets, so each group sees every event                                                                                                                                                                                                |
| `enable_auto_commit` | `False` (aiokafka default: `True`)                  | The loop calls`consumer.commit()` after each message, once the handler succeeded or the message was parked in the DLQ. That's at-least-once: a crash before the commit means the message is redelivered.                                                           |
| `auto_offset_reset`  | `"earliest"` (aiokafka default: `"latest"`)         | Only matters when a group has**no** committed offset (first start, or after a reset). It then reads from the beginning instead of skipping existing events. With `"latest"`, events published while a brand-new consumer group was down would never be read. |

**DLQ producer**, also in `runner.py`: `AIOKafkaProducer(bootstrap_servers=..., acks="all")`, with no idempotence (it sends rarely, and a duplicate there is harmless). `send_and_wait(dlq_topic, key=msg.key, value=msg.value, headers=[error, consumer, source])` keeps the original key and bytes, so a parked message can be replayed exactly.

## Is it working?

Yes. These were captured on the running stack after one live run:

```bash
docker compose exec kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:29092 --describe --topic RUN_FINISHED
```

```
Topic: RUN_FINISHED  PartitionCount: 3  ReplicationFactor: 1
  Partition: 0  Leader: 1  Replicas: 1  Isr: 1
  Partition: 1  Leader: 1  Replicas: 1  Isr: 1
  Partition: 2  Leader: 1  Replicas: 1  Isr: 1
```

```bash
docker compose exec kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server kafka:29092 --describe --group persistence   # same for notifications
```

```
GROUP        TOPIC         PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG
persistence  RUN_FINISHED  0          1               1               0
persistence  RUN_FINISHED  1          0               0               0
persistence  RUN_FINISHED  2          0               0               0
```

- **One send, one partition:** the run's single event sits on partition 0 (log-end offset 1). Its `run_id` hashed there.
- **Both groups read it:** `persistence` and `notifications` are each at offset 1 with **lag 0**.
- **Nothing failed:** `RUN_FINISHED.DLQ` is empty (all end offsets are 0).
- **The side effects happened:** Postgres has 1 `runs` row and 1 `notifications_sent` row.

Quick checks you can rerun:

```bash
# messages per partition (end offsets)
docker compose exec kafka /opt/kafka/bin/kafka-get-offsets.sh --bootstrap-server kafka:29092 --topic RUN_FINISHED
# read the events themselves
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server kafka:29092 \
  --topic RUN_FINISHED --from-beginning --property print.key=true --timeout-ms 5000
# or browse topics, messages and group lag in Kafka UI
open http://127.0.0.1:8081
```

## Dummy notification service (local)

`docker-compose.yml` runs [`webhook-tester`](https://github.com/tarampampam/webhook-tester) on `127.0.0.1:8082`, a webhook inbox with a web UI. It starts with `--auto-create-sessions`, so any UUID path accepts POSTs right away and nothing has to be set up in the UI first. Requests are kept in memory and lost when the container restarts, which is fine for dev.

| | |
| --- | --- |
| Webhook URL (set as `NOTIFY_WEBHOOK_URL` in `.env.example`) | `http://127.0.0.1:8082/a9e7c0de-0000-4000-8000-000000000001` |
| Inbox UI | http://127.0.0.1:8082/s/a9e7c0de-0000-4000-8000-000000000001 (also linked from Tilt's `webhook-tester` resource) |
| Read it from a script | `curl http://127.0.0.1:8082/api/session/a9e7c0de-0000-4000-8000-000000000001/requests` |

Every finished run shows up there as one `POST` whose body is the full `RUN_FINISHED` JSON (`event_id`, `run_id`, `status`, `prompt`, `answer`/`error`, timestamps, `user_id`). To turn notifications into log lines only, unset `NOTIFY_WEBHOOK_URL`. To use a real receiver (Slack, an internal service), point it at that URL instead.

## Running it

```bash
tilt up      # adds consumer-persistence and consumer-notifications to the dashboard
# or by hand, next to the worker and API:
uv run consumer persistence
uv run consumer notifications
```

Inspect the results:

```bash
docker compose exec postgres psql -U postgres -d agentic -c "select run_id, status, left(answer, 60) from runs"
open http://127.0.0.1:8081    # Kafka UI: RUN_FINISHED, RUN_FINISHED.DLQ, consumer group lag
```

## Done when

- [X] One run → one Postgres row and one notification, even after a consumer restart replays messages. Verified by resetting both groups to the earliest offset and restarting: the row was re-upserted, and the notification was skipped as already sent.
- [X] A failed run still publishes `RUN_FINISHED` with `status: failed`
- [X] Integration tests against the compose Kafka and Postgres:
  - persistence handler idempotency
  - notification sent once, with the claim released on webhook failure
  - real-topic round trip, with a poison message landing in the DLQ with error headers
