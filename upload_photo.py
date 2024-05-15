import hashlib
import subprocess
import sys
from pathlib import Path
import pyexiv2
from PIL import Image, ImageOps
import shutil

from remove_gps_if_banned import remove_gps_if_banned

Image.MAX_IMAGE_PIXELS = None  # suppress stupid decompression bomb warning


def upload_photo(src_file, resize=None):
    src_path = Path(src_file)
    thumb_path = Path("/home/dllu/pictures/thumbs")

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

    metadata = pyexiv2.ImageMetadata(str(upload_src))
    metadata.read()
    gps_banned = remove_gps_if_banned(metadata)
    if gps_banned:
        metadata.write()

    # Calculate SHA1 checksum
    with open(upload_src, "rb") as f:
        sha1 = hashlib.sha1(f.read()).hexdigest()
    dst_filename = f"{filename_no_ext}_{sha1[:16]}{ext}"
    dst = f"b2:dllu-pics/{dst_filename}"

    # Check if the file exists on the remote and upload it if necessary
    result = subprocess.run(["rclone", "lsf", dst], capture_output=True, text=True)
    dst_url = f"https://i.dllu.net/{dst_filename}"
    if dst_filename not in result.stdout:
        # Upload the file
        subprocess.run(["rclone", "copyto", upload_src, dst])
    return dst_url


if __name__ == "__main__":
    if len(sys.argv) == 2:
        dst = upload_photo(sys.argv[1])
    elif len(sys.argv) == 3:
        dst = upload_photo(sys.argv[1], resize=int(sys.argv[2]))
    print(dst)
