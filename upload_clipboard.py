#!/usr/bin/env python3

from clipboard_util import clear_clipboard


def main() -> None:
    # Do this before argparse and the image stack are imported: even if argument
    # parsing or image processing takes time, a paste cannot use a stale URL.
    if not clear_clipboard():
        raise SystemExit(1)

    import argparse

    from upload_photo import upload_photo

    parser = argparse.ArgumentParser(
        description="Upload a photo, copy its public URL to the clipboard, and print it."
    )
    parser.add_argument("src_file")
    parser.add_argument("resize", nargs="?", type=int)
    args = parser.parse_args()

    print(upload_photo(args.src_file, resize=args.resize, clipboard=True))


if __name__ == "__main__":
    main()
