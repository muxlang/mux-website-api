import ipaddress
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("mux-api")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024

CORS(
    app,
    origins=[
        "https://mux-lang.dev",
        "http://localhost:3000",
    ],
)


def _rate_limit_storage_uri() -> str:
    """Return the configured shared limiter store, failing closed in Fly.

    An in-process store cannot enforce a limit across Gunicorn workers or Fly
    Machines. Keeping it as a local-development default is useful, but
    silently deploying it would make the advertised request budget false.
    Production therefore requires an explicitly configured shared store.
    """
    configured = os.environ.get("RATE_LIMIT_STORAGE_URI")
    if os.environ.get("MUX_ENV") == "production":
        if not configured:
            raise RuntimeError(
                "RATE_LIMIT_STORAGE_URI must be configured for production; "
                "use a shared Redis or Valkey URI"
            )
        if urlparse(configured).scheme not in {"redis", "rediss"}:
            raise RuntimeError(
                "RATE_LIMIT_STORAGE_URI must use redis:// or rediss:// in production"
            )
        return configured
    if configured:
        return configured
    return "memory://"


def _rate_limit_key() -> str:
    """Use Fly's edge-authenticated client address when deployed.

    ``X-Forwarded-For`` is client-controlled unless every proxy hop is
    configured explicitly. Fly Proxy provides ``Fly-Client-IP`` for this
    deployment, so only a syntactically valid address from that header is
    trusted in production; local development keeps Flask's peer address.
    """
    if os.environ.get("MUX_ENV") == "production":
        forwarded_address = request.headers.get("Fly-Client-IP", "")
        try:
            ipaddress.ip_address(forwarded_address)
        except ValueError:
            logger.warning("Invalid Fly-Client-IP header; using peer address")
        else:
            return forwarded_address
    return get_remote_address()


limiter = Limiter(
    app=app,
    key_func=_rate_limit_key,
    default_limits=["20 per minute"],
    storage_uri=_rate_limit_storage_uri(),
    # A limiter backend outage must reject requests instead of silently
    # allowing unlimited compilation work.
    swallow_errors=False,
    in_memory_fallback_enabled=False,
)

MAX_CODE_SIZE = 100 * 1024
MAX_OUTPUT_SIZE = 1 * 1024 * 1024
READ_POLL_INTERVAL = 0.05

# Production compilation runs inside bubblewrap on the existing Fly machine.
# This keeps the deployment at one machine while making the process boundary
# explicit: if the runner is missing or cannot create the required namespaces,
# compilation fails closed instead of silently falling back to a normal child
# process.  The image installs bubblewrap at this path; the override exists for
# staging and for a future maintained runner.
SANDBOX_BIN = "/usr/bin/bwrap"
PRLIMIT_BIN = "/usr/bin/prlimit"
SANDBOX_MEMORY_BYTES = 512 * 1024 * 1024
SANDBOX_CPU_SECONDS = 31
SANDBOX_MAX_PROCESSES = 64
SANDBOX_MAX_FILES = 256
SANDBOX_MAX_FILE_BYTES = 64 * 1024 * 1024


class SandboxUnavailable(RuntimeError):
    """The production isolation boundary could not be established."""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default=%d", name, raw, default)
        return default
    if parsed <= 0:
        logger.warning("Non-positive %s=%r; using default=%d", name, raw, default)
        return default
    return parsed


COMPILE_TIMEOUT = _env_int("COMPILE_TIMEOUT", 30)


def _find_mux() -> str:
    env_val = os.environ.get("MUX_BIN")
    if env_val:
        return env_val

    found = shutil.which("mux")
    if found:
        return found

    root = Path(__file__).resolve().parent.parent
    for candidate in [root / "target/release/mux", root / "target/debug/mux"]:
        if candidate.exists():
            return str(candidate)

    return "mux"


MUX_BIN = _find_mux()


def _read_stream(
    stream,
    chunks,
    limit,
    limit_exceeded_event,
    total_bytes,
    total_lock,
):
    """Read a compiler pipe to EOF while retaining at most ``limit`` bytes.

    Readers must continue draining after the limit is reached. Otherwise a
    compiler that writes to both pipes can block on the pipe we stopped
    reading, preventing the supervisor from completing process shutdown.
    """
    try:
        for chunk in iter(lambda: stream.read(4096), b""):
            chunk_size = len(chunk)
            with total_lock:
                available = max(limit - total_bytes[0], 0)
                if available:
                    chunks.append(chunk[:available])
                total_bytes[0] += chunk_size
                if total_bytes[0] > limit:
                    limit_exceeded_event.set()
    except (OSError, ValueError):
        # The supervisor closes pipes after killing the process. A concurrent
        # close can surface as either exception and is part of normal teardown.
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _clean_output(text: str) -> str:
    return text.replace("\x00", "")


def _production_sandbox() -> bool:
    return os.environ.get("MUX_ENV") == "production"


def _resolve_executable(configured: str) -> str:
    resolved = configured if os.path.isabs(configured) else shutil.which(configured)
    if not resolved or not os.access(resolved, os.X_OK):
        raise SandboxUnavailable(f"required executable is unavailable: {configured}")
    return resolved


def _compiler_environment() -> dict[str, str]:
    """Return the only environment visible to an untrusted compiler process."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": "/tmp",
        "MUX_RUNTIME_LIB": "/usr/local/lib/mux/libmux_runtime.a",
        "LD_LIBRARY_PATH": "/usr/lib/llvm-22/lib",
    }


def _compiler_command(src_file: str, tmp_dir: str) -> list[str]:
    """Build the compiler command, enforcing isolation in production."""
    if not _production_sandbox():
        return [MUX_BIN, "run", src_file]

    sandbox = _resolve_executable(os.environ.get("MUX_SANDBOX_BIN", SANDBOX_BIN))
    prlimit = _resolve_executable(os.environ.get("MUX_PRLIMIT_BIN", PRLIMIT_BIN))
    compiler = _resolve_executable(MUX_BIN)
    source_name = os.path.basename(src_file)
    sandbox_source = f"/workspace/{source_name}"

    sandbox_args = [
        sandbox,
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-net",
        "--disable-userns",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/etc",
        "/etc",
        "--ro-bind",
        "/bin",
        "/bin",
        "--ro-bind",
        "/lib",
        "/lib",
        "--ro-bind",
        "/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--bind",
        tmp_dir,
        "/workspace",
        "--chdir",
        "/workspace",
        "--setenv",
        "PATH",
        "/usr/local/bin:/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "LC_ALL",
        "C.UTF-8",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "MUX_RUNTIME_LIB",
        "/usr/local/lib/mux/libmux_runtime.a",
        "--setenv",
        "LD_LIBRARY_PATH",
        "/usr/lib/llvm-22/lib",
        "--",
        compiler,
        "run",
        sandbox_source,
    ]
    return [
        prlimit,
        f"--cpu={SANDBOX_CPU_SECONDS}",
        f"--as={SANDBOX_MEMORY_BYTES}",
        f"--nproc={SANDBOX_MAX_PROCESSES}",
        f"--nofile={SANDBOX_MAX_FILES}",
        f"--fsize={SANDBOX_MAX_FILE_BYTES}",
        "--",
        *sandbox_args,
    ]


def _sandbox_setup_failed(stderr: str, returncode: int) -> bool:
    """Recognize runner setup failures without hiding compiler diagnostics."""
    return (
        _production_sandbox()
        and returncode != 0
        and stderr.lstrip().startswith(("bwrap:", "prlimit:"))
    )


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:
        logger.exception("Failed to kill process group for pid=%s", proc.pid)


def _stop_process(proc: subprocess.Popen) -> None:
    """Terminate a process group and wait for the direct child to reap."""
    _kill_process_group(proc)
    if proc.poll() is not None:
        return
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        # A process that ignores SIGKILL is not expected on Linux, but retrying
        # the group kill keeps this boundary safe if the first signal races a
        # process-group transition.
        _kill_process_group(proc)
        proc.wait(timeout=1)


@app.errorhandler(413)
def request_too_large(_err):
    return jsonify({"error": "Request body exceeds 512KB limit"}), 413


@app.errorhandler(429)
def too_many_requests(_err):
    return jsonify({"error": "Too many requests. Please wait and try again."}), 429


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


def _format_result(stdout, stderr, returncode, timed_out, output_too_large):
    if timed_out:
        return jsonify({"error": f"Execution timed out after {COMPILE_TIMEOUT}s"}), 504
    if output_too_large:
        return jsonify(
            {"error": f"Program output exceeds {MAX_OUTPUT_SIZE // 1024}KB limit"}
        ), 413
    if returncode != 0:
        msg = stderr if stderr else f"Process exited with code {returncode}"
        return jsonify({"error": msg}), 200
    return jsonify({"output": stdout}), 200


def _wait_for_compiler(proc, output_limit_exceeded):
    timed_out = False
    deadline = time.monotonic() + COMPILE_TIMEOUT
    while proc.poll() is None:
        if output_limit_exceeded.is_set():
            _stop_process(proc)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _stop_process(proc)
            break
        time.sleep(READ_POLL_INTERVAL)
    _stop_process(proc)
    return timed_out


def _finish_readers(proc, readers):
    # Let readers consume buffered bytes through EOF before closing their
    # streams. If a platform reports EOF late, close the pipes to unblock the
    # final join and prevent a reader from outliving this request.
    for reader in readers:
        reader.join(timeout=1)
    if not any(reader.is_alive() for reader in readers):
        return
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()
    for reader in readers:
        reader.join(timeout=1)
        if reader.is_alive():
            logger.error("Compiler output reader did not stop for pid=%s", proc.pid)


def _validate_compile_request(data):
    if not isinstance(data, dict):
        return None, (jsonify({"error": "Request body must be a JSON object"}), 400)

    if "code" not in data:
        return None, (jsonify({"error": "Missing 'code' in request body"}), 400)

    raw_code = data["code"]
    if not isinstance(raw_code, str):
        return None, (jsonify({"error": "'code' must be a string"}), 400)

    code_size = len(raw_code.encode("utf-8", errors="replace"))
    if code_size > MAX_CODE_SIZE:
        return None, (
            jsonify({"error": f"Source code exceeds {MAX_CODE_SIZE // 1024}KB limit"}),
            413,
        )

    return raw_code, None


def _execute_compiler(code):
    tmp_dir = tempfile.mkdtemp(prefix="mux_")
    src_file = os.path.join(tmp_dir, f"input_{uuid.uuid4().hex}.mux")
    with open(src_file, "w", encoding="utf-8") as f:
        f.write(code)

    proc = subprocess.Popen(
        _compiler_command(src_file, tmp_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        cwd=tmp_dir,
        start_new_session=True,
        env=_compiler_environment() if _production_sandbox() else None,
    )

    output_limit_exceeded = threading.Event()
    total_output_bytes = [0]
    total_output_lock = threading.Lock()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    readers = []

    for stream, chunks in [(proc.stdout, stdout_chunks), (proc.stderr, stderr_chunks)]:
        t = threading.Thread(
            target=_read_stream,
            args=(
                stream,
                chunks,
                MAX_OUTPUT_SIZE,
                output_limit_exceeded,
                total_output_bytes,
                total_output_lock,
            ),
        )
        t.start()
        readers.append(t)

    timed_out = _wait_for_compiler(proc, output_limit_exceeded)
    _finish_readers(proc, readers)

    output_too_large = output_limit_exceeded.is_set()
    stdout = _clean_output(b"".join(stdout_chunks).decode("utf-8", errors="replace"))
    stderr = _clean_output(
        b"".join(stderr_chunks).decode("utf-8", errors="replace")
    ).strip()

    return stdout, stderr, proc.returncode, timed_out, output_too_large, tmp_dir


def _compile_with_cleanup(code):
    tmp_dir = None
    try:
        stdout, stderr, returncode, timed_out, output_too_large, tmp_dir = (
            _execute_compiler(code)
        )
        if _sandbox_setup_failed(stderr, returncode):
            raise SandboxUnavailable(stderr)
        return _format_result(stdout, stderr, returncode, timed_out, output_too_large)
    except FileNotFoundError:
        logger.error("Compiler binary not found at %s", MUX_BIN)
        return jsonify({"error": "Compiler not found on server"}), 500
    except SandboxUnavailable:
        logger.exception("Compiler sandbox is unavailable")
        return jsonify({"error": "Compiler sandbox unavailable"}), 503
    except Exception:
        logger.exception("Unexpected error during compilation")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


@app.route("/api/compile", methods=["POST"])
@limiter.limit("20 per minute")
def compile_code():
    data = request.get_json(silent=True)
    validated_code, error_response = _validate_compile_request(data)
    if error_response:
        return error_response
    return _compile_with_cleanup(validated_code)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8080)))
