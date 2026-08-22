from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Optimize and validate a DCC showcase image.")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--quality", type=int, default=82)
    args = parser.parse_args()

    try:
        from PIL import Image
    except ImportError:
        print("Missing dependency: Pillow. Install it outside this Skill, then retry.", file=sys.stderr)
        return 2

    if not args.input.is_file():
        parser.error("input does not exist: {}".format(args.input))
    if not 1 <= args.quality <= 100:
        parser.error("--quality must be between 1 and 100")

    with Image.open(args.input) as image:
        if image.width < 1280 or image.height < 720:
            parser.error("showcase must be at least 1280x720")
        if abs(image.width / image.height - 16 / 9) > 0.04:
            parser.error("showcase must use a 16:9 canvas")
        output = image.convert("RGB")
        if output.width > 1920:
            output.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        output.save(str(args.output), "WEBP", quality=args.quality, method=6)

    size_kb = args.output.stat().st_size / 1024
    print("{} ({:.0f} KB)".format(args.output, size_kb))
    return 0 if size_kb <= 500 else 3


if __name__ == "__main__":
    raise SystemExit(main())
