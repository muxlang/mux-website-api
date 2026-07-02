# mux-website-api

[![Sonar Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=muxlang_mux-website-api&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=muxlang_mux-website-api)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=muxlang_mux-website-api&metric=coverage)](https://sonarcloud.io/summary/new_code?id=muxlang_mux-website-api)

The compile/run API behind the [Mux playground](https://mux-lang.dev). A small
Flask service that runs submitted Mux programs with the released `mux` binary and
returns their output.

Hosted on [Fly.io](https://fly.io) as `mux-lang-api` (`mux-lang-api.fly.dev`),
consumed by [mux-website](https://github.com/muxlang/mux-website) over HTTP.

## How it works

- `POST /api/compile` with `{ "code": "<mux source>" }` -> `{ "output": "..." }`
- `GET /health` for health checks
- The service shells out to `mux run` (rate-limited, time-limited) and returns
  stdout/stderr.

## Pinned compiler version

The `Dockerfile` installs a **released** `mux` binary (no Rust/LLVM build),
pinned via `ARG MUX_VERSION`. The playground therefore runs a known, deliberately
chosen compiler release. To upgrade the playground:

1. Ensure the target version is released in
   [mux-compiler](https://github.com/muxlang/mux-compiler) (the
   `mux-linux-x86_64.tar.gz` asset must exist).
2. Bump `MUX_VERSION` in the `Dockerfile`.
3. Deploy (below).

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

## Local development

```bash
pip install -r requirements.txt
# Needs a `mux` binary on PATH (see the mux-compiler install instructions).
MUX_BIN=mux gunicorn --bind 0.0.0.0:8080 server:app
```

Or build/run the production image (matches Fly):

```bash
docker build -t mux-website-api .
docker run --rm -p 8080:8080 mux-website-api
```

## Deployment

```bash
fly deploy
```

The slim image bundles clang-22 + the LLVM runtime libraries (the compiler shells
out to clang and links LLVM at compile time) and sets `MUX_RUNTIME_LIB` so it
never tries to build the runtime from source.

## Related repositories

- [mux-compiler](https://github.com/muxlang/mux-compiler) - the compiler whose release this serves
- [mux-website](https://github.com/muxlang/mux-website) - the docs site + playground UI
- [mux-context](https://github.com/muxlang/mux-context) - cross-repo architecture, design notes, glossary, releases

## License

[MIT](LICENSE)
