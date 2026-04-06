#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import requests

from config import CommonsConfig, load_config


def _category_wikitext(category_name: str) -> str:
    return f"[[Category:{category_name}]]"


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


class CommonsApi:
    _TITLE_BATCH_SIZE = 50
    _MAX_RETRIES = 4
    _RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(self, config: CommonsConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "pupphoto/0.1 Wikimedia Commons quality image tagger",
            }
        )

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
                        f"Commons API returned HTTP {response.status_code}; retrying in {delay:.1f}s...",
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
                            f"Commons API returned {error.get('code')}; retrying in {delay:.1f}s...",
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
        return self.get(action="query", meta="tokens")["query"]["tokens"]["csrftoken"]

    def recent_uploaded_files(
        self, limit: int, continue_from: str | None = None
    ) -> tuple[list[str], str | None]:
        upload_user = _botpassword_owner_username(self.config.username)
        params: dict[str, Any] = {
            "action": "query",
            "list": "logevents",
            "letype": "upload",
            "leuser": upload_user,
            "lenamespace": "6",
            "ledir": "older",
            "lelimit": min(limit, self._TITLE_BATCH_SIZE),
            "leprop": "title|timestamp|type|details|comment",
        }
        if continue_from:
            params["lecontinue"] = continue_from
        print(
            "Requesting recent upload log entries: "
            + json.dumps(params, sort_keys=True, ensure_ascii=True),
            flush=True,
        )
        data = self.get(**params)
        events = data.get("query", {}).get("logevents", [])
        print(
            f"Upload log query returned {len(events)} entries for user {upload_user!r}.",
            flush=True,
        )
        titles = _dedupe_preserve_order(
            [
                item["title"]
                for item in events
                if item.get("title", "").startswith("File:")
            ]
        )
        return titles, data.get("continue", {}).get("lecontinue")

    def recent_upload_debug_sample(self, limit: int = 5) -> list[dict[str, Any]]:
        data = self.get(
            action="query",
            list="logevents",
            letype="upload",
            lenamespace="6",
            ledir="older",
            lelimit=min(limit, self._TITLE_BATCH_SIZE),
            leprop="title|timestamp|user|comment",
        )
        return data.get("query", {}).get("logevents", [])

    def file_page_contents(self, titles: list[str]) -> dict[str, str]:
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
                    content = revisions[0].get("slots", {}).get("main", {}).get("content", "")
                result[page["title"]] = content
        return result

    def append_category(self, title: str, category_name: str) -> None:
        token = self.csrf_token()
        category_wikitext = _category_wikitext(category_name)
        print(f"Adding {category_wikitext} to {title}...", flush=True)
        result = self.post(
            action="edit",
            title=title,
            summary=f"Adding [[Category:{category_name}]] via pupphoto",
            appendtext=f"\n{category_wikitext}",
            bot="1",
            token=token,
        )
        if result.get("edit", {}).get("result") != "Success":
            raise RuntimeError(f"Commons edit failed: {result}")


def _validate_required_config(commons_config: CommonsConfig) -> None:
    missing: list[str] = []
    if not commons_config.username.strip():
        missing.append("commons.username")
    if not commons_config.password.strip():
        missing.append("commons.password")
    if not commons_config.quality_images_category.strip():
        missing.append("commons.quality_images_category")
    if commons_config.quality_images_scan_limit <= 0:
        raise SystemExit("commons.quality_images_scan_limit must be positive")
    if missing:
        raise SystemExit(
            "Missing required config values in config.toml: " + ", ".join(missing)
        )


def _contains_quality_image_template(wikitext: str) -> bool:
    lowered = wikitext.lower()
    return "{{qualityimage" in lowered or "{{quality image" in lowered


def _contains_category(wikitext: str, category_name: str) -> bool:
    target = _category_wikitext(category_name).lower()
    return target in wikitext.lower()


def run(config_path: Path | None = None) -> None:
    app_config = load_config(config_path)
    commons_config = app_config.commons
    _validate_required_config(commons_config)
    commons_api = CommonsApi(commons_config)
    commons_api.login()

    target_category = commons_config.quality_images_category.strip()
    scan_limit = commons_config.quality_images_scan_limit
    processed = 0
    updated = 0
    continue_from: str | None = None

    print(
        "Scanning recent uploads for QualityImage files missing "
        + json.dumps(target_category),
        flush=True,
    )
    while processed < scan_limit:
        remaining = scan_limit - processed
        titles, continue_from = commons_api.recent_uploaded_files(
            min(remaining, CommonsApi._TITLE_BATCH_SIZE), continue_from
        )
        if not titles:
            if processed == 0:
                debug_events = commons_api.recent_upload_debug_sample()
                print(
                    "No upload log entries matched the configured username. "
                    f"Configured commons.username={commons_config.username!r}; "
                    f"derived upload-log username={_botpassword_owner_username(commons_config.username)!r}. "
                    "Recent upload-log sample: "
                    + json.dumps(debug_events, ensure_ascii=True),
                    flush=True,
                )
            print("No more recent uploads found.", flush=True)
            break
        print(
            "Checking recent uploads: " + json.dumps(titles, ensure_ascii=True),
            flush=True,
        )
        contents_by_title = commons_api.file_page_contents(titles)
        for title in titles:
            processed += 1
            wikitext = contents_by_title.get(title, "")
            if not _contains_quality_image_template(wikitext):
                continue
            if _contains_category(wikitext, target_category):
                print(
                    f"Stopping at {title}: it is already tagged with [[Category:{target_category}]].",
                    flush=True,
                )
                print(
                    f"Processed {processed} recent uploads and updated {updated}.",
                    flush=True,
                )
                return
            commons_api.append_category(title, target_category)
            updated += 1
        if continue_from is None:
            break

    print(f"Processed {processed} recent uploads and updated {updated}.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add a configured category to recent quality-image uploads on Commons."
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
