"""Revalidate a preview receipt and push the exact build through butler."""

from __future__ import annotations

from typing import Any, Dict

from _publish import PublishContractError, push_build
from dcc_mcp_core.skills_helper import skill_entry, skill_exception, skill_success


@skill_entry
def main(
    source_directory: str,
    license_provenance_path: str,
    project: str,
    channel: str,
    version: str,
    preview_receipt: Dict[str, Any],
    hidden: bool = True,
    butler_path: str = "",
    **_: Any
) -> Dict[str, Any]:
    try:
        result = push_build(
            source_directory,
            license_provenance_path,
            project,
            channel,
            version,
            preview_receipt,
            hidden,
            butler_path,
        )
        return skill_success(
            "itch.io build push completed",
            prompt="Inspect the channel until the new build reaches its terminal state.",
            **result
        )
    except PublishContractError as exc:
        return skill_exception(exc, message="itch.io build was not pushed", include_traceback=False)


if __name__ == "__main__":
    from dcc_mcp_core.skills_helper import run_main

    run_main(main)
