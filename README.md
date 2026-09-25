# DCC itch.io Skills

Two independently installable DCC-MCP Skills for safe itch.io asset acquisition
and preview-gated game publishing.

![Asset discovery, verification, and delivery workflow](docs/images/dcc-itchio-showcase.webp)

Illustration generated for this repository; it contains no third-party logos,
copied marketplace thumbnails, or product UI.

## Skills

### `itchio-assets`

- Browse only itch.io's official game-assets RSS feed.
- List and inspect asset projects with direct download-key ownership on the
  authenticated account through the official Butler daemon API.
- Download one exact owned upload without running installers or downloaded code.
- Require explicit license evidence before any download, then return a validated
  DCC-MCP `AssetDescriptor` with hashes and attribution.

This Skill does not buy assets, scrape project pages, infer a license from price
or ownership, or import downloaded content into a DCC application.
Version 0.1 fails closed for bundle-only ownership.

### `itchio-publish`

- List projects owned by the authenticated creator account.
- Inspect Butler channels and their head/pending build states.
- Create a hash-bound preview receipt for a prepared game build.
- Revalidate the source tree, license provenance, Butler binary, channel parent,
  and preview result before an authenticated `butler push`.

Publishing consumes `license-provenance.json` produced by the marketplace
`game-release-package` Skill. Core's asynchronous job cancellation terminates
the local Butler process only; it is not a remote build rollback.

## Runtime requirements

- DCC-MCP Core 0.19.91 or newer
- Python 3.7 or newer for production scripts
- itch.io Butler 15.30.0 or newer
- `BUTLER_API_KEY` in the local environment for authenticated operations

The API key is never accepted as a tool argument, written to a receipt, placed
on a command line, or returned in tool output. The asset Skill passes it to an
ephemeral, isolated Butler daemon over JSON-RPC and removes that daemon's local
state after the request.

## Development

```powershell
python -m pip install "dcc-mcp-core>=0.19.91" "pytest>=7.4,<9" "PyYAML>=6" "ruff>=0.9" Pillow
python scripts/validate_skills.py
python -m pytest -q
ruff check .
```

All network and Butler unit tests use fixtures or fake processes. CI never
performs a real upload or download.

## License

MIT

## PyPI status: not published

This repository is an **agent skill pack**, not a distributable Python package.
It contains no importable module under `src/` — the deliverable is the set of
markdown skill definitions under `skill/`, which agents load from the repository
or the skill marketplace rather than via `pip install`.

It is therefore intentionally **not published to PyPI**, and no release
workflow exists for that purpose. Tracked in [PIP-3630][pip3630].

[pip3630]: https://monica.woa.com/issues/01a0d880-e0c8-7ee2-8f7c-ccc783e279dc
