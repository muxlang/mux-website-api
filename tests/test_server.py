"""Tests for the Mux playground API.

These deliberately avoid needing a real `mux` compiler: the compile endpoint is
exercised by pointing MUX_BIN at stock binaries (`/bin/echo` for a success-style
run, `/bin/false` for a non-zero exit, a bogus path for the not-found branch), so
the suite runs anywhere Python does.
"""

import pytest

import server


@pytest.fixture
def client():
    server.app.config.update(TESTING=True)
    # The rate limiter would make repeated requests flaky; disable it for tests.
    server.limiter.enabled = False
    with server.app.test_client() as test_client:
        yield test_client


# --- helpers -----------------------------------------------------------------


def test_env_int_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_INT", raising=False)
    assert server._env_int("SOME_INT", 7) == 7


def test_env_int_parses_valid(monkeypatch):
    monkeypatch.setenv("SOME_INT", "42")
    assert server._env_int("SOME_INT", 7) == 42


def test_env_int_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("SOME_INT", "not-a-number")
    assert server._env_int("SOME_INT", 7) == 7


def test_env_int_non_positive_falls_back(monkeypatch):
    monkeypatch.setenv("SOME_INT", "0")
    assert server._env_int("SOME_INT", 7) == 7


def test_clean_output_strips_null_bytes():
    assert server._clean_output("a\x00b\x00") == "ab"


def test_format_result_branches():
    with server.app.app_context():
        _, status = server._format_result("", "", 0, timed_out=True, output_too_large=False)
        assert status == 504
        _, status = server._format_result("", "", 0, timed_out=False, output_too_large=True)
        assert status == 413
        body, status = server._format_result("", "boom", 1, timed_out=False, output_too_large=False)
        assert status == 200
        assert body.get_json()["error"] == "boom"
        body, status = server._format_result("hi", "", 0, timed_out=False, output_too_large=False)
        assert status == 200
        assert body.get_json()["output"] == "hi"


# --- /health -----------------------------------------------------------------


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


# --- /api/compile validation -------------------------------------------------


def test_compile_rejects_non_object(client):
    resp = client.post("/api/compile", data="not json", content_type="application/json")
    assert resp.status_code == 400


def test_compile_rejects_missing_code(client):
    resp = client.post("/api/compile", json={"nope": 1})
    assert resp.status_code == 400


def test_compile_rejects_non_string_code(client):
    resp = client.post("/api/compile", json={"code": 123})
    assert resp.status_code == 400


def test_compile_rejects_oversized_code(client):
    resp = client.post("/api/compile", json={"code": "x" * (server.MAX_CODE_SIZE + 1)})
    assert resp.status_code == 413


def test_request_body_over_max_content_length(client):
    # MAX_CONTENT_LENGTH is 512KB; exceed it to hit the 413 error handler.
    resp = client.post(
        "/api/compile",
        data=b"{" + b"x" * (512 * 1024 + 1) + b"}",
        content_type="application/json",
    )
    assert resp.status_code == 413
    assert "512KB" in resp.get_json()["error"]


# --- /api/compile execution (via stand-in binaries) --------------------------


def test_compile_success_path(client, monkeypatch):
    # /bin/echo exits 0 and prints, exercising the success branch.
    monkeypatch.setattr(server, "MUX_BIN", "/bin/echo")
    resp = client.post("/api/compile", json={"code": "print(\"hi\")"})
    assert resp.status_code == 200
    assert "output" in resp.get_json()


def test_compile_nonzero_exit(client, monkeypatch):
    # /bin/false exits 1 with no output, exercising the error branch.
    monkeypatch.setattr(server, "MUX_BIN", "/bin/false")
    resp = client.post("/api/compile", json={"code": "boom"})
    assert resp.status_code == 200
    assert "error" in resp.get_json()


def test_compile_binary_missing(client, monkeypatch):
    monkeypatch.setattr(server, "MUX_BIN", "/nonexistent/mux-binary")
    resp = client.post("/api/compile", json={"code": "x"})
    assert resp.status_code == 500
    assert resp.get_json()["error"] == "Compiler not found on server"
