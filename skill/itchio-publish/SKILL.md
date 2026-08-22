---
name: itchio-publish
description: Safely preview and publish prebuilt game releases to exact itch.io channels with butler, license provenance, immutable receipts, and parent-build drift gates.
license: MIT
compatibility: "dcc-mcp-core 0.19+, Python 3.7+, butler 15.30+"
metadata:
  dcc-mcp:
    dcc: python
    layer: domain
    stage: pipeline
    version: "0.1.0"
    tags: [itchio, game, release, publish, butler, godot, unreal, unity]
    search-hint: "publish or upload a packaged Godot Unity Unreal game build to an itch.io project channel with a safe preview"
    tools: tools.yaml
    depends: [game-release-package]
---

# itch.io Publish

Use this skill after `game-release-package` has created
`license-provenance.json` for the complete exported game directory. Credentials
must come from `ITCHIO_API_KEY`, `BUTLER_API_KEY`, or butler's own credential
store; never place a key in a tool argument.

Read `references/WORKFLOW.md` before the first authenticated push.

The required flow is:

1. Call `list_itchio_projects` and `inspect_itchio_channels` to establish the
   exact `owner/game-slug:channel` destination.
2. Call `preview_itchio_push`. It does not upload. It hashes the source tree,
   validates license provenance, pins the butler binary, records the channel
   parent, and binds the structured butler diff into a receipt.
3. Show the target, version, hidden state, parent build, and diff to the user.
4. Only after explicit approval, pass the unchanged receipt and unchanged
   arguments to `push_itchio_build`.

`push_itchio_build` revalidates every local and remote binding, reruns the
preview, and refuses to start when the parent build drifted. It always uses
`--if-changed`, disables butler auto-wrap and auto-unzip, and defaults a new
channel to hidden. `hidden=true` is rejected for an existing channel because
butler's hidden flag is a channel-creation setting; pass `hidden=false` when
updating an existing channel.

Cancellation terminates only the local butler process. Once a push has created
a remote build, cancellation cannot roll it back; inspect the channel to learn
the final remote state.

This skill does not edit store pages, buy content, log in through a browser, or
import files into a DCC.
