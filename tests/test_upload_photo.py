from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import call, patch

from PIL import Image, ImageCms

from upload_photo import (
    copy_to_clipboard,
    image_path_needs_orientation_normalization,
    upload_photo,
    upload_temp_filename,
)


def _srgb_profile_bytes() -> bytes:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
    return profile.tobytes()


class UploadPhotoHeifTest(unittest.TestCase):
    def test_upload_temp_filename_converts_heif_extension_to_jpg(self) -> None:
        self.assertEqual(upload_temp_filename("photo.HEIC"), "photo.jpg")
        self.assertEqual(upload_temp_filename("photo.heif", 600), "photo_600.jpg")
        self.assertEqual(upload_temp_filename("photo.JPG", 600), "photo_600.JPG")

    def test_heif_upload_converts_to_jpeg_without_applying_original_orientation(
        self,
    ) -> None:
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
            self.assertEqual(converted.size, (20, 10))
            self.assertEqual(converted_exif.get(271), "Camera Make")
            self.assertEqual(converted_exif.get(272), "Camera Model")
            self.assertIsNone(converted_exif.get(274))
            self.assertEqual(converted.info.get("icc_profile"), icc_profile)
            self.assertEqual(converted.info.get("xmp"), xmp)

    def test_jpeg_upload_normalizes_orientation_and_preserves_metadata(self) -> None:
        source_dir = self.enterContext(tempfile.TemporaryDirectory())
        source_path = Path(source_dir) / "photo.jpg"
        thumb_dir = Path(source_dir) / "thumb"

        image = Image.new("RGB", (20, 10), "red")
        exif = Image.Exif()
        exif[271] = "Camera Make"
        exif[272] = "Camera Model"
        exif[274] = 6
        image.save(source_path, exif=exif, quality=90)

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

        with Image.open(upload_path) as normalized:
            normalized_exif = normalized.getexif()
            self.assertEqual(normalized.format, "JPEG")
            self.assertEqual(normalized.size, (10, 20))
            self.assertEqual(normalized_exif.get(271), "Camera Make")
            self.assertEqual(normalized_exif.get(272), "Camera Model")
            self.assertIsNone(normalized_exif.get(274))

    def test_jpeg_upload_with_orientation_one_is_copied_without_reencoding(
        self,
    ) -> None:
        source_dir = self.enterContext(tempfile.TemporaryDirectory())
        source_path = Path(source_dir) / "photo.jpg"
        thumb_dir = Path(source_dir) / "thumb"

        image = Image.new("RGB", (20, 10), "red")
        exif = Image.Exif()
        exif[271] = "Camera Make"
        exif[274] = 1
        image.save(source_path, exif=exif, quality=90)
        source_bytes = source_path.read_bytes()

        config = SimpleNamespace(
            upload=SimpleNamespace(
                thumb_dir=thumb_dir,
                rclone_destination="remote:photos",
                public_base_url="https://example.test/photos",
            )
        )

        self.assertFalse(image_path_needs_orientation_normalization(source_path))

        with (
            patch("upload_photo.load_config", return_value=config),
            patch("upload_photo.remove_gps_if_banned", return_value=False),
            patch("upload_photo.subprocess.run") as run,
        ):
            upload_photo(source_path)

        upload_path = thumb_dir / "photo.jpg"
        self.assertEqual(upload_path.read_bytes(), source_bytes)
        run.assert_called_once()


class UploadPhotoClipboardTest(unittest.TestCase):
    def test_copy_to_clipboard_uses_xclip_in_new_session(self) -> None:
        with (
            patch.dict("clipboard_util.os.environ", {"DISPLAY": ":1"}, clear=True),
            patch("clipboard_util.subprocess.run") as run,
        ):
            self.assertTrue(copy_to_clipboard("https://example.test/photo.jpg"))

        run.assert_called_once_with(
            ["xclip", "-selection", "c"],
            input="https://example.test/photo.jpg",
            text=True,
            check=True,
            start_new_session=True,
        )

    def test_clipboard_is_populated_before_rclone_upload(self) -> None:
        source_dir = self.enterContext(tempfile.TemporaryDirectory())
        source_path = Path(source_dir) / "photo.jpg"
        thumb_dir = Path(source_dir) / "thumb"
        source_path.write_bytes(b"photo bytes")

        config = SimpleNamespace(
            upload=SimpleNamespace(
                thumb_dir=thumb_dir,
                rclone_destination="remote:photos",
                public_base_url="https://example.test/photos",
            )
        )

        with (
            patch.dict("clipboard_util.os.environ", {"DISPLAY": ":1"}, clear=True),
            patch("upload_photo.load_config", return_value=config),
            patch("upload_photo.remove_gps_if_banned", return_value=False),
            patch("upload_photo.subprocess.run") as run,
        ):
            url = upload_photo(source_path, clipboard=True)

        self.assertEqual(run.call_args_list[0].args[0], ["xclip", "-selection", "c"])
        self.assertEqual(
            run.call_args_list[0].kwargs,
            {
                "input": url,
                "text": True,
                "check": True,
                "start_new_session": True,
            },
        )
        self.assertEqual(
            run.call_args_list[1],
            call(
                [
                    "rclone",
                    "copyto",
                    "--ignore-existing",
                    thumb_dir / "photo.jpg",
                    f"remote:photos/{url.rsplit('/', 1)[-1]}",
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()
