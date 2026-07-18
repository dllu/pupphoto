#!/usr/bin/env python3

from clipboard_util import clear_clipboard


def main() -> None:
    # Clear stale content before argument parsing or importing the image stack.
    if not clear_clipboard():
        raise SystemExit(1)

    import argparse

    parser = argparse.ArgumentParser(
        description="Upload a photo, copy blog markup to the clipboard, and mirror the processed file locally."
    )
    parser.add_argument("src_file")
    args = parser.parse_args()

    from upload_photo import upload_photo, upload_temp_filename

    full_size_link = upload_photo(
        args.src_file,
        clipboard=True,
        clipboard_format="![]({url})",
    )

    # Everything below happens after upload_photo has populated the clipboard.
    import shutil

    from config import load_config

    config = load_config().upload
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
