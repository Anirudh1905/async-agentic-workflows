# Phase 1 — Infrastructure & Targets

Goal: a `uv`-managed Python 3.13 project, plus a Docker Compose network running the target app (Spree's Rails backend) and its email digital twin (Mailpit), reachable from the host.

**Corrected, 2026-09-02: image-only, no source checkout.** An earlier version of this doc cloned `spree/spree_starter` and built its Dockerfile locally. That was unnecessary — Spree publishes a genuine pre-built image, **`ghcr.io/spree/spree:5.6.1`**, confirmed by inspecting its OCI labels directly (`org.opencontainers.image.source: https://github.com/spree/spree-starter`, `title: "spree-starter"` — this really is `spree_starter`, not a different app). All three services (`postgres`, `mailpit`, `web`) now come from pre-built images; nothing is cloned or built locally, matching the original architecture doc's intent (pure image pulls, like Mailpit/stripe-mock always were).

**Scope note (2026-09-01, still applies):** everything below describes and verifies a single, manually-run, long-lived stack. Phase 3's 3a sub-stage (see `phase-3-backend-orchestration.md`) needs this same stack spun up **per agent run**, isolated from other concurrent runs — confirmed working by an earlier smoke test on the clone-based setup; re-verify the per-run pattern against this image-based setup specifically before relying on it (should be easier now, since there's no image build step to repeat per run — only container/DB state to isolate).

**Target app note**: this project went through several corrections before landing here, each caught by direct verification rather than trusting documentation — OWASP Juice Shop (no real email/payment) → Parasoft Parabank (assumed real email; **disproven by PoC**) → Spree's Next.js storefront (real UI, but emails bypass SMTP — rejected) → `spree_starter` cloned and built locally (worked, but unnecessary — no published image was checked for first) → **`ghcr.io/spree/spree:5.6.1`, pre-built, image-only**. Full history in `phase-5-digital-twins.md`.

## 1. Python environment (`uv`)

System Python on the dev machine is 3.9.6 — too old for LangGraph, Playwright, FastAPI, Streamlit, and LangChain, which all currently require 3.10+. Pin 3.13 explicitly rather than relying on whatever `python3` resolves to. **Verified**: LangGraph 1.2.11, Playwright 1.62.0, FastAPI 0.141.1, Streamlit 1.63.0, and LangChain 1.x all installed cleanly under 3.13.15.

```bash
uv init .                  # scaffolds pyproject.toml, .python-version
uv python pin 3.13         # writes 3.13 into .python-version; uv downloads the interpreter if needed
uv add fastapi uvicorn langgraph langchain playwright streamlit
uv run playwright install chromium   # REQUIRED separate step — uv add only installs the pip package,
                                      # it does not fetch browser binaries
```

**Confirmed by direct run, 2026-09-01**: this exact sequence completed cleanly — `uv run python --version` reports `Python 3.13.15`, all packages installed at the versions above, Chromium + FFmpeg + Chrome Headless Shell downloaded successfully.

## 2. Docker Compose — pre-built images only, no build step

```yaml
services:
  postgres:
    image: postgres:18-alpine
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: postgres
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 3s
      timeout: 3s
      retries: 10
    ports:
      - "127.0.0.1:5433:5432"
    volumes:
      - spree_postgres_data:/var/lib/postgresql   # NOT /var/lib/postgresql/data — see gotcha below

  mailpit:
    image: axllent/mailpit:latest
    ports:
      - "127.0.0.1:8025:8025"
      - "127.0.0.1:1025:1025"

  web:
    image: ghcr.io/spree/spree:5.6.1
    depends_on:
      postgres: { condition: service_healthy }
      mailpit: { condition: service_started }
    environment:
      RAILS_ENV: production
      DATABASE_URL: postgres://postgres:postgres@postgres:5432/postgres
      SECRET_KEY_BASE: <generate with `openssl rand -hex 64`>
      SMTP_HOST: mailpit
      SMTP_PORT: "1025"
      RAILS_FORCE_SSL: "false"     # required — defaults to true in production, breaks plain HTTP
      RAILS_ASSUME_SSL: "false"    # required — same reason
    ports:
      - "3000:3000"

volumes:
  spree_postgres_data:
```

**Real gotcha found and fixed during the PoC**: Postgres 18's image changed its expected volume layout — mounting at `/var/lib/postgresql/data` (the old convention, still used by many older Compose examples) makes the container refuse to start with an explicit error about `pg_ctlcluster`-style versioned directories. **Fix, confirmed working**: mount the volume one level up, at `/var/lib/postgresql`, and let the image manage the versioned subdirectory itself.

**Real gotcha found and fixed**: Spree's production `config/environments/production.rb` sets `config.force_ssl` and `config.assume_ssl` to `true` by default, gated only on `RAILS_FORCE_SSL`/`RAILS_ASSUME_SSL` explicitly equaling the string `"false"` — confirmed by reading that file directly inside the image (`docker run --rm --entrypoint grep ghcr.io/spree/spree:5.6.1 ...`). Without both env vars set, the app would redirect all plain-HTTP requests, breaking local `curl`/Playwright access. Both are required, not optional, for local dev use of this image.

No Rancher Desktop `/tmp`-bind-mount concern applies to this setup — there's no bind mount at all, since nothing is built from local source.

## 3. First-time setup — none required, handled automatically

Unlike the dev-mode Dockerfile build (which needed manual `db:prepare`/`db:seed` run once), **this image's entrypoint auto-runs `db:prepare` on every boot of the `rails server` command** — confirmed by reading `/rails/bin/docker-entrypoint` directly inside the image:

```bash
#!/bin/bash -e
if [[ "$*" == *"./bin/rails server"* ]]; then
  ./bin/rails db:prepare
fi
exec "${@}"
```

**Confirmed by direct boot**: `docker compose up -d` alone (no separate setup commands) results in a fully working app — Puma up, SolidQueue worker/dispatcher/scheduler running, and (bonus, not present in the dev-mode setup) **an admin user already seeded** (`Spree.admin_user_class.count` returned `1` immediately after boot, no manual `db:seed` needed).

## 4. What's confirmed by full boot

- `docker compose up -d` brings up all three containers with zero manual setup steps.
- `curl http://localhost:3000/users/sign_in` returns 200 with a real Devise login form.
- An admin user (`spree@example.com`) already exists from the image's own seed data.
- **Real, verified email path**: triggered `Spree.admin_user_class.first.send_reset_password_instructions` directly. This enqueues an `ActionMailer::MailDeliveryJob` onto SolidQueue's `mailers` queue rather than sending synchronously (production-mode behavior, different from dev mode) — the email arrived in Mailpit **within about 5 seconds**, once SolidQueue's background worker processed the job. If asserting on email delivery from the agent, allow a few seconds of delay after triggering the action, don't assume instant delivery the way the dev-mode setup showed.

## 5. Phase 1 exit criteria

- `uv run python --version` reports 3.13.x.
- `docker compose up -d` (repo root — `docker-compose.yml` pins `name: target-app`) brings up `postgres`, `mailpit`, `web` — all healthy/running, no manual setup step.
- `curl http://localhost:3000/users/sign_in` returns a real login page.
- `curl http://localhost:8025/api/v1/messages` returns Mailpit's message list (empty until an email is triggered).
- Triggering `send_reset_password_instructions` against the pre-seeded admin user results in an email appearing in Mailpit within a few seconds (SolidQueue processing delay).

## Verification status

Every claim below was confirmed by actually running the stack, not by reading documentation.

| Claim | Status |
| --- | --- |
| `ghcr.io/spree/spree:5.6.1` really is `spree_starter`, pre-built | **Confirmed** — OCI image labels read directly (`image.source`, `image.title`) match the `spree/spree-starter` repo |
| No `db:prepare`/`db:seed` step needed — entrypoint handles it, and an admin user is pre-seeded | **Confirmed by direct boot** — read the entrypoint script directly, then confirmed `Spree.admin_user_class.count == 1` immediately after boot |
| Postgres 18 needs its volume mounted at `/var/lib/postgresql`, not `/var/lib/postgresql/data` | **Confirmed by direct failure and fix** — hit the exact error, corrected the mount path |
| Production mode requires `RAILS_FORCE_SSL=false` and `RAILS_ASSUME_SSL=false` to allow plain HTTP | **Confirmed by reading `production.rb` directly** inside the image |
| `SMTP_HOST`/`SMTP_PORT` env vars still work identically in production mode | **Confirmed** — same password-reset-email PoC as the dev-mode setup, landed in Mailpit successfully |
| Mail delivery is asynchronous (via SolidQueue) in this image's production mode, not synchronous | **Confirmed by direct observation** — the email did not appear in Mailpit immediately; appeared ~5 seconds later once the job was processed |
| Python 3.13 supported by all five core Python deps | **Confirmed** via successful `uv add` install |
| `uv add` doesn't install browser binaries; `playwright install` required | **Confirmed** against Playwright's own docs and a successful install run |

No remaining "confirm at kickoff" items for this phase.
