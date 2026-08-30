<div align="center">

<img src="https://mux-lang.dev/img/mux-logo.png" alt="Mux Logo" width="120">

# mux-website-api

**The compile/run API behind the [Mux](https://github.com/muxlang) playground**

[![License](https://img.shields.io/badge/license-MIT-green.svg?style=flat-square)](LICENSE)
[![Playground](https://img.shields.io/badge/playground-mux--lang.dev-blue.svg?style=flat-square)](https://mux-lang.dev)
[![Sonar Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=muxlang_mux-website-api&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=muxlang_mux-website-api)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=muxlang_mux-website-api&metric=coverage)](https://sonarcloud.io/summary/new_code?id=muxlang_mux-website-api)

</div>

A small Flask service that runs submitted Mux programs with the released `mux`
binary and returns their output. Hosted on [Fly.io](https://fly.io) as
`mux-lang-api` (`mux-lang-api.fly.dev`), consumed by
[mux-website](https://github.com/muxlang/mux-website) over HTTP.

---

## How it works

- `POST /api/compile` with `{ "code": "<mux source>" }` -> `{ "output": "..." }`.
  Browser traffic reaches this endpoint through the Cloudflare Worker. The Fly
  origin accepts compile requests only when the Worker supplies the private
  `MUX_API_ORIGIN_TOKEN` header.
- `GET /health` for health checks
- The service runs `mux run` inside a disposable bubblewrap namespace on the
  existing Fly machine. The child has no network, a read-only view of the
  runtime image, an isolated writable workspace, an allowlisted environment,
  and CPU/memory/process/file limits. If that boundary cannot be created, the
  request fails closed; it never falls back to an ordinary child process.

---

## Pinned compiler version

The `Dockerfile` installs a **released** `mux` binary (no Rust/LLVM build),
pinned via `ARG MUX_VERSION`. The playground therefore runs a known, deliberately
chosen compiler release. To upgrade the playground:

1. Ensure the target version is released in
   [mux-compiler](https://github.com/muxlang/mux-compiler) (the
   `mux-linux-x86_64.tar.gz` asset must exist).
2. Bump `MUX_VERSION` in the `Dockerfile`.
3. Deploy (below).

---

## Compiler-main canary (non-gating)

The release pin above is intentional: the playground must run a stable released
compiler, not arbitrary `main`. To catch a compiler-`main` regression that would
break this API's contract before the next release bump, a scheduled
`.github/workflows/canary-compiler-main.yml` job builds `mux` from
[mux-compiler](https://github.com/muxlang/mux-compiler) `main` and runs a smoke
test (`tests/canary_smoke.py`) against it.

It runs only on a nightly schedule and manual dispatch (never on push or pull
request), so it does not gate normal CI or deploys and is not a required check.
It never changes the `MUX_VERSION` pin.

---

## Local development

```bash
pip install -r requirements.txt
# Needs a `mux` binary on PATH (see the mux-compiler install instructions).
MUX_BIN=mux gunicorn --bind 0.0.0.0:8080 server:app
```

Or build/run the production image (matches Fly):

```bash
docker build -t mux-website-api .
docker run --rm -p 8080:8080 \
  -e MUX_ENV=production \
  -e MUX_API_ORIGIN_TOKEN="replace-with-a-random-secret" \
  mux-website-api
```

The production image requires `MUX_API_ORIGIN_TOKEN` so the public compile
endpoint cannot be bypassed by calling the Fly hostname directly. Generate one
random value and configure it as a Fly secret and a Cloudflare Worker secret;
never commit it to `fly.toml`. The Worker provides the distributed edge rate
limit and Cloudflare absorbs volumetric traffic. The API keeps a bounded
in-process limiter as defense in depth, so no Redis or Valkey service is needed
for the single-machine deployment. A shared Redis or Valkey URI remains
supported through `RATE_LIMIT_STORAGE_URI` if one is already available. The
image also requires `/usr/bin/bwrap` and `/usr/bin/prlimit`; these are installed
in the same image and do not require a second Fly application or machine.

---

## Deployment

```bash
fly deploy
```

The slim image bundles clang-22 + the LLVM runtime libraries (the compiler shells
out to clang and links LLVM at compile time) and sets `MUX_RUNTIME_LIB` so it
never tries to build the runtime from source.

After both the API and Worker deploys, run the credential-free integration
smoke from a trusted machine. It checks that direct Fly requests are rejected
and that the Worker can compile through the authenticated origin path:

```bash
MUX_WORKER_COMPILE_URL=https://mux-ai.corniedj.workers.dev/api/compile \
MUX_API_ORIGIN_URL=https://mux-lang-api.fly.dev/api/compile \
python tests/origin_contract_smoke.py
```

---

## Related repositories

| Repo | What it is |
|------|------------|
| [mux-compiler](https://github.com/muxlang/mux-compiler) | The language and compiler whose release this serves |
| [mux-runtime](https://github.com/muxlang/mux-runtime) | Runtime + standard library linked by compiled programs |
| [mux-website](https://github.com/muxlang/mux-website) | Docs site (mux-lang.dev) and playground UI |
| [tree-sitter-mux](https://github.com/muxlang/tree-sitter-mux) | Tree-sitter grammar + highlight queries |
| [mux-syntax-highlighting](https://github.com/muxlang/mux-syntax-highlighting) | TextMate grammar, VSCode extension, canonical syntax spec |
| [mux-context](https://github.com/muxlang/mux-context) | Cross-repo architecture, design rationale, glossary, releases |

---

## License

[MIT](LICENSE) - Maintained by [Derek Corniello](https://github.com/DerekCorniello)
