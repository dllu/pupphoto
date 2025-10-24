from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
from typing import Optional, Tuple

import yaml
import subprocess
import re


# Load banned areas from YAML file
def load_banned_areas(yaml_path=None):
    if yaml_path is None:
        yaml_path = Path(__file__).parent / "banned_areas.yaml"
    else:
        yaml_path = Path(yaml_path)
    with open(yaml_path, "r") as file:
        data = yaml.safe_load(file)
    return data["banned_areas"]


banned_areas = load_banned_areas()


# Function to check if a coordinate is within a banned area
def is_in_banned_area(lat, lon):
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
        distance = haversine(lat, lon, area["latitude"], area["longitude"])
        if distance <= area["radius"]:
            return True
    return False


def lat_lon_from_metadata(image_path: str) -> Optional[Tuple[float, float]]:
    """
    Extract latitude and longitude from image metadata using exiv2 CLI.
    Returns (lat, lon) in decimal degrees or None if not available.
    """
    path = str(image_path)
    try:
        raw_lat = subprocess.check_output(
            ["exiv2", "-g", "Exif.GPSInfo.GPSLatitude", "-Pv", path], text=True
        ).strip()
        raw_lon = subprocess.check_output(
            ["exiv2", "-g", "Exif.GPSInfo.GPSLongitude", "-Pv", path], text=True
        ).strip()
        lat_ref = subprocess.check_output(
            ["exiv2", "-g", "Exif.GPSInfo.GPSLatitudeRef", "-Pv", path], text=True
        ).strip()
        lon_ref = subprocess.check_output(
            ["exiv2", "-g", "Exif.GPSInfo.GPSLongitudeRef", "-Pv", path], text=True
        ).strip()
    except subprocess.CalledProcessError:
        return None

    if not raw_lat or not raw_lon or not lat_ref or not lon_ref:
        return None

    nums_lat = re.findall(r"[-+]?\d*\.\d+|\d+", raw_lat)
    nums_lon = re.findall(r"[-+]?\d*\.\d+|\d+", raw_lon)
    if len(nums_lat) < 3 or len(nums_lon) < 3:
        return None
    try:
        d_lat, m_lat, s_lat = map(float, nums_lat[:3])
        d_lon, m_lon, s_lon = map(float, nums_lon[:3])
    except ValueError:
        return None

    lat = d_lat + m_lat / 60 + s_lat / 3600
    lon = d_lon + m_lon / 60 + s_lon / 3600

    if lat_ref.upper() != "N":
        lat = -lat
    if lon_ref.upper() != "E":
        lon = -lon

    return (lat, lon)


# Function to remove GPS data if within banned area
def remove_gps_if_banned(image_path: str) -> bool:
    """
    Remove GPS metadata tags if image taken within a banned area.
    Returns True if metadata was modified.
    """
    path = str(image_path)
    coords = lat_lon_from_metadata(path)
    if coords is None:
        return False
    lat, lon = coords

    if is_in_banned_area(lat, lon):
        try:
            output = subprocess.check_output(
                ["exiv2", "-g", "Exif.GPSInfo", "-pa", path], text=True
            )
        except subprocess.CalledProcessError:
            return False

        tags_to_delete = []
        for line in output.splitlines():
            parts = line.split()
            if parts:
                tag = parts[0]
                if tag.startswith("Exif.GPSInfo"):
                    tags_to_delete.append(tag)

        for tag in tags_to_delete:
            subprocess.run(
                ["exiv2", "-M", f"del {tag}", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return True

    return False
