from _itchio_assets import list_owned_assets
from dcc_mcp_core.skills_helper import run_main, skill_entry


@skill_entry
def main(query=None, limit=20, cursor=None, butler_path=None, **_):
    return list_owned_assets(query=query, limit=limit, cursor=cursor, butler_path=butler_path)


if __name__ == "__main__":
    run_main(main)
