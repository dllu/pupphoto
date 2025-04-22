#!/usr/bin/env python3

import pyexiv2
import webbrowser
from gps import lat_lon_from_metadata


def open_location_in_maps(image_path: str):
    metadata = pyexiv2.ImageMetadata(image_path)
    metadata.read()
    lat_lon = lat_lon_from_metadata(metadata)

    if lat_lon:
        lat, lon = lat_lon
        url = f"https://www.google.com/maps/?q={lat},{lon}"
        webbrowser.open(url)
    else:
        print("No GPS information found in the image.")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python open_in_maps.py <image_path>")
    else:
        open_location_in_maps(sys.argv[1])
