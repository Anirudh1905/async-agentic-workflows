"""Log in as a local dev user (registering it on first use) and start one run.

Run: uv run python scripts/send_test_run.py ["prompt"]   (Tilt's send-test-run button)
"""

import sys

import httpx

API = "http://127.0.0.1:8000"
EMAIL, PASSWORD = "dev@example.com", "dev-password"  # local dev account only
PROMPT = sys.argv[1] if len(sys.argv) > 1 else "What is the latest Python release? Search it."

with httpx.Client(base_url=API, timeout=10) as http:
    r = http.post("/auth/register", json={"email": EMAIL, "password": PASSWORD})
    if r.status_code not in (201, 409):  # 409: already registered
        sys.exit(f"register failed: {r.status_code} {r.text}")
    token = http.post("/auth/login", data={"username": EMAIL, "password": PASSWORD}).raise_for_status().json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    run_id = http.post("/runs", json={"prompt": PROMPT}, headers=auth).raise_for_status().json()["run_id"]
    print(f"started run {run_id} as {EMAIL}")
    print(f"poll: curl -H 'Authorization: Bearer {token}' {API}/runs/{run_id}")
