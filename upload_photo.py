import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from struct import error as StructError
from struct import pack, unpack

from PIL import Image, JpegImagePlugin
from pillow_heif import register_heif_opener

from config import load_config
from gps import remove_gps_if_banned

Image.MAX_IMAGE_PIXELS = None  # suppress stupid decompression bomb warning
register_heif_opener()

HEIF_SUFFIXES = {".heic", ".heics", ".heif", ".heifs", ".hif"}
JPEG_SUFFIX = ".jpg"
EXIF_ORIENTATION_TAG = 274


def is_heif_path(path: Path | str) -> bool:
    return Path(path).suffix.lower() in HEIF_SUFFIXES


def upload_temp_filename(src_file: Path | str, resize: int | None = None) -> str:
    src_path = Path(src_file)
    output_suffix = JPEG_SUFFIX if is_heif_path(src_path) else src_path.suffix
    resize_suffix = f"_{resize}" if resize else ""
    return f"{src_path.stem}{resize_suffix}{output_suffix}"


def _restore_exif_orientation(
    exif_bytes: bytes | None, orientation: int | None
) -> bytes | None:
    if not exif_bytes or orientation is None:
        return exif_bytes

    try:
        orientation = int(orientation)
    except (TypeError, ValueError):
        return exif_bytes
    if not 1 <= orientation <= 8:
        return exif_bytes

    prefix_len = 6 if exif_bytes.startswith(b"Exif\x00\x00") else 0
    tiff = exif_bytes[prefix_len:]
    if len(tiff) < 8:
        return exif_bytes

    if tiff[:2] == b"II":
        endian = "<"
    elif tiff[:2] == b"MM":
        endian = ">"
    else:
        return exif_bytes

    try:
        ifd_offset = unpack(endian + "L", tiff[4:8])[0]
        tag_count_offset = prefix_len + ifd_offset
        tag_count = unpack(
            endian + "H", exif_bytes[tag_count_offset : tag_count_offset + 2]
        )[0]
    except (IndexError, StructError, TypeError, ValueError):
        return exif_bytes

    for tag_n in range(tag_count):
        entry_offset = tag_count_offset + 2 + 12 * tag_n
        entry = exif_bytes[entry_offset : entry_offset + 12]
        if len(entry) < 12:
            return exif_bytes
        try:
            tag = unpack(endian + "H", entry[:2])[0]
        except (StructError, TypeError, ValueError):
            return exif_bytes
        if tag != EXIF_ORIENTATION_TAG:
            continue

        value_offset = entry_offset + 8
        value = pack(endian + "H", orientation)
        return exif_bytes[:value_offset] + value + exif_bytes[value_offset + 2 :]

    return exif_bytes


def _exif_bytes_for_save(img: Image.Image) -> bytes | None:
    exif = img.info.get("exif")
    if isinstance(exif, Image.Exif):
        exif_bytes = exif.tobytes()
    elif isinstance(exif, bytes):
        exif_bytes = exif
    else:
        image_exif = img.getexif()
        exif_bytes = image_exif.tobytes() if image_exif else None
    return _restore_exif_orientation(exif_bytes, img.info.get("original_orientation"))


def _jpeg_save_kwargs(img: Image.Image, quality: int) -> dict[str, object]:
    save_kwargs: dict[str, object] = {"format": "JPEG", "quality": quality}
    exif = _exif_bytes_for_save(img)
    if exif:
        save_kwargs["exif"] = exif
    for key in ("icc_profile", "xmp"):
        value = img.info.get(key)
        if value:
            save_kwargs[key] = value
    return save_kwargs


def _jpeg_compatible_image(img: Image.Image) -> Image.Image:
    if img.mode in JpegImagePlugin.RAWMODE:
        return img
    converted = img.convert("RGB")
    converted.info = img.info.copy()
    return converted


def save_jpeg_with_metadata(img: Image.Image, dst: Path, quality: int = 95) -> None:
    _jpeg_compatible_image(img).save(dst, **_jpeg_save_kwargs(img, quality))


def copy_to_clipboard(text: str) -> bool:
    if os.environ.get("WAYLAND_DISPLAY"):
        cmd = ["wl-copy"]
    elif os.environ.get("DISPLAY"):
        cmd = ["xclip", "-selection", "c"]
    else:
        print(
            "Error: Unable to detect display server. Clipboard not updated.",
            file=sys.stderr,
        )
        return False

    try:
        subprocess.run(cmd, input=text, text=True, check=True, start_new_session=True)
    except (OSError, subprocess.CalledProcessError):
        print("Error: Clipboard command failed.", file=sys.stderr)
        return False
    return True


def upload_photo(src_file, resize=None, clipboard=False, clipboard_format=None):
    config = load_config().upload
    src_path = Path(src_file)
    thumb_path = config.thumb_dir
    thumb_path.mkdir(exist_ok=True, parents=True)

    filename_no_ext = src_path.stem
    upload_filename = upload_temp_filename(src_path, resize)
    upload_src = thumb_path / upload_filename
    upload_ext = Path(upload_filename).suffix

    if resize:
        with Image.open(src_path) as img:
            img.thumbnail((resize, resize), Image.Resampling.LANCZOS)
            if is_heif_path(src_path) or upload_ext.lower() in {".jpg", ".jpeg"}:
                save_jpeg_with_metadata(img, upload_src)
            else:
                img.save(upload_src, quality=95)
    elif is_heif_path(src_path):
        with Image.open(src_path) as img:
            save_jpeg_with_metadata(img, upload_src)
    else:
        shutil.copyfile(src_path, upload_src)

    gps_banned = remove_gps_if_banned(upload_src)

    # Calculate SHA1 checksum
    with open(upload_src, "rb") as f:
        sha1 = hashlib.sha1(f.read()).hexdigest()
    dst_filename = f"{filename_no_ext}_{sha1[:16]}{upload_ext}"
    dst = f"{config.rclone_destination}/{dst_filename}"

    # Upload file, skipping if it already exists remotely
    dst_url = f"{config.public_base_url}/{dst_filename}"
    if clipboard or clipboard_format is not None:
        clipboard_text = (
            dst_url
            if clipboard_format is None
            else clipboard_format.format(url=dst_url)
        )
        if not copy_to_clipboard(clipboard_text):
            raise SystemExit(1)
    subprocess.run(["rclone", "copyto", "--ignore-existing", upload_src, dst])
    return dst_url


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Upload a photo and output its public URL."
    )
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
