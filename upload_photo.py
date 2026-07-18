import argparse
import hashlib
import re
import subprocess
from pathlib import Path

from PIL import Image, JpegImagePlugin
from pillow_heif import register_heif_opener

from clipboard_util import copy_to_clipboard
from config import load_config
from gps import remove_gps_if_banned

Image.MAX_IMAGE_PIXELS = None  # suppress stupid decompression bomb warning
register_heif_opener()

HEIF_SUFFIXES = {".heic", ".heics", ".heif", ".heifs", ".hif"}
JPEG_SUFFIX = ".jpg"
JPEG_SUFFIXES = {".jpg", ".jpeg"}
EXIF_ORIENTATION_TAG = 274
ORIENTATION_TRANSPOSE_METHODS = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def is_heif_path(path: Path | str) -> bool:
    return Path(path).suffix.lower() in HEIF_SUFFIXES


def upload_temp_filename(src_file: Path | str, resize: int | None = None) -> str:
    src_path = Path(src_file)
    output_suffix = JPEG_SUFFIX if is_heif_path(src_path) else src_path.suffix
    resize_suffix = f"_{resize}" if resize else ""
    return f"{src_path.stem}{resize_suffix}{output_suffix}"


def _valid_orientation(value: object) -> int | None:
    try:
        orientation = int(value)
    except (TypeError, ValueError):
        return None
    return orientation if 1 <= orientation <= 8 else None


def _xmp_bytes(value: object) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return None


def _xmp_orientation_value(value: object) -> int | None:
    xmp = _xmp_bytes(value)
    if not xmp:
        return None
    content = xmp.rsplit(b"\x00", 1)[0]
    decoded = None
    for encoding in ("utf-8", "latin1"):
        try:
            decoded = content.decode(encoding)
            break
        except UnicodeDecodeError:
            pass
    if decoded is None:
        return None
    match = re.search(r'tiff:Orientation(="|>)([0-9])', decoded)
    return _valid_orientation(match.group(2)) if match else None


def _strip_xmp_orientation(value: object) -> bytes | None:
    xmp = _xmp_bytes(value)
    if not xmp:
        return None
    content, separator, trailing = xmp.rpartition(b"\x00")
    if not separator:
        content = xmp
        trailing = b""
    decoded = None
    for encoding in ("utf-8", "latin1"):
        try:
            decoded = content.decode(encoding)
            break
        except UnicodeDecodeError:
            pass
    if decoded is None:
        return xmp
    decoded = re.sub(r'\s*tiff:Orientation="([0-9])"', "", decoded)
    decoded = re.sub(r"\s*<tiff:Orientation>([0-9])</tiff:Orientation>", "", decoded)
    return decoded.encode("utf-8") + (separator + trailing if separator else b"")


def _image_orientation(img: Image.Image) -> int | None:
    orientation = _valid_orientation(img.getexif().get(EXIF_ORIENTATION_TAG))
    if orientation is not None:
        return orientation
    return _xmp_orientation_value(img.info.get("xmp")) or _xmp_orientation_value(
        img.info.get("XML:com.adobe.xmp")
    )


def image_has_orientation_tag(img: Image.Image) -> bool:
    return (
        EXIF_ORIENTATION_TAG in img.getexif()
        or _xmp_orientation_value(img.info.get("xmp")) is not None
        or _xmp_orientation_value(img.info.get("XML:com.adobe.xmp")) is not None
    )


def image_path_has_orientation_tag(path: Path | str) -> bool:
    try:
        with Image.open(path) as img:
            return image_has_orientation_tag(img)
    except (OSError, ValueError):
        return False


def image_needs_orientation_normalization(img: Image.Image) -> bool:
    return _image_orientation(img) in ORIENTATION_TRANSPOSE_METHODS


def image_path_needs_orientation_normalization(path: Path | str) -> bool:
    try:
        with Image.open(path) as img:
            return image_needs_orientation_normalization(img)
    except (OSError, ValueError):
        return False


def _exif_bytes_for_save(img: Image.Image) -> bytes | None:
    exif = img.getexif()
    if not exif:
        return None
    if EXIF_ORIENTATION_TAG in exif:
        del exif[EXIF_ORIENTATION_TAG]
    return exif.tobytes() if exif else None


def _info_without_orientation(img: Image.Image) -> dict[str, object]:
    info = img.info.copy()
    exif = _exif_bytes_for_save(img)
    if exif:
        info["exif"] = exif
    else:
        info.pop("exif", None)
    info.pop("original_orientation", None)
    for key in ("xmp", "XML:com.adobe.xmp"):
        xmp = _strip_xmp_orientation(info.get(key))
        if xmp:
            info[key] = xmp
        else:
            info.pop(key, None)
    return info


def apply_exif_orientation(img: Image.Image) -> Image.Image:
    orientation = _image_orientation(img)
    method = ORIENTATION_TRANSPOSE_METHODS.get(orientation)
    if method is None:
        return img
    normalized = img.transpose(method) if method is not None else img.copy()
    normalized.info = _info_without_orientation(img)
    return normalized


def _metadata_save_kwargs(img: Image.Image, quality: int) -> dict[str, object]:
    save_kwargs: dict[str, object] = {"quality": quality}
    exif = _exif_bytes_for_save(img)
    if exif:
        save_kwargs["exif"] = exif
    for key in ("icc_profile", "xmp"):
        value = (
            _strip_xmp_orientation(img.info.get(key))
            if key == "xmp"
            else img.info.get(key)
        )
        if value:
            save_kwargs[key] = value
    return save_kwargs


def _jpeg_save_kwargs(img: Image.Image, quality: int) -> dict[str, object]:
    return {"format": "JPEG", **_metadata_save_kwargs(img, quality)}


def _jpeg_compatible_image(img: Image.Image) -> Image.Image:
    if img.mode in JpegImagePlugin.RAWMODE:
        return img
    converted = img.convert("RGB")
    converted.info = img.info.copy()
    return converted


def save_jpeg_with_metadata(img: Image.Image, dst: Path, quality: int = 95) -> None:
    normalized = apply_exif_orientation(img)
    _jpeg_compatible_image(normalized).save(
        dst, **_jpeg_save_kwargs(normalized, quality)
    )


def save_image_with_metadata(img: Image.Image, dst: Path, quality: int = 95) -> None:
    normalized = apply_exif_orientation(img)
    normalized.save(dst, **_metadata_save_kwargs(normalized, quality))


def _copy_with_sha1(src: Path, dst: Path) -> str:
    """Copy a file and hash it in the same read pass."""
    digest = hashlib.sha1()
    buffer = bytearray(1024 * 1024)
    view = memoryview(buffer)
    with src.open("rb") as source, dst.open("wb") as destination:
        while size := source.readinto(buffer):
            chunk = view[:size]
            digest.update(chunk)
            destination.write(chunk)
    return digest.hexdigest()


def upload_photo(src_file, resize=None, clipboard=False, clipboard_format=None):
    config = load_config().upload
    src_path = Path(src_file)
    thumb_path = config.thumb_dir
    thumb_path.mkdir(exist_ok=True, parents=True)

    filename_no_ext = src_path.stem
    upload_filename = upload_temp_filename(src_path, resize)
    upload_src = thumb_path / upload_filename
    upload_ext = Path(upload_filename).suffix

    needs_conversion = is_heif_path(src_path)
    needs_orientation_normalization = (
        not needs_conversion and image_path_needs_orientation_normalization(src_path)
    )
    sha1 = None

    if resize:
        with Image.open(src_path) as img:
            # JPEG decoders can discard high-frequency DCT data while decoding
            # when the requested result is much smaller than the source.
            if img.format == "JPEG":
                scale = min(resize / max(img.size), 1.0)
                draft_size = tuple(max(1, round(side * scale)) for side in img.size)
                img.draft(None, draft_size)
            img = apply_exif_orientation(img)
            img.thumbnail((resize, resize), Image.Resampling.LANCZOS)
            if needs_conversion or upload_ext.lower() in JPEG_SUFFIXES:
                save_jpeg_with_metadata(img, upload_src)
            else:
                save_image_with_metadata(img, upload_src)
    elif needs_conversion:
        with Image.open(src_path) as img:
            save_jpeg_with_metadata(img, upload_src)
    elif needs_orientation_normalization:
        with Image.open(src_path) as img:
            if upload_ext.lower() in JPEG_SUFFIXES:
                save_jpeg_with_metadata(img, upload_src)
            else:
                save_image_with_metadata(img, upload_src)
    else:
        sha1 = _copy_with_sha1(src_path, upload_src)

    gps_removed = remove_gps_if_banned(upload_src)

    # Transformed files, and files modified by GPS redaction, still need a pass
    # over the final bytes. Plain copies were already hashed while being copied.
    if sha1 is None or gps_removed:
        with open(upload_src, "rb") as f:
            sha1 = hashlib.file_digest(f, "sha1").hexdigest()
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
