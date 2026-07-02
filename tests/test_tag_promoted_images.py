from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tag_promoted_images import (
    CommonsApi,
    FileCategoryStatus,
    _extract_promoted_files,
    run,
)


class ExtractPromotedFilesTest(unittest.TestCase):
    def test_extracts_quality_featured_and_valued_promotions(self) -> None:
        wikitext = """
{{QICpromoted|File:Quality_image.jpg|nomination|review}}
{{ FPpromotion | 1 = [[:File:Featured image.jpg]] |subpage=Example}}
{{Template:VICpromoted|Valued image.webp|scope|review=Example}}
"""

        self.assertEqual(
            _extract_promoted_files(wikitext),
            [
                ("qicpromoted", "File:Quality_image.jpg"),
                ("fppromotion", "File:Featured image.jpg"),
                ("vicpromoted", "File:Valued image.webp"),
            ],
        )

    def test_ignores_unrelated_templates(self) -> None:
        self.assertEqual(_extract_promoted_files("{{QualityImage}}"), [])


class FileCategoryStatusesTest(unittest.TestCase):
    def test_resolves_normalized_and_redirected_file_titles(self) -> None:
        api = object.__new__(CommonsApi)
        api.get = Mock(
            return_value={
                "query": {
                    "normalized": [
                        {"from": "File:Old_name.jpg", "to": "File:Old name.jpg"}
                    ],
                    "redirects": [
                        {"from": "File:Old name.jpg", "to": "File:New name.jpg"}
                    ],
                    "pages": [
                        {
                            "pageid": 1,
                            "ns": 6,
                            "title": "File:New name.jpg",
                            "imageinfo": [
                                {"user": "Overwriter"},
                                {"user": "Original uploader"},
                            ],
                            "categories": [
                                {"ns": 14, "title": "Category:Featured by Example"}
                            ],
                        },
                        {"ns": 6, "title": "File:Deleted.jpg", "missing": True},
                    ],
                }
            }
        )

        statuses = api.file_category_statuses(
            ["File:Old_name.jpg", "File:Deleted.jpg"], ["Featured by Example"]
        )

        self.assertEqual(
            statuses["File:Old_name.jpg"],
            FileCategoryStatus(
                title="File:New name.jpg",
                categories=frozenset({"Category:Featured by Example"}),
                original_uploader="Original uploader",
            ),
        )
        self.assertIsNone(statuses["File:Deleted.jpg"])


class RunTest(unittest.TestCase):
    def test_adds_each_missing_category_once_per_file(self) -> None:
        config = SimpleNamespace(
            username="Example@tagger",
            password="secret",
            quality_images_category="Quality images by Example",
            featured_pictures_category="Featured pictures by Example",
            valued_images_category="Valued images by Example",
        )
        api = Mock()
        api.user_talk_page_titles.return_value = [
            "User talk:Example",
            "User talk:Example/Archive 1",
        ]
        api.page_contents.return_value = {
            "User talk:Example": """
{{QICpromoted|File:Already quality.jpg|x|y}}
{{QICpromoted|File:Multiple awards.jpg|x|y}}
{{FPpromotion|File:Multiple awards.jpg}}
{{FPpromotion|File:Nominated by Example.jpg}}
""",
            "User talk:Example/Archive 1": """
{{VICpromoted|Old valued image.jpg|scope|review=x}}
{{QICpromoted|File:Already quality.jpg|duplicate|notice}}
""",
        }
        api.file_category_statuses.return_value = {
            "File:Already quality.jpg": FileCategoryStatus(
                "File:Already quality.jpg",
                frozenset({"Category:Quality images by Example"}),
                "Example",
            ),
            "File:Multiple awards.jpg": FileCategoryStatus(
                "File:Multiple awards.jpg", frozenset(), "Example"
            ),
            "File:Old valued image.jpg": FileCategoryStatus(
                "File:Old valued image.jpg", frozenset(), "Example"
            ),
            "File:Nominated by Example.jpg": FileCategoryStatus(
                "File:Nominated by Example.jpg", frozenset(), "Another uploader"
            ),
        }

        with (
            patch(
                "tag_promoted_images.load_config",
                return_value=SimpleNamespace(commons=config),
            ),
            patch("tag_promoted_images.CommonsApi", return_value=api),
        ):
            run()

        api.login.assert_called_once_with()
        self.assertEqual(
            api.append_categories.call_args_list,
            [
                unittest.mock.call(
                    "File:Multiple awards.jpg",
                    [
                        "Quality images by Example",
                        "Featured pictures by Example",
                    ],
                ),
                unittest.mock.call(
                    "File:Old valued image.jpg", ["Valued images by Example"]
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
