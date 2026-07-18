import unittest
from unittest.mock import patch

from clipboard_util import clear_clipboard


class ClearClipboardTest(unittest.TestCase):
    def test_x11_clipboard_is_replaced_with_empty_text(self) -> None:
        with (
            patch.dict("clipboard_util.os.environ", {"DISPLAY": ":1"}, clear=True),
            patch("clipboard_util.subprocess.run") as run,
        ):
            self.assertTrue(clear_clipboard())

        run.assert_called_once_with(
            ["xclip", "-selection", "c"],
            input="",
            text=True,
            check=True,
            start_new_session=True,
        )

    def test_wayland_uses_explicit_clear_operation(self) -> None:
        with (
            patch.dict(
                "clipboard_util.os.environ",
                {"WAYLAND_DISPLAY": "wayland-0"},
                clear=True,
            ),
            patch("clipboard_util.subprocess.run") as run,
        ):
            self.assertTrue(clear_clipboard())

        run.assert_called_once_with(
            ["wl-copy", "--clear"],
            check=True,
            start_new_session=True,
        )


if __name__ == "__main__":
    unittest.main()
