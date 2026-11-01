"""Local HTTP API integration tests and spec TEST 12 (restart), using the real service."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from typing import Any

import pytest

from colab_idle_guard.watchdog.main import WatchdogService
from colab_idle_guard.watchdog.paths import AppPaths
from colab_idle_guard.watchdog.shutdown_controller import GuardState

from conftest import FakeClock, MockAdapter

EXT_A = "a" * 32
EXT_B = "b" * 32


class Client:
    def __init__(self, service: WatchdogService) -> None:
        self.port = service.api.port
        self.token = service.token

    def call(self, method: str, path: str, body: Any = None, *, token: str | None | bool = True,
             host: str | None = None, origin: str | None = None, ctype: str = "application/json",
             raw: bytes | None = None) -> tuple[int, dict, http.client.HTTPResponse]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token is True:
            headers["Authorization"] = f"Bearer {self.token}"
        elif isinstance(token, str):
            headers["Authorization"] = f"Bearer {token}"
        if origin:
            headers["Origin"] = origin
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        if data is not None:
            headers["Content-Type"] = ctype
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp.status, (json.loads(payload) if payload else {}), resp


def make_service(root: Path, clock: FakeClock | None = None) -> WatchdogService:
    paths = AppPaths(config_dir=root / "config", data_dir=root / "data")
    service = WatchdogService(paths=paths, clock=clock or FakeClock(), port=0, foreground_sampler=lambda: "chrome.exe")
    service.config.update({"require_foreground_corroboration": False})
    return service


@pytest.fixture
def service(tmp_path: Path):
    svc = make_service(tmp_path)
    svc.start()
    yield svc
    svc.stop()


@pytest.fixture
def client(service: WatchdogService) -> Client:
    return Client(service)


def hb(instance="c1", integration="chrome", notebooks=None, **extra) -> dict:
    nbs = notebooks if notebooks is not None else [
        {"notebook_id": "nb1", "execution": "idle", "running_cells": 0, "runtime": "connected"}]
    return {"instance_id": instance, "integration": integration, "version": "0.1.0", "notebooks": nbs, **extra}


# --- authentication & transport security ---------------------------------------

def test_health_without_auth_reveals_nothing_sensitive(client: Client) -> None:
    status, body, _ = client.call("GET", "/v1/health", token=False)
    assert status == 200 and body["authenticated"] is False and set(body) == {"app", "version", "authenticated"}
    assert client.call("GET", "/v1/health")[1]["authenticated"] is True


def test_requires_valid_token(client: Client) -> None:
    assert client.call("GET", "/v1/status", token=False)[0] == 401
    assert client.call("GET", "/v1/status", token="x" * 43)[0] == 401
    assert client.call("POST", "/v1/heartbeat", hb(), token=False)[0] == 401
    assert client.call("GET", "/v1/status")[0] == 200


def test_rejects_foreign_host_header(client: Client) -> None:
    # DNS rebinding: a web page resolving evil.example to 127.0.0.1.
    assert client.call("GET", "/v1/status", host="evil.example:47821")[0] == 403
    assert client.call("GET", "/v1/status", host=f"localhost:{client.port}")[0] == 200


def test_rejects_web_origins_and_preflight(client: Client) -> None:
    assert client.call("POST", "/v1/heartbeat", hb(), origin="https://colab.research.google.com")[0] == 403
    assert client.call("POST", "/v1/heartbeat", hb(), origin="null")[0] == 403
    status, _, resp = client.call("OPTIONS", "/v1/heartbeat", token=False, origin="https://evil.example")
    assert status == 403
    assert resp.getheader("Access-Control-Allow-Origin") is None


def test_chrome_extension_id_pinned_on_first_use(service: WatchdogService, client: Client) -> None:
    origin_a, origin_b = f"chrome-extension://{EXT_A}", f"chrome-extension://{EXT_B}"
    # An unauthenticated request must not be able to pin an extension ID.
    assert client.call("POST", "/v1/hello", hb(), origin=origin_b, token=False)[0] == 401
    assert service.config.get("allowed_chrome_extension_ids") == []
    assert client.call("POST", "/v1/hello", hb(), origin=origin_a)[0] == 200
    assert service.config.get("allowed_chrome_extension_ids") == [EXT_A]
    assert client.call("POST", "/v1/hello", hb(), origin=origin_b)[0] == 403
    assert client.call("POST", "/v1/hello", hb(), origin="chrome-extension://not-an-id")[0] == 403


def test_vscode_integration_cannot_use_browser_origin(client: Client) -> None:
    body = hb(instance="v1", integration="vscode")
    assert client.call("POST", "/v1/hello", body, origin=f"chrome-extension://{EXT_A}")[0] == 403
    assert client.call("POST", "/v1/hello", body)[0] == 200


# --- request validation ----------------------------------------------------------

def test_content_type_and_size_limits(client: Client) -> None:
    assert client.call("POST", "/v1/heartbeat", hb(), ctype="text/plain")[0] == 415
    assert client.call("POST", "/v1/heartbeat", raw=b"{" + b" " * 70_000 + b"}")[0] == 413
    assert client.call("POST", "/v1/heartbeat", raw=b"not json")[0] == 400
    assert client.call("POST", "/v1/heartbeat", raw=b"[1,2]")[0] == 400


@pytest.mark.parametrize("body", [
    hb(instance="bad id with spaces"),
    hb(integration="firefox"),
    hb(notebooks=[{"notebook_id": "n", "execution": "running", "running_cells": 0, "runtime": "connected"}]),
    hb(notebooks=[{"notebook_id": "n", "execution": "idle", "running_cells": -1, "runtime": "connected"}]),
    hb(notebooks=[{"notebook_id": "n", "execution": "idle", "running_cells": True, "runtime": "connected"}]),
    hb(notebooks="all"),
    {"integration": "chrome", "notebooks": []},
])
def test_invalid_payloads_rejected(client: Client, body: dict) -> None:
    status, resp, _ = client.call("POST", "/v1/heartbeat", body)
    assert status == 400 and "error" in resp


def test_keep_alive_survives_rejected_request(client: Client) -> None:
    """A rejected POST's unread body must not corrupt the next request on the same connection."""
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=5)
    body = json.dumps(hb()).encode()
    conn.request("POST", "/v1/heartbeat", body=body, headers={"Content-Type": "application/json"})
    first = conn.getresponse()
    first.read()
    assert first.status == 401 and first.getheader("Connection") == "close"
    conn.close()
    status, _, _ = client.call("POST", "/v1/heartbeat", hb())
    assert status == 200


def test_keep_alive_reused_after_success(client: Client) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=5)
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {client.token}"}
    for _ in range(3):
        conn.request("POST", "/v1/heartbeat", body=json.dumps(hb()).encode(), headers=headers)
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 200
    conn.close()


def test_unknown_route_and_method(client: Client) -> None:
    assert client.call("GET", "/v1/nope")[0] == 404
    assert client.call("PUT", "/v1/status")[0] == 405


def test_instance_kind_conflict(client: Client) -> None:
    assert client.call("POST", "/v1/hello", hb(instance="x1", integration="chrome"))[0] == 200
    assert client.call("POST", "/v1/hello", hb(instance="x1", integration="vscode"))[0] == 409


# --- functional flow -------------------------------------------------------------

def test_heartbeat_activity_and_status(service: WatchdogService, client: Client) -> None:
    assert client.call("POST", "/v1/hello", hb())[1] == {"heartbeat_interval_seconds": 15, "monitored": True}
    assert client.call("POST", "/v1/heartbeat", hb())[1]["commands"] == []
    status, body, _ = client.call("POST", "/v1/activity", {"instance_id": "c1", "integration": "chrome",
                                                           "events": [{"kind": "click", "age_ms": 0},
                                                                      {"kind": "typed:hello", "age_ms": 0}]})
    assert status == 200 and body == {"accepted": 1, "rejected": 1}
    service.controller.tick()
    st = client.call("GET", "/v1/status")[1]
    assert st["runtime"] == "connected" and st["execution"] == "idle" and st["state"] == "active"
    assert st["integrations"][0]["kind"] == "chrome"


def test_command_round_trip(service: WatchdogService, client: Client) -> None:
    client.call("POST", "/v1/heartbeat", hb())
    cmd = service.commands.enqueue("c1", "release_runtime", {"notebook_id": "nb1"})
    commands = client.call("POST", "/v1/heartbeat", hb())[1]["commands"]
    assert commands == [{"id": cmd.id, "nonce": cmd.nonce, "type": "release_runtime", "payload": {"notebook_id": "nb1"}}]
    assert client.call("POST", "/v1/heartbeat", hb())[1]["commands"] == []
    bad = {"instance_id": "c1", "integration": "chrome", "command_id": cmd.id, "nonce": "wrong", "ok": True}
    assert client.call("POST", "/v1/command_result", bad)[0] == 409
    good = {**bad, "nonce": cmd.nonce, "detail": "done"}
    assert client.call("POST", "/v1/command_result", good)[0] == 200
    assert client.call("POST", "/v1/command_result", good)[0] == 409  # replay rejected


def test_goodbye_and_partial_state(service: WatchdogService, client: Client) -> None:
    busy = [{"notebook_id": "nb1", "execution": "busy", "running_cells": 1, "runtime": "connected"}]
    client.call("POST", "/v1/heartbeat", hb(instance="v1", integration="vscode"))
    client.call("POST", "/v1/state", hb(instance="v1", integration="vscode", notebooks=busy))
    service.controller.tick()
    assert service.status()["execution"] == "busy"
    client.call("POST", "/v1/goodbye", {"instance_id": "v1", "integration": "vscode"})
    service.controller.tick()
    st = service.status()
    assert st["execution"] == "unknown" and st["stale_notebooks"] == 1  # closed while busy


def test_token_never_written_to_log(service: WatchdogService, client: Client) -> None:
    client.call("POST", "/v1/heartbeat", hb())
    client.call("GET", "/v1/status", token="wrong-token-value-1234567890")
    service.rotate_token()
    for handler in __import__("logging").getLogger("colab_idle_guard").handlers:
        handler.flush()
    text = service.paths.log_file.read_text(encoding="utf-8")
    assert "api_server_started" in text
    assert client.token not in text and service.token not in text


# --- spec TEST 12 ----------------------------------------------------------------

def test_12_restart_preserves_config_and_reevaluates_state(tmp_path: Path) -> None:
    clock = FakeClock()
    first = make_service(tmp_path, clock)
    first.config.update({"idle_timeout_minutes": 20, "automatic_disconnect_enabled": True,
                         "release_consent_acknowledged": True})
    first.start()
    c = Client(first)
    token = first.token
    c.call("POST", "/v1/heartbeat", hb())
    c.call("POST", "/v1/activity", {"instance_id": "c1", "integration": "chrome", "events": [{"kind": "click", "age_ms": 0}]})
    first.stop()

    clock.advance(19 * 60)  # app was down for 19 of the 20 idle minutes
    second = make_service(tmp_path, clock)
    adapter = MockAdapter()
    second.runtime.register("chrome", adapter)
    assert second.config.get("idle_timeout_minutes") == 20
    assert second.config.auto_release_allowed is True
    assert second.token == token  # integrations stay paired
    second.start()
    try:
        status = second.controller.tick()
        assert status.execution == "unknown" and status.runtime == "unknown"
        assert status.idle_seconds == 0  # the idle clock restarted; no early release

        clock.advance(25 * 60)  # no integration has reported since the restart
        second.controller.tick()
        assert second.controller.state is GuardState.MONITORING and adapter.calls == []

        c2 = Client(second)
        c2.call("POST", "/v1/heartbeat", hb())
        assert second.controller.tick().state == GuardState.WARNING
        assert adapter.calls == []
    finally:
        second.stop()


def test_official_extension_id_always_accepted(service: WatchdogService, client: Client) -> None:
    from colab_idle_guard.watchdog.api_server import OFFICIAL_CHROME_EXTENSION_ID

    official = f"chrome-extension://{OFFICIAL_CHROME_EXTENSION_ID}"
    assert client.call("POST", "/v1/hello", hb(), origin=f"chrome-extension://{EXT_A}")[0] == 200  # pins EXT_A
    assert client.call("POST", "/v1/hello", hb(instance="c2"), origin=official)[0] == 200
    assert service.config.get("allowed_chrome_extension_ids") == [EXT_A]  # official ID is not pinned
    assert client.call("POST", "/v1/hello", hb(instance="c3"), origin=f"chrome-extension://{EXT_B}")[0] == 403
    assert client.call("POST", "/v1/hello", hb(), origin=official, token=False)[0] == 401  # token still required


def test_manifest_key_matches_official_id() -> None:
    import base64
    import hashlib
    from colab_idle_guard.watchdog.api_server import OFFICIAL_CHROME_EXTENSION_ID

    manifest = json.loads((Path(__file__).resolve().parents[1] / "chrome_extension" / "manifest.json").read_text())
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
    assert digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop")) == OFFICIAL_CHROME_EXTENSION_ID
