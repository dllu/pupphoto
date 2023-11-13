#!/usr/bin/env python

from dataclasses import dataclass, field
from typing import List
from pathlib import Path
import hashlib
import shutil
import pyexiv2
from tqdm import tqdm


# Compute SHA1 hash of a file
def sha1sum(filename: Path):
    with open(filename, "rb", buffering=0) as f:
        return hashlib.file_digest(f, "sha1").hexdigest()


# Extract the datetime from the EXIF data using exiv2
def get_exif_datetime(image_path: Path):
    try:
        exif_data = pyexiv2.ImageMetadata(str(image_path))
        exif_data.read()
        datetime_original = exif_data["Exif.Photo.DateTimeOriginal"].value
        return datetime_original.strftime("%Y-%m-%d-%H-%M-%S")
    except KeyError:
        return None
    except IOError:
        return None


# Check if file already exists with the same date and filename prefix
def file_already_exists(destination: Path, filename_prefix: str):
    return any(destination.glob(f"{filename_prefix}*"))


@dataclass
class Summary:
    skipped_files: List[str] = field(default_factory=list)
    no_jpeg_files: List[str] = field(default_factory=list)
    no_raw_files: List[str] = field(default_factory=list)
    invalid_exif_files: List[str] = field(default_factory=list)
    unsupported_files: List[str] = field(default_factory=list)
    successful_import: List[str] = field(default_factory=list)

    def __repr__(self):
        out = [
            f"Successfully imported: {len(self.successful_import)}",
            f"Total files skipped (already existing): {len(self.skipped_files)}",
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
            out.append("Files with no valid exif")
            out.append(print_files(self.invalid_exif_files))
        if self.unsupported_files:
            out.append("Unsupported files:")
            out.append(print_files(self.unsupported_files))

        return "\n".join(out)


# Main function to copy and rename files
def copy_and_rename_files(source, destination):
    destination.mkdir(parents=True, exist_ok=True)

    supported_raw_formats = [".raf", ".dng", ".cr3", ".arw"]

    summary = Summary()

    # Process files with progress bar
    raw_files = list(source.glob("*.*"))
    for raw_file_path in tqdm(raw_files, desc="Processing images"):
        if raw_file_path.suffix.lower() == ".jpg":
            has_raw_file = 0
            for raw_suffix in supported_raw_formats:
                if raw_file_path.with_suffix(raw_suffix.upper()).exists():
                    has_raw_file += 1
            if has_raw_file == 0:
                summary.no_raw_files.append(raw_file_path)
            if has_raw_file == 1:
                # everything good, continue
                continue
            if has_raw_file > 1:
                print(f"Multiple raw files found for {raw_file_path}")

        if raw_file_path.suffix.lower() not in supported_raw_formats:
            summary.unsupported_files.append(raw_file_path.name)
            continue

        jpg_file_path = raw_file_path.with_suffix(".JPG")
        if not jpg_file_path.exists():
            summary.no_jpeg_files.append(raw_file_path.name)
            continue

        datetime_taken = get_exif_datetime(jpg_file_path)
        if not datetime_taken:
            summary.invalid_exif_files.append(jpg_file_path.name)
            continue

        new_base_filename = f"{datetime_taken}_{raw_file_path.stem}"
        if file_already_exists(destination, new_base_filename):
            summary.skipped_files.append(raw_file_path)
            continue

        # Compute SHA1 of the raw file
        sha1 = sha1sum(raw_file_path)
        new_filename = f"{new_base_filename}_{sha1}"

        # Copy and rename JPEG file
        shutil.copy2(jpg_file_path, destination / f"{new_filename}.jpg")
        # Copy and rename raw file
        shutil.copy2(
            raw_file_path, destination / f"{new_filename}{raw_file_path.suffix.lower()}"
        )
        summary.successful_import.append(raw_file_path)

    print(summary)


def main():
    camera_dir = Path("/mnt/camera/DCIM")
    destination_dir = Path("/home/dllu/pictures/raw")

    # Iterate over all source directories found by globbing /mnt/camera/DCIM/*
    for source_dir in camera_dir.glob("*/"):
        if source_dir.is_dir():
            copy_and_rename_files(source_dir, destination_dir)


if __name__ == "__main__":
    main()
