from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageCms

from upload_photo import upload_photo, upload_temp_filename


def _srgb_profile_bytes() -> bytes:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
    return profile.tobytes()


class UploadPhotoHeifTest(unittest.TestCase):
    def test_upload_temp_filename_converts_heif_extension_to_jpg(self) -> None:
        self.assertEqual(upload_temp_filename("photo.HEIC"), "photo.jpg")
        self.assertEqual(upload_temp_filename("photo.heif", 600), "photo_600.jpg")
        self.assertEqual(upload_temp_filename("photo.JPG", 600), "photo_600.JPG")

    def test_heif_upload_converts_to_jpeg_and_preserves_metadata(self) -> None:
        source_dir = self.enterContext(tempfile.TemporaryDirectory())
        source_path = Path(source_dir) / "photo.heic"
        thumb_dir = Path(source_dir) / "thumb"
        icc_profile = _srgb_profile_bytes()
        xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF /></x:xmpmeta>'

        image = Image.new("RGB", (20, 10), "red")
        exif = Image.Exif()
        exif[271] = "Camera Make"
        exif[272] = "Camera Model"
        exif[274] = 6
        image.save(
            source_path,
            exif=exif,
            icc_profile=icc_profile,
            xmp=xmp,
            quality=90,
        )

        config = SimpleNamespace(
            upload=SimpleNamespace(
                thumb_dir=thumb_dir,
                rclone_destination="remote:photos",
                public_base_url="https://example.test/photos",
            )
        )

        with (
            patch("upload_photo.load_config", return_value=config),
            patch("upload_photo.remove_gps_if_banned", return_value=False),
            patch("upload_photo.subprocess.run") as run,
        ):
            url = upload_photo(source_path)

        self.assertRegex(
            url,
            r"^https://example\.test/photos/photo_[0-9a-f]{16}\.jpg$",
        )
        upload_path = thumb_dir / "photo.jpg"
        self.assertTrue(upload_path.is_file())
        run.assert_called_once()
        self.assertEqual(Path(run.call_args.args[0][3]).suffix, ".jpg")

        with Image.open(upload_path) as converted:
            converted_exif = converted.getexif()
            self.assertEqual(converted.format, "JPEG")
            self.assertEqual(converted_exif.get(271), "Camera Make")
            self.assertEqual(converted_exif.get(272), "Camera Model")
            self.assertEqual(converted_exif.get(274), 6)
            self.assertEqual(converted.info.get("icc_profile"), icc_profile)
            self.assertEqual(converted.info.get("xmp"), xmp)


if __name__ == "__main__":
    unittest.main()
