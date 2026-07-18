import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from upload_photo import upload_photo


class UploadHashTest(unittest.TestCase):
    def setUp(self) -> None:
        source_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.source_path = Path(source_dir) / "photo.jpg"
        self.source_path.write_bytes(b"original photo bytes")
        self.thumb_dir = Path(source_dir) / "thumb"
        self.config = SimpleNamespace(
            upload=SimpleNamespace(
                thumb_dir=self.thumb_dir,
                rclone_destination="remote:photos",
                public_base_url="https://example.test/photos",
            )
        )

    def test_plain_copy_is_hashed_during_copy(self) -> None:
        expected = hashlib.sha1(self.source_path.read_bytes()).hexdigest()[:16]
        with (
            patch("upload_photo.load_config", return_value=self.config),
            patch("upload_photo.remove_gps_if_banned", return_value=False),
            patch("upload_photo.hashlib.file_digest", side_effect=AssertionError),
            patch("upload_photo.subprocess.run"),
        ):
            url = upload_photo(self.source_path)

        self.assertTrue(url.endswith(f"photo_{expected}.jpg"))

    def test_file_is_rehashed_after_gps_redaction(self) -> None:
        redacted_bytes = b"photo bytes without GPS"

        def redact(path: Path) -> bool:
            path.write_bytes(redacted_bytes)
            return True

        expected = hashlib.sha1(redacted_bytes).hexdigest()[:16]
        with (
            patch("upload_photo.load_config", return_value=self.config),
            patch("upload_photo.remove_gps_if_banned", side_effect=redact),
            patch("upload_photo.subprocess.run"),
        ):
            url = upload_photo(self.source_path)

        self.assertTrue(url.endswith(f"photo_{expected}.jpg"))


if __name__ == "__main__":
    unittest.main()
