#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from config import CommonsConfig, load_config


def _category_wikitext(category_name: str) -> str:
    return f"[[Category:{category_name}]]"


def _category_title(category_name: str) -> str:
    return f"Category:{category_name}"


def _botpassword_owner_username(username: str) -> str:
    return username.split("@", 1)[0].strip()


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _title_key(title: str) -> str:
    return " ".join(title.replace("_", " ").split()).casefold()


@dataclass(frozen=True)
class PromotionSpec:
    key: str
    label: str
    category_name: str


@dataclass(frozen=True)
class FileCategoryStatus:
    title: str
    categories: frozenset[str]
    original_uploader: str | None


_PROMOTION_TEMPLATE_PATTERN = re.compile(
    r"\{\{\s*(?:Template\s*:\s*)?"
    r"(QICpromoted|FPpromotion|VICpromoted)\s*\|\s*"
    r"(?:1\s*=\s*)?([^|{}\n]+)",
    re.IGNORECASE,
)


def _normalize_promoted_file_title(value: str) -> str | None:
    title = value.strip()
    if title.startswith("[[") and title.endswith("]]"):
        title = title[2:-2].strip()
    title = title.removeprefix(":").strip()
    if not title:
        return None
    if title.casefold().startswith("file:"):
        return "File:" + title.split(":", 1)[1].strip()
    return "File:" + title


def _extract_promoted_files(wikitext: str) -> list[tuple[str, str]]:
    promotions: list[tuple[str, str]] = []
    for match in _PROMOTION_TEMPLATE_PATTERN.finditer(wikitext):
        title = _normalize_promoted_file_title(match.group(2))
        if title:
            promotions.append((match.group(1).casefold(), title))
    return promotions


class CommonsApi:
    _TITLE_BATCH_SIZE = 50
    _MAX_RETRIES = 4
    _RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(self, config: CommonsConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "pupphoto/0.1 Wikimedia Commons promotion category tagger"
                ),
            }
        )
        self._csrf_token: str | None = None

    def _retry_delay_seconds(
        self, response: requests.Response | None, attempt: int
    ) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(float(retry_after), 0.5)
                except ValueError:
                    pass
        return min(1.5 * (2**attempt), 20.0)

    def _request_json(
        self,
        method: str,
        *,
        timeout: int,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload_params = {**(params or {}), "format": "json", "formatversion": "2"}
        for attempt in range(self._MAX_RETRIES + 1):
            response = self.session.request(
                method,
                self.config.api_url,
                params=payload_params,
                data=data,
                timeout=timeout,
            )
            if response.status_code in self._RETRYABLE_STATUS_CODES:
                if attempt < self._MAX_RETRIES:
                    delay = self._retry_delay_seconds(response, attempt)
                    print(
                        f"Commons API returned HTTP {response.status_code}; "
                        f"retrying in {delay:.1f}s...",
                        flush=True,
                    )
                    time.sleep(delay)
                    continue
            response.raise_for_status()
            data_json = response.json()
            if "error" in data_json:
                error = data_json["error"]
                if error.get("code") in {"maxlag", "ratelimited"}:
                    if attempt < self._MAX_RETRIES:
                        delay = self._retry_delay_seconds(response, attempt)
                        print(
                            f"Commons API returned {error.get('code')}; "
                            f"retrying in {delay:.1f}s...",
                            flush=True,
                        )
                        time.sleep(delay)
                        continue
                raise RuntimeError(error)
            return data_json
        raise RuntimeError("Commons API request failed after retries")

    def get(self, **params: Any) -> dict[str, Any]:
        return self._request_json("GET", params=params, timeout=30)

    def post(self, **params: Any) -> dict[str, Any]:
        return self._request_json(
            "POST",
            data={**params, "format": "json", "formatversion": "2"},
            timeout=60,
        )

    def login(self) -> None:
        print("Logging in to Wikimedia Commons...", flush=True)
        login_token = self.get(action="query", meta="tokens", type="login")["query"][
            "tokens"
        ]["logintoken"]
        result = self.post(
            action="login",
            lgname=self.config.username,
            lgpassword=self.config.password,
            lgtoken=login_token,
        )
        if result["login"]["result"] != "Success":
            raise RuntimeError(f"Commons login failed: {result['login']}")

    def csrf_token(self) -> str:
        if self._csrf_token is None:
            self._csrf_token = self.get(action="query", meta="tokens")["query"][
                "tokens"
            ]["csrftoken"]
        return self._csrf_token

    def user_talk_page_titles(self, username: str) -> list[str]:
        base_title = f"User talk:{username}"
        titles = [base_title]
        continue_from: str | None = None
        while True:
            params: dict[str, Any] = {
                "action": "query",
                "list": "allpages",
                "apnamespace": "3",
                "apprefix": f"{username}/",
                "aplimit": "max",
            }
            if continue_from:
                params["apcontinue"] = continue_from
            data = self.get(**params)
            titles.extend(
                page["title"] for page in data.get("query", {}).get("allpages", [])
            )
            continue_from = data.get("continue", {}).get("apcontinue")
            if continue_from is None:
                return _dedupe_preserve_order(titles)

    def page_contents(self, titles: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for batch_start in range(0, len(titles), self._TITLE_BATCH_SIZE):
            batch = titles[batch_start : batch_start + self._TITLE_BATCH_SIZE]
            pages = self.get(
                action="query",
                titles="|".join(batch),
                prop="revisions",
                rvprop="content",
                rvslots="main",
            )["query"]["pages"]
            for page in pages:
                revisions = page.get("revisions", [])
                content = ""
                if revisions:
                    content = (
                        revisions[0].get("slots", {}).get("main", {}).get("content", "")
                    )
                result[page["title"]] = content
        return result

    def file_category_statuses(
        self, titles: list[str], category_names: list[str]
    ) -> dict[str, FileCategoryStatus | None]:
        result: dict[str, FileCategoryStatus | None] = {}
        category_titles = [_category_title(name) for name in category_names]

        for batch_start in range(0, len(titles), self._TITLE_BATCH_SIZE):
            batch = titles[batch_start : batch_start + self._TITLE_BATCH_SIZE]
            data = self.get(
                action="query",
                titles="|".join(batch),
                redirects="1",
                prop="categories|imageinfo",
                clcategories="|".join(category_titles),
                cllimit="max",
                iiprop="user",
                iilimit="max",
            )

            aliases: dict[str, str] = {}
            for normalization in data.get("query", {}).get("normalized", []):
                aliases[_title_key(normalization["from"])] = normalization["to"]
            for redirect in data.get("query", {}).get("redirects", []):
                aliases[_title_key(redirect["from"])] = redirect["to"]

            statuses: dict[str, FileCategoryStatus] = {}
            for page in data.get("query", {}).get("pages", []):
                if page.get("missing") or page.get("ns") != 6:
                    continue
                status = FileCategoryStatus(
                    title=page["title"],
                    categories=frozenset(
                        category["title"] for category in page.get("categories", [])
                    ),
                    original_uploader=(
                        page["imageinfo"][-1].get("user")
                        if page.get("imageinfo")
                        else None
                    ),
                )
                statuses[_title_key(status.title)] = status

            for requested_title in batch:
                resolved_title = requested_title
                seen: set[str] = set()
                while _title_key(resolved_title) in aliases:
                    key = _title_key(resolved_title)
                    if key in seen:
                        break
                    seen.add(key)
                    resolved_title = aliases[key]
                result[requested_title] = statuses.get(_title_key(resolved_title))

        return result

    def append_categories(self, title: str, category_names: list[str]) -> None:
        category_wikitext = [_category_wikitext(name) for name in category_names]
        print(f"Adding {', '.join(category_wikitext)} to {title}...", flush=True)
        if len(category_names) == 1:
            summary = f"Adding {category_wikitext[0]} via pupphoto"
        else:
            summary = "Adding personal promotion categories via pupphoto"
        result = self.post(
            action="edit",
            title=title,
            summary=summary,
            appendtext="\n" + "\n".join(category_wikitext),
            bot="1",
            token=self.csrf_token(),
        )
        if result.get("edit", {}).get("result") != "Success":
            raise RuntimeError(f"Commons edit failed: {result}")


def _promotion_specs(commons_config: CommonsConfig) -> list[PromotionSpec]:
    return [
        PromotionSpec(
            key="qicpromoted",
            label="Quality Image",
            category_name=commons_config.quality_images_category.strip(),
        ),
        PromotionSpec(
            key="fppromotion",
            label="Featured Picture",
            category_name=commons_config.featured_pictures_category.strip(),
        ),
        PromotionSpec(
            key="vicpromoted",
            label="Valued Image",
            category_name=commons_config.valued_images_category.strip(),
        ),
    ]


def _validate_required_config(commons_config: CommonsConfig) -> None:
    required = {
        "commons.username": commons_config.username,
        "commons.password": commons_config.password,
        "commons.quality_images_category": commons_config.quality_images_category,
        "commons.featured_pictures_category": commons_config.featured_pictures_category,
        "commons.valued_images_category": commons_config.valued_images_category,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        raise SystemExit(
            "Missing required config values in config.toml: " + ", ".join(missing)
        )


def run(config_path: Path | None = None) -> None:
    app_config = load_config(config_path)
    commons_config = app_config.commons
    _validate_required_config(commons_config)
    commons_api = CommonsApi(commons_config)
    commons_api.login()

    username = _botpassword_owner_username(commons_config.username)
    talk_page_titles = commons_api.user_talk_page_titles(username)
    print(
        f"Scanning {len(talk_page_titles)} user talk page(s) for promotion notices: "
        + json.dumps(talk_page_titles, ensure_ascii=True),
        flush=True,
    )
    talk_page_contents = commons_api.page_contents(talk_page_titles)

    specs = _promotion_specs(commons_config)
    spec_by_key = {spec.key: spec for spec in specs}
    promotion_keys_by_title: dict[str, set[str]] = {}
    promotion_count_by_key = {spec.key: 0 for spec in specs}
    for wikitext in talk_page_contents.values():
        for promotion_key, file_title in _extract_promoted_files(wikitext):
            if promotion_key not in spec_by_key:
                continue
            promotion_count_by_key[promotion_key] += 1
            promotion_keys_by_title.setdefault(file_title, set()).add(promotion_key)

    print(
        "Found promotion notices: "
        + ", ".join(
            f"{spec.label}={promotion_count_by_key[spec.key]}" for spec in specs
        ),
        flush=True,
    )
    candidate_titles = list(promotion_keys_by_title)
    if not candidate_titles:
        print("No promoted files found.", flush=True)
        return

    category_names = [spec.category_name for spec in specs]
    statuses = commons_api.file_category_statuses(candidate_titles, category_names)
    missing_by_file: dict[str, list[str]] = {}
    skipped_missing = 0
    skipped_other_uploaders = 0
    for candidate_title, promotion_keys in promotion_keys_by_title.items():
        status = statuses.get(candidate_title)
        if status is None:
            skipped_missing += 1
            continue
        if status.original_uploader is None or (
            status.original_uploader.casefold() != username.casefold()
        ):
            skipped_other_uploaders += 1
            continue
        existing_category_keys = {_title_key(name) for name in status.categories}
        missing_categories = [
            spec.category_name
            for spec in specs
            if spec.key in promotion_keys
            and _title_key(_category_title(spec.category_name))
            not in existing_category_keys
        ]
        if missing_categories:
            existing = missing_by_file.setdefault(status.title, [])
            for category_name in missing_categories:
                if category_name not in existing:
                    existing.append(category_name)

    added_categories = 0
    for file_title, category_names_to_add in missing_by_file.items():
        commons_api.append_categories(file_title, category_names_to_add)
        added_categories += len(category_names_to_add)

    print(
        f"Checked {len(candidate_titles)} promoted file references; "
        f"updated {len(missing_by_file)} files with {added_categories} categories."
        + (
            f" Skipped {skipped_missing} missing or non-file pages."
            if skipped_missing
            else ""
        )
        + (
            f" Skipped {skipped_other_uploaders} file"
            + ("s" if skipped_other_uploaders != 1 else "")
            + " uploaded by other users."
            if skipped_other_uploaders
            else ""
        ),
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Add configured personal categories to promoted Commons files found in "
            "user talk-page notices."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional path to config.toml (defaults to ./config.toml).",
    )
    args = parser.parse_args()
    run(args.config)


if __name__ == "__main__":
    main()
