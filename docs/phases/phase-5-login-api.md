# Phase 5: Login API

**Goal:** only authenticated users can start or read runs.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant AUTH as Login API
    participant DB as Postgres users
    participant RUNS as Agent run API
    U->>AUTH: POST /auth/login {email, password}
    AUTH->>DB: fetch user + password hash
    AUTH->>AUTH: verify hash
    AUTH-->>U: 200 {access_token (JWT)}
    U->>RUNS: POST /runs (Authorization: Bearer JWT)
    RUNS->>RUNS: current_user dependency validates JWT
    alt valid
        RUNS-->>U: 202 {run_id} (workflow gets user_id)
    else missing / invalid
        RUNS-->>U: 401
    end
```

## Design

- `users` table in Postgres (email, bcrypt/argon2 password hash)
- `POST /auth/register` and `POST /auth/login` → short-lived JWT access token
- FastAPI dependency `current_user` protects `/runs`. `user_id` is passed into the workflow and the `RUN_FINISHED` event.
- Users can read only their own runs.

## Done when

- [ ] `/runs` returns 401 without a valid token
- [ ] A run row in Postgres is tied to the user who started it
