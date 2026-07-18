from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
from typing import Optional, Tuple

import subprocess

from config import load_config


banned_areas = load_config().banned_areas


# Function to check if a coordinate is within a banned area
def is_in_banned_area(lat: float, lon: float) -> bool:
    def haversine(lat1, lon1, lat2, lon2):
        earth_radius = 6371000
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = (
            sin(dlat / 2) ** 2
            + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
        )
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        return earth_radius * c

    for area in banned_areas:
        distance = haversine(lat, lon, area.latitude, area.longitude)
        if distance <= area.radius_meters:
            return True
    return False


def _gps_metadata(image_path: Path | str) -> dict[str, str]:
    path = str(image_path)
    try:
        output = subprocess.check_output(
            ["exiv2", "-Pkyv", "-g", "Exif.GPSInfo", path],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return {}

    metadata = {}
    for line in output.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) == 3:
            key, _value_type, value = parts
            metadata[key] = value
    return metadata


def _rational(value: str) -> float:
    numerator, separator, denominator = value.partition("/")
    return float(numerator) / float(denominator) if separator else float(numerator)


def _coordinate(value: str) -> float | None:
    components = value.split()
    if len(components) != 3:
        return None
    try:
        degrees, minutes, seconds = map(_rational, components)
    except (ValueError, ZeroDivisionError):
        return None
    return degrees + minutes / 60 + seconds / 3600


def _lat_lon_from_metadata(
    metadata: dict[str, str],
) -> Optional[Tuple[float, float]]:
    latitude = _coordinate(metadata.get("Exif.GPSInfo.GPSLatitude", ""))
    longitude = _coordinate(metadata.get("Exif.GPSInfo.GPSLongitude", ""))
    lat_ref = metadata.get("Exif.GPSInfo.GPSLatitudeRef", "").upper()
    lon_ref = metadata.get("Exif.GPSInfo.GPSLongitudeRef", "").upper()
    if (
        latitude is None
        or longitude is None
        or lat_ref not in {"N", "S"}
        or lon_ref not in {"E", "W"}
    ):
        return None
    return (
        -latitude if lat_ref == "S" else latitude,
        -longitude if lon_ref == "W" else longitude,
    )


def lat_lon_from_metadata(image_path: Path | str) -> Optional[Tuple[float, float]]:
    """
    Extract latitude and longitude from image metadata using exiv2 CLI.
    Returns (lat, lon) in decimal degrees or None if not available.
    """
    return _lat_lon_from_metadata(_gps_metadata(image_path))


# Function to remove GPS data if within banned area
def remove_gps_if_banned(image_path: Path | str) -> bool:
    """
    Remove GPS metadata tags if image taken within a banned area.
    Returns True if metadata was modified.
    """
    path = str(image_path)
    metadata = _gps_metadata(path)
    coords = _lat_lon_from_metadata(metadata)
    if coords is None:
        return False
    lat, lon = coords

    if is_in_banned_area(lat, lon):
        tags_to_delete = [key for key in metadata if key.startswith("Exif.GPSInfo")]
        if not tags_to_delete:
            return False
        cmd = ["exiv2"]
        for tag in tags_to_delete:
            cmd.extend(["-M", f"del {tag}"])
        cmd.append(path)
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True

    return False
