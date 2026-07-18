import sys
import unittest
from unittest.mock import patch

import upload_clipboard


class UploadClipboardStartupTest(unittest.TestCase):
    def test_clipboard_is_cleared_before_upload_work_starts(self) -> None:
        events = []

        def clear_clipboard() -> bool:
            events.append("clear")
            return True

        def upload_photo(*args, **kwargs) -> str:
            events.append("upload")
            return "https://example.test/photo.jpg"

        with (
            patch.object(sys, "argv", ["upload_clipboard.py", "photo.jpg"]),
            patch("upload_clipboard.clear_clipboard", side_effect=clear_clipboard),
            patch("upload_photo.upload_photo", side_effect=upload_photo),
            patch("builtins.print"),
        ):
            upload_clipboard.main()

        self.assertEqual(events, ["clear", "upload"])


if __name__ == "__main__":
    unittest.main()
