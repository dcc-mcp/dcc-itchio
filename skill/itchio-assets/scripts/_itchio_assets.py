import hashlib
import html
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager, suppress
from urllib.parse import urlencode, urlsplit, urlunsplit

from _butlerd import ButlerdClient, ButlerdError
from dcc_mcp_core.asset_import import AssetAttribution, AssetDescriptor, AssetFileVariant
from dcc_mcp_core.skills_helper import (
    CancelledError,
    http_request,
    skill_error,
    skill_success,
)

ALLOWED_TAGS = {
    "2d", "3d", "audio", "fonts", "godot", "gui", "low-poly", "maya",
    "music", "pixel-art", "sound-effects", "sprites", "textures", "unity",
    "unreal-engine",
}
BLOCKED_EXTENSIONS = {
    ".app", ".bat", ".class", ".cmd", ".com", ".dll", ".dmg", ".exe",
    ".jar", ".js", ".jse", ".lnk", ".msi", ".msp", ".pkg", ".ps1",
    ".psm1", ".py", ".pyc", ".scr", ".sh", ".so", ".url", ".vbs",
    ".wsf", ".wsh",
}


class AssetPolicyError(RuntimeError):
    def __init__(self, message, quarantine_path=None):
        RuntimeError.__init__(self, message)
        self.quarantine_path = quarantine_path


def _api_key():
    value = os.environ.get("ITCHIO_API_KEY") or os.environ.get("BUTLER_API_KEY")
    if not value or not value.strip():
        raise AssetPolicyError("ITCHIO_API_KEY or BUTLER_API_KEY is required for owned assets")
    return value.strip()


@contextmanager
def _authenticated_client(butler_path=None, timeout_secs=30):
    with ButlerdClient(_api_key(), butler_path=butler_path, timeout_secs=timeout_secs) as client:
        yield client, client.login()


def build_feed_url(price="any", tag=None, page=1):
    if price not in ("any", "free", "on-sale"):
        raise AssetPolicyError("price must be any, free, or on-sale")
    if tag is not None and tag not in ALLOWED_TAGS:
        raise AssetPolicyError("tag is not in the supported itch.io asset tag allowlist")
    if not isinstance(page, int) or page < 1 or page > 100:
        raise AssetPolicyError("page must be between 1 and 100")
    path = "https://itch.io/game-assets"
    if price != "any":
        path += "/" + price
    if tag:
        path += "/tag-" + tag
    path += ".xml"
    return path if page == 1 else path + "?" + urlencode({"page": page})


def _project_url(value):
    parsed = urlsplit(str(value or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not re.match(r"^[a-z0-9][a-z0-9-]*\.itch\.io$", host):
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 1 or not re.match(r"^[a-z0-9][a-z0-9-]*$", parts[0].lower()):
        return None
    return urlunsplit(("https", host, "/" + parts[0], "", ""))


def parse_rss(body, limit=20):
    if len(body) > 2 * 1024 * 1024:
        raise AssetPolicyError("itch.io RSS response exceeded the 2 MiB safety limit")
    lowered = body.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered or b"<html" in lowered:
        raise AssetPolicyError("itch.io did not return a safe RSS document")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise AssetPolicyError("itch.io returned invalid RSS XML") from exc
    assets = []
    seen = set()
    for item in root.findall(".//item"):
        link = _project_url(item.findtext("link") or item.findtext("guid"))
        if not link or link in seen:
            continue
        seen.add(link)
        description = item.findtext("description") or ""
        description = html.unescape(re.sub(r"<[^>]+>", " ", description))
        description = re.sub(r"\s+", " ", description).strip()[:500]
        assets.append({
            "title": (item.findtext("title") or "Untitled itch.io asset")[:300],
            "project_url": link,
            "summary": description,
            "published_at": item.findtext("pubDate"),
            "license_status": "review_required",
            "download_status": "ownership_required",
            "source": "itchio_official_rss",
        })
        if len(assets) >= limit:
            break
    return assets


def browse_assets(price="any", tag=None, page=1, limit=20, _request_fn=None):
    try:
        if not isinstance(limit, int) or limit < 1 or limit > 50:
            raise AssetPolicyError("limit must be between 1 and 50")
        url = build_feed_url(price=price, tag=tag, page=page)
        request_fn = _request_fn or http_request
        response = request_fn("GET", url, timeout_ms=15000, max_bytes=2 * 1024 * 1024)
        if response.status != 200:
            error_code = (
                "upstream_access_blocked"
                if response.status == 403
                else "upstream_http_error"
            )
            return skill_error(
                "itch.io RSS browse is unavailable",
                "official RSS returned HTTP " + str(response.status),
                possible_solutions=[
                    "Retry later; this skill does not fall back to HTML scraping "
                    "or browser automation."
                ],
                error_code=error_code,
                feed_url=url,
            )
        final_host = (urlsplit(str(getattr(response, "url", ""))).hostname or "").lower()
        response_headers = {
            str(key).lower(): str(value)
            for key, value in (getattr(response, "headers", {}) or {}).items()
        }
        content_type = response_headers.get("content-type", "").lower()
        if final_host != "itch.io" or not ("xml" in content_type or "rss" in content_type):
            raise AssetPolicyError("official RSS redirected or returned a non-RSS content type")
        assets = parse_rss(response.bytes, limit=limit)
        return skill_success("itch.io assets found", assets=assets, count=len(assets), feed_url=url)
    except AssetPolicyError as exc:
        return skill_error("Could not browse itch.io assets", str(exc))


def _safe_game(game):
    return {
        "game_id": game.get("id"),
        "title": game.get("title"),
        "project_url": _project_url(game.get("url")) or game.get("url"),
        "short_text": game.get("shortText"),
        "cover_url": game.get("coverUrl"),
        "classification": game.get("classification"),
        "min_price_cents": game.get("minPrice"),
    }


def _safe_upload(upload):
    return {
        "upload_id": upload.get("id"),
        "display_name": upload.get("displayName"),
        "filename": upload.get("filename"),
        "size_bytes": upload.get("size"),
        "storage": upload.get("storage"),
        "type": upload.get("type"),
        "channel_name": upload.get("channelName"),
        "preorder": bool(upload.get("preorder")),
        "downloadable_via_butlerd": (
            upload.get("storage") in ("hosted", "build")
            and not upload.get("preorder")
        ),
    }


def list_owned_assets(query=None, limit=20, cursor=None, butler_path=None):
    try:
        if not isinstance(limit, int) or limit < 1 or limit > 50:
            raise AssetPolicyError("limit must be between 1 and 50")
        with _authenticated_client(butler_path=butler_path) as pair:
            client, profile_id = pair
            params = {
                "profileId": profile_id,
                "limit": limit,
                "filters": {"installed": False, "classification": "assets"},
                "fresh": True,
            }
            if query:
                params["search"] = str(query)[:200]
            if cursor is not None:
                params["cursor"] = cursor
            result = client.call("Fetch.ProfileOwnedKeys", params)
            items = []
            for key in result.get("items") or []:
                game = key.get("game") or {}
                if game.get("classification") != "assets":
                    continue
                entry = _safe_game(game)
                entry["download_key_id"] = key.get("id")
                entry["license_status"] = "review_required"
                items.append(entry)
            return skill_success(
                "Owned itch.io assets listed",
                assets=items,
                count=len(items),
                next_cursor=result.get("nextCursor"),
                license_notice=(
                    "Ownership grants access, not a reusable license. Review each "
                    "asset license before download."
                ),
            )
    except (AssetPolicyError, ButlerdError) as exc:
        return skill_error("Could not list owned itch.io assets", str(exc))


def _inspect_owned(client, profile_id, download_key_id, game_id):
    key_result = client.call("Fetch.DownloadKey", {
        "downloadKeyId": download_key_id,
        "profileId": profile_id,
        "fresh": True,
    })
    key = key_result.get("downloadKey") or {}
    if key.get("id") != download_key_id or key.get("gameId") != game_id or key.get("ownerId") != profile_id:
        raise AssetPolicyError("the exact download key does not prove ownership of this asset")
    game_result = client.call("Fetch.Game", {"gameId": game_id, "fresh": True})
    game = game_result.get("game") or {}
    if game.get("id") != game_id or game.get("classification") != "assets":
        raise AssetPolicyError("the owned itch.io project is not classified as an asset")
    uploads_result = client.call("Fetch.GameUploads", {
        "gameId": game_id,
        "compatible": False,
        "fresh": True,
    })
    uploads = uploads_result.get("uploads") or []
    return game, uploads


def inspect_owned_asset(download_key_id, game_id, butler_path=None):
    try:
        if not isinstance(download_key_id, int) or download_key_id <= 0:
            raise AssetPolicyError("download_key_id must be a positive integer")
        if not isinstance(game_id, int) or game_id <= 0:
            raise AssetPolicyError("game_id must be a positive integer")
        with _authenticated_client(butler_path=butler_path) as pair:
            client, profile_id = pair
            game, uploads = _inspect_owned(client, profile_id, download_key_id, game_id)
            return skill_success(
                "Owned itch.io asset inspected",
                asset=_safe_game(game),
                uploads=[_safe_upload(item) for item in uploads],
                license_status="review_required",
                license_notice=(
                    "itch.io ownership does not identify the creator's reuse license; "
                    "supply explicit license evidence to download."
                ),
            )
    except (AssetPolicyError, ButlerdError) as exc:
        return skill_error("Could not inspect owned itch.io asset", str(exc))


def _validate_license(license_text, license_source_url, license_spdx=None):
    if not license_text or not str(license_text).strip():
        raise AssetPolicyError("license_text is required; license inference is forbidden")
    parsed = urlsplit(str(license_source_url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise AssetPolicyError("license_source_url must be an explicit HTTPS provenance URL")
    if license_spdx and not re.match(r"^[A-Za-z0-9.+() -]{1,128}$", str(license_spdx)):
        raise AssetPolicyError("license_spdx contains unsupported characters")


def _validate_output(output_dir):
    output = os.path.abspath(output_dir)
    parent = os.path.dirname(output)
    if output == os.path.abspath(os.path.sep) or not os.path.isdir(parent):
        raise AssetPolicyError("output_dir must be a new absolute path under an existing directory")
    if os.path.exists(output):
        raise AssetPolicyError("output_dir already exists; owned asset downloads never overwrite")
    return output, parent


def _is_reparse(path):
    attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _scan_tree(root, max_files, max_total_bytes):
    entries = []
    total = 0
    folded = set()
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs):
            path = os.path.join(current, name)
            if os.path.islink(path) or _is_reparse(path):
                raise AssetPolicyError("download contains a symbolic link or reparse point")
        for name in files:
            path = os.path.join(current, name)
            if os.path.islink(path) or _is_reparse(path):
                raise AssetPolicyError("download contains a symbolic link or reparse point")
            info = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise AssetPolicyError("download contains a non-regular file")
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            folded_name = relative.casefold()
            if folded_name in folded:
                raise AssetPolicyError("download contains case-colliding paths")
            folded.add(folded_name)
            if os.path.splitext(name)[1].lower() in BLOCKED_EXTENSIONS:
                raise AssetPolicyError("download contains executable or script content: " + relative)
            with open(path, "rb") as stream:
                prefix = stream.read(4)
                digest = hashlib.sha256()
                digest.update(prefix)
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
            if prefix.startswith((b"MZ", b"\x7fELF", b"#!")):
                raise AssetPolicyError("download contains executable or script content: " + relative)
            total += info.st_size
            entries.append({"path": relative, "size": info.st_size, "sha256": digest.hexdigest()})
            if len(entries) > max_files or total > max_total_bytes:
                raise AssetPolicyError("download exceeds the configured extraction limits")
    if not entries:
        raise AssetPolicyError("download produced no asset files")
    entries.sort(key=lambda item: item["path"])
    manifest = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return entries, total, hashlib.sha256(manifest).hexdigest()


def _quarantine(work_dir, parent, leaf):
    target = os.path.join(parent, "." + leaf + ".itchio-quarantine-" + uuid.uuid4().hex)
    try:
        os.replace(work_dir, target)
        return target
    except Exception:
        return work_dir


def download_owned_asset(
    download_key_id,
    game_id,
    upload_id,
    output_dir,
    license_text,
    license_source_url,
    license_spdx=None,
    author=None,
    title=None,
    butler_path=None,
    max_download_bytes=2147483648,
    max_total_bytes=4294967296,
    max_files=10000,
):
    work_dir = None
    parent = None
    output = None
    started = False
    try:
        _validate_license(license_text, license_source_url, license_spdx=license_spdx)
        if not all(isinstance(value, int) and value > 0 for value in (download_key_id, game_id, upload_id)):
            raise AssetPolicyError("download_key_id, game_id, and upload_id must be positive integers")
        output, parent = _validate_output(output_dir)
        leaf = os.path.basename(output)
        work_dir = tempfile.mkdtemp(prefix="." + leaf + ".itchio-staging-", dir=parent)
        content_dir = os.path.join(work_dir, "content")
        staging_dir = os.path.join(work_dir, "butler-staging")
        os.makedirs(staging_dir)
        with _authenticated_client(butler_path=butler_path, timeout_secs=1800) as pair:
            client, profile_id = pair
            game, uploads = _inspect_owned(client, profile_id, download_key_id, game_id)
            upload = next((item for item in uploads if item.get("id") == upload_id), None)
            if upload is None:
                raise AssetPolicyError("upload_id is not part of the owned asset")
            if upload.get("storage") not in ("hosted", "build") or upload.get("preorder"):
                raise AssetPolicyError("the selected upload is not downloadable through butlerd")
            if int(upload.get("size") or 0) > int(max_download_bytes):
                raise AssetPolicyError("the compressed upload exceeds max_download_bytes")
            install_uploads = client.call("Install.GetUploads", {
                "gameId": game_id,
                "profileId": profile_id,
            }, timeout_secs=60)
            planned_uploads = (install_uploads.get("uploads") or []) + (
                install_uploads.get("incompatibleUploads") or []
            )
            planned_upload = next((item for item in planned_uploads if item.get("id") == upload_id), None)
            if planned_upload is None:
                raise AssetPolicyError("butlerd install planning did not confirm the exact upload")
            plan_id = "itchio-plan-" + uuid.uuid4().hex
            plan = client.call("Install.PlanUpload", {
                "id": plan_id,
                "uploadId": upload_id,
            }, timeout_secs=300, cancel_operation_id=plan_id)
            plan_info = plan.get("info") or {}
            if plan_info.get("error") or plan_info.get("errorCode"):
                raise AssetPolicyError("butlerd could not safely plan the selected upload")
            disk_usage = plan_info.get("diskUsage") or {}
            if int(disk_usage.get("finalDiskUsage") or 0) > int(max_total_bytes):
                raise AssetPolicyError("planned extracted size exceeds max_total_bytes")
            upload = planned_upload
            started = True
            queued = client.call("Install.Queue", {
                "reason": "install",
                "noCave": True,
                "installFolder": content_dir,
                "game": game,
                "upload": upload,
                "ignoreInstallers": True,
                "stagingFolder": staging_dir,
                "queueDownload": False,
                "profileId": profile_id,
            }, timeout_secs=120)
            queued_game_id = (queued.get("game") or {}).get("id")
            queued_upload_id = (queued.get("upload") or {}).get("id")
            if queued_game_id != game_id or queued_upload_id != upload_id:
                raise AssetPolicyError("butlerd queued a different game or upload")
            client.call("Install.Perform", {
                "id": queued.get("id"),
                "stagingFolder": queued.get("stagingFolder") or staging_dir,
            }, timeout_secs=1800, cancel_operation_id=queued.get("id"))
            butler_version = client.version
        entries, total_bytes, manifest_sha256 = _scan_tree(content_dir, int(max_files), int(max_total_bytes))
        provenance_path = os.path.join(content_dir, "dcc-mcp-itchio-provenance.json")
        if os.path.exists(provenance_path):
            raise AssetPolicyError("download collides with the reserved provenance filename")
        provenance = {
            "schema": "dcc-mcp.itchio-asset-provenance.v1",
            "game_id": game_id,
            "upload_id": upload_id,
            "project_url": _project_url(game.get("url")) or game.get("url"),
            "license_text": str(license_text).strip(),
            "license_source_url": str(license_source_url).strip(),
            "license_spdx": str(license_spdx).strip() if license_spdx else None,
        }
        with open(provenance_path, "x", encoding="utf-8", newline="\n") as stream:
            json.dump(provenance, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        entries, total_bytes, manifest_sha256 = _scan_tree(content_dir, int(max_files), int(max_total_bytes))
        descriptor = AssetDescriptor(
            asset_id="itchio:{0}:{1}:{2}".format(game_id, upload_id, manifest_sha256[:16]),
            variants=[AssetFileVariant(local_path=output, format="directory", preferred=True)],
            attribution=AssetAttribution(
                source_url=_project_url(game.get("url")) or game.get("url"),
                license_spdx=str(license_spdx).strip() if license_spdx else None,
                license_text=str(license_text).strip(),
                author=author or ((game.get("user") or {}).get("username")),
                title=title or game.get("title"),
            ),
            tags=["itchio", "game-asset"],
            extra={
                "provider": "itchio",
                "game_id": game_id,
                "upload_id": upload_id,
                "project_url": _project_url(game.get("url")) or game.get("url"),
                "license_source_url": str(license_source_url).strip(),
                "manifest_sha256": manifest_sha256,
                "file_count": len(entries),
                "total_bytes": total_bytes,
                "butler_version": butler_version,
            },
        )
        descriptor.validate()
        if os.path.exists(output):
            raise AssetPolicyError("output_dir appeared during download; refusing to overwrite")
        os.rename(content_dir, output)
        with suppress(Exception):
            shutil.rmtree(work_dir)
        work_dir = None
        return skill_success(
            "Owned itch.io asset downloaded and quarantined checks passed",
            asset_descriptor=descriptor.to_dict(),
            provenance_path=os.path.join(output, "dcc-mcp-itchio-provenance.json"),
            manifest_sha256=manifest_sha256,
        )
    except CancelledError:
        if work_dir and os.path.isdir(work_dir):
            if started:
                _quarantine(work_dir, parent, os.path.basename(output))
            else:
                shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except (AssetPolicyError, ButlerdError, OSError, ValueError) as exc:
        quarantine = getattr(exc, "quarantine_path", None)
        if work_dir and os.path.isdir(work_dir):
            if started:
                quarantine = _quarantine(work_dir, parent, os.path.basename(output))
            else:
                shutil.rmtree(work_dir, ignore_errors=True)
        return skill_error(
            "Could not safely download owned itch.io asset",
            str(exc),
            possible_solutions=["Verify ownership and license evidence, then choose a new output path."],
            quarantine_path=quarantine,
        )
