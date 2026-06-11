from pathlib import Path
import tempfile
import unittest

from sync_hot_to_cold import build_rsync_command, directory_arg, validate_roots


class SyncHotToColdTest(unittest.TestCase):
    def test_directory_arg_adds_trailing_slash(self) -> None:
        self.assertEqual(directory_arg(Path("/home/example/pictures")), "/home/example/pictures/")

    def test_build_rsync_command_syncs_directory_contents_without_delete(self) -> None:
        command = build_rsync_command(
            Path("/home/example/pictures"),
            Path("/mnt/archive/pictures"),
            dry_run=False,
        )

        self.assertEqual(command[-2], "/home/example/pictures/")
        self.assertEqual(command[-1], "/mnt/archive/pictures/")
        self.assertIn("--update", command)
        self.assertNotIn("--delete", command)

    def test_build_rsync_command_supports_dry_run(self) -> None:
        command = build_rsync_command(
            Path("/home/example/pictures"),
            Path("/mnt/archive/pictures"),
            dry_run=True,
        )

        self.assertIn("--dry-run", command)

    def test_validate_roots_rejects_cold_root_inside_hot_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            hot_root = Path(temp_dir) / "pictures"
            hot_root.mkdir()
            cold_root = hot_root / "archive"

            with self.assertRaisesRegex(ValueError, "inside the hot"):
                validate_roots(hot_root, cold_root)


if __name__ == "__main__":
    unittest.main()
