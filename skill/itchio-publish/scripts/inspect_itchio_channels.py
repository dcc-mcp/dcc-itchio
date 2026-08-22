"""Inspect structured channel state through butler."""

from __future__ import annotations

from typing import Any, Dict

from _publish import PublishContractError, inspect_channels
from dcc_mcp_core.skills_helper import skill_entry, skill_exception, skill_success


@skill_entry
def main(
    project: str,
    channel: str = "",
    butler_path: str = "",
    **_: Any
) -> Dict[str, Any]:
    try:
        result = inspect_channels(project, channel, butler_path)
        return skill_success("itch.io channels inspected", **result)
    except PublishContractError as exc:
        return skill_exception(exc, message="Could not inspect itch.io channels", include_traceback=False)


if __name__ == "__main__":
    from dcc_mcp_core.skills_helper import run_main

    run_main(main)
