# Phase 5 — Stateful Digital Twin (Mailpit / Email Only)

Goal: wire the target app's real outbound email to Mailpit, so agent-driven tests can assert on intercepted email content with zero external side effects.

**Status: fully verified, zero open items — for a single shared stack.** Every claim in this doc was confirmed by actually booting the full stack and triggering a real email — not by reading documentation. No "confirm at kickoff" items remain for that scope.

**Scope note (2026-09-01, updated):** the end-goal design (`end-goal-architecture.md`) spins up this exact stack **per agent run**, isolated per run, rather than once as a shared long-lived instance. That per-run orchestration is Phase 3's 3a sub-stage — **confirmed working by direct smoke test** (two concurrent isolated stacks, zero cross-contamination; see `phase-3-backend-orchestration.md` for the full result and the port-collision bug it surfaced and fixed along the way).

## 1. Target app: Spree's Rails backend (`spree/spree_starter`) — UI only, no code changes

**Final decision, 2026-08-31**: use `spree_starter` alone — no Next.js storefront, no payment twin. Rationale: the goal is the simplest possible app that sends real email with zero source-code changes; `spree_starter` already does this out of the box. Its login (`/users/sign_in`) and admin panel are real server-rendered pages the agent can drive; product/cart/checkout pages were considered (via Spree's separate `spree/storefront` Next.js repo) and explicitly rejected — see section 3.

## 2. Full history — three target-app corrections, each caught by direct verification, not assumption

1. **OWASP Juice Shop** (original doc) — no real email flow (password reset uses a security question), no real payment call. Caught by docs-research before any code was written.
2. **Parasoft Parabank** (first replacement) — assumed from docs to have JavaMail-based email. **Disproven by PoC**: searched every compiled class in a running container for mail-related code — zero hits. Its "loan processing" is a self-contained internal ActiveMQ round-trip with no external call at all.
3. **Spree Commerce, full stack including the Next.js storefront** (second replacement, briefly considered) — the Next.js storefront (`spree/storefront`) has real product/cart/checkout UI, but **its own transactional emails go through Resend's HTTP API or a dev-mode file preview (`.next/emails/*.html`) — never SMTP**, confirmed by reading `src/lib/emails/send.ts` directly. Mailpit (an SMTP catcher) cannot intercept these under any configuration; this isn't a config gap, it's a different transport entirely. Rejected in favor of the Rails backend alone, which does use real SMTP.

**Lesson applied each round**: verify against running code (compiled classes, actual source files, live network traffic), not framework docs or READMEs describing what an app "should" do.

## 3. What was tested and confirmed (full end-to-end PoC, 2026-08-31) — historical, dev-mode clone approach, superseded by section 6

**Superseded, 2026-09-02**: this section describes the original PoC, which cloned `spree/spree_starter` and built its Dockerfile locally. That worked, but was unnecessary — see section 6 for the current image-only approach (`ghcr.io/spree/spree:5.6.1`, no clone, no build). Kept here since the underlying facts (real SMTP path, real email captured) are still true and this PoC is what first proved them; sections 4 (Rancher Desktop bind-mount gotcha) is now moot since there's no bind mount at all in the current approach, but is kept as a real, still-true fact about this dev machine worth knowing if anyone ever does need to build from source again.

Booted the complete stack from `spree_starter`'s own `docker-compose.dev.yml` (Postgres, Mailpit, Rails `web`), ran first-time setup, and triggered a real backend action that sends email — no synthetic SMTP test, no code patch:

- **Postgres and Mailpit**: both healthy within ~8 seconds of `docker compose up -d`.
- **First-time setup required and confirmed**: `bin/rails db:prepare` (creates `spree_development`/`spree_test` databases) then `bin/rails db:seed` (creates the default `Spree::Store` record) — matches the pattern documented in the repo's own `CLAUDE.md` (`bin/setup`). Skipping this causes `ActiveRecord::NoDatabaseError` on boot, confirmed by hitting it directly before running setup.
- **Rails boots cleanly after setup**: Puma up, SolidQueue worker/dispatcher registered, no crash. `curl http://localhost:3000` returns a 301 to `/admin` (expected — no storefront route exists in this backend-only app, by design).
- **Login page renders correctly**: `GET /users/sign_in` → 200 OK, real Devise-rendered HTML form.
- **Triggered a real email with zero code changes**: called Devise's own `user.send_reset_password_instructions` (the same method the "forgot password" link on the login page calls) against the seeded admin user. Result — an email titled **"Shop Password Reset Instructions"** appeared in Mailpit within seconds, retrieved via `GET http://localhost:8025/api/v1/messages`. This is the real password-reset flow, not a synthetic SMTP test — confirms the full path: UI action → Devise mailer → Rails `action_mailer.smtp_settings` (reading `ENV["SMTP_HOST"]=mailpit`) → Mailpit capture.

**Known cosmetic issue, not blocking**: `/admin` itself 500s in dev mode with `Propshaft::MissingAssetError: spree/admin/application.css` — the admin dashboard's CSS isn't precompiled by default in this dev setup. This doesn't affect the login page (which rendered fine) or the email-trigger path (rendered via `bin/rails runner`, no HTTP request needed). If the agent later needs to click through the actual admin UI (rather than just triggering actions via console/API), this asset issue would need a fix — out of scope for now since the agent's primary target is the login flow.

## 4. Environment-specific gotcha (historical — not applicable to the current image-only approach)

This dev machine runs **Rancher Desktop** as its Docker backend. Cloning the repo into `/tmp` and bind-mounting it (`docker-compose.dev.yml`'s `.:/rails` mount) produced a container where `/rails` was **silently empty** — Rancher Desktop's VM doesn't share `/tmp` by default, and the bind-mount failed silently rather than erroring, producing a confusing `bin/rails: no such file or directory` at container start. **Fix confirmed**: clone/work from a path under the actual project directory (which Rancher Desktop does share), not `/tmp` or other paths outside the default shared set. **No longer relevant to the current setup** (section 6) since there's no bind mount or local build at all — kept here as a real fact about this machine, in case any future work does need to build a Docker image from local source.

## 5. Payment twin: dropped, confirmed final

Per your explicit direction — no payment scope at all. Not deferred, not a placeholder: this project has no payment-twin goal. (For reference, prior research found no OSS demo app supports a config-only Stripe API base override; every candidate, including Spree's own `spree_stripe` gem, needs a source patch. Moot now, since payments are out of scope entirely.)

## 6. Current approach: pre-built image, no clone, no build (2026-09-02)

**Real finding**: `spree/spree_starter` publishes a genuine pre-built image, `ghcr.io/spree/spree:5.6.1`, on GitHub Container Registry — confirmed by reading the image's own OCI labels (`org.opencontainers.image.source: https://github.com/spree/spree-starter`, `title: "spree-starter"`). This was missed initially; the clone+build approach in sections 3–4 above worked but was never necessary. See `phase-1-infrastructure.md` for the full current setup and the two real Postgres-18/SSL-related gotchas found while switching to it.

**Re-verified with the image-based setup**: booted `postgres` (image) + `mailpit` (image) + `web` (`ghcr.io/spree/spree:5.6.1`, image, zero build) via `docker compose up -d`. No manual `db:prepare`/`db:seed` needed — the image's own entrypoint runs `db:prepare` automatically, and an admin user (`spree@example.com`) is already seeded. Triggered `send_reset_password_instructions` again against this setup: the email appeared in Mailpit after ~5 seconds (production-mode mail delivery goes through SolidQueue asynchronously, unlike the earlier dev-mode setup which appeared to deliver near-instantly) — same real password-reset flow, same zero source-code changes, now with less setup than before.

## Phase 5 exit criteria — all confirmed (re-verified against the current image-only setup)

- [x] Full stack (`docker compose up -d`, `docker-compose.yml` at repo root, images only) boots cleanly with no manual setup step at all.
- [x] A real backend action (password-reset request) results in a message appearing in Mailpit's API within a few seconds (allow for SolidQueue's async delivery delay in this production-mode image).
- [ ] The agent (Phase 2) can call a new tool (e.g. `check_mailpit_inbox()`) and use its result in a `mark_assertion` call — e.g. "verify a password-reset email arrived." *(Not yet built — this is Phase 2/5 integration work, not a verification gap.)*
- [x] Zero source-code changes were needed anywhere in this path.

## Verification status

Every claim below was confirmed by direct action against a running system, not by reading documentation.

| Claim | Status |
| --- | --- |
| Parabank has a real JavaMail-based flow | **Disproven by PoC** — Parabank is not the target app |
| Next.js storefront's emails go through Resend/dev-file-preview, not SMTP | **Confirmed by reading source** (`src/lib/emails/send.ts`) — this is why the storefront was rejected |
| Rails backend reads `ENV["SMTP_HOST"]` into real SMTP delivery | **Confirmed by source** and **by triggering a real email that was captured** |
| Full stack boots cleanly with documented first-time setup (`db:prepare`, `db:seed`) | **Confirmed for the historical dev-mode clone setup** — no manual intervention beyond those two commands. **Superseded**: the current image-based setup needs zero manual setup at all — see section 6. |
| A real, unmodified application action (password reset) sends a real, interceptable email | **Confirmed twice** — "Shop Password Reset Instructions" landed in Mailpit both on the original dev-mode clone setup and on the current image-only setup |
| Zero source-code patches needed anywhere in this path | **Confirmed** — nothing in the app was modified, in either setup |
| `ghcr.io/spree/spree:5.6.1` is a genuine, current, pre-built `spree_starter` image | **Confirmed** — read the image's OCI labels directly |

No remaining "confirm at kickoff" items for this phase.
