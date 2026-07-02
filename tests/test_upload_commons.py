from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageCms

from upload_commons import (
    PhotoMetadata,
    _category_intersection_query,
    _count_quality_image_nominations,
    _insert_quality_image_nomination,
    _quality_image_description,
    _read_metadata,
    _refine_category_intersections,
    _remove_ancestor_duplicates,
    _remove_gps_metadata,
    _utc_date_label,
    _utc_signature,
    _upload_safe_image_path,
)


def _metadata(location_allowed: bool) -> PhotoMetadata:
    return PhotoMetadata(
        width=100,
        height=100,
        captured_at=None,
        captured_on=None,
        captured_year=None,
        make=None,
        model=None,
        lens_model=None,
        focal_length_mm=None,
        exposure_time_seconds=None,
        exposure_time_label=None,
        f_number=None,
        f_number_label=None,
        iso=None,
        latitude=37.0,
        longitude=-122.0,
        location_allowed=location_allowed,
        raw_sha1sum=None,
    )


def _srgb_profile_bytes() -> bytes:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
    return profile.tobytes()


class UploadSafeImagePathTest(unittest.TestCase):
    def test_allowed_location_uses_original_file(self) -> None:
        image_path = Path("photo.jpg")

        safe_path, temp_dir = _upload_safe_image_path(image_path, _metadata(True))

        self.assertEqual(safe_path, image_path)
        self.assertIsNone(temp_dir)

    def test_allowed_heif_uses_converted_jpeg_copy_with_metadata(self) -> None:
        source_dir = self.enterContext(tempfile.TemporaryDirectory())
        image_path = Path(source_dir) / "photo.heic"
        icc_profile = _srgb_profile_bytes()
        xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF /></x:xmpmeta>'
        image = Image.new("RGB", (20, 10), "red")
        exif = Image.Exif()
        exif[271] = "Camera Make"
        exif[272] = "Camera Model"
        exif[274] = 6
        image.save(
            image_path,
            exif=exif,
            icc_profile=icc_profile,
            xmp=xmp,
            quality=90,
        )

        with patch("builtins.print"):
            safe_path, temp_dir = _upload_safe_image_path(image_path, _metadata(True))

        self.assertIsNotNone(temp_dir)
        self.addCleanup(temp_dir.cleanup)
        self.assertNotEqual(safe_path, image_path)
        self.assertEqual(safe_path.name, "photo.jpg")
        with Image.open(safe_path) as converted:
            converted_exif = converted.getexif()
            self.assertEqual(converted.format, "JPEG")
            self.assertEqual(converted_exif.get(271), "Camera Make")
            self.assertEqual(converted_exif.get(272), "Camera Model")
            self.assertEqual(converted_exif.get(274), 6)
            self.assertEqual(converted.info.get("icc_profile"), icc_profile)
            self.assertEqual(converted.info.get("xmp"), xmp)

    def test_banned_location_uses_verified_scrubbed_copy_real_tempdir(self) -> None:
        source_dir = self.enterContext(tempfile.TemporaryDirectory())
        image_path = Path(source_dir) / "photo.jpg"
        image_path.write_bytes(b"fake image")

        with (
            patch("builtins.print"),
            patch("upload_commons._remove_gps_metadata") as scrub,
            patch(
                "upload_commons._read_metadata", return_value=_metadata(True)
            ) as read,
        ):
            safe_path, temp_dir = _upload_safe_image_path(image_path, _metadata(False))

        self.addCleanup(temp_dir.cleanup)
        self.assertNotEqual(safe_path, image_path)
        self.assertTrue(safe_path.exists())
        self.assertEqual(safe_path.read_bytes(), b"fake image")
        scrub.assert_called_once_with(safe_path)
        read.assert_called_once_with(safe_path)

    def test_banned_location_refuses_if_scrubbed_copy_still_has_banned_gps(
        self,
    ) -> None:
        source_dir = self.enterContext(tempfile.TemporaryDirectory())
        image_path = Path(source_dir) / "photo.jpg"
        image_path.write_bytes(b"fake image")

        with (
            patch("upload_commons._remove_gps_metadata"),
            patch("upload_commons._read_metadata", return_value=_metadata(False)),
        ):
            with self.assertRaisesRegex(RuntimeError, "still present"):
                _upload_safe_image_path(image_path, _metadata(False))

    def test_remove_gps_metadata_deletes_exif_gps_tags(self) -> None:
        source_dir = self.enterContext(tempfile.TemporaryDirectory())
        image_path = Path(source_dir) / "photo.jpg"
        image = Image.new("RGB", (10, 10), "red")
        exif = Image.Exif()
        exif[34853] = {
            1: "N",
            2: (37.0, 15.0, 42.0),
            3: "W",
            4: (121.0, 54.0, 54.0),
        }
        image.save(image_path, exif=exif)

        self.assertIsNotNone(_read_metadata(image_path).latitude)

        _remove_gps_metadata(image_path)

        scrubbed_metadata = _read_metadata(image_path)
        self.assertIsNone(scrubbed_metadata.latitude)
        self.assertIsNone(scrubbed_metadata.longitude)


class CategorySelectionTest(unittest.TestCase):
    class FakeCommonsApi:
        def __init__(self, details: dict[str, dict[str, object]]) -> None:
            self.details = details
            self.calls: list[tuple[list[tuple[str, str]], int]] = []

        def search_category_intersections(
            self,
            category_pairs: list[tuple[str, str]],
            limit: int,
        ) -> dict[str, dict[str, object]]:
            self.calls.append((category_pairs, limit))
            return self.details

    def test_removes_selected_ancestor_and_keeps_specific_descendants(self) -> None:
        graph = {
            "nodes": {
                "Category:Nankai 50000 series": {"parents": ["Category:Rapi:t"]},
                "Category:Rapi:t": {"parents": ["Category:Nankai Main Line"]},
                "Category:Trains at Imamiyaebisu Station": {
                    "parents": ["Category:Imamiyaebisu Station"]
                },
                "Category:Imamiyaebisu Station": {
                    "parents": ["Category:Stations of Nankai Main Line"]
                },
                "Category:Stations of Nankai Main Line": {
                    "parents": ["Category:Nankai Main Line"]
                },
                "Category:Nankai Main Line": {"parents": []},
            }
        }

        result = _remove_ancestor_duplicates(
            [
                "Nankai Main Line",
                "Nankai 50000 series",
                "Trains at Imamiyaebisu Station",
            ],
            graph,
        )

        self.assertEqual(
            result,
            ["Nankai 50000 series", "Trains at Imamiyaebisu Station"],
        )

    def test_intersection_query_uses_bounded_shared_category_search(self) -> None:
        query = _category_intersection_query(
            [("Cervus nippon (female)", 'Cervus nippon (juvenile "young")')]
        )

        self.assertEqual(
            query,
            'incategory:"Cervus nippon (female)" '
            'incategory:"Cervus nippon (juvenile \\"young\\")"',
        )

    def test_refines_selected_categories_to_supported_shared_child(self) -> None:
        graph = {
            "nodes": {
                "Category:Cervus nippon (female)": {
                    "parents": ["Category:Cervus nippon"],
                    "children": [],
                },
                "Category:Cervus nippon (juvenile)": {
                    "parents": ["Category:Cervus nippon"],
                    "children": [],
                },
                "Category:Cervus nippon": {"parents": [], "children": []},
                "Category:Deer in Nara": {
                    "parents": ["Category:Deer in Japan"],
                    "children": [],
                },
            }
        }
        api = self.FakeCommonsApi(
            {
                "Category:Cervus nippon (doe with fawn)": {
                    "parents": [
                        "Category:Cervidae (female with juvenile)",
                        "Category:Cervus nippon (female)",
                        "Category:Cervus nippon (juvenile)",
                    ],
                    "file_count": 31,
                    "subcategory_count": 0,
                }
            }
        )

        with patch("builtins.print"):
            result = _refine_category_intersections(
                api,
                [
                    "Deer in Nara",
                    "Cervus nippon (female)",
                    "Cervus nippon (juvenile)",
                ],
                graph,
                ["Sika deer (Cervus nippon) doe with fawn in Nara Park."],
            )

        self.assertEqual(
            result,
            ["Deer in Nara", "Cervus nippon (doe with fawn)"],
        )
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(
            api.calls[0][0],
            [("Cervus nippon (female)", "Cervus nippon (juvenile)")],
        )

    def test_does_not_apply_shared_child_unsupported_by_evidence(self) -> None:
        graph = {
            "nodes": {
                "Category:Cervus nippon (female)": {"parents": [], "children": []},
                "Category:Cervus nippon (juvenile)": {"parents": [], "children": []},
            }
        }
        api = self.FakeCommonsApi(
            {
                "Category:Cervus nippon in zoos": {
                    "parents": [
                        "Category:Cervus nippon (female)",
                        "Category:Cervus nippon (juvenile)",
                    ],
                    "file_count": 10,
                    "subcategory_count": 0,
                }
            }
        )

        result = _refine_category_intersections(
            api,
            ["Cervus nippon (female)", "Cervus nippon (juvenile)"],
            graph,
            ["Sika deer doe with fawn in Nara Park."],
        )

        self.assertEqual(
            result,
            ["Cervus nippon (female)", "Cervus nippon (juvenile)"],
        )


class QualityImageNominationTest(unittest.TestCase):
    def test_utc_date_label_and_signature(self) -> None:
        timestamp = datetime(2026, 5, 19, 3, 4, tzinfo=timezone.utc)

        self.assertEqual(_utc_date_label(date(2026, 5, 9)), "May 9, 2026")
        self.assertEqual(
            _utc_signature("Example", timestamp),
            "--[[User:Example|Example]] 03:04, May 19, 2026 (UTC)",
        )

    def test_count_quality_image_nominations_for_user_in_date_section(self) -> None:
        wikitext = """=Nominations=
== May 19, 2026 ==
<gallery>
File:One.jpg|{{/Nomination|One --[[User:Example|Example]] 01:00, May 19, 2026 (UTC)|}}
File:Two.jpg|{{/Promotion|Two --[[User:Example|Example]] 02:00, May 19, 2026 (UTC)|<br />{{s}} Good --[[User:Reviewer|Reviewer]] 03:00, May 19, 2026 (UTC)}}
File:Other.jpg|{{/Nomination|Other --[[User:Someone|Someone]] 02:00, May 19, 2026 (UTC)|<br />{{s}} Good --[[User:Example|Example]] 03:00, May 19, 2026 (UTC)}}
</gallery>

== May 18, 2026 ==
<gallery>
File:Old.jpg|{{/Nomination|Old --[[User:Example|Example]] 02:00, May 18, 2026 (UTC)|}}
</gallery>
"""

        self.assertEqual(
            _count_quality_image_nominations(wikitext, "Example", "May 19, 2026"),
            2,
        )

    def test_insert_quality_image_nomination_existing_section(self) -> None:
        wikitext = """=Nominations=
<!-- add nomination below this line, inside the gallery tags, in the following form &mdash; 
new nominations -->
== May 19, 2026 ==
<gallery>
File:Existing.jpg|{{/Nomination|Existing --[[User:Someone|Someone]] 01:00, May 19, 2026 (UTC)|}}
</gallery>
"""

        updated = _insert_quality_image_nomination(
            wikitext,
            "May 19, 2026",
            "File:New.jpg|{{/Nomination|New --[[User:Example|Example]] 03:04, May 19, 2026 (UTC)|}}",
        )

        self.assertIn("<gallery>\nFile:New.jpg", updated)
        self.assertLess(
            updated.index("File:New.jpg"), updated.index("File:Existing.jpg")
        )

    def test_insert_quality_image_nomination_new_utc_day_section(self) -> None:
        wikitext = """=Nominations=
<!-- add nomination below this line, inside the gallery tags, in the following form &mdash; 
new nominations -->
== May 18, 2026 ==
<gallery>
File:Existing.jpg|{{/Nomination|Existing --[[User:Someone|Someone]] 01:00, May 18, 2026 (UTC)|}}
</gallery>
"""

        updated = _insert_quality_image_nomination(
            wikitext,
            "May 19, 2026",
            "File:New.jpg|{{/Nomination|New --[[User:Example|Example]] 03:04, May 19, 2026 (UTC)|}}",
        )

        self.assertLess(
            updated.index("== May 19, 2026 =="), updated.index("== May 18, 2026 ==")
        )
        self.assertIn("== May 19, 2026 ==\n<gallery>\nFile:New.jpg", updated)

    def test_quality_image_description_is_terse_and_template_safe(self) -> None:
        description = _quality_image_description(
            "A detailed view of [[Nvidia]] DGX Spark rear ports with HDMI, Ethernet, and USB-C connectors on a wooden desk",
            "Fallback_name.jpg",
        )

        self.assertNotIn("[", description)
        self.assertNotIn("]", description)
        self.assertLessEqual(len(description.split()), 14)


if __name__ == "__main__":
    unittest.main()
