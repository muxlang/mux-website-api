# mux-website-api: AI Agent Guidelines

A small Flask service that compiles and runs Mux programs for the playground at
mux-lang.dev. Part of the multi-repo [muxlang](https://github.com/muxlang)
ecosystem. Deployed to Fly.io as `mux-lang-api`.

> Cross-repo architecture, design rationale, the feature map, and the release
> process live in [muxlang/context](https://github.com/muxlang/context).

## Critical Rules

- **No special characters** - avoid em-dashes, emojis, or other non-ASCII in code,
  comments, or commit messages.
- **No Rust/LLVM toolchain here** - this repo installs the RELEASED `mux` binary;
  it never builds the compiler. Keep it that way (the whole point of the split).
- **Understand existing code first** - read `server.py` and the `Dockerfile`
  before changing anything.
- **Security matters** - this runs untrusted user code. Preserve the sandboxing,
  rate limits (`flask-limiter`), time limits (`COMPILE_TIMEOUT`), payload caps
  (`MAX_CONTENT_LENGTH`), CORS origins, and the non-root container user.
- **Hash-pinned deps** - `requirements.lock` is hash-pinned and installed via uv;
  regenerate it properly rather than hand-editing.

## Structure

- `server.py` - the Flask app (`/api/compile`, `/health`).
- `requirements.txt` / `requirements.lock` - Python deps (lock is hash-pinned).
- `Dockerfile` - single-stage image; downloads the released `mux` binary
  (`ARG MUX_VERSION`), installs clang-22 + LLVM runtime libs + Python, sets
  `MUX_RUNTIME_LIB`, runs gunicorn.
- `fly.toml` - Fly.io config (app `mux-lang-api`).

## Upgrading the pinned compiler

Bump `ARG MUX_VERSION` in the `Dockerfile` to a version that is released in
mux-compiler (the `mux-linux-x86_64.tar.gz` asset must exist), then `fly deploy`.

## Important Dockerfile facts (learned the hard way)

- The base image glibc must be >= the glibc the release binary was built against
  (currently `ubuntu:24.04` / glibc 2.39; `debian:bookworm` is too old).
- The published `mux-*.tar.gz.sha256` references a `dist/` path, so verify by
  hash directly (`echo "<hash>  <file>" | sha256sum -c -`), not `sha256sum -c file`.

## Development & checks

```bash
pip install -r requirements.txt
python -m py_compile server.py            # CI runs this
docker build -t mux-website-api .         # validate the production image
docker run --rm -p 8080:8080 mux-website-api
```

CI runs the Python checks + a SonarQube scan. Deploy with `fly deploy`.

## Related repos

- `mux-compiler` - the compiler/CLI whose release this serves.
- `mux-website` - the docs site + playground UI that calls this API.

**Add to this document as you learn vital information.**
