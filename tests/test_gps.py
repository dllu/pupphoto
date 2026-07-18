from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from gps import lat_lon_from_metadata, remove_gps_if_banned


GPS_OUTPUT = """\
Exif.GPSInfo.GPSVersionID Byte 2 2 0 0
Exif.GPSInfo.GPSLatitudeRef Ascii S
Exif.GPSInfo.GPSLatitude Rational 34/1 42558199/1000000 0/1
Exif.GPSInfo.GPSLongitudeRef Ascii W
Exif.GPSInfo.GPSLongitude Rational 135/1 11626249/1000000 0/1
Exif.GPSInfo.GPSAltitude Rational 1580/10
"""


class GpsMetadataTest(unittest.TestCase):
    def test_coordinates_are_read_with_one_exiv2_process(self) -> None:
        with patch("gps.subprocess.check_output", return_value=GPS_OUTPUT) as check:
            coords = lat_lon_from_metadata("photo.jpg")

        self.assertIsNotNone(coords)
        assert coords is not None
        self.assertAlmostEqual(coords[0], -(34 + 42.558199 / 60))
        self.assertAlmostEqual(coords[1], -(135 + 11.626249 / 60))
        check.assert_called_once_with(
            ["exiv2", "-Pkyv", "-g", "Exif.GPSInfo", "photo.jpg"],
            text=True,
            stderr=subprocess.DEVNULL,
        )

    def test_all_gps_tags_are_removed_with_one_exiv2_process(self) -> None:
        with (
            patch(
                "gps._gps_metadata",
                return_value={
                    "Exif.GPSInfo.GPSLatitudeRef": "N",
                    "Exif.GPSInfo.GPSLatitude": "37/1 0/1 0/1",
                    "Exif.GPSInfo.GPSLongitudeRef": "W",
                    "Exif.GPSInfo.GPSLongitude": "122/1 0/1 0/1",
                },
            ),
            patch("gps.is_in_banned_area", return_value=True),
            patch("gps.subprocess.run") as run,
        ):
            self.assertTrue(remove_gps_if_banned(Path("photo.jpg")))

        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[0], "exiv2")
        self.assertEqual(command[-1], "photo.jpg")
        self.assertEqual(command.count("-M"), 4)


if __name__ == "__main__":
    unittest.main()
