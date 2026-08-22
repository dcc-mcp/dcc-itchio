"""List itch.io projects owned by the authenticated profile."""

from __future__ import annotations

from typing import Any, Dict

from _publish import PublishContractError, list_projects
from dcc_mcp_core.skills_helper import skill_entry, skill_exception, skill_success


@skill_entry
def main(max_results: int = 100, **_: Any) -> Dict[str, Any]:
    try:
        return skill_success("itch.io projects listed", **list_projects(max_results))
    except PublishContractError as exc:
        return skill_exception(exc, message="Could not list itch.io projects", include_traceback=False)


if __name__ == "__main__":
    from dcc_mcp_core.skills_helper import run_main

    run_main(main)
