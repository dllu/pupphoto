from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tag_promoted_images import (
    CommonsApi,
    FileCategoryStatus,
    _extract_promoted_files,
    _is_authored_by,
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

    def test_recognizes_uploader_notices_as_featured_promotions(self) -> None:
        self.assertEqual(
            _extract_promoted_files(
                "{{FPpromotedUploader|File:Samoyed dog two year old female dllu.jpg"
                "|subpage=File:Samoyed dog two year old female dllu.jpg}}\n"
                "{{ Template:fppromoteduploader | 1 = "
                "[[:File:BC Highway 20 Sheep Creek Bridge dllu.jpg]] |subpage=x}}"
            ),
            [
                ("fppromotion", "File:Samoyed dog two year old female dllu.jpg"),
                ("fppromotion", "File:BC Highway 20 Sheep Creek Bridge dllu.jpg"),
            ],
        )


class AuthorshipTest(unittest.TestCase):
    def test_accepts_plain_username_and_commons_user_links(self) -> None:
        for artist in (
            "Dllu",
            "<span> dllu </span>",
            '<a href="//commons.wikimedia.org/wiki/User:Dllu">Dllu</a>',
            'Daniel Lu (<a href="https://commons.wikimedia.org/wiki/User:Dllu">'
            "User:dllu</a>)",
            '<a href="/wiki/User%3ADllu">Daniel L. Lu</a>',
            '<a href="/w/index.php?title=User%3ADllu&amp;action=edit&amp;redlink=1">'
            "Daniel Lu</a>",
        ):
            with self.subTest(artist=artist):
                self.assertTrue(_is_authored_by(artist, "Dllu"))

    def test_normalizes_username_spaces_and_html_entities(self) -> None:
        self.assertTrue(_is_authored_by("Example&nbsp;User", "Example User"))
        self.assertTrue(
            _is_authored_by('<a href="/wiki/User:Example_User">Name</a>', "Example User")
        )

    def test_rejects_other_missing_or_ambiguous_authors(self) -> None:
        for artist in (
            None,
            "",
            "Someone else",
            "Dllu Jr",
            "Photo by someone else, uploaded by Dllu",
            '<a href="/wiki/User:Someone_else">Dllu</a>',
            '<a href="/wiki/User:Dllu_Jr">Dllu</a>',
            '<a href="/wiki/User:Dllu/Gallery">Dllu</a>',
            '<a href="/wiki/User_talk:Dllu">Dllu</a>',
            '<a href="https://example.org/wiki/User:Dllu">Dllu</a>',
            '<a href="https://en.wikipedia.org/wiki/User:Dllu">Dllu</a>',
        ):
            with self.subTest(artist=artist):
                self.assertFalse(_is_authored_by(artist, "Dllu"))


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
                                {
                                    "user": "Another uploader",
                                    "extmetadata": {"Artist": {"value": "Example"}},
                                },
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
                artist="Example",
            ),
        )
        self.assertIsNone(statuses["File:Deleted.jpg"])

    def test_missing_author_metadata_does_not_fall_back_to_uploader(self) -> None:
        api = object.__new__(CommonsApi)
        api.get = Mock(
            return_value={
                "query": {
                    "pages": [
                        {
                            "ns": 6,
                            "title": "File:No author.jpg",
                            "imageinfo": [{"user": "Example"}],
                        },
                        {"ns": 6, "title": "File:No imageinfo.jpg"},
                    ]
                }
            }
        )

        statuses = api.file_category_statuses(
            ["File:No author.jpg", "File:No imageinfo.jpg"], ["Featured by Example"]
        )

        for status in statuses.values():
            self.assertIsNotNone(status)
            self.assertIsNone(status.artist)


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
{{FPpromotedUploader|File:Multiple awards.jpg}}
{{FPpromotion|File:Nominated by Example.jpg}}
{{FPpromotedUploader|File:Uploaded by Example.jpg}}
{{FPpromotedUploader|File:Unknown author.jpg}}
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
                "File:Nominated by Example.jpg", frozenset(), "Another photographer"
            ),
            "File:Uploaded by Example.jpg": FileCategoryStatus(
                "File:Uploaded by Example.jpg", frozenset(), "Another photographer"
            ),
            "File:Unknown author.jpg": FileCategoryStatus(
                "File:Unknown author.jpg", frozenset(), None
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

    def test_tags_both_recent_uploader_notices_using_author_metadata(self) -> None:
        config = SimpleNamespace(
            username="Dllu@tagger",
            password="secret",
            quality_images_category="Quality images by dllu",
            featured_pictures_category="Featured pictures by dllu",
            valued_images_category="Valued images by dllu",
        )
        titles_and_artists = {
            "File:Samoyed dog two year old female dllu.jpg": "Dllu",
            "File:BC Highway 20 Sheep Creek Bridge dllu.jpg": (
                '<a href="//commons.wikimedia.org/wiki/User:Dllu" '
                'title="User:Dllu">Dllu</a>'
            ),
            "File:Someone else's photograph.jpg": "Another photographer",
        }
        api = CommonsApi(config)
        api.login = Mock()
        api.user_talk_page_titles = Mock(return_value=["User talk:Dllu"])
        api.page_contents = Mock(
            return_value={
                "User talk:Dllu": "\n".join(
                    "{{FPpromotedUploader|" + title + "|subpage=" + title + "}}"
                    for title in titles_and_artists
                )
            }
        )
        api.get = Mock(
            return_value={
                "query": {
                    "pages": [
                        {
                            "ns": 6,
                            "title": title,
                            "imageinfo": [
                                {
                                    # Uploader identity must not establish authorship.
                                    "user": "Dllu",
                                    "extmetadata": {"Artist": {"value": artist}},
                                }
                            ],
                        }
                        for title, artist in titles_and_artists.items()
                    ]
                }
            }
        )
        api.append_categories = Mock()

        with (
            patch(
                "tag_promoted_images.load_config",
                return_value=SimpleNamespace(commons=config),
            ),
            patch("tag_promoted_images.CommonsApi", return_value=api),
        ):
            run()

        self.assertEqual(
            api.append_categories.call_args_list,
            [
                unittest.mock.call(title, ["Featured pictures by dllu"])
                for title in list(titles_and_artists)[:2]
            ],
        )
        self.assertEqual(api.get.call_args.kwargs["iiprop"], "extmetadata")
        self.assertEqual(api.get.call_args.kwargs["iiextmetadatafilter"], "Artist")


if __name__ == "__main__":
    unittest.main()
