from _itchio_assets import inspect_owned_asset
from dcc_mcp_core.skills_helper import run_main, skill_entry


@skill_entry
def main(download_key_id, game_id, butler_path=None, **_):
    return inspect_owned_asset(download_key_id=download_key_id, game_id=game_id, butler_path=butler_path)


if __name__ == "__main__":
    run_main(main)
