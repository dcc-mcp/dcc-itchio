import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time

from dcc_mcp_core.skills_helper import CancelledError, check_cancelled

MIN_VERSION = (15, 29, 0)


class ButlerdError(RuntimeError):
    pass


def resolve_butler_path(explicit_path=None):
    candidate = explicit_path or os.environ.get("BUTLER_PATH") or shutil.which("butler")
    if not candidate:
        raise ButlerdError("butler was not found; install butler 15.29+ or set BUTLER_PATH")
    resolved = os.path.abspath(candidate)
    if not os.path.isfile(resolved):
        raise ButlerdError("the configured butler executable does not exist")
    if os.path.basename(resolved).lower() not in ("butler", "butler.exe"):
        raise ButlerdError("BUTLER_PATH must point to an executable named butler")
    return resolved


def parse_version(value):
    match = re.search(r"(?:^|\s)v?(\d+)\.(\d+)\.(\d+)", str(value or ""))
    if not match:
        raise ButlerdError("butlerd returned an unrecognized version")
    return tuple(int(part) for part in match.groups())


class ButlerdClient(object):
    """One-process, one-database JSON-RPC client for an authenticated operation."""

    def __init__(self, api_key, butler_path=None, timeout_secs=30):
        if not api_key:
            raise ButlerdError("ITCHIO_API_KEY or BUTLER_API_KEY is required")
        self.api_key = api_key
        self.butler_path = resolve_butler_path(butler_path)
        self.timeout_secs = int(timeout_secs)
        self._temp = None
        self._process = None
        self._messages = queue.Queue()
        self._stderr = []
        self._reader = None
        self._stderr_reader = None
        self._write_lock = threading.Lock()
        self._next_id = 1
        self.version = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def start(self):
        try:
            self._temp = tempfile.TemporaryDirectory(prefix="dcc-mcp-itchio-butlerd-")
            db_path = os.path.join(self._temp.name, "butler.db")
            identity_path = os.path.join(self._temp.name, "no-persistent-identity")
            args = [
                self.butler_path,
                "--json",
                "--dbpath=" + db_path,
                "--identity=" + identity_path,
                "--context-timeout=" + str(self.timeout_secs),
                "daemon",
                "--transport=stdio",
                "--destiny-pid=" + str(os.getpid()),
            ]
            child_env = dict(os.environ)
            child_env.pop("BUTLER_API_KEY", None)
            child_env.pop("ITCHIO_API_KEY", None)
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            self._process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                env=child_env,
                creationflags=creationflags,
            )
            self._reader = threading.Thread(target=self._read_stdout)
            self._reader.daemon = True
            self._reader.start()
            self._stderr_reader = threading.Thread(target=self._read_stderr)
            self._stderr_reader.daemon = True
            self._stderr_reader.start()
            version_result = self.call("Version.Get", {}, timeout_secs=15)
            self.version = str(version_result.get("versionString") or version_result.get("version") or "")
            if parse_version(self.version) < MIN_VERSION:
                raise ButlerdError("butler 15.29.0 or newer is required")
        except BaseException:
            self.close()
            raise

    def _read_stdout(self):
        try:
            for line in self._process.stdout:
                try:
                    message = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(message, dict):
                    self._messages.put(message)
        finally:
            self._messages.put({"_eof": True})

    def _read_stderr(self):
        for line in self._process.stderr:
            self._stderr.append(line.rstrip())
            if len(self._stderr) > 20:
                del self._stderr[0]

    def _write(self, message):
        if self._process is None or self._process.stdin is None:
            raise ButlerdError("butlerd is not running")
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            self._process.stdin.write(data + "\n")
            self._process.stdin.flush()

    def call(self, method, params, timeout_secs=None, cancel_operation_id=None):
        request_id = self._next_id
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + float(timeout_secs or self.timeout_secs)
        while True:
            try:
                check_cancelled()
            except CancelledError:
                if cancel_operation_id:
                    cancel_request_id = self._next_id
                    self._next_id += 1
                    self._write(
                        {
                            "jsonrpc": "2.0",
                            "id": cancel_request_id,
                            "method": "Install.Cancel",
                            "params": {"id": cancel_operation_id},
                        }
                    )
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ButlerdError("butlerd request timed out: " + method)
            try:
                message = self._messages.get(timeout=min(0.2, remaining))
            except queue.Empty:
                if self._process.poll() is not None:
                    raise ButlerdError("butlerd exited before replying") from None
                continue
            if message.get("_eof"):
                raise ButlerdError("butlerd closed its response stream")
            if "method" in message and "id" in message:
                self._write({
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "interactive request refused"},
                })
                continue
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                error = message.get("error") or {}
                safe_message = str(error.get("message") or "JSON-RPC error")
                safe_message = safe_message.replace(self.api_key, "[REDACTED]")
                raise ButlerdError(method + " failed: " + safe_message[:500])
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def login(self):
        result = self.call("Profile.LoginWithAPIKey", {"apiKey": self.api_key})
        profile = result.get("profile") or {}
        profile_id = profile.get("id")
        if not isinstance(profile_id, int) or profile_id <= 0:
            raise ButlerdError("itch.io authentication did not return a valid profile")
        return profile_id

    def close(self):
        process = self._process
        self._process = None
        if process is not None:
            try:
                if process.stdin:
                    process.stdin.close()
            except Exception:
                pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        if self._reader:
            self._reader.join(timeout=1)
        if self._stderr_reader:
            self._stderr_reader.join(timeout=1)
        if self._temp is not None:
            self._temp.cleanup()
            self._temp = None
