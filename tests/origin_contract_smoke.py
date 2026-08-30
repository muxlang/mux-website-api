"""Validate the deployed Cloudflare Worker to Fly compile contract.

This smoke is intentionally separate from pytest because it requires the
deployed public URLs. It never accepts or prints the origin token: the browser
facing Worker URL needs no credential, while the Fly origin must reject a
direct request without the private header.

Run after both deployments:

    MUX_WORKER_COMPILE_URL=https://mux-ai.corniedj.workers.dev/api/compile \
    MUX_API_ORIGIN_URL=https://mux-lang-api.fly.dev/api/compile \
    python tests/origin_contract_smoke.py
"""

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CANARY_CODE = 'auto x = 40 + 2\nprint("mux-contract:" + x.to_string())\n'
EXPECTED_MARKER = "mux-contract:42"


def fail(message: str) -> None:
    print(f"ORIGIN CONTRACT FAILURE: {message}", file=sys.stderr)
    raise SystemExit(1)


def post_json(url: str) -> tuple[int, dict[str, object]]:
    request = Request(
        url,
        data=json.dumps({"code": CANARY_CODE}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; MuxOriginSmoke/1.0)",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            status = response.status
            body = json.loads(response.read())
    except HTTPError as error:
        status = error.code
        try:
            body = json.loads(error.read())
        except json.JSONDecodeError:
            body = {}
    except (URLError, TimeoutError) as error:
        fail(f"could not reach {url}: {error}")
    if not isinstance(body, dict):
        fail(f"{url} returned a non-object JSON body")
    return status, body


def main() -> None:
    worker_url = os.environ.get("MUX_WORKER_COMPILE_URL")
    origin_url = os.environ.get("MUX_API_ORIGIN_URL")
    if not worker_url:
        fail("MUX_WORKER_COMPILE_URL is required")
    if not origin_url:
        fail("MUX_API_ORIGIN_URL is required")

    direct_status, direct_body = post_json(origin_url)
    if direct_status != 403 or direct_body.get("errorCode") != "ORIGIN_AUTH_REQUIRED":
        fail(
            "direct Fly compile request was not rejected with ORIGIN_AUTH_REQUIRED: "
            f"{direct_status} {direct_body!r}"
        )

    worker_status, worker_body = post_json(worker_url)
    worker_output = str(worker_body.get("output", ""))
    if worker_status != 200 or EXPECTED_MARKER not in worker_output:
        fail(
            "Worker compile request did not return the canary output: "
            f"{worker_status} {worker_body!r}"
        )

    print("ORIGIN CONTRACT OK: direct origin rejected and Worker compile succeeded.")


if __name__ == "__main__":
    main()
