from pathlib import Path
from dataclasses import dataclass, field
from typing import List
import hashlib
import shutil
import subprocess
import datetime
import os
from tqdm import tqdm

from config import load_config


# Compute SHA1 hash of a file
def sha1sum(filename: Path):
    with open(filename, "rb", buffering=0) as f:
        return hashlib.file_digest(f, "sha1").hexdigest()


# Extract the datetime from the EXIF data using exiv2 CLI
def get_exif_datetime(image_path: Path):
    try:
        output = subprocess.check_output(
            ["exiv2", "-g", "Exif.Photo.DateTimeOriginal", "-Pv", str(image_path)],
            text=True,
        ).strip()
        if not output:
            return None
        dt = datetime.datetime.strptime(output, "%Y:%m:%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d-%H-%M-%S")
    except (subprocess.CalledProcessError, ValueError):
        return None


# Check if a file with the same date and filename prefix already exists
def file_already_exists(destination: Path, filename_prefix: str):
    return any(destination.glob(f"{filename_prefix}*"))


@dataclass
class Summary:
    skipped_photo_files: List[str] = field(default_factory=list)
    successful_photo_import: List[str] = field(default_factory=list)
    skipped_video_files: List[str] = field(default_factory=list)
    successful_video_import: List[str] = field(default_factory=list)
    no_jpeg_files: List[str] = field(default_factory=list)
    no_raw_files: List[str] = field(default_factory=list)
    invalid_exif_files: List[str] = field(default_factory=list)
    unsupported_files: List[str] = field(default_factory=list)

    def __repr__(self):
        out = [
            f"Successfully imported photos: {len(self.successful_photo_import)}",
            f"Total photo files skipped (already existing): {len(self.skipped_photo_files)}",
            f"Successfully imported videos: {len(self.successful_video_import)}",
            f"Total video files skipped (already existing): {len(self.skipped_video_files)}",
        ]

        def print_files(files):
            return "\n".join("   " + x for x in files)

        if self.no_jpeg_files:
            out.append("Files with no corresponding JPEG:")
            out.append(print_files(self.no_jpeg_files))
        if self.no_raw_files:
            out.append("JPEG files with no corresponding raw file:")
            out.append(print_files(self.no_raw_files))
        if self.invalid_exif_files:
            out.append("Files with no valid EXIF:")
            out.append(print_files(self.invalid_exif_files))
        if self.unsupported_files:
            out.append("Unsupported files:")
            out.append(print_files(self.unsupported_files))

        return "\n".join(out)


def copy_and_rename_files(
    source: Path,
    photo_destination: Path,
    video_destination: Path,
    supported_raw_formats: list[str],
    supported_video_formats: list[str],
):
    # Ensure destination directories exist
    photo_destination.mkdir(parents=True, exist_ok=True)
    video_destination.mkdir(parents=True, exist_ok=True)

    summary = Summary()

    # Process all files in the source directory
    for file_path in tqdm(
        list(source.glob("*.*")), desc="Processing files", dynamic_ncols=True
    ):
        suffix = file_path.suffix.lower()

        if suffix in supported_video_formats:
            # Handle video files
            mtime = os.path.getmtime(file_path)
            datetime_taken = datetime.datetime.fromtimestamp(mtime).strftime(
                "%Y-%m-%d-%H-%M-%S"
            )
            sha1 = sha1sum(file_path)
            new_base_filename = f"{datetime_taken}_{file_path.stem}"
            if file_already_exists(video_destination, new_base_filename):
                summary.skipped_video_files.append(str(file_path))
                continue
            new_filename = f"{new_base_filename}_{sha1}{suffix}"
            shutil.copy2(file_path, video_destination / new_filename)
            summary.successful_video_import.append(str(file_path))

        elif suffix == ".jpg":
            # Handle JPEG files (check for corresponding RAW)
            has_raw_file = sum(
                1
                for raw_suffix in supported_raw_formats
                if file_path.with_suffix(raw_suffix.upper()).exists()
            )
            if has_raw_file == 0:
                summary.no_raw_files.append(str(file_path))
            elif has_raw_file > 1:
                print(f"Multiple raw files found for {file_path}")

        elif suffix in supported_raw_formats:
            # Handle RAW files
            jpg_file_path = file_path.with_suffix(".JPG")
            if not jpg_file_path.exists():
                summary.no_jpeg_files.append(file_path.name)
                continue
            datetime_taken = get_exif_datetime(jpg_file_path)
            if not datetime_taken:
                summary.invalid_exif_files.append(jpg_file_path.name)
                continue
            new_base_filename = f"{datetime_taken}_{file_path.stem}"
            if file_already_exists(photo_destination, new_base_filename):
                summary.skipped_photo_files.append(str(file_path))
                continue
            sha1 = sha1sum(file_path)
            new_filename = f"{new_base_filename}_{sha1}"
            shutil.copy2(jpg_file_path, photo_destination / f"{new_filename}.jpg")
            shutil.copy2(file_path, photo_destination / f"{new_filename}{suffix}")
            summary.successful_photo_import.append(str(file_path))

        else:
            summary.unsupported_files.append(file_path.name)

    print(summary)


def main():
    app_config = load_config()
    config = app_config.import_config

    # Process each subdirectory in the camera directory
    for source_dir in config.camera_dir.glob("*/"):
        if source_dir.is_dir():
            copy_and_rename_files(
                source_dir,
                config.photo_destination,
                config.video_destination,
                config.supported_raw_formats,
                config.supported_video_formats,
            )


if __name__ == "__main__":
    main()
