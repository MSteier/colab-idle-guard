"""API robustness: randomised malformed requests and concurrent clients."""

from __future__ import annotations

import http.client
import json
import random
import threading
from pathlib import Path
from typing import Any

import pytest

from colab_idle_guard.watchdog.main import WatchdogService
from colab_idle_guard.watchdog.paths import AppPaths

from conftest import FakeClock

EXPECTED_STATUSES = {200, 400, 401, 403, 404, 405, 409, 411, 413, 415}


@pytest.fixture
def service(tmp_path: Path):
    svc = WatchdogService(paths=AppPaths(tmp_path / "c", tmp_path / "d"), clock=FakeClock(), port=0,
                          foreground_sampler=lambda: None)
    svc.start()
    yield svc
    svc.stop()


def request(port: int, method: str, path: str, body: bytes | None, headers: dict[str, str]) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers)
    status = conn.getresponse().status
    conn.close()
    return status


def random_value(rng: random.Random, depth: int = 0) -> Any:
    choices = [None, True, False, 0, -1, 2**63, 1.5, float("1e308"), "", "x" * rng.randint(1, 300),
               "idle", "busy", "connected", "nb_" + "a" * 32, "chrome", "vscode", "../../etc", "\u0000‮"]
    if depth < 3 and rng.random() < 0.3:
        return [random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    if depth < 3 and rng.random() < 0.3:
        keys = ["instance_id", "integration", "notebooks", "events", "kind", "age_ms", "notebook_id",
                "execution", "running_cells", "runtime", "command_id", "nonce", "ok", "full_snapshot", "zzz"]
        return {rng.choice(keys): random_value(rng, depth + 1) for _ in range(rng.randint(0, 6))}
    return rng.choice(choices)


def test_fuzzed_requests_never_crash_the_server(service: WatchdogService) -> None:
    rng = random.Random(1234)
    port, token = service.api.port, service.token
    paths = ["/v1/hello", "/v1/heartbeat", "/v1/state", "/v1/activity", "/v1/goodbye", "/v1/command_result",
             "/v1/status", "/v1/health", "/v1/../v1/status", "/v1/heartbeat?x=1", "/", "/v1/nope"]
    seen: set[int] = set()
    for i in range(600):
        method = rng.choice(["POST", "POST", "POST", "GET", "PUT", "OPTIONS"])
        path = rng.choice(paths)
        headers = {"Host": f"127.0.0.1:{port}", "Content-Type": rng.choice(["application/json", "application/json; charset=utf-8", "text/plain"])}
        if rng.random() < 0.85:
            headers["Authorization"] = f"Bearer {token}"
        if rng.random() < 0.05:
            headers["Origin"] = rng.choice(["https://evil.example", "chrome-extension://" + "a" * 32, "null"])
        if rng.random() < 0.2:
            body = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 200)))
        else:
            payload = random_value(rng)
            if rng.random() < 0.6:
                payload = {"instance_id": rng.choice(["c1", "v1", "bad id", ""]),
                           "integration": rng.choice(["chrome", "vscode", "x"]),
                           **(payload if isinstance(payload, dict) else {"notebooks": payload})}
            body = json.dumps(payload).encode()
        status = request(port, method, path, body if method in ("POST", "PUT") else None, headers)
        assert status in EXPECTED_STATUSES, f"request {i}: {method} {path} -> {status}"
        seen.add(status)
    assert request(port, "GET", "/v1/health", None, {"Host": f"127.0.0.1:{port}"}) == 200
    assert {200, 400, 401}.issubset(seen)
    log = service.paths.log_file.read_text(encoding="utf-8")
    assert "api handler error" not in log, "an unexpected exception reached the handler"


def test_concurrent_clients_keep_state_consistent(service: WatchdogService) -> None:
    port, token = service.api.port, service.token
    errors: list[str] = []

    def client(n: int) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        kind = "chrome" if n % 2 else "vscode"
        try:
            for i in range(40):
                execution = "busy" if (n == 0 and i == 39) else "idle"
                body = {"instance_id": f"{kind}-{n}", "integration": kind,
                        "notebooks": [{"notebook_id": f"nb{n}", "execution": execution,
                                       "running_cells": 1 if execution == "busy" else 0, "runtime": "connected"}]}
                conn.request("POST", "/v1/heartbeat", body=json.dumps(body), headers=headers)
                resp = conn.getresponse()
                resp.read()
                if resp.status != 200:
                    errors.append(f"{n}/{i}: {resp.status}")
                conn.request("POST", "/v1/activity", body=json.dumps(
                    {"instance_id": f"{kind}-{n}", "integration": kind, "events": [{"kind": "click", "age_ms": 0}]}),
                    headers=headers)
                resp = conn.getresponse()
                resp.read()
                if resp.status != 200:
                    errors.append(f"{n}/{i} activity: {resp.status}")
                service.controller.tick()
        finally:
            conn.close()

    threads = [threading.Thread(target=client, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    status = service.controller.tick().to_dict()
    assert len(status["integrations"]) == 16
    assert status["execution"] == "busy" and status["running_cells"] == 1  # client 0 ended busy
    assert len(service.execution.entries()) == 16
