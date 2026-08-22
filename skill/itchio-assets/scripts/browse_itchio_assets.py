
from _itchio_assets import browse_assets
from dcc_mcp_core.skills_helper import run_main, skill_entry


@skill_entry
def main(price="any", tag=None, page=1, limit=20, **_):
    return browse_assets(price=price, tag=tag, page=page, limit=limit)


if __name__ == "__main__":
    run_main(main)
