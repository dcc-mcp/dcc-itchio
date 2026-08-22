---
name: itchio-assets
description: Browse official itch.io game-asset RSS feeds and acquire explicitly licensed assets already owned by the authenticated profile.
license: MIT
compatibility: "dcc-mcp-core 0.19+, Python 3.7+, butler 15.29+"
metadata:
  dcc-mcp:
    version: 0.1.0
    dcc: python
    layer: domain
    tags:
      - asset-provider
      - itch.io
      - game-assets
      - butlerd
      - license-provenance
    search-hint: "itch.io owned game assets, asset packs, sprites, textures, audio, butler download"
    produces: [asset_descriptor]
    tools: tools.yaml
---

# itch.io Assets

Use this skill to discover itch.io game-asset project pages through official RSS,
or to list, inspect, and download asset projects already owned by the configured
itch.io account.

Authenticated tools read `ITCHIO_API_KEY` or `BUTLER_API_KEY` from the environment.
They start a temporary `butlerd` 15.29+ process with an independent SQLite database,
pass the key through `Profile.LoginWithAPIKey`, and destroy that database afterward.
The key is never accepted as a tool argument or passed on a command line.

Downloads are fail-closed: ownership is rechecked using the exact download-key,
game, and upload IDs; explicit license text and an HTTPS provenance URL are required;
installers, scripts, executables, links, reparse points, case collisions, and
oversized trees are rejected. Unsafe results stay in a reported quarantine path.
A successful download returns a validated `AssetDescriptor`; importing it belongs
to the destination DCC skill.

This skill does not scrape HTML, automate a browser, buy assets, infer licenses, or
download unowned projects.

Version 0.1 supports direct owned download keys only. Bundle-only ownership is
reported as unsupported rather than being inferred or silently broadened.
