"""Security-sensitive itch.io publishing primitives.

This module deliberately keeps authentication out of function signatures. The
itch.io API and butler credentials are read from the process environment (or
butler's own credential file) and are never included in receipts or command
arguments.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from dcc_mcp_core.skills_helper import check_cancelled, http_get_json

RECEIPT_SCHEMA = "dcc-mcp.itchio.push-preview-receipt.v1"
PROVENANCE_SCHEMA = "dcc-mcp.game-release.license-provenance.v1"
MANIFEST_ALGORITHM = "sha256-tree-v1"
MIN_BUTLER_VERSION = (15, 30, 0)
MAX_API_BYTES = 4 * 1024 * 1024
MAX_PROVENANCE_BYTES = 2 * 1024 * 1024
MAX_PROCESS_STREAM_BYTES = 16 * 1024 * 1024
MAX_SOURCE_FILES = 250000
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

PROJECT_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?/[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$"
)
CHANNEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
VERSION_RE = re.compile(r"^[0-9A-Za-z](?:[0-9A-Za-z._+-]{0,126}[0-9A-Za-z])?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BUTLER_VERSION_RE = re.compile(r"\bv?(\d+)\.(\d+)\.(\d+)\b")


class PublishContractError(RuntimeError):
    """Raised when local or remote evidence violates the publish contract."""


class ButlerCommandError(PublishContractError):
    """Raised when a bounded butler command fails."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _require_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise PublishContractError("{} must be a string".format(label))
    result = value.strip()
    if not result:
        raise PublishContractError("{} must not be empty".format(label))
    if len(result) > maximum:
        raise PublishContractError("{} exceeds {} characters".format(label, maximum))
    if any(ord(char) < 32 or ord(char) == 127 for char in result):
        raise PublishContractError("{} contains control characters".format(label))
    return result


def normalize_project(project: Any) -> str:
    value = _require_text(project, "project", 200).lower()
    if not PROJECT_RE.match(value):
        raise PublishContractError(
            "project must use the exact lowercase owner/game-slug form"
        )
    return value


def normalize_channel(channel: Any) -> str:
    value = _require_text(channel, "channel", 100)
    if not CHANNEL_RE.match(value):
        raise PublishContractError(
            "channel may contain only letters, numbers, dots, underscores, and hyphens"
        )
    return value


def normalize_version(version: Any) -> str:
    value = _require_text(version, "version", 128)
    if not VERSION_RE.match(value):
        raise PublishContractError(
            "version may contain only letters, numbers, dots, underscores, plus signs, and hyphens"
        )
    return value


def _is_reparse_point(path_stat: os.stat_result) -> bool:
    attributes = int(getattr(path_stat, "st_file_attributes", 0))
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _assert_regular_local_file(path: Path, label: str) -> os.stat_result:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise PublishContractError("{} is not readable: {}".format(label, exc)) from exc
    if stat.S_ISLNK(path_stat.st_mode) or _is_reparse_point(path_stat):
        raise PublishContractError("{} must not be a symlink or reparse point".format(label))
    if not stat.S_ISREG(path_stat.st_mode):
        raise PublishContractError("{} must be a regular file".format(label))
    return path_stat


def _assert_within_root(root: Path, candidate: Path, label: str) -> Path:
    resolved = candidate.resolve()
    try:
        common = os.path.commonpath([str(root), str(resolved)])
    except ValueError as exc:
        raise PublishContractError(
            "{} is outside source_directory".format(label)
        ) from exc
    if os.path.normcase(common) != os.path.normcase(str(root)):
        raise PublishContractError("{} is outside source_directory".format(label))
    return resolved


def file_sha256(
    path: Path,
    cancellation: Callable[[], None] = check_cancelled,
) -> str:
    _assert_regular_local_file(path, "file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            cancellation()
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def build_source_manifest(
    source_directory: str,
    cancellation: Callable[[], None] = check_cancelled,
) -> Dict[str, Any]:
    source = Path(source_directory).expanduser().resolve()
    if not source.is_dir():
        raise PublishContractError("source_directory must be an existing directory")
    root_stat = source.lstat()
    if stat.S_ISLNK(root_stat.st_mode) or _is_reparse_point(root_stat):
        raise PublishContractError(
            "source_directory must not be a symlink or reparse point"
        )

    relative_files: List[str] = []
    for current_raw, directories, files in os.walk(str(source), topdown=True, followlinks=False):
        cancellation()
        current = Path(current_raw)
        safe_directories: List[str] = []
        for name in sorted(directories):
            directory = current / name
            directory_stat = directory.lstat()
            if stat.S_ISLNK(directory_stat.st_mode) or _is_reparse_point(directory_stat):
                raise PublishContractError(
                    "source tree contains a symlink or reparse point: {}".format(
                        directory.relative_to(source).as_posix()
                    )
                )
            if not stat.S_ISDIR(directory_stat.st_mode):
                raise PublishContractError(
                    "source tree contains a non-directory entry: {}".format(
                        directory.relative_to(source).as_posix()
                    )
                )
            safe_directories.append(name)
        directories[:] = safe_directories
        for name in sorted(files):
            candidate = current / name
            _assert_regular_local_file(candidate, "source file")
            relative_files.append(candidate.relative_to(source).as_posix())
            if len(relative_files) > MAX_SOURCE_FILES:
                raise PublishContractError(
                    "source tree exceeds the {} file safety limit".format(MAX_SOURCE_FILES)
                )

    relative_files.sort()
    manifest_digest = hashlib.sha256()
    total_bytes = 0
    for relative_path in relative_files:
        cancellation()
        candidate = _assert_within_root(
            source, source.joinpath(*PurePosixPath(relative_path).parts), "source file"
        )
        before = _assert_regular_local_file(candidate, "source file")
        digest = file_sha256(candidate, cancellation=cancellation)
        after = _assert_regular_local_file(candidate, "source file")
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise PublishContractError(
                "source file changed while hashing: {}".format(relative_path)
            )
        entry = {
            "path": relative_path,
            "bytes": int(after.st_size),
            "sha256": digest,
        }
        manifest_digest.update(_canonical_json(entry).encode("utf-8"))
        manifest_digest.update(b"\n")
        total_bytes += int(after.st_size)

    return {
        "algorithm": MANIFEST_ALGORITHM,
        "source_directory": str(source),
        "file_count": len(relative_files),
        "total_bytes": total_bytes,
        "manifest_sha256": manifest_digest.hexdigest(),
    }


def _safe_relative_file(root: Path, raw_path: Any, label: str) -> Tuple[str, Path]:
    value = _require_text(raw_path, label, 1024).replace("\\", "/")
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        raise PublishContractError("{} must be a safe relative path".format(label))
    resolved = _assert_within_root(root, root.joinpath(*pure.parts), label)
    _assert_regular_local_file(resolved, label)
    return pure.as_posix(), resolved


def validate_license_provenance(
    license_provenance_path: str,
    source_directory: str,
    expected_version: str,
    cancellation: Callable[[], None] = check_cancelled,
) -> Dict[str, Any]:
    provenance_path = Path(license_provenance_path).expanduser().resolve()
    provenance_stat = _assert_regular_local_file(
        provenance_path, "license_provenance_path"
    )
    if provenance_stat.st_size > MAX_PROVENANCE_BYTES:
        raise PublishContractError(
            "license provenance exceeds {} bytes".format(MAX_PROVENANCE_BYTES)
        )
    try:
        raw = provenance_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise PublishContractError(
            "license provenance is invalid JSON: {}".format(exc)
        ) from exc
    if not isinstance(payload, dict):
        raise PublishContractError("license provenance root must be an object")
    if payload.get("schema") != PROVENANCE_SCHEMA:
        raise PublishContractError(
            "license provenance schema must be {}".format(PROVENANCE_SCHEMA)
        )

    source = Path(source_directory).expanduser().resolve()
    product_name = _require_text(payload.get("product_name"), "product_name", 256)
    product_version = normalize_version(payload.get("product_version"))
    if product_version != expected_version:
        raise PublishContractError(
            "version must match license provenance product_version"
        )

    executable_relative, executable_path = _safe_relative_file(
        source, payload.get("executable"), "executable"
    )
    executable_digest = _require_text(
        payload.get("executable_sha256"), "executable_sha256", 64
    ).lower()
    if not SHA256_RE.match(executable_digest):
        raise PublishContractError("executable_sha256 must be a lowercase SHA-256 digest")
    if file_sha256(executable_path, cancellation=cancellation) != executable_digest:
        raise PublishContractError(
            "executable no longer matches the license provenance digest"
        )

    mode = payload.get("content_license_mode")
    notices_summary: Optional[Dict[str, Any]] = None
    notices = payload.get("third_party_notices")
    if mode == "original_only":
        if notices is not None:
            raise PublishContractError(
                "original_only provenance must not contain third_party_notices"
            )
    elif mode == "third_party_notices":
        if not isinstance(notices, dict):
            raise PublishContractError(
                "third_party_notices provenance requires a notices object"
            )
        notices_relative, notices_path = _safe_relative_file(
            source, notices.get("relative_path"), "third_party_notices.relative_path"
        )
        notices_stat = notices_path.stat()
        notices_bytes = notices.get("bytes")
        if not isinstance(notices_bytes, int) or isinstance(notices_bytes, bool):
            raise PublishContractError("third_party_notices.bytes must be an integer")
        if notices_bytes != notices_stat.st_size:
            raise PublishContractError("third-party notices size no longer matches provenance")
        notices_digest = _require_text(
            notices.get("sha256"), "third_party_notices.sha256", 64
        ).lower()
        if not SHA256_RE.match(notices_digest):
            raise PublishContractError(
                "third_party_notices.sha256 must be a lowercase SHA-256 digest"
            )
        if file_sha256(notices_path, cancellation=cancellation) != notices_digest:
            raise PublishContractError(
                "third-party notices no longer match the provenance digest"
            )
        notices_summary = {
            "relative_path": notices_relative,
            "bytes": notices_bytes,
            "sha256": notices_digest,
        }
    else:
        raise PublishContractError(
            "content_license_mode must be original_only or third_party_notices"
        )

    cancellation()
    return {
        "schema": PROVENANCE_SCHEMA,
        "path": str(provenance_path),
        "sha256": _sha256_bytes(raw),
        "product_name": product_name,
        "product_version": product_version,
        "content_license_mode": mode,
        "executable": executable_relative,
        "executable_sha256": executable_digest,
        "third_party_notices": notices_summary,
    }


class _BoundedBuffer(object):
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.data = bytearray()
        self.overflowed = False
        self.lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        with self.lock:
            remaining = self.maximum - len(self.data)
            if remaining > 0:
                self.data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.overflowed = True

    def bytes(self) -> bytes:
        with self.lock:
            return bytes(self.data)


def _drain_pipe(pipe: Any, buffer: _BoundedBuffer) -> None:
    try:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                return
            buffer.append(chunk)
    finally:
        with suppress(Exception):
            pipe.close()


def _stop_local_process(process: subprocess.Popen) -> None:
    """Stop only the local butler process; never issue a remote cancellation."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            pass


def _credential_values(environment: Dict[str, str]) -> List[str]:
    values: List[str] = []
    for name in ("BUTLER_API_KEY", "ITCHIO_API_KEY"):
        value = environment.get(name, "")
        if len(value) >= 4:
            values.append(value)
    return values


def _redact(text: str, secrets: Iterable[str]) -> str:
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    result = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+",
        r"\1[REDACTED]",
        result,
    )
    result = re.sub(
        r"(?i)((?:api[_-]?key|token)\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        result,
    )
    return result


def _butler_environment() -> Dict[str, str]:
    environment = dict(os.environ)
    if not environment.get("BUTLER_API_KEY") and environment.get("ITCHIO_API_KEY"):
        environment["BUTLER_API_KEY"] = environment["ITCHIO_API_KEY"]
    environment.setdefault("NO_COLOR", "1")
    return environment


def _default_butler_credential_paths(environment: Dict[str, str]) -> List[Path]:
    paths: List[Path] = []
    configured = environment.get("XDG_CONFIG_HOME", "").strip()
    if configured:
        paths.append(Path(configured) / "itch" / "butler_creds")
    paths.append(Path.home() / ".config" / "itch" / "butler_creds")
    return paths


def _require_butler_authentication(environment: Dict[str, str]) -> None:
    if environment.get("BUTLER_API_KEY"):
        return
    for path in _default_butler_credential_paths(environment):
        if path.is_file():
            return
    raise PublishContractError(
        "butler authentication is unavailable; set BUTLER_API_KEY or run butler login outside the skill"
    )


def _run_process(
    argv: Sequence[str],
    environment: Optional[Dict[str, str]] = None,
    cancellation: Callable[[], None] = check_cancelled,
) -> Tuple[int, str, str]:
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise PublishContractError("process argv must be a non-empty string list")
    child_environment = dict(environment or os.environ)
    stdout_buffer = _BoundedBuffer(MAX_PROCESS_STREAM_BYTES)
    stderr_buffer = _BoundedBuffer(MAX_PROCESS_STREAM_BYTES)
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=child_environment,
    )
    stdout_thread = threading.Thread(
        target=_drain_pipe, args=(process.stdout, stdout_buffer)
    )
    stderr_thread = threading.Thread(
        target=_drain_pipe, args=(process.stderr, stderr_buffer)
    )
    stdout_thread.daemon = True
    stderr_thread.daemon = True
    stdout_thread.start()
    stderr_thread.start()
    try:
        while process.poll() is None:
            cancellation()
            if stdout_buffer.overflowed or stderr_buffer.overflowed:
                _stop_local_process(process)
                raise ButlerCommandError(
                    "butler output exceeded the bounded capture limit"
                )
            time.sleep(0.05)
    except BaseException:
        _stop_local_process(process)
        raise
    finally:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        raise ButlerCommandError("butler output reader did not terminate")
    secrets = _credential_values(child_environment)
    stdout = _redact(stdout_buffer.bytes().decode("utf-8", "replace"), secrets)
    stderr = _redact(stderr_buffer.bytes().decode("utf-8", "replace"), secrets)
    return int(process.returncode or 0), stdout, stderr


def _resolve_butler_path(butler_path: str = "") -> Path:
    candidate = butler_path.strip() if isinstance(butler_path, str) else ""
    if not candidate:
        candidate = os.environ.get("BUTLER_PATH", "").strip()
    if not candidate:
        candidate = shutil.which("butler") or ""
    if not candidate:
        raise PublishContractError(
            "butler executable was not found; install butler or set BUTLER_PATH"
        )
    resolved = Path(candidate).expanduser().resolve()
    if resolved.name.lower() not in ("butler", "butler.exe"):
        raise PublishContractError("butler_path must name butler or butler.exe")
    _assert_regular_local_file(resolved, "butler executable")
    return resolved


def resolve_butler_info(
    butler_path: str = "",
    cancellation: Callable[[], None] = check_cancelled,
) -> Dict[str, Any]:
    resolved = _resolve_butler_path(butler_path)
    digest_before = file_sha256(resolved, cancellation=cancellation)
    return_code, stdout, stderr = _run_process(
        [str(resolved), "version"], cancellation=cancellation
    )
    if return_code != 0:
        message = (stderr or stdout).strip()[-4096:]
        raise ButlerCommandError("butler version failed: {}".format(message))
    match = BUTLER_VERSION_RE.search(stdout + "\n" + stderr)
    if not match:
        raise PublishContractError("could not parse the butler version")
    version_tuple = tuple(int(part) for part in match.groups())
    if version_tuple < MIN_BUTLER_VERSION:
        raise PublishContractError("butler 15.30.0 or newer is required")
    digest_after = file_sha256(resolved, cancellation=cancellation)
    if digest_before != digest_after:
        raise PublishContractError("butler executable changed during verification")
    return {
        "path": str(resolved),
        "version": "{}.{}.{}".format(*version_tuple),
        "binary_sha256": digest_after,
    }


def _assert_butler_unchanged(
    butler_info: Dict[str, Any],
    cancellation: Callable[[], None] = check_cancelled,
) -> None:
    actual = file_sha256(Path(butler_info["path"]), cancellation=cancellation)
    if actual != butler_info.get("binary_sha256"):
        raise PublishContractError("butler executable changed after verification")


def _parse_json_events(stdout: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise ButlerCommandError(
                "butler emitted non-JSON output in --json mode"
            ) from exc
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise ButlerCommandError("butler emitted an invalid JSON event")
        events.append(value)
    return events


def _run_butler_json(
    butler_info: Dict[str, Any],
    arguments: Sequence[str],
    cancellation: Callable[[], None] = check_cancelled,
) -> Dict[str, Any]:
    _assert_butler_unchanged(butler_info, cancellation=cancellation)
    environment = _butler_environment()
    _require_butler_authentication(environment)
    argv = [str(butler_info["path"])] + list(arguments)
    return_code, stdout, stderr = _run_process(
        argv, environment=environment, cancellation=cancellation
    )
    events = _parse_json_events(stdout)
    if return_code != 0:
        errors = [event.get("message") for event in events if event.get("type") == "error"]
        message = str(errors[-1]) if errors else (stderr or stdout).strip()[-4096:]
        raise ButlerCommandError("butler command failed: {}".format(message))
    results = [event.get("value") for event in events if event.get("type") == "result"]
    if not results or not isinstance(results[-1], dict):
        raise ButlerCommandError("butler command did not emit a structured result")
    return results[-1]


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ButlerCommandError("{} must be an integer >= {}".format(label, minimum))
    return value


def _normalize_build(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ButlerCommandError("butler status build must be an object")
    result = {
        "id": _integer(value.get("id"), "build.id", 1),
        "state": _require_text(value.get("state"), "build.state", 64),
        "version": _integer(value.get("version"), "build.version", 0),
        "user_version": str(value.get("userVersion") or ""),
    }
    if "parentBuildId" in value:
        result["parent_build_id"] = _integer(
            value.get("parentBuildId"), "build.parentBuildId", 1
        )
    return result


def status_snapshot(
    project: str,
    channel: str,
    butler_info: Dict[str, Any],
    cancellation: Callable[[], None] = check_cancelled,
) -> Dict[str, Any]:
    target = "{}:{}".format(project, channel)
    value = _run_butler_json(
        butler_info,
        ["status", "--json", target],
        cancellation=cancellation,
    )
    channels = value.get("channels")
    if value.get("target") != project or not isinstance(channels, list):
        raise ButlerCommandError("butler status returned an unexpected target")
    matches = [item for item in channels if isinstance(item, dict) and item.get("name") == channel]
    if len(matches) > 1:
        raise ButlerCommandError("butler status returned duplicate channels")
    channel_value = matches[0] if matches else None
    head = _normalize_build(channel_value.get("head")) if channel_value else None
    return {
        "project": project,
        "channel": channel,
        "channel_exists": channel_value is not None,
        "parent_build": head,
        "parent_build_id": head.get("id") if head else None,
    }


def inspect_channels(
    project: str,
    channel: str = "",
    butler_path: str = "",
    cancellation: Callable[[], None] = check_cancelled,
) -> Dict[str, Any]:
    normalized_project = normalize_project(project)
    normalized_channel = normalize_channel(channel) if channel else ""
    butler_info = resolve_butler_info(butler_path, cancellation=cancellation)
    target = (
        "{}:{}".format(normalized_project, normalized_channel)
        if normalized_channel
        else normalized_project
    )
    value = _run_butler_json(
        butler_info, ["status", "--json", target], cancellation=cancellation
    )
    channels = value.get("channels")
    if value.get("target") != normalized_project or not isinstance(channels, list):
        raise ButlerCommandError("butler status returned an unexpected target")
    normalized_channels: List[Dict[str, Any]] = []
    for item in channels:
        if not isinstance(item, dict):
            raise ButlerCommandError("butler status channel must be an object")
        name = normalize_channel(item.get("name"))
        normalized_channels.append(
            {
                "name": name,
                "upload_id": item.get("uploadId"),
                "tags": list(item.get("tags") or []),
                "head": _normalize_build(item.get("head")),
                "pending": _normalize_build(item.get("pending")),
            }
        )
    return {
        "project": normalized_project,
        "requested_channel": normalized_channel or None,
        "channels": normalized_channels,
        "butler": {
            "version": butler_info["version"],
            "binary_sha256": butler_info["binary_sha256"],
        },
    }


def _normalize_count(value: Any, label: str) -> int:
    return _integer(value, label, 0)


def _normalize_preview_result(value: Any, expected_channel: str) -> Dict[str, Any]:
    if not isinstance(value, dict) or value.get("channel") != expected_channel:
        raise ButlerCommandError("butler preview returned an unexpected channel")
    has_parent = value.get("hasParent")
    if not isinstance(has_parent, bool):
        raise ButlerCommandError("butler preview hasParent must be boolean")
    parent_build_id: Optional[int] = None
    if has_parent:
        parent_build_id = _integer(value.get("parentBuildId"), "parentBuildId", 1)
    elif value.get("parentBuildId") is not None:
        raise ButlerCommandError("butler preview returned a parent for a new channel")
    comparison = value.get("comparison")
    if not isinstance(comparison, dict):
        raise ButlerCommandError("butler preview comparison must be an object")
    comparison_result = {
        key: _normalize_count(comparison.get(key), "comparison.{}".format(key))
        for key in (
            "new",
            "modified",
            "deleted",
            "same",
            "newBytes",
            "modifiedBytes",
            "deletedBytes",
            "sameBytes",
        )
    }
    top_value = value.get("topChangedFiles")
    if not isinstance(top_value, dict):
        raise ButlerCommandError("butler preview topChangedFiles must be an object")
    top_result: Dict[str, List[Dict[str, Any]]] = {}
    for category in ("new", "modified", "deleted"):
        entries = top_value.get(category)
        if not isinstance(entries, list) or len(entries) > 20:
            raise ButlerCommandError(
                "butler preview topChangedFiles.{} must be a bounded array".format(category)
            )
        normalized_entries: List[Dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ButlerCommandError("butler preview changed entry must be an object")
            path = _require_text(entry.get("path"), "changed path", 4096)
            status_value = entry.get("status")
            if status_value != category:
                raise ButlerCommandError("butler preview changed status is inconsistent")
            normalized_entries.append(
                {
                    "path": path,
                    "status": status_value,
                    "size": _integer(entry.get("size"), "changed size", 0),
                }
            )
        top_result[category] = normalized_entries
    return {
        "channel": expected_channel,
        "has_parent": has_parent,
        "parent_build_id": parent_build_id,
        "source_size": _integer(value.get("sourceSize"), "sourceSize", 0),
        "comparison": comparison_result,
        "top_changed_files": top_result,
    }


def _preview_once(
    source_directory: str,
    project: str,
    channel: str,
    butler_info: Dict[str, Any],
    cancellation: Callable[[], None] = check_cancelled,
) -> Dict[str, Any]:
    target = "{}:{}".format(project, channel)
    result = _run_butler_json(
        butler_info,
        [
            "push-preview",
            "--json",
            "--changes-only",
            "--no-auto-wrap",
            "--no-auto-unzip",
            source_directory,
            target,
        ],
        cancellation=cancellation,
    )
    return _normalize_preview_result(result, channel)


def _same_parent(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return (
        left.get("channel_exists") == right.get("channel_exists")
        and left.get("parent_build_id") == right.get("parent_build_id")
        and left.get("parent_build") == right.get("parent_build")
    )


def _assert_preview_parent(
    status: Dict[str, Any], preview: Dict[str, Any]
) -> None:
    if preview.get("has_parent") != (status.get("parent_build_id") is not None):
        raise PublishContractError("channel parent changed during preview")
    if preview.get("parent_build_id") != status.get("parent_build_id"):
        raise PublishContractError("channel parent changed during preview")


def _receipt_payload(
    source_manifest: Dict[str, Any],
    license_provenance: Dict[str, Any],
    project: str,
    channel: str,
    version: str,
    hidden: bool,
    butler_info: Dict[str, Any],
    status_parent: Dict[str, Any],
    preview_result: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "issued_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "source_manifest": source_manifest,
        "license_provenance": license_provenance,
        "destination": {
            "project": project,
            "channel": channel,
            "target": "{}:{}".format(project, channel),
            "version": version,
            "hidden": hidden,
        },
        "butler": {
            "version": butler_info["version"],
            "binary_sha256": butler_info["binary_sha256"],
        },
        "options": {
            "auto_wrap": False,
            "auto_unzip": False,
            "fix_permissions": True,
            "if_changed": True,
        },
        "status_parent": status_parent,
        "preview": {
            "result": preview_result,
            "result_sha256": _sha256_json(preview_result),
        },
    }


def _seal_receipt(payload: Dict[str, Any]) -> Dict[str, Any]:
    receipt = dict(payload)
    receipt["receipt_sha256"] = _sha256_json(payload)
    return receipt


def _verify_receipt(receipt: Any) -> Dict[str, Any]:
    if not isinstance(receipt, dict):
        raise PublishContractError("preview_receipt must be an object")
    digest = receipt.get("receipt_sha256")
    if not isinstance(digest, str) or not SHA256_RE.match(digest):
        raise PublishContractError("preview receipt digest is missing or invalid")
    payload = dict(receipt)
    payload.pop("receipt_sha256", None)
    if payload.get("schema") != RECEIPT_SCHEMA:
        raise PublishContractError("preview receipt schema is unsupported")
    if _sha256_json(payload) != digest:
        raise PublishContractError("preview receipt digest does not match its contents")
    return payload


def preview_push(
    source_directory: str,
    license_provenance_path: str,
    project: str,
    channel: str,
    version: str,
    hidden: bool = True,
    butler_path: str = "",
    cancellation: Callable[[], None] = check_cancelled,
) -> Dict[str, Any]:
    normalized_project = normalize_project(project)
    normalized_channel = normalize_channel(channel)
    normalized_version = normalize_version(version)
    if not isinstance(hidden, bool):
        raise PublishContractError("hidden must be boolean")
    source_manifest = build_source_manifest(
        source_directory, cancellation=cancellation
    )
    provenance = validate_license_provenance(
        license_provenance_path,
        source_manifest["source_directory"],
        normalized_version,
        cancellation=cancellation,
    )
    butler_info = resolve_butler_info(butler_path, cancellation=cancellation)
    status_before = status_snapshot(
        normalized_project,
        normalized_channel,
        butler_info,
        cancellation=cancellation,
    )
    if hidden and status_before.get("channel_exists"):
        raise PublishContractError(
            "hidden=true is valid only when creating a new channel; use hidden=false for an existing channel"
        )
    preview_result = _preview_once(
        source_manifest["source_directory"],
        normalized_project,
        normalized_channel,
        butler_info,
        cancellation=cancellation,
    )
    _assert_preview_parent(status_before, preview_result)
    source_manifest_after = build_source_manifest(
        source_manifest["source_directory"], cancellation=cancellation
    )
    _assert_equal("source manifest", source_manifest_after, source_manifest)
    provenance_after = validate_license_provenance(
        license_provenance_path,
        source_manifest["source_directory"],
        normalized_version,
        cancellation=cancellation,
    )
    _assert_equal("license provenance", provenance_after, provenance)
    status_after = status_snapshot(
        normalized_project,
        normalized_channel,
        butler_info,
        cancellation=cancellation,
    )
    if not _same_parent(status_before, status_after):
        raise PublishContractError("channel parent changed while creating the preview receipt")
    payload = _receipt_payload(
        source_manifest,
        provenance,
        normalized_project,
        normalized_channel,
        normalized_version,
        hidden,
        butler_info,
        status_after,
        preview_result,
    )
    receipt = _seal_receipt(payload)
    return {
        "preview_receipt": receipt,
        "receipt_sha256": receipt["receipt_sha256"],
        "target": "{}:{}".format(normalized_project, normalized_channel),
        "version": normalized_version,
        "hidden": hidden,
        "source_manifest": source_manifest,
        "license_provenance": provenance,
        "status_parent": status_after,
        "preview": preview_result,
    }


def _assert_equal(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise PublishContractError("{} no longer matches the preview receipt".format(label))


def push_build(
    source_directory: str,
    license_provenance_path: str,
    project: str,
    channel: str,
    version: str,
    preview_receipt: Dict[str, Any],
    hidden: bool = True,
    butler_path: str = "",
    cancellation: Callable[[], None] = check_cancelled,
) -> Dict[str, Any]:
    payload = _verify_receipt(preview_receipt)
    normalized_project = normalize_project(project)
    normalized_channel = normalize_channel(channel)
    normalized_version = normalize_version(version)
    if not isinstance(hidden, bool):
        raise PublishContractError("hidden must be boolean")
    destination = payload.get("destination")
    if not isinstance(destination, dict):
        raise PublishContractError("preview receipt destination is invalid")
    expected_destination = {
        "project": normalized_project,
        "channel": normalized_channel,
        "target": "{}:{}".format(normalized_project, normalized_channel),
        "version": normalized_version,
        "hidden": hidden,
    }
    _assert_equal("destination", expected_destination, destination)
    _assert_equal(
        "push options",
        {
            "auto_wrap": False,
            "auto_unzip": False,
            "fix_permissions": True,
            "if_changed": True,
        },
        payload.get("options"),
    )

    source_manifest = build_source_manifest(
        source_directory, cancellation=cancellation
    )
    _assert_equal("source manifest", source_manifest, payload.get("source_manifest"))
    provenance = validate_license_provenance(
        license_provenance_path,
        source_manifest["source_directory"],
        normalized_version,
        cancellation=cancellation,
    )
    _assert_equal("license provenance", provenance, payload.get("license_provenance"))
    butler_info = resolve_butler_info(butler_path, cancellation=cancellation)
    expected_butler = {
        "version": butler_info["version"],
        "binary_sha256": butler_info["binary_sha256"],
    }
    _assert_equal("butler identity", expected_butler, payload.get("butler"))

    expected_parent = payload.get("status_parent")
    if not isinstance(expected_parent, dict):
        raise PublishContractError("preview receipt status_parent is invalid")
    status_before = status_snapshot(
        normalized_project,
        normalized_channel,
        butler_info,
        cancellation=cancellation,
    )
    if not _same_parent(status_before, expected_parent):
        raise PublishContractError(
            "channel parent drifted after preview; create a fresh preview receipt"
        )

    fresh_preview = _preview_once(
        source_manifest["source_directory"],
        normalized_project,
        normalized_channel,
        butler_info,
        cancellation=cancellation,
    )
    _assert_preview_parent(status_before, fresh_preview)
    preview_evidence = payload.get("preview")
    if not isinstance(preview_evidence, dict):
        raise PublishContractError("preview receipt result is invalid")
    _assert_equal("preview result", fresh_preview, preview_evidence.get("result"))
    _assert_equal(
        "preview result digest",
        _sha256_json(fresh_preview),
        preview_evidence.get("result_sha256"),
    )

    final_source_manifest = build_source_manifest(
        source_manifest["source_directory"], cancellation=cancellation
    )
    _assert_equal("source manifest", final_source_manifest, source_manifest)
    final_provenance = validate_license_provenance(
        license_provenance_path,
        source_manifest["source_directory"],
        normalized_version,
        cancellation=cancellation,
    )
    _assert_equal("license provenance", final_provenance, provenance)
    _assert_butler_unchanged(butler_info, cancellation=cancellation)

    status_immediately_before_push = status_snapshot(
        normalized_project,
        normalized_channel,
        butler_info,
        cancellation=cancellation,
    )
    if not _same_parent(status_before, status_immediately_before_push):
        raise PublishContractError(
            "channel parent drifted during final validation; no push was started"
        )

    target = "{}:{}".format(normalized_project, normalized_channel)
    arguments = [
        "push",
        "--json",
        "--no-auto-wrap",
        "--no-auto-unzip",
        "--if-changed",
        "--userversion={}".format(normalized_version),
    ]
    if hidden:
        arguments.append("--hidden")
    arguments.extend([source_manifest["source_directory"], target])
    push_result = _run_butler_json(
        butler_info, arguments, cancellation=cancellation
    )
    if push_result.get("channel") != normalized_channel:
        raise ButlerCommandError("butler push returned an unexpected channel")
    if push_result.get("dryRun") is not False:
        raise ButlerCommandError("butler push unexpectedly reported dry-run mode")
    build_id = push_result.get("buildId")
    skipped = push_result.get("skipped")
    if not isinstance(skipped, bool):
        raise ButlerCommandError("butler push skipped must be boolean")
    if not skipped:
        _integer(build_id, "push buildId", 1)

    post_push_status: Optional[Dict[str, Any]] = None
    post_push_warning = ""
    try:
        post_push_status = status_snapshot(
            normalized_project,
            normalized_channel,
            butler_info,
            cancellation=cancellation,
        )
    except Exception as exc:
        post_push_warning = (
            "Push completed, but the follow-up status check failed: {}".format(exc)
        )
    return {
        "target": target,
        "version": normalized_version,
        "hidden": hidden,
        "receipt_sha256": preview_receipt["receipt_sha256"],
        "push": {
            "build_id": build_id,
            "channel": normalized_channel,
            "skipped": skipped,
            "reason": str(push_result.get("reason") or ""),
        },
        "post_push_status": post_push_status,
        "warning": post_push_warning or None,
    }


def list_projects(
    max_results: int = 100,
    request: Callable[..., Any] = http_get_json,
) -> Dict[str, Any]:
    if not isinstance(max_results, int) or isinstance(max_results, bool):
        raise PublishContractError("max_results must be an integer")
    if max_results < 1 or max_results > 200:
        raise PublishContractError("max_results must be between 1 and 200")
    api_key = os.environ.get("ITCHIO_API_KEY") or os.environ.get("BUTLER_API_KEY")
    if not api_key:
        raise PublishContractError(
            "itch.io API authentication is unavailable; set ITCHIO_API_KEY or BUTLER_API_KEY"
        )
    payload = request(
        "https://api.itch.io/profile/games",
        headers={"Authorization": "Bearer {}".format(api_key)},
        timeout_ms=30000,
        max_bytes=MAX_API_BYTES,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("games"), list):
        raise PublishContractError("itch.io profile/games returned an invalid response")
    allowed_fields = (
        "id",
        "title",
        "url",
        "short_text",
        "type",
        "classification",
        "published",
        "created_at",
        "published_at",
        "views_count",
        "downloads_count",
        "purchases_count",
    )
    projects: List[Dict[str, Any]] = []
    for raw_game in payload["games"][:max_results]:
        if not isinstance(raw_game, dict):
            raise PublishContractError("itch.io game entry must be an object")
        projects.append(
            {field: raw_game[field] for field in allowed_fields if field in raw_game}
        )
    return {
        "projects": projects,
        "returned": len(projects),
        "available": len(payload["games"]),
        "truncated": len(payload["games"]) > len(projects),
    }
