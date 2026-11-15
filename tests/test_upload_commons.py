import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from openai import OpenAI
from PIL import Image, ImageCms

from commons_categories import _build_category_graph
from config import OpenAIConfig
from upload_commons import (
    CommonsApi,
    PhotoMetadata,
    VisionClient,
    _category_intersection_query,
    _count_quality_image_nominations,
    _html_page,
    _insert_quality_image_nomination,
    _numbered_filenames,
    _quality_image_description,
    _read_metadata,
    _refine_category_intersections,
    _remove_ancestor_duplicates,
    _remove_gps_metadata,
    _upload_safe_image_path,
    _utc_date_label,
    _utc_signature,
    build_description_wikitext,
    run_app,
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

    def test_allowed_heif_uses_converted_jpeg_copy_without_original_orientation(
        self,
    ) -> None:
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
            self.assertEqual(converted.size, (20, 10))
            self.assertEqual(converted_exif.get(271), "Camera Make")
            self.assertEqual(converted_exif.get(272), "Camera Model")
            self.assertIsNone(converted_exif.get(274))
            self.assertEqual(converted.info.get("icc_profile"), icc_profile)
            self.assertEqual(converted.info.get("xmp"), xmp)

    def test_allowed_jpeg_with_orientation_uses_normalized_copy(self) -> None:
        source_dir = self.enterContext(tempfile.TemporaryDirectory())
        image_path = Path(source_dir) / "photo.jpg"
        image = Image.new("RGB", (20, 10), "red")
        exif = Image.Exif()
        exif[271] = "Camera Make"
        exif[272] = "Camera Model"
        exif[274] = 6
        image.save(image_path, exif=exif, quality=90)

        with patch("builtins.print"):
            safe_path, temp_dir = _upload_safe_image_path(image_path, _metadata(True))

        self.assertIsNotNone(temp_dir)
        self.addCleanup(temp_dir.cleanup)
        self.assertNotEqual(safe_path, image_path)
        self.assertEqual(safe_path.name, "photo.jpg")
        with Image.open(safe_path) as normalized:
            normalized_exif = normalized.getexif()
            self.assertEqual(normalized.format, "JPEG")
            self.assertEqual(normalized.size, (10, 20))
            self.assertEqual(normalized_exif.get(271), "Camera Make")
            self.assertEqual(normalized_exif.get(272), "Camera Model")
            self.assertIsNone(normalized_exif.get(274))

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


class CommonsChunkedUploadTest(unittest.TestCase):
    def _image_path(self, contents: bytes) -> Path:
        source_dir = self.enterContext(tempfile.TemporaryDirectory())
        image_path = Path(source_dir) / "photo.jpg"
        image_path.write_bytes(contents)
        return image_path

    def _api(self, responses: list[dict[str, object]]) -> CommonsApi:
        api = CommonsApi(SimpleNamespace(api_url="https://commons.example/api.php"))
        api.login = MagicMock()
        api.csrf_token = MagicMock(return_value="csrf-token")
        api.post_upload = MagicMock(side_effect=responses)
        return api

    def test_small_file_still_uses_one_direct_upload_request(self) -> None:
        image_path = self._image_path(b"tiny")
        api = self._api([{"upload": {"result": "Success"}}])

        with (
            patch.object(CommonsApi, "_CHUNKED_UPLOAD_THRESHOLD_BYTES", 5),
            patch("builtins.print"),
        ):
            url = api.upload_file(
                image_path=image_path,
                filename="Example photo.jpg",
                description_wikitext="Description text",
                summary="Upload summary",
            )

        self.assertEqual(
            url,
            "https://commons.wikimedia.org/wiki/File:Example_photo.jpg",
        )
        api.post_upload.assert_called_once()
        request = api.post_upload.call_args.kwargs
        self.assertEqual(
            request["params"],
            {
                "action": "upload",
                "filename": "Example photo.jpg",
                "comment": "Upload summary",
                "text": "Description text",
                "token": "csrf-token",
            },
        )
        self.assertEqual(request["files"]["file"][0], "Example photo.jpg")
        self.assertNotIn("chunk", request["files"])

    def test_large_file_stashes_ordered_chunks_then_commits_metadata(self) -> None:
        image_path = self._image_path(b"abcdefghij")
        api = self._api(
            [
                {
                    "upload": {
                        "result": "Continue",
                        "offset": 4,
                        "filekey": "stash-one.jpg",
                    }
                },
                {
                    "upload": {
                        "result": "Continue",
                        "offset": 8,
                        "filekey": "stash-two.jpg",
                    }
                },
                {
                    "upload": {
                        "result": "Success",
                        "filekey": "stash-final.jpg",
                    }
                },
                {"upload": {"result": "Success"}},
            ]
        )

        with (
            patch.object(CommonsApi, "_CHUNKED_UPLOAD_THRESHOLD_BYTES", 5),
            patch.object(CommonsApi, "_UPLOAD_CHUNK_SIZE_BYTES", 4),
            patch("builtins.print"),
        ):
            api.upload_file(
                image_path=image_path,
                filename="Example photo.jpg",
                description_wikitext="Description text",
                summary="Upload summary",
            )

        self.assertEqual(api.post_upload.call_count, 4)
        requests = [call.kwargs for call in api.post_upload.call_args_list]
        self.assertEqual(
            [request["files"]["chunk"][1] for request in requests[:3]],
            [b"abcd", b"efgh", b"ij"],
        )
        self.assertEqual(
            [request["params"]["offset"] for request in requests[:3]],
            [0, 4, 8],
        )
        self.assertNotIn("filekey", requests[0]["params"])
        self.assertEqual(requests[1]["params"]["filekey"], "stash-one.jpg")
        self.assertEqual(requests[2]["params"]["filekey"], "stash-two.jpg")
        for request in requests[:3]:
            self.assertEqual(request["params"]["filesize"], 10)
            self.assertEqual(request["params"]["stash"], "1")
            self.assertEqual(request["params"]["ignorewarnings"], "1")

        self.assertNotIn("files", requests[3])
        self.assertEqual(
            requests[3]["params"],
            {
                "action": "upload",
                "filename": "Example photo.jpg",
                "comment": "Upload summary",
                "text": "Description text",
                "token": "csrf-token",
                "filekey": "stash-final.jpg",
            },
        )

    def test_filename_collision_preserves_existing_photo(self) -> None:
        image_path = self._image_path(b"a different photo")
        filename = "Example photo.jpg"

        for chunked in (False, True):
            with self.subTest(chunked=chunked):
                existing_contents = b"the original photo"
                remote_files = {filename: existing_contents}
                stashed_contents = bytearray()
                api = self._api([])

                def respond(params, files=None):
                    if "stash" in params:
                        stashed_contents.extend(files["chunk"][1])
                        return {
                            "upload": {"result": "Success", "filekey": "stash.jpg"}
                        }
                    # Match MediaWiki's boolean semantics: even "0" means true.
                    if (
                        params["filename"] in remote_files
                        and "ignorewarnings" not in params
                    ):
                        return {
                            "upload": {
                                "result": "Warning",
                                "warnings": {"exists": params["filename"]},
                                "filekey": "stash.jpg",
                            }
                        }
                    remote_files[params["filename"]] = (
                        bytes(stashed_contents)
                        if "filekey" in params
                        else files["file"][1].read()
                    )
                    return {"upload": {"result": "Success"}}

                api.post_upload.side_effect = respond
                with (
                    patch.object(
                        CommonsApi,
                        "_CHUNKED_UPLOAD_THRESHOLD_BYTES",
                        1 if chunked else 100,
                    ),
                    patch.object(CommonsApi, "_UPLOAD_CHUNK_SIZE_BYTES", 100),
                    patch("builtins.print"),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "Refusing to overwrite.*Choose a different filename",
                    ):
                        api.upload_file(
                            image_path=image_path,
                            filename=filename,
                            description_wikitext="Description text",
                            summary="Upload summary",
                        )

                self.assertEqual(remote_files, {filename: existing_contents})
                self.assertEqual(api.post_upload.call_count, 2 if chunked else 1)

    def test_other_upload_warnings_are_not_ignored_or_retried(self) -> None:
        image_path = self._image_path(b"tiny")
        api = self._api(
            [
                {
                    "upload": {
                        "result": "Warning",
                        "warnings": {"duplicate": ["Another photo.jpg"]},
                        "filekey": "stash.jpg",
                    }
                }
            ]
        )

        with patch("builtins.print"):
            with self.assertRaisesRegex(RuntimeError, "Commons upload failed.*duplicate"):
                api.upload_file(
                    image_path=image_path,
                    filename="Example photo.jpg",
                    description_wikitext="Description text",
                    summary="Upload summary",
                )

        api.post_upload.assert_called_once()
        self.assertNotIn("ignorewarnings", api.post_upload.call_args.kwargs["params"])

    def test_small_explicit_overwrite_ignores_warnings(self) -> None:
        image_path = self._image_path(b"tiny")
        api = self._api([{"upload": {"result": "Success"}}])

        with patch("builtins.print"):
            api.overwrite_file(
                image_path=image_path,
                title="File:Existing photo.jpg",
                summary="Overwrite summary",
            )

        api.post_upload.assert_called_once()
        request = api.post_upload.call_args.kwargs
        self.assertEqual(request["params"]["ignorewarnings"], "1")
        self.assertEqual(request["params"]["filename"], "Existing photo.jpg")
        self.assertNotIn("text", request["params"])

    def test_chunk_upload_refuses_an_unexpected_server_offset(self) -> None:
        image_path = self._image_path(b"abcdefghij")
        api = self._api(
            [
                {
                    "upload": {
                        "result": "Continue",
                        "offset": 3,
                        "filekey": "stash-one.jpg",
                    }
                }
            ]
        )

        with (
            patch.object(CommonsApi, "_CHUNKED_UPLOAD_THRESHOLD_BYTES", 5),
            patch.object(CommonsApi, "_UPLOAD_CHUNK_SIZE_BYTES", 4),
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected offset"):
                api.upload_file(
                    image_path=image_path,
                    filename="Example photo.jpg",
                    description_wikitext="Description text",
                    summary="Upload summary",
                )

        api.post_upload.assert_called_once()

    def test_large_overwrite_commits_without_new_description_text(self) -> None:
        image_path = self._image_path(b"abcde")
        api = self._api(
            [
                {
                    "upload": {
                        "result": "Success",
                        "filekey": "overwrite-stash.jpg",
                    }
                },
                {"upload": {"result": "Success"}},
            ]
        )

        with (
            patch.object(CommonsApi, "_CHUNKED_UPLOAD_THRESHOLD_BYTES", 5),
            patch.object(CommonsApi, "_UPLOAD_CHUNK_SIZE_BYTES", 5),
            patch("builtins.print"),
        ):
            url = api.overwrite_file(
                image_path=image_path,
                title="File:Existing photo.jpg",
                summary="Overwrite summary",
            )

        self.assertEqual(
            url,
            "https://commons.wikimedia.org/wiki/File:Existing_photo.jpg",
        )
        final_request = api.post_upload.call_args_list[1].kwargs
        self.assertNotIn("files", final_request)
        self.assertEqual(
            final_request["params"],
            {
                "action": "upload",
                "filename": "Existing photo.jpg",
                "comment": "Overwrite summary",
                "ignorewarnings": "1",
                "token": "csrf-token",
                "filekey": "overwrite-stash.jpg",
            },
        )


class HtmlPageTest(unittest.TestCase):
    def test_post_actions_show_local_server_error_on_fetch_failure(self) -> None:
        html = _html_page({"metadata": _metadata(True).to_model_dict()})
        self.assertIn('src="/static/commons_upload.js" defer', html)
        script = (Path(__file__).resolve().parents[1] / "static/commons_upload.js").read_text()
        self.assertIn("serverUnavailableMessage", script)
        self.assertIn("The local upload server is no longer responding", script)
        self.assertIn('await postJson("/analyze"', script)
        self.assertIn('await postJson("/submit"', script)
        self.assertIn('await postJson("/overwrite"', script)
        self.assertEqual(script.count("await fetch("), 1)

    def test_group_page_renders_every_preview_and_batch_editor(self) -> None:
        metadata = _metadata(True).to_model_dict()

        html = _html_page(
            {
                "photos": [
                    {
                        "index": 0,
                        "source_name": "first.jpg",
                        "metadata": metadata,
                        "sha1_matches": [],
                    },
                    {
                        "index": 1,
                        "source_name": "second.jpg",
                        "metadata": metadata,
                        "sha1_matches": [],
                    },
                ]
            }
        )

        self.assertIn('src="/image/0"', html)
        self.assertIn('src="/image/1"', html)
        self.assertIn('"photoCount": 2', html)
        self.assertIn('<details class="meta" open>', html)
        self.assertIn('id="photo-editor-template"', html)

    def test_template_escapes_filenames_metadata_and_overwrite_attributes(self) -> None:
        html = _html_page({
            "photos": [{
                "index": 0,
                "source_name": '<script>alert("source")</script>.jpg',
                "metadata": {"model": "</pre><script>alert('metadata')</script>"},
                "raw_sha1sum": "<raw-sha1>",
                "sha1_matches": [{
                    "title": 'File:Photo " onmouseover="alert(1)',
                    "url": 'https://commons.wikimedia.org/wiki/File:Photo?x="bad"&y=1',
                }],
            }],
        })
        self.assertNotIn('<script>alert(', html)
        self.assertNotIn('data-file-title="File:Photo " onmouseover=', html)
        self.assertIn('&lt;script&gt;alert(&#34;source&#34;)&lt;/script&gt;.jpg', html)
        self.assertIn('data-file-title="File:Photo &#34; onmouseover=&#34;alert(1)"', html)
        self.assertIn('&lt;raw-sha1&gt;', html)

    def test_quality_image_limit_disables_nominations_in_client_config(self) -> None:
        html = _html_page({
            "quality_image_nominations_today": 5,
            "quality_image_nomination_limit": 5,
            "quality_image_nomination_date": "7 November 2026",
        })
        self.assertIn('"qualityImageDisabled": true', html)
        self.assertIn('5/5 Quality Image nominations already listed for 7 November 2026 UTC.', html)


class VisionModelConfigTest(unittest.TestCase):
    def test_defaults_to_sol_and_preserves_explicit_model_override(self) -> None:
        values = {"api_key": "test-key", "image_detail": "high", "downsized_max_dimension": 1600}
        self.assertEqual(OpenAIConfig.from_dict(values, Path(".")).vision_model, "gpt-6.1-sol")
        self.assertEqual(
            OpenAIConfig.from_dict({**values, "vision_model": "custom-model"}, Path(".")).vision_model,
            "custom-model",
        )


class GroupFilenameTest(unittest.TestCase):
    def test_group_names_are_ordered_with_number_before_suffix(self) -> None:
        filenames = _numbered_filenames(
            "Sika deer in Nara Park",
            "by Example",
            [".JPG", ".jpeg", ".png"],
        )

        self.assertEqual(
            filenames,
            [
                "Sika deer in Nara Park 01 by Example.jpg",
                "Sika deer in Nara Park 02 by Example.jpeg",
                "Sika deer in Nara Park 03 by Example.png",
            ],
        )

    def test_single_photo_name_remains_unnumbered(self) -> None:
        self.assertEqual(
            _numbered_filenames("Sika deer", "by Example", [".JPG"]),
            ["Sika deer by Example.jpg"],
        )


class BatchDescriptionTest(unittest.TestCase):
    def test_description_places_ordered_batch_gallery_after_information(self) -> None:
        description = build_description_wikitext(
            caption="Front view of a sika deer in Nara Park.",
            metadata=_metadata(True),
            author="Example",
            license_wikitext="{{self|cc-by-sa-4.0}}",
            categories=["Deer in Nara"],
            batch_filenames=[
                "Sika deer 01 by Example.jpg",
                "Sika deer 02 by Example.jpg",
            ],
        )

        gallery_start = description.index('<gallery mode="packed"')
        self.assertIn("}}\n<gallery", description)
        self.assertLess(gallery_start, description.index("{{Location|"))
        self.assertLess(gallery_start, description.index("=={{int:license-header}}=="))
        self.assertLess(
            description.index("File:Sika deer 01 by Example.jpg"),
            description.index("File:Sika deer 02 by Example.jpg"),
        )

    def test_single_photo_description_does_not_add_gallery(self) -> None:
        description = build_description_wikitext(
            caption="A sika deer in Nara Park.",
            metadata=_metadata(True),
            author="Example",
            license_wikitext="{{self|cc-by-sa-4.0}}",
            categories=["Deer in Nara"],
            batch_filenames=["Sika deer by Example.jpg"],
        )

        self.assertNotIn("<gallery", description)


class GroupVisionRequestTest(unittest.TestCase):
    def test_proposal_sends_all_photos_in_order_with_exact_length_schema(self) -> None:
        client = VisionClient.__new__(VisionClient)
        client.config = SimpleNamespace(vision_model="test-model", image_detail="high")
        client.client = MagicMock()
        client.client.responses.create.return_value = SimpleNamespace(
            output_text=(
                '{"group_filename_stem":"Sika deer in Nara Park",'
                '"photos":['
                '{"caption_en":"First view.","visual_summary":"Front view."},'
                '{"caption_en":"Second view.","visual_summary":"Side view."}],'
                '"keywords":["sika deer"],"search_queries":["Deer in Nara"],'
                '"relationship_queries":[],"group_summary":"Two related views.",'
                '"research_summary":""}'
            )
        )

        with patch("builtins.print"):
            result = client.propose_group_metadata(
                ["data:image/jpeg;base64,first", "data:image/jpeg;base64,second"],
                [_metadata(True), _metadata(True)],
                "by Example",
                "Nara Park",
                ["first.jpg", "second.jpg"],
            )

        self.assertEqual(len(result["photos"]), 2)
        request_kwargs = client.client.responses.create.call_args.kwargs
        self.assertEqual(request_kwargs["model"], "test-model")
        self.assertEqual(request_kwargs["reasoning"], {"effort": "low"})
        user_content = request_kwargs["input"][1]["content"]
        image_urls = [
            item["image_url"]
            for item in user_content
            if item["type"] == "input_image"
        ]
        self.assertEqual(
            image_urls,
            ["data:image/jpeg;base64,first", "data:image/jpeg;base64,second"],
        )
        schema = request_kwargs["text"]["format"]["schema"]
        self.assertEqual(schema["properties"]["photos"]["minItems"], 2)
        self.assertEqual(schema["properties"]["photos"]["maxItems"], 2)

    def test_category_selection_considers_all_photos_together(self) -> None:
        client = VisionClient.__new__(VisionClient)
        client.config = SimpleNamespace(vision_model="test-model", image_detail="high")
        client.client = MagicMock()
        client.client.post.return_value = {
            "answers": [
                {"type": "predicate", "name": "photo_1_category_1", "probability": 0.9},
                {"type": "predicate", "name": "photo_2_category_1", "probability": 0.8},
            ]
        }

        with patch("builtins.print"):
            result = client.choose_group_categories(
                image_data_urls=[
                    "data:image/jpeg;base64,first",
                    "data:image/jpeg;base64,second",
                ],
                metadatas=[_metadata(True), _metadata(True)],
                proposed_photos=[
                    {
                        "caption_en": "First view.",
                        "visual_summary": "Front view.",
                    },
                    {
                        "caption_en": "Second view.",
                        "visual_summary": "Side view.",
                    },
                ],
                group_summary="Two related views.",
                research_summary="",
                category_graph={
                    "nodes": {
                        "Category:Deer in Nara": {
                            "file_count": 10,
                            "subcategory_count": 0,
                            "parents": [],
                            "children": [],
                        }
                    }
                },
                user_hint="Nara Park",
            )

        self.assertEqual(
            result["photo_categories"],
            [["Category:Deer in Nara"], ["Category:Deer in Nara"]],
        )
        client.client.responses.create.assert_not_called()
        self.assertEqual(client.client.post.call_args.args, ("/decisions",))
        request_body = client.client.post.call_args.kwargs["body"]
        self.assertEqual(request_body["model"], "gpt-6-luna")
        self.assertEqual([item["role"] for item in request_body["input"]], ["user"])
        self.assertEqual(
            sum(
                item["type"] == "input_image"
                for item in request_body["input"][0]["content"]
            ),
            2,
        )
        self.assertEqual(
            [question["name"] for question in request_body["questions"]],
            ["photo_1_category_1", "photo_2_category_1"],
        )
        self.assertTrue(
            all(question["type"] == "predicate" for question in request_body["questions"])
        )


class DecisionCategorySelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = VisionClient.__new__(VisionClient)
        self.client.config = SimpleNamespace(vision_model="test-model", image_detail="high")
        self.client.client = MagicMock()
        self.graph = {
            "nodes": {
                "Category:Deer in Nara": {"file_count": 10},
                "Category:Side views of deer": {"file_count": 5},
                "Category:Empty category": {"file_count": 0},
            }
        }

    def _choose(self, photo_count: int = 2) -> dict[str, object]:
        with patch("builtins.print"):
            return self.client.choose_group_categories(
                image_data_urls=["data:image/jpeg;base64,test"] * photo_count,
                metadatas=[_metadata(True)] * photo_count,
                proposed_photos=[
                    {"caption_en": "A deer.", "visual_summary": "Deer in Nara Park."}
                ] * photo_count,
                group_summary="Related views of deer.",
                research_summary="",
                category_graph=self.graph,
                user_hint="Nara Park",
            )

    def test_threshold_allows_multiple_categories_and_distinguishes_photos(self) -> None:
        self.client.client.post.return_value = {
            "answers": [
                {"type": "predicate", "name": "photo_2_category_1", "probability": 0.9},
                {"type": "predicate", "name": "photo_2_category_2", "probability": 0.49},
                {"type": "predicate", "name": "photo_1_category_1", "probability": 1},
                {"type": "predicate", "name": "photo_1_category_2", "probability": 0.5},
            ]
        }

        result = self._choose()

        self.assertEqual(result["photo_categories"], [
            ["Category:Deer in Nara", "Category:Side views of deer"],
            ["Category:Deer in Nara"],
        ])
        questions = self.client.client.post.call_args.kwargs["body"]["questions"]
        self.assertEqual(len(questions), 4)
        self.assertFalse(any("Empty category" in item["instructions"] for item in questions))

    def test_single_photo_wrapper_works_with_installed_sdk_http_transport(self) -> None:
        request_bodies: list[dict[str, object]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/v1/decisions")
            request_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={
                "answers": [
                    {"type": "predicate", "name": "photo_1_category_1", "probability": 0.9},
                    {"type": "predicate", "name": "photo_1_category_2", "probability": 0.1},
                ]
            })

        with OpenAI(
            api_key="test-key",
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        ) as api_client:
            self.client.client = api_client
            with patch("builtins.print"):
                result = self.client.choose_categories(
                    image_data_url="data:image/jpeg;base64,test",
                    metadata=_metadata(True),
                    proposed_caption="A deer.",
                    proposed_summary="Deer in Nara Park.",
                    research_summary="",
                    category_graph=self.graph,
                    user_hint="Nara Park",
                )

        self.assertEqual(result["selected_categories"], ["Category:Deer in Nara"])
        self.assertEqual(len(request_bodies), 1)
        self.assertEqual(request_bodies[0]["model"], "gpt-6-luna")

    def test_no_candidates_skips_decisions_request(self) -> None:
        for nodes in ({}, {"Category:Empty category": {"file_count": 0}}):
            with self.subTest(nodes=nodes):
                self.graph = {"nodes": nodes}
                self.assertEqual(self._choose()["photo_categories"], [[], []])
        self.client.client.post.assert_not_called()

    def test_large_category_selections_use_bounded_requests_with_shared_context(self) -> None:
        cases = ((1, 400), (1, 401), (1, 448), (2, 224), (4, 224))
        for photo_count, category_count in cases:
            with self.subTest(photo_count=photo_count, category_count=category_count):
                titles = [
                    f"Category:Candidate {index}"
                    for index in range(1, category_count + 1)
                ]
                self.graph = {"nodes": {title: {"file_count": 1} for title in titles}}
                request_bodies = []

                def handle(request: httpx.Request) -> httpx.Response:
                    body = json.loads(request.content)
                    request_bodies.append(body)
                    questions = body["questions"]
                    self.assertGreater(len(questions), 0)
                    self.assertLessEqual(len(questions), 400)
                    answers = []
                    for question in reversed(questions):
                        photo_number, category_number = (
                            int(part) for part in question["name"].split("_")[1::2]
                        )
                        if category_number == 2:
                            answers.append({"type": "refusal", "name": question["name"]})
                        else:
                            answers.append({
                                "type": "predicate",
                                "name": question["name"],
                                "probability": (
                                    0.5 if category_number % photo_number == 0 else 0.49
                                ),
                            })
                    return httpx.Response(200, json={"answers": answers})

                with OpenAI(
                    api_key="test-key",
                    max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handle)),
                ) as api_client:
                    self.client.client = api_client
                    result = self._choose(photo_count=photo_count)

                question_count = photo_count * category_count
                self.assertEqual(
                    [len(body["questions"]) for body in request_bodies],
                    [
                        min(400, question_count - start)
                        for start in range(0, question_count, 400)
                    ],
                )
                self.assertEqual(
                    [
                        question["name"]
                        for body in request_bodies
                        for question in body["questions"]
                    ],
                    [
                        f"photo_{photo}_category_{category}"
                        for photo in range(1, photo_count + 1)
                        for category in range(1, category_count + 1)
                    ],
                )
                for body in request_bodies:
                    self.assertEqual(body["input"], request_bodies[0]["input"])
                    self.assertEqual(
                        sum(
                            item["type"] == "input_image"
                            for item in body["input"][0]["content"]
                        ),
                        photo_count,
                    )
                self.assertEqual(len(result["photo_categories"]), photo_count)
                for photo_number, categories in enumerate(
                    result["photo_categories"], start=1
                ):
                    self.assertCountEqual(categories, [
                        title for index, title in enumerate(titles, start=1)
                        if index != 2 and index % photo_number == 0
                    ])
                self.assertIn(
                    f"Skipped {photo_count} refused", result["reasoning_summary"]
                )

    def test_each_batch_validates_its_own_answers(self) -> None:
        self.graph = {"nodes": {
            f"Category:Candidate {index}": {"file_count": 1}
            for index in range(1, 402)
        }}
        valid_answers = [
            {"type": "predicate", "name": f"photo_1_category_{index}", "probability": 0.9}
            for index in range(1, 401)
        ]
        for answers in (
            valid_answers[:-1],
            valid_answers[:-1] + [valid_answers[0]],
            valid_answers[:-1] + [{**valid_answers[-1], "name": "photo_1_category_401"}],
        ):
            with self.subTest(last_answer=answers[-1]["name"], answer_count=len(answers)):
                self.client.client.post.reset_mock()
                self.client.client.post.return_value = {"answers": answers}
                with self.assertRaisesRegex(RuntimeError, "unexpected"):
                    self._choose(photo_count=1)
                self.client.client.post.assert_called_once()

    def test_refused_questions_are_skipped(self) -> None:
        self.client.client.post.return_value = {
            "answers": [
                {"type": "refusal", "name": "photo_1_category_1"},
                {"type": "predicate", "name": "photo_1_category_2", "probability": 0.9},
            ]
        }

        result = self._choose(photo_count=1)

        self.assertEqual(result["photo_categories"], [["Category:Side views of deer"]])
        self.assertIn("Skipped 1 refused", result["reasoning_summary"])

    def test_missing_duplicate_or_unknown_answers_fail(self) -> None:
        valid_answer = {
            "type": "predicate", "name": "photo_1_category_1", "probability": 0.9,
        }
        for answers in (
            [],
            [valid_answer],
            [valid_answer, valid_answer],
            [valid_answer, {**valid_answer, "name": "unknown"}],
        ):
            with self.subTest(answers=answers):
                self.client.client.post.return_value = {"answers": answers}
                with self.assertRaisesRegex(RuntimeError, "unexpected"):
                    self._choose(photo_count=1)

    def test_invalid_probabilities_and_answer_types_fail(self) -> None:
        for probability in (None, "0.9", True, -0.1, 1.1, float("nan")):
            with self.subTest(probability=probability):
                self.client.client.post.return_value = {
                    "answers": [
                        {"type": "predicate", "name": "photo_1_category_1", "probability": probability},
                        {"type": "refusal", "name": "photo_1_category_2"},
                    ]
                }
                with self.assertRaisesRegex(RuntimeError, "invalid category probability"):
                    self._choose(photo_count=1)
        self.client.client.post.return_value = {
            "answers": [
                {"type": "choice", "name": "photo_1_category_1", "probability": 0.9},
                {"type": "refusal", "name": "photo_1_category_2"},
            ]
        }
        with self.assertRaisesRegex(RuntimeError, "invalid category probability"):
            self._choose(photo_count=1)

    def test_too_many_images_fails_before_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "at most 128 photos"):
            self._choose(photo_count=129)
        self.client.client.post.assert_not_called()


class CategoryReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = VisionClient.__new__(VisionClient)
        self.client.config = SimpleNamespace(vision_model="test-model", image_detail="high")
        self.client.client = MagicMock()
        self.model = "Category:Toyota Camry (XV10)"
        self.white_sedans = "Category:White Toyota sedans"
        self.white = "Category:White automobiles in California"
        self.parked = "Category:Parked automobiles in California"
        self.transport = "Category:Transport in San Jose, California"
        self.city = "Category:San Jose, California"
        self.categories = [
            [self.city, "Category:Sedans", self.model, self.white_sedans,
             self.white, self.parked, self.transport],
            [self.model, "Category:Black automobiles in California", self.city],
        ]

    def _review(self, photos: list[object]) -> dict[str, object]:
        self.client.client.responses.create.return_value = SimpleNamespace(
            output_text=json.dumps({"photos": photos, "reasoning_summary": "Specific facets."})
        )
        with patch("builtins.print"):
            return self.client.review_group_categories(
                image_data_urls=["data:image/jpeg;base64,white", "data:image/jpeg;base64,black"],
                metadatas=[_metadata(True), _metadata(False)],
                proposed_photos=[
                    {"caption_en": "A white Camry.", "visual_summary": "Parked white sedan."},
                    {"caption_en": "A black Camry.", "visual_summary": "Black sedan."},
                ],
                photo_categories=self.categories,
                group_summary="Two cars in San Jose.",
                research_summary="",
                category_graph={"nodes": {self.model: {"parents": ["Category:Toyota Camry"]}}},
                user_hint="San Jose, California",
            )

    def test_review_keeps_independent_facets_in_their_original_photos(self) -> None:
        retained = [self.model, self.white_sedans, self.white, self.parked, self.transport]
        result = self._review([
            {"selected_categories": list(reversed(retained))},
            {"selected_categories": self.categories[1][:-1]},
        ])

        self.assertEqual(result["photo_categories"], [retained, self.categories[1][:-1]])
        kwargs = self.client.client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "test-model")
        content = kwargs["input"][1]["content"]
        self.assertEqual(
            [item["image_url"] for item in content if item["type"] == "input_image"],
            ["data:image/jpeg;base64,white", "data:image/jpeg;base64,black"],
        )
        context = json.loads(content[0]["text"])
        self.assertEqual([photo["categories"] for photo in context["photos"]], self.categories)
        self.assertIsNone(context["photos"][1]["metadata"]["latitude"])
        schema = kwargs["text"]["format"]["schema"]["properties"]["photos"]
        self.assertEqual(schema["minItems"], 2)
        self.assertEqual(schema["maxItems"], 2)

    def test_review_rejects_invented_or_transferred_categories_and_bad_counts(self) -> None:
        for photos in (
            [],
            [{"selected_categories": [self.model]}],
            [{"selected_categories": ["Category:Invented"]}, {"selected_categories": []}],
            [{"selected_categories": [self.categories[1][1]]}, {"selected_categories": []}],
            [{"selected_categories": [None]}, {"selected_categories": []}],
        ):
            with self.subTest(photos=photos):
                with self.assertRaises(RuntimeError):
                    self._review(photos)

    def test_empty_selection_skips_review_request(self) -> None:
        self.categories = [[], []]
        self.assertEqual(self._review([])["photo_categories"], [[], []])
        self.client.client.responses.create.assert_not_called()


class BatchUploadFlowTest(unittest.TestCase):
    class FakeVisionClient:
        def __init__(self, _config: object) -> None:
            pass

        def propose_group_metadata(self, **kwargs: object) -> dict[str, object]:
            image_data_urls = kwargs["image_data_urls"]
            assert isinstance(image_data_urls, list)
            if len(image_data_urls) not in (1, 2):
                raise AssertionError("Expected one or two photos in the proposal request")
            return {
                "group_filename_stem": "Sika deer in Nara Park",
                "photos": [
                    {
                        "caption_en": "Front view of a sika deer in Nara Park.",
                        "visual_summary": "Front view of the deer.",
                    },
                    {
                        "caption_en": "Side view of a sika deer in Nara Park.",
                        "visual_summary": "Side view of the deer.",
                    },
                ][:len(image_data_urls)],
                "keywords": ["sika deer", "Nara Park"],
                "search_queries": ["Deer in Nara"],
                "relationship_queries": [],
                "group_summary": "Two views of the same subject and place.",
                "research_summary": "",
            }

        def choose_group_categories(self, **kwargs: object) -> dict[str, object]:
            image_data_urls = kwargs["image_data_urls"]
            assert isinstance(image_data_urls, list)
            return {
                "photo_categories": [["Category:Deer in Nara"] for _ in image_data_urls],
                "reasoning_summary": "The category applies to the whole group.",
            }

        def review_group_categories(self, **kwargs: object) -> dict[str, object]:
            categories = kwargs["photo_categories"]
            assert len(categories) == len(kwargs["image_data_urls"])
            assert categories == [["Category:Deer in Nara"] for _ in categories]
            return {"photo_categories": categories, "reasoning_summary": "Specific category."}

    class FakeCommonsApi:
        last_instance: "BatchUploadFlowTest.FakeCommonsApi | None" = None

        def __init__(self, _config: object) -> None:
            self.uploads: list[dict[str, object]] = []
            self.__class__.last_instance = self

        def login(self) -> None:
            pass

        def quality_image_nomination_count(
            self, _username: str, _nomination_date: date
        ) -> int:
            return 0

        def existing_category_titles(self, _titles: list[str]) -> list[str]:
            return []

        def upload_file(self, **kwargs: object) -> str:
            self.uploads.append(kwargs)
            filename = str(kwargs["filename"])
            return f"https://commons.wikimedia.org/wiki/File:{filename}"

    def test_review_and_submit_uploads_the_ordered_group(self) -> None:
        self._check_review_and_submit(photo_count=2)

    def test_single_photo_keeps_legacy_review_and_submit_contracts(self) -> None:
        self._check_review_and_submit(photo_count=1)

    def _check_review_and_submit(self, photo_count: int) -> None:
        source_dir = self.enterContext(tempfile.TemporaryDirectory())
        first_path = Path(source_dir) / "first.jpg"
        second_path = Path(source_dir) / "second.jpg"
        Image.new("RGB", (12, 10), "red").save(first_path)
        Image.new("RGB", (12, 10), "blue").save(second_path)
        commons_config = SimpleNamespace(
            username="Example@Uploader",
            password="secret",
            author="Example",
            license_wikitext="{{self|cc-by-sa-4.0}}",
            filename_suffix="by Example",
            search_limit_per_query=8,
            max_candidate_categories=40,
            max_parent_depth=3,
            ui_host="127.0.0.1",
        )
        openai_config = SimpleNamespace(
            api_key="test-key",
            vision_model="test-model",
            image_detail="high",
            downsized_max_dimension=1600,
        )
        app_config = SimpleNamespace(
            commons=commons_config,
            openai=openai_config,
        )
        category_graph = {
            "nodes": {
                "Category:Deer in Nara": {
                    "file_count": 10,
                    "subcategory_count": 0,
                    "parents": [],
                    "children": [],
                }
            }
        }
        captured: dict[str, object] = {}

        def capture_run(app: object, **_kwargs: object) -> None:
            captured["app"] = app

        with (
            patch("upload_commons.load_config", return_value=app_config),
            patch("upload_commons.VisionClient", self.FakeVisionClient),
            patch("upload_commons.CommonsApi", self.FakeCommonsApi),
            patch("upload_commons._build_category_graph", return_value=category_graph),
            patch("upload_commons._free_port", return_value=54321),
            patch("upload_commons.Flask.run", new=capture_run),
            patch("upload_commons.threading.Thread"),
            patch("upload_commons._schedule_exit") as schedule_exit,
            patch("builtins.print"),
        ):
            run_app([first_path, second_path][:photo_count])
            app = captured["app"]
            client = app.test_client()

            self.assertEqual(client.get("/").status_code, 200)
            for asset in ("commons_upload.css", "commons_upload.js"):
                with client.get(f"/static/{asset}") as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.data)
            analyze_response = client.post("/analyze", json={"hint": "Nara Park"})
            self.assertEqual(analyze_response.status_code, 200)
            review = analyze_response.get_json()
            expected_filenames = (
                ["Sika deer in Nara Park by Example.jpg"] if photo_count == 1 else [
                    "Sika deer in Nara Park 01 by Example.jpg",
                    "Sika deer in Nara Park 02 by Example.jpg",
                ]
            )
            self.assertEqual(
                [photo["filename"] for photo in review["photos"]], expected_filenames
            )
            if photo_count == 1:
                self.assertEqual(review["filename"], review["photos"][0]["filename"])
                self.assertEqual(review["caption"], review["photos"][0]["caption"])
                self.assertEqual(review["categories"], review["photos"][0]["categories"])

            submit_response = client.post(
                "/submit",
                json=review if photo_count == 1 else {"photos": review["photos"]},
            )

        self.assertEqual(submit_response.status_code, 200)
        upload_result = submit_response.get_json()
        self.assertEqual(len(upload_result["uploaded_files"]), photo_count)
        api = self.FakeCommonsApi.last_instance
        self.assertIsNotNone(api)
        self.assertEqual(
            [upload["filename"] for upload in api.uploads],
            expected_filenames,
        )
        self.assertIn("Front view", api.uploads[0]["description_wikitext"])
        if photo_count == 2:
            self.assertIn("Side view", api.uploads[1]["description_wikitext"])
        self.assertIn("[[Category:Deer in Nara]]", api.uploads[0]["description_wikitext"])
        for upload in api.uploads:
            description = upload["description_wikitext"]
            if photo_count == 2:
                for filename in expected_filenames:
                    self.assertIn(f"File:{filename}", description)
            else:
                self.assertNotIn("<gallery", description)
        schedule_exit.assert_called_once_with()


class CategoryGraphTest(unittest.TestCase):
    def test_exact_facet_titles_take_priority_and_only_unresolved_queries_are_searched(self) -> None:
        exact = [
            "Category:Toyota Camry (XV10)",
            "Category:White Toyota sedans",
            "Category:White automobiles in California",
            "Category:Parked automobiles in California",
        ]
        api = MagicMock()
        api.existing_category_titles.return_value = exact
        api.search_categories.return_value = ["Category:Toyota Camry", "Category:Sedans"]
        api.category_details.side_effect = lambda titles: {
            title: {"parents": [], "file_count": 10} for title in titles
        }
        queries = [title.removeprefix("Category:") for title in exact]

        with patch("builtins.print"):
            graph = _build_category_graph(
                api, queries + ["1994 Toyota Camry"], 8, 4, max_parent_depth=0
            )

        self.assertEqual(set(graph["nodes"]), set(exact))
        api.search_categories.assert_called_once_with("1994 Toyota Camry", 8)
        for title in exact:
            self.assertEqual(graph["nodes"][title]["source"], "exact")

    def test_boundary_categories_keep_parent_edges_for_ancestor_cleanup(self) -> None:
        county = "Category:Geography of Santa Clara County, California"
        bay = "Category:Geography of the San Francisco Bay Area"
        seed = "Category:Local park views"
        details = {
            seed: {"parents": [county, bay], "file_count": 10},
            county: {"parents": [bay], "file_count": 3},
            bay: {"parents": ["Category:Geography of California by location"], "file_count": 6},
        }
        api = MagicMock()
        api._MAX_CHILD_CATEGORY_LOOKUPS = 0
        api.search_categories.return_value = [seed]
        api.existing_category_titles.return_value = []
        api.category_subcategories.return_value = {}
        api.category_details.side_effect = lambda titles: {title: details[title] for title in titles}
        api.category_counts.return_value = {county: {"file_count": 3}, bay: {"file_count": 6}}

        with patch("builtins.print"):
            graph = _build_category_graph(api, ["local park"], 8, 40, max_parent_depth=1)

        selected = _remove_ancestor_duplicates(
            [county.removeprefix("Category:"), bay.removeprefix("Category:")], graph
        )
        self.assertEqual(selected, [county.removeprefix("Category:")])
        # Completing boundary edges must not add another layer of candidates.
        self.assertEqual(set(graph["nodes"]), {seed, county, bay})
        self.assertEqual(graph["nodes"][county]["file_count"], 3)

    def test_category_details_merge_paginated_parents_and_reset_between_batches(self) -> None:
        county = "Category:Geography of Santa Clara County, California"
        bay = "Category:Geography of the San Francisco Bay Area"
        state = "Category:Geography of California by county"
        api = CommonsApi.__new__(CommonsApi)
        api._TITLE_BATCH_SIZE = 1
        api.get = MagicMock(side_effect=[
            {
                "query": {"pages": [{
                    "title": county,
                    "categories": [{"title": state}],
                    "categoryinfo": {"files": 3, "subcats": 7},
                }]},
                "continue": {"clcontinue": "next-parent", "continue": "||"},
            },
            {"query": {"pages": [{
                "title": county,
                "categories": [{"title": state}, {"title": bay}],
            }]}},
            {"query": {"pages": [{
                "title": bay,
                "categoryinfo": {"files": 6, "subcats": 24},
            }]}},
        ])

        details = api.category_details([county, bay])

        self.assertEqual(details[county], {
            "parents": [state, bay], "file_count": 3, "subcategory_count": 7,
        })
        self.assertEqual(details[bay], {
            "parents": [], "file_count": 6, "subcategory_count": 24,
        })
        self.assertEqual(api.get.call_args_list[1].kwargs["clcontinue"], "next-parent")
        self.assertNotIn("clcontinue", api.get.call_args_list[2].kwargs)


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
