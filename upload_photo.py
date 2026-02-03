import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageOps

from gps import remove_gps_if_banned

Image.MAX_IMAGE_PIXELS = None  # suppress stupid decompression bomb warning


def get_xdg_user_dir(name: str, fallback: Path) -> Path:
    try:
        output = subprocess.check_output(["xdg-user-dir", name], text=True).strip()
        if output:
            return Path(output)
    except Exception:
        pass
    return fallback


def copy_to_clipboard(text: str) -> bool:
    if os.environ.get("WAYLAND_DISPLAY"):
        cmd = ["wl-copy"]
    elif os.environ.get("DISPLAY"):
        cmd = ["xclip", "-selection", "c"]
    else:
        print("Error: Unable to detect display server. Clipboard not updated.", file=sys.stderr)
        return False

    try:
        subprocess.run(cmd, input=text, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        print("Error: Clipboard command failed.", file=sys.stderr)
        return False
    return True


def upload_photo(src_file, resize=None, clipboard=False, clipboard_format=None):
    src_path = Path(src_file)
    pictures_dir = get_xdg_user_dir("PICTURES", Path.home() / "Pictures")
    thumb_path = pictures_dir / "thumbs"
    thumb_path.mkdir(exist_ok=True, parents=True)

    filename = src_path.name
    filename_no_ext = src_path.stem
    ext = src_path.suffix

    if resize:
        img = Image.open(src_file)
        img = ImageOps.exif_transpose(img)
        img.thumbnail((resize, resize), Image.Resampling.LANCZOS)

        resized_filename = f"{filename_no_ext}_{resize}{ext}"
        upload_src = thumb_path / resized_filename  # Temporary file path

        img.save(upload_src, quality=95)

    else:
        upload_src = thumb_path / filename  # Temporary file path
        shutil.copyfile(src_path, upload_src)

    gps_banned = remove_gps_if_banned(upload_src)

    # Calculate SHA1 checksum
    with open(upload_src, "rb") as f:
        sha1 = hashlib.sha1(f.read()).hexdigest()
    dst_filename = f"{filename_no_ext}_{sha1[:16]}{ext}"
    dst = f"b2:dllu-pics/{dst_filename}"

    # Upload file, skipping if it already exists remotely
    dst_url = f"https://i.dllu.net/{dst_filename}"
    if clipboard or clipboard_format is not None:
        clipboard_text = dst_url if clipboard_format is None else clipboard_format.format(url=dst_url)
        if not copy_to_clipboard(clipboard_text):
            raise SystemExit(1)
    subprocess.run(["rclone", "copyto", "--ignore-existing", upload_src, dst])
    return dst_url


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload a photo and output its public URL.")
    parser.add_argument("src_file")
    parser.add_argument("resize", nargs="?", type=int)
    parser.add_argument(
        "--clipboard",
        action="store_true",
        help="Copy the URL to the clipboard before uploading.",
    )
    parser.add_argument(
        "--clipboard-format",
        help="Format string for clipboard text (use {url}). Implies --clipboard.",
    )
    args = parser.parse_args()

    dst = upload_photo(
        args.src_file,
        resize=args.resize,
        clipboard=args.clipboard or args.clipboard_format is not None,
        clipboard_format=args.clipboard_format,
    )
    print(dst)
