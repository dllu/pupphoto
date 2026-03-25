#!/usr/bin/env python3

import argparse

from upload_photo import upload_photo


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload a photo, copy its public URL to the clipboard, and print it."
    )
    parser.add_argument("src_file")
    parser.add_argument("resize", nargs="?", type=int)
    args = parser.parse_args()

    print(upload_photo(args.src_file, resize=args.resize, clipboard=True))


if __name__ == "__main__":
    main()
