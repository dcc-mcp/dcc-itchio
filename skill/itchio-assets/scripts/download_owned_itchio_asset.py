from _itchio_assets import download_owned_asset
from dcc_mcp_core.skills_helper import run_main, skill_entry


@skill_entry
def main(
    download_key_id,
    game_id,
    upload_id,
    output_dir,
    license_text,
    license_source_url,
    license_spdx=None,
    author=None,
    title=None,
    butler_path=None,
    max_download_bytes=2147483648,
    max_total_bytes=4294967296,
    max_files=10000,
    **_
):
    return download_owned_asset(
        download_key_id=download_key_id,
        game_id=game_id,
        upload_id=upload_id,
        output_dir=output_dir,
        license_text=license_text,
        license_source_url=license_source_url,
        license_spdx=license_spdx,
        author=author,
        title=title,
        butler_path=butler_path,
        max_download_bytes=max_download_bytes,
        max_total_bytes=max_total_bytes,
        max_files=max_files,
    )


if __name__ == "__main__":
    run_main(main)
