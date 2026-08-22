from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "skill" / "itchio-publish" / "scripts" / "_publish.py"
SPEC = importlib.util.spec_from_file_location("itchio_publish_contract", str(MODULE_PATH))
publish = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(publish)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _release_files(tmp_path: Path):
    source = tmp_path / "game"
    source.mkdir()
    executable = source / "MyGame.exe"
    executable.write_bytes(b"game-binary")
    data = source / "data.pck"
    data.write_bytes(b"game-data")
    provenance = tmp_path / "license-provenance.json"
    provenance.write_text(
        json.dumps(
            {
                "schema": publish.PROVENANCE_SCHEMA,
                "product_name": "My Game",
                "product_version": "1.2.3",
                "executable": "MyGame.exe",
                "executable_sha256": _sha256(executable),
                "content_license_mode": "original_only",
                "third_party_notices": None,
            }
        ),
        encoding="utf-8",
    )
    return source, provenance


def _preview_result(parent_id=41):
    return {
        "channel": "windows-64",
        "has_parent": parent_id is not None,
        "parent_build_id": parent_id,
        "source_size": 20,
        "comparison": {
            "new": 1,
            "modified": 1,
            "deleted": 0,
            "same": 0,
            "newBytes": 9,
            "modifiedBytes": 11,
            "deletedBytes": 0,
            "sameBytes": 0,
        },
        "top_changed_files": {
            "new": [{"path": "data.pck", "status": "new", "size": 9}],
            "modified": [
                {"path": "MyGame.exe", "status": "modified", "size": 11}
            ],
            "deleted": [],
        },
    }


def _parent(parent_id=41):
    build = None
    if parent_id is not None:
        build = {
            "id": parent_id,
            "state": "completed",
            "version": 8,
            "user_version": "1.2.2",
        }
    return {
        "project": "studio/my-game",
        "channel": "windows-64",
        "channel_exists": parent_id is not None,
        "parent_build": build,
        "parent_build_id": parent_id,
    }


def _fake_remote(monkeypatch, parent_id=41):
    info = {
        "path": "C:/tools/butler.exe",
        "version": "15.30.0",
        "binary_sha256": "a" * 64,
    }
    monkeypatch.setattr(publish, "resolve_butler_info", lambda *args, **kwargs: info)
    monkeypatch.setattr(
        publish, "status_snapshot", lambda *args, **kwargs: _parent(parent_id)
    )
    monkeypatch.setattr(
        publish, "_preview_once", lambda *args, **kwargs: _preview_result(parent_id)
    )
    monkeypatch.setattr(publish, "_assert_butler_unchanged", lambda *args, **kwargs: None)
    return info


def test_preview_receipt_binds_all_publish_evidence(tmp_path, monkeypatch):
    source, provenance = _release_files(tmp_path)
    _fake_remote(monkeypatch, parent_id=None)

    result = publish.preview_push(
        str(source),
        str(provenance),
        "studio/my-game",
        "windows-64",
        "1.2.3",
        True,
    )

    receipt = result["preview_receipt"]
    assert publish._verify_receipt(receipt)["schema"] == publish.RECEIPT_SCHEMA
    assert receipt["source_manifest"]["file_count"] == 2
    assert receipt["license_provenance"]["sha256"] == _sha256(provenance)
    assert receipt["destination"] == {
        "project": "studio/my-game",
        "channel": "windows-64",
        "target": "studio/my-game:windows-64",
        "version": "1.2.3",
        "hidden": True,
    }
    assert receipt["butler"]["version"] == "15.30.0"
    assert receipt["butler"]["binary_sha256"] == "a" * 64
    assert receipt["status_parent"]["parent_build_id"] is None
    assert receipt["preview"]["result_sha256"] == publish._sha256_json(
        receipt["preview"]["result"]
    )


def test_edited_receipt_is_rejected_before_push(tmp_path, monkeypatch):
    source, provenance = _release_files(tmp_path)
    _fake_remote(monkeypatch)
    receipt = publish.preview_push(
        str(source),
        str(provenance),
        "studio/my-game",
        "windows-64",
        "1.2.3",
        False,
    )["preview_receipt"]
    receipt["destination"]["channel"] = "linux"

    called = []
    monkeypatch.setattr(publish, "_run_butler_json", lambda *args, **kwargs: called.append(args))
    with pytest.raises(publish.PublishContractError, match="digest"):
        publish.push_build(
            str(source),
            str(provenance),
            "studio/my-game",
            "windows-64",
            "1.2.3",
            receipt,
            False,
        )
    assert called == []


def test_push_revalidates_receipt_and_uses_safe_butler_flags(tmp_path, monkeypatch):
    source, provenance = _release_files(tmp_path)
    _fake_remote(monkeypatch, parent_id=None)
    receipt = publish.preview_push(
        str(source),
        str(provenance),
        "studio/my-game",
        "windows-64",
        "1.2.3",
        True,
    )["preview_receipt"]

    calls = []

    def fake_run(_info, arguments, cancellation=None):
        calls.append(list(arguments))
        return {
            "buildId": 55,
            "channel": "windows-64",
            "dryRun": False,
            "skipped": False,
            "reason": "",
        }

    monkeypatch.setattr(publish, "_run_butler_json", fake_run)
    result = publish.push_build(
        str(source),
        str(provenance),
        "studio/my-game",
        "windows-64",
        "1.2.3",
        receipt,
        True,
    )

    assert result["push"]["build_id"] == 55
    assert calls == [
        [
            "push",
            "--json",
            "--no-auto-wrap",
            "--no-auto-unzip",
            "--if-changed",
            "--userversion=1.2.3",
            "--hidden",
            str(source.resolve()),
            "studio/my-game:windows-64",
        ]
    ]


def test_parent_drift_refuses_to_start_push(tmp_path, monkeypatch):
    source, provenance = _release_files(tmp_path)
    _fake_remote(monkeypatch, parent_id=41)
    receipt = publish.preview_push(
        str(source),
        str(provenance),
        "studio/my-game",
        "windows-64",
        "1.2.3",
        False,
    )["preview_receipt"]
    monkeypatch.setattr(
        publish, "status_snapshot", lambda *args, **kwargs: _parent(42)
    )
    calls = []
    monkeypatch.setattr(publish, "_run_butler_json", lambda *args, **kwargs: calls.append(args))

    with pytest.raises(publish.PublishContractError, match="parent drifted"):
        publish.push_build(
            str(source),
            str(provenance),
            "studio/my-game",
            "windows-64",
            "1.2.3",
            receipt,
            False,
        )
    assert calls == []


def test_source_or_license_change_invalidates_receipt(tmp_path, monkeypatch):
    source, provenance = _release_files(tmp_path)
    _fake_remote(monkeypatch)
    receipt = publish.preview_push(
        str(source),
        str(provenance),
        "studio/my-game",
        "windows-64",
        "1.2.3",
        False,
    )["preview_receipt"]
    (source / "data.pck").write_bytes(b"changed")

    with pytest.raises(publish.PublishContractError, match="source manifest"):
        publish.push_build(
            str(source),
            str(provenance),
            "studio/my-game",
            "windows-64",
            "1.2.3",
            receipt,
            False,
        )


def test_hidden_true_is_rejected_for_existing_channel(tmp_path, monkeypatch):
    source, provenance = _release_files(tmp_path)
    _fake_remote(monkeypatch, parent_id=41)

    with pytest.raises(publish.PublishContractError, match="only when creating"):
        publish.preview_push(
            str(source),
            str(provenance),
            "studio/my-game",
            "windows-64",
            "1.2.3",
            True,
        )


def test_list_projects_reads_secret_only_from_environment(monkeypatch):
    monkeypatch.setenv("ITCHIO_API_KEY", "secret-value")
    observed = {}

    def request(url, **kwargs):
        observed["url"] = url
        observed.update(kwargs)
        return {"games": [{"id": 7, "title": "Game", "url": "https://x.itch.io/game"}]}

    result = publish.list_projects(10, request=request)
    assert result["projects"] == [
        {"id": 7, "title": "Game", "url": "https://x.itch.io/game"}
    ]
    assert observed["headers"]["Authorization"] == "Bearer secret-value"
    assert "secret-value" not in json.dumps(result)


def test_tool_schema_has_no_credential_argument():
    schema = (ROOT / "skill" / "itchio-publish" / "tools.yaml").read_text(
        encoding="utf-8"
    )
    assert "api_key:" not in schema.lower()
    assert "token:" not in schema.lower()
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "shell=False" in source
