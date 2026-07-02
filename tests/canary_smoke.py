"""Canary smoke test: exercise the API against a real `mux` binary.

Unlike tests/test_server.py (which points MUX_BIN at stand-in binaries so it can
run anywhere Python does), this script requires an actual compiler. It is NOT a
pytest module and is NOT collected by the normal suite; it is invoked directly by
the non-gating .github/workflows/canary-compiler-main.yml canary after building
`mux` from compiler main.

It posts a tiny, deterministic Mux program to /api/compile and asserts the
contract still holds: HTTP 200 with the expected `output`, and a healthy /health.
Run it with MUX_BIN pointing at the built binary (and MUX_RUNTIME_SRC set so
`mux run` can resolve the runtime):

    MUX_BIN=/path/to/mux python tests/canary_smoke.py

Exit code is 0 on success, 1 on any contract failure.
"""

import os
import sys

# Allow `python tests/canary_smoke.py` from the repo root to import server.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A minimal, deterministic program. Uses only core language features so the
# canary flags real contract breakage, not incidental stdlib churn.
CANARY_CODE = 'auto x = 40 + 2\nprint("mux-canary:" + x.to_string())\n'
EXPECTED_MARKER = "mux-canary:42"


def _fail(message):
    print(f"CANARY FAILURE: {message}", file=sys.stderr)
    sys.exit(1)


def main():
    if not os.environ.get("MUX_BIN"):
        _fail("MUX_BIN is not set; point it at the built `mux` binary.")

    # Import after MUX_BIN is set: server resolves the binary at import time.
    import server

    server.app.config.update(TESTING=True)
    server.limiter.enabled = False
    client = server.app.test_client()

    health = client.get("/health")
    if health.status_code != 200 or health.get_json() != {"status": "ok"}:
        _fail(f"/health returned {health.status_code}: {health.data!r}")

    resp = client.post("/api/compile", json={"code": CANARY_CODE})
    body = resp.get_json()

    if resp.status_code != 200:
        _fail(f"/api/compile returned {resp.status_code}: {body!r}")
    if not isinstance(body, dict) or "output" not in body:
        _fail(f"response missing 'output' key: {body!r}")
    if EXPECTED_MARKER not in body["output"]:
        _fail(f"expected {EXPECTED_MARKER!r} in output, got: {body['output']!r}")

    print(f"CANARY OK: /api/compile returned expected output ({EXPECTED_MARKER}).")
    sys.exit(0)


if __name__ == "__main__":
    main()
