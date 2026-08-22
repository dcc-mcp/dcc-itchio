# Authenticated itch.io Publishing Workflow

## Boundary

Publishing is a remote mutation. Separate discovery, preview, approval, push,
and terminal-state inspection. Never infer approval from an earlier packaging
request.

## Prerequisites

- Install official butler 15.30 or newer.
- Set `ITCHIO_API_KEY` for the profile API and either `BUTLER_API_KEY` or a
  credential created by `butler login` outside this skill.
- Run `game-release-package` and retain its `license_provenance_path`.
- Keep the exported source directory unchanged between preview and push.

Keys are environment state, never tool inputs. The implementation invokes
butler with an argument array and `shell=False`.

## Preview gate

`preview_itchio_push` binds all of the following into a SHA-256 receipt:

- deterministic source-tree manifest;
- validated game-release license provenance;
- exact project, channel, version, and hidden flag;
- butler version and executable SHA-256;
- the current channel parent build;
- structured `butler push-preview --changes-only` output;
- fixed no-auto-wrap, no-auto-unzip, fix-permissions, and if-changed options.

Review the receipt's diff and destination before requesting approval.
`hidden=true` is accepted only for a channel that does not exist yet. Use
`hidden=false` for an existing channel; changing an existing channel's
visibility belongs in itch.io's project settings, outside this skill.

## Push gate

`push_itchio_build` rejects an edited receipt. It then recomputes every local
digest, rechecks the exact butler binary, checks the remote parent, reruns the
preview, and checks the parent again immediately before invoking `butler push`.
There is no atomic compare-and-swap primitive in the butler CLI, so another
publisher can still race after the final check. Serialize publishers for a
channel when that risk matters.

The command uses `--if-changed`; an unchanged build may return `skipped=true`
instead of creating a build. A successful command can return while itch.io is
still processing the remote build. Poll `inspect_itchio_channels` until it is
terminal.

Stopping the local process never requests a remote rollback. If cancellation
or connectivity loss occurs during push, treat the remote outcome as unknown
until channel status is inspected.
