from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from download_quality_images import (
    DownloadRecord,
    build_parser,
    destination_path,
    html_to_text,
    parse_retry_after,
    photo_classification,
    retry_delay,
    sha1_api_to_hex,
    validate_args,
)


class MetadataHelpersTest(unittest.TestCase):
    def test_html_to_text_preserves_visible_attribution(self) -> None:
        self.assertEqual(
            html_to_text(
                '<a href="//commons.wikimedia.org/wiki/User:Example">Jane Doe</a>'
                "<br><span>Own work</span>"
            ),
            "Jane Doe Own work",
        )

    def test_mediawiki_base36_sha1_converts_to_hex(self) -> None:
        # Integer 35 is z in base 36, padded to a normal 160-bit SHA-1 string.
        self.assertEqual(sha1_api_to_hex("z"), "0" * 38 + "23")
        self.assertIsNone(sha1_api_to_hex("not valid!"))

    def test_current_mediawiki_hex_sha1_is_preserved(self) -> None:
        digest = "4f8da32fcf435cb4871c1a05ad9abcce62e76a77"
        self.assertEqual(sha1_api_to_hex(digest), digest)

    def test_photo_filter_accepts_jpeg_and_rejects_diagram_formats(self) -> None:
        self.assertEqual(photo_classification("image/jpeg", "BITMAP"), (True, None))
        self.assertFalse(photo_classification("image/png", "BITMAP")[0])
        self.assertFalse(photo_classification("image/svg+xml", "DRAWING")[0])

    def test_retry_without_header_starts_at_five_seconds(self) -> None:
        self.assertEqual(retry_delay(None, 0), 5.0)
        self.assertEqual(parse_retry_after("60"), 60.0)

    def test_cli_rejects_more_than_three_download_workers(self) -> None:
        args = build_parser().parse_args(["download", "--workers", "4"])
        with self.assertRaisesRegex(ValueError, "between 1 and 3"):
            validate_args(args)

    def test_cli_rejects_request_rate_above_safe_cdn_ceiling(self) -> None:
        args = build_parser().parse_args(["download", "--requests-per-second", "0.6"])
        with self.assertRaisesRegex(ValueError, "no more than 0.5"):
            validate_args(args)


class DestinationPathTest(unittest.TestCase):
    def test_content_addressed_path_is_sharded(self) -> None:
        root = Path("/archive")
        digest = "0123456789abcdef0123456789abcdef01234567"
        record = DownloadRecord(
            page_id=42,
            title="File:Example.jpg",
            original_url="https://upload.wikimedia.org/example.jpg",
            byte_size=123,
            mime_type="image/jpeg",
            sha1_hex=digest,
        )
        self.assertEqual(
            destination_path(root, record),
            root / "images" / "01" / "23" / f"{digest}.jpg",
        )

    def test_destination_parent_can_be_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            digest = "f" * 40
            record = DownloadRecord(
                page_id=1,
                title="File:Photo.tif",
                original_url="https://upload.wikimedia.org/photo.tif",
                byte_size=1,
                mime_type="image/tiff",
                sha1_hex=digest,
            )
            path = destination_path(root, record)
            path.parent.mkdir(parents=True)
            self.assertTrue(path.parent.is_dir())


if __name__ == "__main__":
    unittest.main()
