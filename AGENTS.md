# mux-website-api

`mux-website-api` is the Flask service behind the Mux playground. It compiles
and runs untrusted user programs in the production Fly.io service.

Cross-repository architecture and release facts live in
[`mux-context`](https://github.com/muxlang/mux-context). Read its canonical
[`SKILL.md`](https://github.com/muxlang/mux-context/blob/main/SKILL.md) before
changing the compiler/runtime contract or deployment behavior.

## Invariants

- Treat every request as hostile: preserve isolation, resource limits, timeout
  handling, cleanup, and non-root execution.
- Keep `MAX_CONTENT_LENGTH`, source limits, CORS policy, rate limiting, and the
  released `MUX_VERSION` contract explicit and tested.
- Regenerate `requirements.lock` with uv; never hand-edit dependency hashes.
- Production image changes require a reproducible, pinned, non-live package
  install and an explicit image/security check.

## Quality gate

Run `python -m py_compile server.py`, `pytest`, the configured Ruff checks, and
`docker build -t mux-website-api .` when the image or process runner changes.
For Python, dependency, or `.github/workflows/ci.yml` changes, the pre-commit
hook runs syntax and import checks plus the focused Python test suite. CI keeps
the coverage gate and scheduled compiler-main canary. The Worker-to-Fly origin
contract smoke remains a manual post-deployment check. Run the command in the
README after deploying both the API and Worker.

## Documentation

See [`README.md`](README.md), [`Dockerfile`](Dockerfile), and [`fly.toml`](fly.toml).
The [cross-repo testing policy](https://github.com/muxlang/mux-context/blob/main/docs/testing-policy.md)
describes how local hooks complement CI.
