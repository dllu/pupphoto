#!/usr/bin/env python3

import argparse
import shutil

from config import load_config
from upload_photo import upload_photo, upload_temp_filename


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload a photo, copy blog markup to the clipboard, and mirror the processed file locally."
    )
    parser.add_argument("src_file")
    args = parser.parse_args()

    config = load_config().upload
    full_size_link = upload_photo(
        args.src_file,
        clipboard=True,
        clipboard_format="![]({url})",
    )
    dst_filename = full_size_link.rsplit("/", 1)[-1]
    processed_path = config.thumb_dir / upload_temp_filename(args.src_file)
    if not processed_path.is_file():
        raise SystemExit(f"Processed photo not found at {processed_path}")

    config.blog_image_dir.mkdir(parents=True, exist_ok=True)
    dest_file = config.blog_image_dir / dst_filename
    shutil.copy2(processed_path, dest_file)

    output = f"![]({full_size_link})"
    print(output)
    print(f"Copied {processed_path} to {dest_file}")


if __name__ == "__main__":
    main()
