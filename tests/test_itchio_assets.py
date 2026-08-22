import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "itchio-assets" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _butlerd as butlerd  # noqa: E402
import _itchio_assets as assets  # noqa: E402
from _butlerd import ButlerdError, parse_version  # noqa: E402


class FakeResponse(object):
    def __init__(
        self,
        status,
        body=b"",
        url="https://itch.io/game-assets.xml",
        content_type="application/rss+xml",
    ):
        self.status = status
        self.bytes = body
        self.url = url
        self.headers = {"content-type": content_type}


class FakeButlerd(object):
    unsafe = False
    owner_id = 7

    def __init__(self, api_key, butler_path=None, timeout_secs=30):
        assert api_key == "test-secret"
        self.version = "v15.30.0 test"
        self.queue_params = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def login(self):
        return 7

    def call(self, method, params, timeout_secs=None, cancel_operation_id=None):
        if method == "Fetch.ProfileOwnedKeys":
            assert params["filters"]["classification"] == "assets"
            return {
                "items": [{
                    "id": 11,
                    "game": {
                        "id": 22,
                        "title": "Pixel Village",
                        "url": "https://creator.itch.io/pixel-village",
                        "classification": "assets",
                    },
                }],
                "nextCursor": {"position": "opaque"},
            }
        if method == "Fetch.DownloadKey":
            return {"downloadKey": {"id": 11, "gameId": 22, "ownerId": self.owner_id}}
        if method == "Fetch.Game":
            return {"game": {
                "id": 22,
                "title": "Pixel Village",
                "url": "https://creator.itch.io/pixel-village",
                "classification": "assets",
                "user": {"username": "creator"},
            }}
        if method == "Fetch.GameUploads":
            return {"uploads": [{
                "id": 33,
                "displayName": "Asset zip",
                "filename": "pixel-village.zip",
                "size": 64,
                "storage": "hosted",
                "type": "graphical_assets",
                "preorder": False,
            }]}
        if method == "Install.GetUploads":
            return {"game": {"id": 22}, "uploads": [{
                "id": 33,
                "displayName": "Asset zip",
                "filename": "pixel-village.zip",
                "size": 64,
                "storage": "hosted",
                "type": "graphical_assets",
                "preorder": False,
            }]}
        if method == "Install.PlanUpload":
            return {"info": {"upload": {"id": 33}, "diskUsage": {"finalDiskUsage": 128}}}
        if method == "Install.Queue":
            self.queue_params = params
            return {
                "id": "install-1",
                "game": params["game"],
                "upload": params["upload"],
                "stagingFolder": params["stagingFolder"],
            }
        if method == "Install.Perform":
            content = pathlib.Path(self.queue_params["installFolder"])
            content.mkdir(parents=True)
            filename = "payload.exe" if self.unsafe else "sprite.png"
            (content / filename).write_bytes(b"MZbad" if self.unsafe else b"PNG asset")
            return {"caveId": "", "events": []}
        raise AssertionError("unexpected RPC method: " + method)


@pytest.fixture(autouse=True)
def fake_authenticated_butlerd(monkeypatch):
    monkeypatch.setenv("ITCHIO_API_KEY", "test-secret")
    monkeypatch.setattr(assets, "ButlerdClient", FakeButlerd)
    FakeButlerd.unsafe = False
    FakeButlerd.owner_id = 7


def test_builds_allowlisted_official_rss_url():
    assert assets.build_feed_url("free", "pixel-art", 2) == (
        "https://itch.io/game-assets/free/tag-pixel-art.xml?page=2"
    )
    with pytest.raises(assets.AssetPolicyError):
        assets.build_feed_url("free", "../../search", 1)


def test_parses_only_canonical_itchio_project_links():
    body = (ROOT / "tests" / "fixtures" / "itchio_game_assets.xml").read_bytes()
    result = assets.parse_rss(body)
    assert [item["project_url"] for item in result] == ["https://creator.itch.io/pixel-village"]
    assert result[0]["license_status"] == "review_required"


@pytest.mark.parametrize(
    "body",
    [b"<!DOCTYPE rss><rss/>", b"<!ENTITY x 'y'><rss/>", b"<html>challenge</html>"],
)
def test_rejects_unsafe_or_html_rss_documents(body):
    with pytest.raises(assets.AssetPolicyError):
        assets.parse_rss(body)


def test_rss_http_failure_has_no_html_fallback():
    result = assets.browse_assets(_request_fn=lambda *args, **kwargs: FakeResponse(403))
    assert result["success"] is False
    assert result["context"]["error_code"] == "upstream_access_blocked"
    assert "does not fall back" in result["context"]["possible_solutions"][0]


def test_rss_rejects_redirect_or_non_xml_response():
    body = (ROOT / "tests" / "fixtures" / "itchio_game_assets.xml").read_bytes()
    redirected = assets.browse_assets(
        _request_fn=lambda *args, **kwargs: FakeResponse(200, body, url="https://example.com/feed.xml")
    )
    wrong_type = assets.browse_assets(
        _request_fn=lambda *args, **kwargs: FakeResponse(200, body, content_type="text/html")
    )
    assert redirected["success"] is False
    assert wrong_type["success"] is False


def test_lists_only_owned_asset_classification():
    result = assets.list_owned_assets()
    assert result["success"] is True
    assert result["context"]["assets"][0]["download_key_id"] == 11
    assert result["context"]["assets"][0]["license_status"] == "review_required"


def test_inspection_fails_closed_on_ownership_mismatch():
    FakeButlerd.owner_id = 999
    result = assets.inspect_owned_asset(download_key_id=11, game_id=22)
    assert result["success"] is False
    assert "does not prove ownership" in result["error"]


def test_download_requires_license_before_writing(tmp_path):
    output = tmp_path / "asset"
    result = assets.download_owned_asset(
        11, 22, 33, str(output), "", "https://creator.itch.io/pixel-village"
    )
    assert result["success"] is False
    assert not output.exists()


def test_download_returns_validated_descriptor_and_provenance(tmp_path):
    output = tmp_path / "asset"
    result = assets.download_owned_asset(
        11,
        22,
        33,
        str(output),
        "CC-BY-4.0 per the creator's license page",
        "https://creator.itch.io/pixel-village",
        license_spdx="CC-BY-4.0",
    )
    assert result["success"] is True
    descriptor = result["context"]["asset_descriptor"]
    assert descriptor["asset_id"].startswith("itchio:22:33:")
    assert descriptor["attribution"]["source_url"] == "https://creator.itch.io/pixel-village"
    assert descriptor["variants"][0]["local_path"] == str(output)
    assert descriptor["attribution"]["license_spdx"] == "CC-BY-4.0"
    assert (output / "sprite.png").is_file()
    assert (output / "dcc-mcp-itchio-provenance.json").is_file()


def test_unsafe_download_is_quarantined(tmp_path):
    FakeButlerd.unsafe = True
    output = tmp_path / "asset"
    result = assets.download_owned_asset(
        11,
        22,
        33,
        str(output),
        "Creator supplied license",
        "https://creator.itch.io/pixel-village",
    )
    assert result["success"] is False
    assert not output.exists()
    quarantine = pathlib.Path(result["context"]["quarantine_path"])
    assert quarantine.is_dir()
    assert (quarantine / "content" / "payload.exe").is_file()


def test_production_scripts_parse_as_python_37_and_do_not_mutate_sys_path():
    for path in SCRIPTS.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path), feature_version=(3, 7))
        assert "sys.path.insert" not in source


def test_butler_version_parser_enforces_semver_shape():
    assert parse_version("v15.30.0, built today") == (15, 30, 0)
    with pytest.raises(ButlerdError):
        parse_version("development")


def test_cancelled_install_sends_scoped_butlerd_cancel(monkeypatch):
    client = object.__new__(butlerd.ButlerdClient)
    client._next_id = 1
    client.timeout_secs = 30
    client._messages = butlerd.queue.Queue()
    writes = []
    client._write = writes.append

    def cancel_now():
        raise butlerd.CancelledError("cancelled by test")

    monkeypatch.setattr(butlerd, "check_cancelled", cancel_now)
    with pytest.raises(butlerd.CancelledError):
        client.call(
            "Install.Perform",
            {"id": "install-1", "stagingFolder": "staging"},
            cancel_operation_id="install-1",
        )

    assert writes[0]["method"] == "Install.Perform"
    assert writes[1]["method"] == "Install.Cancel"
    assert writes[1]["params"] == {"id": "install-1"}
