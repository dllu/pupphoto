from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

import upload_blog


class UploadBlogStartupTest(unittest.TestCase):
    def test_clipboard_is_cleared_before_upload_and_config_work(self) -> None:
        temp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        thumb_dir = temp_dir / "thumb"
        thumb_dir.mkdir()
        (thumb_dir / "photo.jpg").write_bytes(b"processed photo")
        config = SimpleNamespace(
            upload=SimpleNamespace(
                thumb_dir=thumb_dir,
                blog_image_dir=temp_dir / "blog",
            )
        )
        events = []

        def clear_clipboard() -> bool:
            events.append("clear")
            return True

        def upload_photo(*args, **kwargs) -> str:
            events.append("upload")
            return "https://example.test/photo_hash.jpg"

        def load_config() -> SimpleNamespace:
            events.append("config")
            return config

        with (
            patch.object(sys, "argv", ["upload_blog.py", "photo.jpg"]),
            patch("upload_blog.clear_clipboard", side_effect=clear_clipboard),
            patch("upload_photo.upload_photo", side_effect=upload_photo),
            patch("config.load_config", side_effect=load_config),
            patch("builtins.print"),
        ):
            upload_blog.main()

        self.assertEqual(events, ["clear", "upload", "config"])
        self.assertEqual(
            (temp_dir / "blog" / "photo_hash.jpg").read_bytes(),
            b"processed photo",
        )


if __name__ == "__main__":
    unittest.main()
