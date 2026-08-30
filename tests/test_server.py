"""Tests for the Mux playground API.

These deliberately avoid needing a real `mux` compiler: the compile endpoint is
exercised by pointing MUX_BIN at stock binaries (`/bin/echo` for a success-style
run, `/bin/false` for a non-zero exit, a bogus path for the not-found branch), so
the suite runs anywhere Python does.
"""

import os
import stat

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


def test_compiler_command_is_direct_outside_production(monkeypatch, tmp_path):
    monkeypatch.delenv("MUX_ENV", raising=False)
    monkeypatch.setattr(server, "MUX_BIN", "/bin/echo")
    source = tmp_path / "input.mux"
    command = server._compiler_command(os.fspath(source), os.fspath(tmp_path))
    assert command == ["/bin/echo", "run", os.fspath(source)]


def test_production_compiler_command_requires_and_configures_sandbox(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MUX_ENV", "production")
    monkeypatch.setenv("MUX_SANDBOX_BIN", "/usr/bin/bwrap")
    monkeypatch.setenv("MUX_PRLIMIT_BIN", "/usr/bin/prlimit")
    monkeypatch.setattr(server, "MUX_BIN", "/bin/echo")
    monkeypatch.setattr(server, "_resolve_executable", lambda configured: configured)

    source = tmp_path / "input.mux"
    command = server._compiler_command(os.fspath(source), os.fspath(tmp_path))

    assert command[0] == "/usr/bin/prlimit"
    assert "/usr/bin/bwrap" in command
    assert "--unshare-net" in command
    assert "--clearenv" in command
    bind = command.index("--bind")
    assert command[bind : bind + 3] == ["--bind", os.fspath(tmp_path), "/workspace"]
    assert command[-3:] == ["/bin/echo", "run", "/workspace/input.mux"]


def test_production_compiler_command_fails_closed_when_runner_missing(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MUX_ENV", "production")
    monkeypatch.setenv("MUX_SANDBOX_BIN", "/nonexistent/bwrap")
    monkeypatch.setattr(server, "MUX_BIN", "/bin/echo")

    source_path = os.fspath(tmp_path / "input.mux")
    temp_path = os.fspath(tmp_path)
    with pytest.raises(server.SandboxUnavailable):
        server._compiler_command(source_path, temp_path)


def test_compiler_environment_is_allowlisted(monkeypatch):
    monkeypatch.setenv("SERVICE_SECRET", "must-not-leak")
    environment = server._compiler_environment()
    assert environment["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert "SERVICE_SECRET" not in environment


def test_bwrap_setup_failure_is_detected_only_in_production(monkeypatch):
    monkeypatch.setenv("MUX_ENV", "production")
    assert server._sandbox_setup_failed("bwrap: namespace setup failed", 1)
    assert not server._sandbox_setup_failed("compiler: syntax error", 1)

    monkeypatch.delenv("MUX_ENV")
    assert not server._sandbox_setup_failed("bwrap: namespace setup failed", 1)


def test_rate_limit_storage_defaults_to_memory_outside_production(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_STORAGE_URI", raising=False)
    monkeypatch.delenv("MUX_ENV", raising=False)
    assert server._rate_limit_storage_uri() == "memory://"


def test_rate_limit_storage_requires_shared_store_in_production(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_STORAGE_URI", raising=False)
    monkeypatch.setenv("MUX_ENV", "production")
    with pytest.raises(RuntimeError, match="RATE_LIMIT_STORAGE_URI"):
        server._rate_limit_storage_uri()


def test_rate_limit_storage_rejects_process_local_store_in_production(monkeypatch):
    monkeypatch.setenv("MUX_ENV", "production")
    monkeypatch.setenv("RATE_LIMIT_STORAGE_URI", "memory://")
    with pytest.raises(RuntimeError, match="redis://"):
        server._rate_limit_storage_uri()


def test_rate_limit_storage_accepts_shared_store_in_production(monkeypatch):
    uri = "rediss://:secret@example.test:6380/0"
    monkeypatch.setenv("MUX_ENV", "production")
    monkeypatch.setenv("RATE_LIMIT_STORAGE_URI", uri)
    assert server._rate_limit_storage_uri() == uri


def test_rate_limit_key_uses_fly_client_ip_in_production(monkeypatch):
    monkeypatch.setenv("MUX_ENV", "production")
    with server.app.test_request_context(
        "/api/compile", headers={"Fly-Client-IP": "203.0.113.9"}
    ):
        assert server._rate_limit_key() == "203.0.113.9"


def test_rate_limit_key_rejects_invalid_fly_client_ip(monkeypatch):
    monkeypatch.setenv("MUX_ENV", "production")
    with server.app.test_request_context(
        "/api/compile", headers={"Fly-Client-IP": "not-an-ip"}
    ):
        assert server._rate_limit_key() == "127.0.0.1"


def test_clean_output_strips_null_bytes():
    assert server._clean_output("a\x00b\x00") == "ab"


def test_format_result_branches():
    with server.app.app_context():
        _, status = server._format_result(
            "", "", 0, timed_out=True, output_too_large=False
        )
        assert status == 504
        _, status = server._format_result(
            "", "", 0, timed_out=False, output_too_large=True
        )
        assert status == 413
        body, status = server._format_result(
            "", "boom", 1, timed_out=False, output_too_large=False
        )
        assert status == 200
        assert body.get_json()["error"] == "boom"
        body, status = server._format_result(
            "hi", "", 0, timed_out=False, output_too_large=False
        )
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
    resp = client.post("/api/compile", json={"code": 'print("hi")'})
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


def test_compile_fails_closed_when_sandbox_is_unavailable(client, monkeypatch):
    def unavailable(_source, _tmp):
        raise server.SandboxUnavailable()

    monkeypatch.setattr(server, "_compiler_command", unavailable)

    resp = client.post("/api/compile", json={"code": "x"})

    assert resp.status_code == 503
    assert resp.get_json()["error"] == "Compiler sandbox unavailable"


def _fake_mux(tmp_path, source):
    path = tmp_path / "fake-mux"
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return os.fspath(path)


def test_compile_rejects_combined_output_limit_after_fast_exit(
    client, monkeypatch, tmp_path
):
    mux = _fake_mux(
        tmp_path,
        "import sys\n"
        "sys.stdout.buffer.write(b'x' * (1024 * 1024 + 1))\n"
        "sys.stderr.buffer.write(b'y' * (1024 * 1024 + 1))\n"
        "sys.stdout.flush()\n",
    )
    monkeypatch.setattr(server, "MUX_BIN", mux)

    resp = client.post("/api/compile", json={"code": "output"})

    assert resp.status_code == 413
    assert "output exceeds" in resp.get_json()["error"]


def test_compile_timeout_reaps_process_group(client, monkeypatch, tmp_path):
    mux = _fake_mux(
        tmp_path,
        "import time\ntime.sleep(10)\n",
    )
    monkeypatch.setattr(server, "MUX_BIN", mux)
    monkeypatch.setattr(server, "COMPILE_TIMEOUT", 1)

    resp = client.post("/api/compile", json={"code": "sleep"})

    assert resp.status_code == 504
    assert "timed out" in resp.get_json()["error"]
