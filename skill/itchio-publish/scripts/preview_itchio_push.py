"""Create a non-mutating, evidence-bound itch.io push preview receipt."""

from __future__ import annotations

from typing import Any, Dict

from _publish import PublishContractError, preview_push
from dcc_mcp_core.skills_helper import skill_entry, skill_exception, skill_success


@skill_entry
def main(
    source_directory: str,
    license_provenance_path: str,
    project: str,
    channel: str,
    version: str,
    hidden: bool = True,
    butler_path: str = "",
    **_: Any
) -> Dict[str, Any]:
    try:
        result = preview_push(
            source_directory,
            license_provenance_path,
            project,
            channel,
            version,
            hidden,
            butler_path,
        )
        return skill_success(
            "itch.io push preview receipt created",
            prompt="Review the bound destination and diff before approving push_itchio_build.",
            **result
        )
    except PublishContractError as exc:
        return skill_exception(exc, message="Could not preview itch.io push", include_traceback=False)


if __name__ == "__main__":
    from dcc_mcp_core.skills_helper import run_main

    run_main(main)
