# Single-stage image for the Mux playground API. Installs the RELEASED mux
# binary (no Rust/LLVM build): the playground runs a known, deliberately-pinned
# compiler release, not arbitrary main.
#
# Base must match (or exceed) the glibc the release binary was built against
# (ubuntu-24.04 / glibc 2.39); debian:bookworm (2.36) is too old to run it.
# Pin the multi-platform Ubuntu manifest. A floating tag would allow a rebuild
# of the same API commit to silently change its OS and toolchain inputs.
FROM ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517

# The Mux compiler release the playground runs. Bump deliberately to upgrade.
ARG MUX_VERSION=0.10.1
# The released compiler currently ships amd64 only. BuildKit supplies
# TARGETARCH for the requested target platform; use it rather than uname so a
# cross-build cannot accidentally inspect the builder's architecture.
ARG TARGETARCH=amd64
# Keep the isolation boundary packages explicit. These are amd64 versions from
# Ubuntu Noble security; update them deliberately with the base-image refresh.
ARG BUBBLEWRAP_VERSION=0.9.0-1ubuntu0.1
ARG UTIL_LINUX_VERSION=2.39.3-9ubuntu6.5

# `mux run` shells out to clang and the mux binary dynamically links LLVM, so the
# slim image still needs clang-22 + the LLVM runtime libraries. Python runs the API.
# One layer: install the toolchain, then download/verify/install the released mux
# binary. The published .sha256 references a "dist/" path, so verify by hash.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        "bubblewrap=${BUBBLEWRAP_VERSION}" ca-certificates curl gnupg lsb-release \
        "util-linux=${UTIL_LINUX_VERSION}" wget; \
    wget --max-redirect=0 -O /usr/share/keyrings/llvm-snapshot.gpg.key https://apt.llvm.org/llvm-snapshot.gpg.key; \
    echo '8b2a587ffd672c4687e7581dad4b2f6c1bb2ad6b480cd9771ba2ff48e0b8c75d  /usr/share/keyrings/llvm-snapshot.gpg.key' | sha256sum -c -; \
    echo "deb [signed-by=/usr/share/keyrings/llvm-snapshot.gpg.key] https://apt.llvm.org/$(lsb_release -cs)/ llvm-toolchain-$(lsb_release -cs)-22 main" > /etc/apt/sources.list.d/llvm.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends clang-22 llvm-22 python3 python3-pip; \
    case "$TARGETARCH" in \
        amd64) target="linux-x86_64"; archive_sha="65d283894f984f0c761033c0ed052bdd1fe33503c32f0e4ad19a2c226c3861fb" ;; \
        arm64) echo "unsupported architecture: $TARGETARCH (mux v${MUX_VERSION} has no published arm64 compiler asset)" >&2; exit 1 ;; \
        *) echo "unsupported architecture: ${TARGETARCH:-unknown}" >&2; exit 1 ;; \
    esac; \
    base="https://github.com/muxlang/mux-compiler/releases/download/v${MUX_VERSION}"; \
    archive="mux-${target}.tar.gz"; \
    cd /tmp; \
    curl --proto '=https' -fsSL "${base}/${archive}" -o "${archive}"; \
    echo "${archive_sha}  ${archive}" | sha256sum -c -; \
    tar -xzf "${archive}"; \
    install -Dm755 "mux-${target}/bin/mux" /usr/local/bin/mux; \
    mkdir -p /usr/local/lib/mux; \
    cp "mux-${target}/lib/"* /usr/local/lib/mux/; \
    rm -rf "/tmp/${archive}" "/tmp/mux-${target}"; \
    apt-get purge -y --auto-remove curl gnupg lsb-release wget; \
    rm -rf /var/lib/apt/lists/*

# Point the compiler at the bundled runtime lib so it never tries to cargo-build
# the runtime in the slim container.
ENV MUX_RUNTIME_LIB=/usr/local/lib/mux/libmux_runtime.a \
    LD_LIBRARY_PATH=/usr/lib/llvm-22/lib

# Make 'clang' resolve to clang-22.
RUN ln -sf /usr/bin/clang-22 /usr/local/bin/clang

# Install Python dependencies with uv (hash-pinned, binary-only).
COPY requirements.lock /app/requirements.lock
COPY requirements.txt /app/requirements.txt
COPY server.py /app/server.py
RUN echo 'uv==0.6.5' \
        '--hash=sha256:15dae245979add192c4845947da1a9141f95c19403d1c0d75019182e6882e7d4' \
        '--hash=sha256:b5445a509f500bbf18faba4e7cf5cc9763617c335d58afaa5f3e5a6e388dd4ee' \
        '--hash=sha256:26a90e69d6438de2ec03ab452cc48d1cb375249c6b6980f4ed177f324a5ad8b3' \
        > /tmp/uv-req.txt && \
    pip3 install --no-cache-dir --break-system-packages --only-binary :all: --require-hashes \
        -r /tmp/uv-req.txt && \
    rm /tmp/uv-req.txt && \
    uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH" \
        uv pip install --no-cache --only-binary :all: --require-hashes \
            -r /app/requirements.lock

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app" \
    MUX_ENV="production"

# Run as non-root in production.
RUN useradd --create-home --uid 10001 appuser
USER appuser

WORKDIR /tmp
EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "60", "server:app"]
