from math import radians, sin, cos, sqrt, atan2
import pyexiv2
import yaml


# Load banned areas from YAML file
def load_banned_areas(yaml_path="/home/dllu/proj/pupphoto/banned_areas.yaml"):
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


# Function to remove GPS data if within banned area
def remove_gps_if_banned(metadata: pyexiv2.ImageMetadata) -> bool:
    gps_lat = metadata.get("Exif.GPSInfo.GPSLatitude")
    gps_lon = metadata.get("Exif.GPSInfo.GPSLongitude")
    gps_lat_ref = metadata.get("Exif.GPSInfo.GPSLatitudeRef")
    gps_lon_ref = metadata.get("Exif.GPSInfo.GPSLongitudeRef")

    if gps_lat and gps_lon and gps_lat_ref and gps_lon_ref:
        gps_lat = gps_lat.value
        gps_lon = gps_lon.value
        gps_lat_ref = gps_lat_ref.value
        gps_lon_ref = gps_lon_ref.value

        lat_deg = gps_lat[0] + gps_lat[1] / 60 + gps_lat[2] / 3600
        lon_deg = gps_lon[0] + gps_lon[1] / 60 + gps_lon[2] / 3600
    else:
        return False

    if gps_lat_ref != "N":
        lat_deg = -lat_deg
    if gps_lon_ref != "E":
        lon_deg = -lon_deg
    if is_in_banned_area(lat_deg, lon_deg):
        # Clear GPS info from the metadata
        keys_to_delete = [key for key in metadata if key.startswith("Exif.GPSInfo")]
        for key in keys_to_delete:
            del metadata[key]
        return True

    return False
