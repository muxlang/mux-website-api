# Single-stage image for the Mux playground API. Installs the RELEASED mux
# binary (no Rust/LLVM build): the playground runs a known, deliberately-pinned
# compiler release, not arbitrary main.
#
# Base must match (or exceed) the glibc the release binary was built against
# (ubuntu-24.04 / glibc 2.39); debian:bookworm (2.36) is too old to run it.
FROM ubuntu:24.04

# The Mux compiler release the playground runs. Bump deliberately to upgrade.
ARG MUX_VERSION=0.5.0

# `mux run` shells out to clang and the mux binary dynamically links LLVM, so the
# slim image still needs clang-22 + the LLVM runtime libraries. Python runs the API.
# One layer: install the toolchain, then download/verify/install the released mux
# binary. The published .sha256 references a "dist/" path, so verify by hash.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg lsb-release wget; \
    wget --max-redirect=0 -O /usr/share/keyrings/llvm-snapshot.gpg.key https://apt.llvm.org/llvm-snapshot.gpg.key; \
    echo "deb [signed-by=/usr/share/keyrings/llvm-snapshot.gpg.key] https://apt.llvm.org/$(lsb_release -cs)/ llvm-toolchain-$(lsb_release -cs)-22 main" > /etc/apt/sources.list.d/llvm.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends clang-22 llvm-22 python3 python3-pip; \
    rm -rf /var/lib/apt/lists/*; \
    arch="$(uname -m)"; \
    case "$arch" in \
        x86_64|amd64) target="linux-x86_64" ;; \
        aarch64|arm64) target="linux-aarch64" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    base="https://github.com/muxlang/mux-compiler/releases/download/v${MUX_VERSION}"; \
    archive="mux-${target}.tar.gz"; \
    cd /tmp; \
    curl --proto '=https' -fsSL "${base}/${archive}" -o "${archive}"; \
    curl --proto '=https' -fsSL "${base}/${archive}.sha256" -o "${archive}.sha256"; \
    echo "$(awk '{print $1}' "${archive}.sha256")  ${archive}" | sha256sum -c -; \
    tar -xzf "${archive}"; \
    install -Dm755 "mux-${target}/bin/mux" /usr/local/bin/mux; \
    mkdir -p /usr/local/lib/mux; \
    cp "mux-${target}/lib/"* /usr/local/lib/mux/; \
    rm -rf "/tmp/${archive}" "/tmp/${archive}.sha256" "/tmp/mux-${target}"

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
        > /tmp/uv-req.txt && \
    pip3 install --no-cache-dir --break-system-packages --only-binary :all: --require-hashes \
        -r /tmp/uv-req.txt && \
    rm /tmp/uv-req.txt && \
    uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH" \
        uv pip install --no-cache --only-binary :all: --require-hashes \
            -r /app/requirements.lock

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app"

# Run as non-root in production.
RUN useradd --create-home --uid 10001 appuser
USER appuser

WORKDIR /tmp
EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "60", "server:app"]
