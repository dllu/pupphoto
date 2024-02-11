import hashlib
import subprocess
import sys
from pathlib import Path
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None  # suppress stupid decompression bomb warning


def upload_photo(src_file, resize=None):
    src_path = Path(src_file)
    print("uploading", src_path)

    dir_name = src_path.parent
    # filename = src_path.name
    filename_no_ext = src_path.stem
    ext = src_path.suffix

    if resize:
        img = Image.open(src_file)
        img = ImageOps.exif_transpose(img)
        img.thumbnail((resize, resize), Image.Resampling.LANCZOS)
        resized_filename = f"{filename_no_ext}_{resize}{ext}"
        src = dir_name / resized_filename  # Temporary file path
        img.save(src, quality=95)
    else:
        src = src_path

    # Calculate SHA1 checksum
    with open(src, "rb") as f:
        sha1 = hashlib.sha1(f.read()).hexdigest()
    dst_filename = f"{filename_no_ext}_{sha1[:16]}{ext}"
    dst = f"b2:dllu-pics/{dst_filename}"

    # Check if the file exists on the remote and upload it if necessary
    result = subprocess.run(["rclone", "lsf", dst], capture_output=True, text=True)
    dst_url = f"https://i.dllu.net/{dst_filename}"
    if dst_filename not in result.stdout:
        # Upload the file
        subprocess.run(["rclone", "copyto", src, dst])
    return dst_url


if __name__ == "__main__":
    if len(sys.argv) == 2:
        dst = upload_photo(sys.argv[1])
    elif len(sys.argv) == 3:
        dst = upload_photo(sys.argv[1], resize=int(sys.argv[2]))
    print(dst)
