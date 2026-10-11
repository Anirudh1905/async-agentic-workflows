"""Check that every service in docker-compose.yml is reachable from the host.

Run: uv run python scripts/smoke.py   (or `make smoke`)
"""

import os
import socket
import sys
import urllib.request


def port(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


TCP = {
    "postgres": port("POSTGRES_PORT", 5433),
    "temporal": port("TEMPORAL_PORT", 7233),
    "kafka": port("KAFKA_PORT", 9092),
    "redis": port("REDIS_PORT", 6380),
}
HTTP = {
    "temporal-ui": port("TEMPORAL_UI_PORT", 8233),
    "kafka-ui": port("KAFKA_UI_PORT", 8081),
    "webhook-tester": port("WEBHOOK_TESTER_PORT", 8082),
    "pgweb": port("PGWEB_PORT", 8083),
}


def check_tcp(p: int) -> None:
    socket.create_connection(("127.0.0.1", p), timeout=3).close()


def check_redis(p: int) -> None:
    with socket.create_connection(("127.0.0.1", p), timeout=3) as s:
        s.sendall(b"PING\r\n")
        if not s.recv(16).startswith(b"+PONG"):
            raise RuntimeError("no PONG")


def check_http(p: int) -> None:
    urllib.request.urlopen(f"http://127.0.0.1:{p}", timeout=5).close()


def main() -> int:
    failed = 0
    checks = [(n, p, check_redis if n == "redis" else check_tcp) for n, p in TCP.items()]
    checks += [(n, p, check_http) for n, p in HTTP.items()]
    for name, p, check in checks:
        try:
            check(p)
            print(f"ok    {name:<15} :{p}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {name:<15} :{p}  {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
