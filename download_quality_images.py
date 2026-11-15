#!/usr/bin/env python3

"""Archive original photographic Quality Images from Wikimedia Commons.

Discovery and downloads are deliberately separate, resumable phases.  Commons
metadata and every state transition are recorded in SQLite, and files are only
moved into their final content-addressed path after their size and SHA-1 match
the values returned by Commons.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import requests

from config import load_config

COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
QUALITY_IMAGES_CATEGORY = "Category:Quality images"
DEFAULT_ROOT = Path("/mnt/external/qi")
DEFAULT_WORKERS = 3
DEFAULT_REQUESTS_PER_SECOND = 0.5
MAX_DOWNLOAD_WORKERS = 3
# The image CDN began returning 429s around 0.6 requests/s from this host.
# Keep a conservative hard ceiling below the observed operational limit.
MAX_REQUESTS_PER_SECOND = 0.5
DEFAULT_RESERVE_GIB = 20.0
DISCOVERY_BATCH_SIZE = 500
# Extmetadata parsing for 500 files frequently exceeds Commons' gateway
# deadline. Smaller attribution batches avoid repeated 504 responses.
ENRICHMENT_BATCH_SIZE = 100
API_RETRIES = 6
DOWNLOAD_RETRIES = 10
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
MIB = 1024 * 1024
GIB = 1024 * MIB

# These formats are normally used to store photographs.  In particular, PNG,
# GIF, SVG, PDF, DjVu, and video are intentionally absent.  This provides a
# reproducible high-precision filter without guessing from captions.
PHOTO_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/tiff": ".tif",
    "image/x-tiff": ".tif",
    "image/avif": ".avif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/jxl": ".jxl",
}

EXTMETADATA_FIELDS = (
    "Artist",
    "LicenseShortName",
    "LicenseUrl",
    "UsageTerms",
    "Credit",
    "Attribution",
    "AttributionRequired",
)

LOGGER = logging.getLogger("commons_quality_images")
STOP_EVENT = threading.Event()
DOWNLOAD_SESSION_LOCAL = threading.local()


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def format_bytes(value: float | None) -> str:
    if value is None:
        return "unknown"
    number = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if abs(number) < 1024 or unit == units[-1]:
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} PiB"


class _MetadataHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"br", "p", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "div", "li"}:
            self.parts.append(" ")


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    parser = _MetadataHTMLParser()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts)
    cleaned = " ".join(unescape(text).split())
    return cleaned or None


def sha1_api_to_hex(value: str | None) -> str | None:
    """Normalize current hexadecimal and legacy base-36 MediaWiki SHA-1s."""
    if not value:
        return None
    normalized = value.lower()
    if re.fullmatch(r"[0-9a-f]{40}", normalized):
        return normalized
    if not re.fullmatch(r"[0-9a-z]+", normalized):
        return None
    try:
        number = int(normalized, 36)
    except ValueError:
        return None
    if number.bit_length() > 160:
        return None
    return f"{number:040x}"


def page_url(title: str) -> str:
    encoded = quote(title.replace(" ", "_"), safe="/:(),-'!~")
    return f"https://commons.wikimedia.org/wiki/{encoded}"


def metadata_value(metadata: dict[str, Any], name: str) -> str | None:
    item = metadata.get(name)
    if isinstance(item, dict):
        value = item.get("value")
    else:
        value = item
    if value is None:
        return None
    return str(value).strip() or None


def photo_classification(
    mime_type: str | None, media_type: str | None
) -> tuple[bool, str | None]:
    normalized_mime = (mime_type or "").lower()
    normalized_media = (media_type or "").upper()
    if normalized_media != "BITMAP":
        return False, f"media type {media_type or 'unknown'} is not BITMAP"
    if normalized_mime not in PHOTO_MIME_EXTENSIONS:
        return False, f"MIME type {mime_type or 'unknown'} is not photographic"
    return True, None


def original_filename(url: str | None, title: str) -> str:
    if url:
        candidate = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        if candidate:
            return candidate
    return title.removeprefix("File:")


def configure_logging(log_file: Path | None, verbose: bool) -> None:
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOGGER.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S%z"
    )
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    LOGGER.addHandler(stream)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)


def connect_database(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        uri = f"file:{quote(str(db_path))}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=60)
    else:
        connection = sqlite3.connect(db_path, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 60000")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS images (
            page_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            page_url TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            original_url TEXT,
            image_timestamp TEXT,
            category_added_at TEXT,
            width INTEGER,
            height INTEGER,
            byte_size INTEGER,
            mime_type TEXT,
            media_type TEXT,
            sha1 TEXT,
            sha1_hex TEXT,
            author TEXT,
            author_html TEXT,
            license TEXT,
            license_url TEXT,
            usage_terms TEXT,
            credit TEXT,
            credit_html TEXT,
            attribution TEXT,
            attribution_html TEXT,
            attribution_required INTEGER,
            extmetadata_json TEXT,
            is_photo INTEGER NOT NULL,
            eligible INTEGER NOT NULL,
            excluded_reason TEXT,
            in_category INTEGER NOT NULL DEFAULT 1,
            seen_generation INTEGER NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            download_path TEXT,
            downloaded_bytes INTEGER,
            http_etag TEXT,
            http_last_modified TEXT,
            last_error TEXT,
            discovered_at TEXT NOT NULL,
            download_started_at TEXT,
            downloaded_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS images_download_queue
            ON images (status, eligible, in_category, byte_size, page_id);
        CREATE INDEX IF NOT EXISTS images_sha1_hex
            ON images (sha1_hex);
        CREATE INDEX IF NOT EXISTS images_mime_type
            ON images (mime_type);

        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(images)").fetchall()
    }
    if "sha1_base36" in columns and "sha1" not in columns:
        connection.execute("ALTER TABLE images RENAME COLUMN sha1_base36 TO sha1")

    # Repair rows catalogued by a pre-hex-compatible version of this script.
    rows_needing_hash = connection.execute(
        "SELECT page_id, sha1, is_photo, original_url, byte_size, excluded_reason "
        "FROM images WHERE sha1_hex IS NULL OR sha1_hex = ''"
    ).fetchall()
    for row in rows_needing_hash:
        sha1_hex = sha1_api_to_hex(row["sha1"])
        eligible = bool(
            row["is_photo"]
            and row["original_url"]
            and isinstance(row["byte_size"], int)
            and int(row["byte_size"]) >= 0
            and sha1_hex
        )
        connection.execute(
            "UPDATE images SET sha1_hex = ?, eligible = ?, status = ?, "
            "excluded_reason = ?, updated_at = ? WHERE page_id = ?",
            (
                sha1_hex,
                int(eligible),
                "pending" if eligible else "excluded",
                None if eligible else row["excluded_reason"],
                utc_now(),
                row["page_id"],
            ),
        )
    connection.commit()


def get_state(
    connection: sqlite3.Connection, key: str, default: str | None = None
) -> str | None:
    row = connection.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row is not None else default


def set_state(connection: sqlite3.Connection, key: str, value: str) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value, now),
    )


class CommonsClient:
    def __init__(
        self,
        *,
        api_url: str,
        user_agent: str,
        username: str | None,
        password: str | None,
    ) -> None:
        self.api_url = api_url
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            }
        )

    def close(self) -> None:
        self.session.close()

    def request(self, method: str = "GET", **params: Any) -> dict[str, Any]:
        payload = {
            **params,
            "format": "json",
            "formatversion": "2",
            "maxlag": "5",
        }
        last_error: Exception | None = None
        for attempt in range(API_RETRIES):
            response: requests.Response | None = None
            try:
                kwargs: dict[str, Any] = {
                    "timeout": (30, 120),
                }
                if method.upper() == "POST":
                    kwargs["data"] = payload
                else:
                    kwargs["params"] = payload
                response = self.session.request(method, self.api_url, **kwargs)
                if response.status_code in RETRYABLE_STATUS_CODES:
                    raise requests.HTTPError(
                        f"retryable HTTP {response.status_code}", response=response
                    )
                response.raise_for_status()
                result = response.json()
                error = result.get("error")
                if error:
                    code = str(error.get("code", "unknown"))
                    if code in {"maxlag", "ratelimited", "readonly"}:
                        raise RuntimeError(f"retryable Commons API error: {code}")
                    raise RuntimeError(f"Commons API error: {error}")
                return result
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt + 1 >= API_RETRIES:
                    break
                retry_after = None
                if response is not None:
                    retry_after = response.headers.get("Retry-After")
                try:
                    delay = (
                        float(retry_after) if retry_after is not None else 2**attempt
                    )
                except ValueError:
                    delay = 2**attempt
                delay = min(max(delay, 1.0), 30.0)
                LOGGER.warning(
                    "Commons API request failed (%s); retrying in %.1fs",
                    exc,
                    delay,
                )
                STOP_EVENT.wait(delay)
                if STOP_EVENT.is_set():
                    raise InterruptedError("stopped during Commons API retry") from exc
        raise RuntimeError(f"Commons API request failed after retries: {last_error}")

    def login(self) -> None:
        if not self.username or not self.password:
            LOGGER.info("Using anonymous Commons API access")
            return
        token_response = self.request(action="query", meta="tokens", type="login")
        token = token_response["query"]["tokens"]["logintoken"]
        result = self.request(
            "POST",
            action="login",
            lgname=self.username,
            lgpassword=self.password,
            lgtoken=token,
        )
        login_result = result.get("login", {}).get("result")
        if login_result != "Success":
            raise RuntimeError(
                f"Commons login failed: {login_result or 'unknown result'}"
            )
        LOGGER.info("Authenticated to Wikimedia Commons")

    def category_count(self) -> int | None:
        result = self.request(
            action="query", titles=QUALITY_IMAGES_CATEGORY, prop="categoryinfo"
        )
        pages = result.get("query", {}).get("pages", [])
        if not pages:
            return None
        count = pages[0].get("categoryinfo", {}).get("files")
        return int(count) if count is not None else None

    def quality_image_batch(
        self,
        continuation: dict[str, str],
        limit: int,
        *,
        include_extmetadata: bool,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            **continuation,
            "action": "query",
            "generator": "categorymembers",
            "gcmtitle": QUALITY_IMAGES_CATEGORY,
            "gcmtype": "file",
            "gcmprop": "ids|title|timestamp|type",
            "gcmlimit": str(limit),
            "prop": "imageinfo",
            "iiprop": "url|size|mime|mediatype|sha1|timestamp",
        }
        if include_extmetadata:
            params.update(
                iiprop=params["iiprop"] + "|extmetadata",
                iiextmetadatalanguage="en",
                iiextmetadatafilter="|".join(EXTMETADATA_FIELDS),
            )
        return self.request(**params)


def client_from_config(config_path: Path | None, anonymous: bool) -> CommonsClient:
    config = load_config(config_path).commons
    owner_name = config.username.split("@", 1)[0].strip() or "pupphoto"
    owner_url = "https://commons.wikimedia.org/wiki/User:" + quote(owner_name)
    user_agent = f"pupphoto-quality-image-archiver/1.0 ({owner_url})"
    return CommonsClient(
        api_url=config.api_url or COMMONS_API_URL,
        user_agent=user_agent,
        username=None if anonymous else config.username,
        password=None if anonymous else config.password,
    )


def record_from_page(page: dict[str, Any], generation: int) -> dict[str, Any]:
    now = utc_now()
    title = str(page.get("title") or f"File:page-{page.get('pageid', 'unknown')}")
    infos = page.get("imageinfo") or []
    info = infos[0] if infos else {}
    extmetadata = info.get("extmetadata") or {}
    mime_type = info.get("mime")
    media_type = info.get("mediatype")
    is_photo, classification_reason = photo_classification(mime_type, media_type)
    sha1 = info.get("sha1")
    sha1_hex = sha1_api_to_hex(sha1)
    source_url = info.get("url")
    byte_size = info.get("size")
    missing: list[str] = []
    if not source_url:
        missing.append("original URL")
    if not sha1_hex:
        missing.append("valid SHA-1")
    if not isinstance(byte_size, int) or byte_size < 0:
        missing.append("byte size")
    eligible = is_photo and not missing
    excluded_reason = classification_reason
    if is_photo and missing:
        excluded_reason = "missing " + ", ".join(missing)

    artist_html = metadata_value(extmetadata, "Artist")
    attribution_html = metadata_value(extmetadata, "Attribution")
    credit_html = metadata_value(extmetadata, "Credit")
    author = html_to_text(artist_html) or html_to_text(attribution_html)
    license_short = metadata_value(extmetadata, "LicenseShortName")
    usage_terms = metadata_value(extmetadata, "UsageTerms")
    license_name = html_to_text(license_short) or html_to_text(usage_terms)
    attribution_required_raw = metadata_value(extmetadata, "AttributionRequired")
    attribution_required = None
    if attribution_required_raw is not None:
        attribution_required = int(
            attribution_required_raw.strip().lower() in {"1", "true", "yes"}
        )

    return {
        "page_id": int(page["pageid"]),
        "title": title,
        "page_url": page_url(title),
        "original_filename": original_filename(source_url, title),
        "original_url": source_url,
        "image_timestamp": info.get("timestamp"),
        "category_added_at": page.get("categorytimestamp"),
        "width": info.get("width"),
        "height": info.get("height"),
        "byte_size": byte_size,
        "mime_type": mime_type,
        "media_type": media_type,
        "sha1": sha1,
        "sha1_hex": sha1_hex,
        "author": author,
        "author_html": artist_html,
        "license": license_name,
        "license_url": metadata_value(extmetadata, "LicenseUrl"),
        "usage_terms": html_to_text(usage_terms),
        "credit": html_to_text(credit_html),
        "credit_html": credit_html,
        "attribution": html_to_text(attribution_html),
        "attribution_html": attribution_html,
        "attribution_required": attribution_required,
        "extmetadata_json": (
            json.dumps(extmetadata, ensure_ascii=False, separators=(",", ":"))
            if extmetadata
            else None
        ),
        "is_photo": int(is_photo),
        "eligible": int(eligible),
        "excluded_reason": excluded_reason,
        "in_category": 1,
        "seen_generation": generation,
        "status": "pending" if eligible else "excluded",
        "discovered_at": now,
        "updated_at": now,
    }


UPSERT_IMAGE_SQL = """
    INSERT INTO images (
        page_id, title, page_url, original_filename, original_url,
        image_timestamp, category_added_at, width, height, byte_size,
        mime_type, media_type, sha1, sha1_hex, author, author_html,
        license, license_url, usage_terms, credit, credit_html, attribution,
        attribution_html, attribution_required, extmetadata_json, is_photo,
        eligible, excluded_reason, in_category, seen_generation, status,
        discovered_at, updated_at
    ) VALUES (
        :page_id, :title, :page_url, :original_filename, :original_url,
        :image_timestamp, :category_added_at, :width, :height, :byte_size,
        :mime_type, :media_type, :sha1, :sha1_hex, :author, :author_html,
        :license, :license_url, :usage_terms, :credit, :credit_html,
        :attribution, :attribution_html, :attribution_required,
        :extmetadata_json, :is_photo, :eligible, :excluded_reason,
        :in_category, :seen_generation, :status, :discovered_at, :updated_at
    )
    ON CONFLICT(page_id) DO UPDATE SET
        title = excluded.title,
        page_url = excluded.page_url,
        original_filename = excluded.original_filename,
        original_url = excluded.original_url,
        image_timestamp = excluded.image_timestamp,
        category_added_at = excluded.category_added_at,
        width = excluded.width,
        height = excluded.height,
        byte_size = excluded.byte_size,
        mime_type = excluded.mime_type,
        media_type = excluded.media_type,
        sha1 = excluded.sha1,
        sha1_hex = excluded.sha1_hex,
        author = COALESCE(excluded.author, images.author),
        author_html = COALESCE(excluded.author_html, images.author_html),
        license = COALESCE(excluded.license, images.license),
        license_url = COALESCE(excluded.license_url, images.license_url),
        usage_terms = COALESCE(excluded.usage_terms, images.usage_terms),
        credit = COALESCE(excluded.credit, images.credit),
        credit_html = COALESCE(excluded.credit_html, images.credit_html),
        attribution = COALESCE(excluded.attribution, images.attribution),
        attribution_html = COALESCE(
            excluded.attribution_html, images.attribution_html
        ),
        attribution_required = COALESCE(
            excluded.attribution_required, images.attribution_required
        ),
        extmetadata_json = COALESCE(
            excluded.extmetadata_json, images.extmetadata_json
        ),
        is_photo = excluded.is_photo,
        eligible = excluded.eligible,
        excluded_reason = excluded.excluded_reason,
        in_category = 1,
        seen_generation = excluded.seen_generation,
        status = CASE
            WHEN excluded.eligible = 0 THEN 'excluded'
            WHEN images.sha1 IS NOT excluded.sha1 THEN 'pending'
            WHEN images.status = 'excluded' THEN 'pending'
            ELSE images.status
        END,
        attempts = CASE
            WHEN images.sha1 IS NOT excluded.sha1 THEN 0
            ELSE images.attempts
        END,
        download_path = CASE
            WHEN images.sha1 IS NOT excluded.sha1 THEN NULL
            ELSE images.download_path
        END,
        downloaded_bytes = CASE
            WHEN images.sha1 IS NOT excluded.sha1 THEN NULL
            ELSE images.downloaded_bytes
        END,
        http_etag = CASE
            WHEN images.sha1 IS NOT excluded.sha1 THEN NULL
            ELSE images.http_etag
        END,
        http_last_modified = CASE
            WHEN images.sha1 IS NOT excluded.sha1 THEN NULL
            ELSE images.http_last_modified
        END,
        last_error = CASE
            WHEN images.sha1 IS NOT excluded.sha1 THEN NULL
            ELSE images.last_error
        END,
        download_started_at = CASE
            WHEN images.sha1 IS NOT excluded.sha1 THEN NULL
            ELSE images.download_started_at
        END,
        downloaded_at = CASE
            WHEN images.sha1 IS NOT excluded.sha1 THEN NULL
            ELSE images.downloaded_at
        END,
        discovered_at = excluded.discovered_at,
        updated_at = excluded.updated_at
"""


def prepare_discovery(
    connection: sqlite3.Connection, *, refresh: bool
) -> tuple[int, dict[str, str], bool]:
    generation_text = get_state(connection, "discovery_generation")
    generation = int(generation_text) if generation_text is not None else 1
    complete = get_state(connection, "discovery_complete", "0") == "1"
    if refresh:
        generation += 1
        complete = False
        continuation: dict[str, str] = {}
        set_state(connection, "discovery_generation", str(generation))
        set_state(connection, "discovery_complete", "0")
        set_state(connection, "discovery_continuation", "{}")
        set_state(connection, "metadata_enrichment_complete", "0")
        set_state(connection, "metadata_enrichment_continuation", "{}")
        connection.commit()
        return generation, continuation, complete

    if generation_text is None:
        set_state(connection, "discovery_generation", str(generation))
    continuation_raw = get_state(connection, "discovery_continuation", "{}") or "{}"
    try:
        parsed = json.loads(continuation_raw)
        continuation = (
            {str(key): str(value) for key, value in parsed.items()}
            if isinstance(parsed, dict)
            else {}
        )
    except json.JSONDecodeError:
        continuation = {}
    connection.commit()
    return generation, continuation, complete


def discover_images(
    connection: sqlite3.Connection,
    client: CommonsClient,
    *,
    refresh: bool,
    limit: int | None,
) -> int:
    generation, continuation, complete = prepare_discovery(connection, refresh=refresh)
    if complete:
        count = connection.execute(
            "SELECT COUNT(*) FROM images WHERE in_category = 1"
        ).fetchone()[0]
        LOGGER.info("Discovery is already complete (%s category files)", f"{count:,}")
        return 0

    expected_count = client.category_count()
    if expected_count is not None:
        set_state(connection, "category_file_count_at_start", str(expected_count))
        connection.commit()
        LOGGER.info("Commons reports %s files in Quality images", f"{expected_count:,}")

    processed = 0
    started = time.monotonic()
    while not STOP_EVENT.is_set():
        remaining = DISCOVERY_BATCH_SIZE if limit is None else limit - processed
        if remaining <= 0:
            break
        request_limit = min(DISCOVERY_BATCH_SIZE, remaining)
        result = client.quality_image_batch(
            continuation, request_limit, include_extmetadata=False
        )
        pages = result.get("query", {}).get("pages", [])
        records = [record_from_page(page, generation) for page in pages]
        connection.executemany(UPSERT_IMAGE_SQL, records)
        processed += len(records)

        next_continuation = result.get("continue")
        if isinstance(next_continuation, dict):
            continuation = {
                str(key): str(value) for key, value in next_continuation.items()
            }
            set_state(
                connection,
                "discovery_continuation",
                json.dumps(continuation, separators=(",", ":")),
            )
        else:
            continuation = {}
            set_state(connection, "discovery_continuation", "{}")
            set_state(connection, "discovery_complete", "1")
            set_state(connection, "discovery_completed_at", utc_now())
            connection.execute(
                "UPDATE images SET in_category = 0, updated_at = ? "
                "WHERE seen_generation <> ? AND in_category = 1",
                (utc_now(), generation),
            )
        set_state(connection, "last_discovery_batch_at", utc_now())
        connection.commit()

        elapsed = max(time.monotonic() - started, 0.001)
        total_seen = connection.execute(
            "SELECT COUNT(*) FROM images WHERE seen_generation = ?",
            (generation,),
        ).fetchone()[0]
        LOGGER.info(
            "Discovered %s this run (%s in generation, %.0f files/s)",
            f"{processed:,}",
            f"{total_seen:,}",
            processed / elapsed,
        )
        if not continuation:
            LOGGER.info("Quality Images discovery completed")
            break

    return processed


ATTRIBUTION_UPDATE_SQL = """
    UPDATE images SET
        author = COALESCE(:author, author),
        author_html = COALESCE(:author_html, author_html),
        license = COALESCE(:license, license),
        license_url = COALESCE(:license_url, license_url),
        usage_terms = COALESCE(:usage_terms, usage_terms),
        credit = COALESCE(:credit, credit),
        credit_html = COALESCE(:credit_html, credit_html),
        attribution = COALESCE(:attribution, attribution),
        attribution_html = COALESCE(:attribution_html, attribution_html),
        attribution_required = COALESCE(
            :attribution_required, attribution_required
        ),
        extmetadata_json = COALESCE(:extmetadata_json, extmetadata_json),
        updated_at = :updated_at
    WHERE page_id = :page_id
"""


def prepare_metadata_enrichment(
    connection: sqlite3.Connection,
) -> tuple[dict[str, str], bool]:
    complete = get_state(connection, "metadata_enrichment_complete", "0") == "1"
    raw = get_state(connection, "metadata_enrichment_continuation", "{}") or "{}"
    try:
        parsed = json.loads(raw)
        continuation = (
            {str(key): str(value) for key, value in parsed.items()}
            if isinstance(parsed, dict)
            else {}
        )
    except json.JSONDecodeError:
        continuation = {}
    return continuation, complete


def enrich_metadata(
    connection: sqlite3.Connection,
    client: CommonsClient,
    *,
    limit: int | None,
) -> int:
    continuation, complete = prepare_metadata_enrichment(connection)
    if complete:
        populated = connection.execute(
            "SELECT COUNT(*) FROM images WHERE author IS NOT NULL OR license IS NOT NULL"
        ).fetchone()[0]
        LOGGER.info(
            "Attribution metadata is already complete (%s populated rows)",
            f"{populated:,}",
        )
        return 0

    processed = 0
    started = time.monotonic()
    while not STOP_EVENT.is_set():
        remaining = ENRICHMENT_BATCH_SIZE if limit is None else limit - processed
        if remaining <= 0:
            break
        request_limit = min(ENRICHMENT_BATCH_SIZE, remaining)
        result = client.quality_image_batch(
            continuation, request_limit, include_extmetadata=True
        )
        pages = result.get("query", {}).get("pages", [])
        records = [record_from_page(page, generation=0) for page in pages]
        connection.executemany(ATTRIBUTION_UPDATE_SQL, records)
        processed += len(records)

        next_continuation = result.get("continue")
        if isinstance(next_continuation, dict):
            continuation = {
                str(key): str(value) for key, value in next_continuation.items()
            }
            set_state(
                connection,
                "metadata_enrichment_continuation",
                json.dumps(continuation, separators=(",", ":")),
            )
        else:
            continuation = {}
            set_state(connection, "metadata_enrichment_continuation", "{}")
            set_state(connection, "metadata_enrichment_complete", "1")
            set_state(connection, "metadata_enrichment_completed_at", utc_now())
        set_state(connection, "last_metadata_enrichment_batch_at", utc_now())
        connection.commit()

        elapsed = max(time.monotonic() - started, 0.001)
        LOGGER.info(
            "Enriched attribution for %s files this run (%.0f files/s)",
            f"{processed:,}",
            processed / elapsed,
        )
        if not continuation:
            LOGGER.info("Quality Images attribution enrichment completed")
            break

    return processed


@dataclass(frozen=True)
class DownloadRecord:
    page_id: int
    title: str
    original_url: str
    byte_size: int
    mime_type: str
    sha1_hex: str


@dataclass(frozen=True)
class DownloadResult:
    page_id: int
    ok: bool
    relative_path: str | None
    downloaded_bytes: int
    etag: str | None
    last_modified: str | None
    error: str | None
    elapsed_seconds: float


def destination_path(root: Path, record: DownloadRecord) -> Path:
    extension = PHOTO_MIME_EXTENSIONS.get(record.mime_type.lower(), ".img")
    digest = record.sha1_hex.lower()
    return root / "images" / digest[:2] / digest[2:4] / f"{digest}{extension}"


def hash_file_state(path: Path) -> tuple[Any, int]:
    digest = hashlib.sha1()
    size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(4 * MIB)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest, size


def hash_file(path: Path) -> tuple[str, int]:
    digest, size = hash_file_state(path)
    return digest.hexdigest(), size


def download_session(user_agent: str) -> requests.Session:
    session = getattr(DOWNLOAD_SESSION_LOCAL, "session", None)
    session_user_agent = getattr(DOWNLOAD_SESSION_LOCAL, "user_agent", None)
    if session is None or session_user_agent != user_agent:
        if session is not None:
            session.close()
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            }
        )
        DOWNLOAD_SESSION_LOCAL.session = session
        DOWNLOAD_SESSION_LOCAL.user_agent = user_agent
    return session


class DownloadIntegrityError(OSError):
    """The CDN response completed but did not match Commons metadata."""


class RequestPacer:
    """Globally pace request starts and share server-requested cooldowns."""

    def __init__(self, requests_per_second: float) -> None:
        self.interval_seconds = 1.0 / requests_per_second
        self._lock = threading.Lock()
        self._next_request_at = 0.0
        self._cooldown_until = 0.0
        self._last_cooldown_log_at = 0.0
        self._last_rate_reduction_at = 0.0

    def acquire(self) -> bool:
        while not STOP_EVENT.is_set():
            with self._lock:
                now = time.monotonic()
                ready_at = max(self._next_request_at, self._cooldown_until)
                delay = ready_at - now
                if delay <= 0:
                    self._next_request_at = now + self.interval_seconds
                    return True
            STOP_EVENT.wait(min(delay, 1.0))
        return False

    def defer(
        self, delay_seconds: float, reason: str, *, reduce_rate: bool = False
    ) -> None:
        now = time.monotonic()
        should_log = False
        reduced_rate: float | None = None
        with self._lock:
            if reduce_rate and now - self._last_rate_reduction_at >= 30.0:
                self.interval_seconds = min(self.interval_seconds * 2.0, 20.0)
                self._last_rate_reduction_at = now
                reduced_rate = 1.0 / self.interval_seconds
            self._cooldown_until = max(
                self._cooldown_until, now + max(delay_seconds, 0.0)
            )
            self._next_request_at = max(self._next_request_at, self._cooldown_until)
            if now - self._last_cooldown_log_at >= 5.0:
                self._last_cooldown_log_at = now
                should_log = True
        if should_log:
            message = "Pausing all download requests for %.1fs after %s"
            args: tuple[Any, ...] = (delay_seconds, reason)
            if reduced_rate is not None:
                message += "; reducing the run to %.3f request starts/s"
                args += (reduced_rate,)
            LOGGER.warning(message, *args)


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max((retry_at - datetime.now(UTC)).total_seconds(), 0.0)
    except (TypeError, ValueError, OverflowError):
        return None


def retry_delay(response: requests.Response | None, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After") if response is not None else None
    requested_delay = parse_retry_after(retry_after)
    if requested_delay is not None:
        return min(max(requested_delay, 1.0), 300.0)
    # Wikimedia asks clients to wait at least five seconds without a header.
    return min(5.0 * (2**attempt), 120.0)


def download_one(
    root: Path,
    record: DownloadRecord,
    user_agent: str,
    request_pacer: RequestPacer,
) -> DownloadResult:
    started = time.monotonic()
    final_path = destination_path(root, record)
    part_path = final_path.with_name(final_path.name + ".part")
    final_path.parent.mkdir(parents=True, exist_ok=True)

    if final_path.exists():
        actual_hash, actual_size = hash_file(final_path)
        if actual_size == record.byte_size and actual_hash == record.sha1_hex:
            return DownloadResult(
                record.page_id,
                True,
                str(final_path.relative_to(root)),
                actual_size,
                None,
                None,
                None,
                time.monotonic() - started,
            )
        os.replace(final_path, part_path)

    session = download_session(user_agent)
    last_error: Exception | None = None
    last_etag: str | None = None
    last_modified: str | None = None
    for attempt in range(DOWNLOAD_RETRIES):
        if STOP_EVENT.is_set():
            last_error = InterruptedError("download stopped")
            break
        response: requests.Response | None = None
        try:
            if part_path.exists():
                digest, offset = hash_file_state(part_path)
            else:
                digest, offset = hashlib.sha1(), 0
            if offset > record.byte_size:
                with part_path.open("wb"):
                    pass
                digest, offset = hashlib.sha1(), 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            if not request_pacer.acquire():
                raise InterruptedError("download stopped")
            response = session.get(
                record.original_url,
                headers=headers,
                stream=True,
                timeout=(30, 180),
                allow_redirects=True,
            )
            if response.status_code == 416 and offset == record.byte_size:
                response.close()
            else:
                if response.status_code in RETRYABLE_STATUS_CODES:
                    raise requests.HTTPError(
                        f"retryable HTTP {response.status_code}", response=response
                    )
                response.raise_for_status()
                resumed = offset > 0 and response.status_code == 206
                if resumed:
                    content_range = response.headers.get("Content-Range", "")
                    if not content_range.startswith(f"bytes {offset}-"):
                        raise OSError(
                            f"unexpected Content-Range for offset {offset}: "
                            f"{content_range or 'missing'}"
                        )
                else:
                    digest, offset = hashlib.sha1(), 0
                mode = "ab" if resumed else "wb"
                last_etag = response.headers.get("ETag")
                last_modified = response.headers.get("Last-Modified")
                actual_size = offset
                with part_path.open(mode) as destination:
                    for chunk in response.iter_content(chunk_size=4 * MIB):
                        if STOP_EVENT.is_set():
                            raise InterruptedError("download stopped")
                        if chunk:
                            destination.write(chunk)
                            digest.update(chunk)
                            actual_size += len(chunk)
                response.close()

            actual_hash = digest.hexdigest()
            actual_size = part_path.stat().st_size
            if actual_size < record.byte_size:
                raise OSError(
                    f"short download: {actual_size} of {record.byte_size} bytes"
                )
            if actual_size != record.byte_size:
                raise OSError(
                    f"size mismatch: expected {record.byte_size}, got {actual_size}"
                )
            if actual_hash != record.sha1_hex:
                # A complete file with the wrong content cannot be resumed.
                with part_path.open("wb"):
                    pass
                raise DownloadIntegrityError(
                    f"SHA-1 mismatch: expected {record.sha1_hex}, got {actual_hash}"
                )
            os.replace(part_path, final_path)
            return DownloadResult(
                record.page_id,
                True,
                str(final_path.relative_to(root)),
                actual_size,
                last_etag,
                last_modified,
                None,
                time.monotonic() - started,
            )
        except (requests.RequestException, OSError, InterruptedError) as exc:
            last_error = exc
            if response is not None:
                response.close()
            status_code = response.status_code if response is not None else None
            nonretryable_http = (
                isinstance(exc, requests.HTTPError)
                and status_code not in RETRYABLE_STATUS_CODES
            )
            if (
                isinstance(exc, (InterruptedError, DownloadIntegrityError))
                or nonretryable_http
                or attempt + 1 >= DOWNLOAD_RETRIES
            ):
                break
            delay = retry_delay(response, attempt)
            if status_code in {429, 503}:
                request_pacer.defer(
                    delay,
                    f"HTTP {status_code}",
                    reduce_rate=status_code == 429,
                )
            else:
                STOP_EVENT.wait(delay)
    partial_size = part_path.stat().st_size if part_path.exists() else 0
    return DownloadResult(
        record.page_id,
        False,
        None,
        partial_size,
        last_etag,
        last_modified,
        str(last_error or "unknown download error")[:2000],
        time.monotonic() - started,
    )


def rows_to_download_records(rows: Iterable[sqlite3.Row]) -> list[DownloadRecord]:
    return [
        DownloadRecord(
            page_id=int(row["page_id"]),
            title=str(row["title"]),
            original_url=str(row["original_url"]),
            byte_size=int(row["byte_size"]),
            mime_type=str(row["mime_type"]),
            sha1_hex=str(row["sha1_hex"]),
        )
        for row in rows
    ]


def download_images(
    connection: sqlite3.Connection,
    root: Path,
    *,
    workers: int,
    reserve_bytes: int,
    limit: int | None,
    user_agent: str,
    retry_failed: bool,
    requests_per_second: float,
) -> int:
    if retry_failed:
        connection.execute(
            "UPDATE images SET status = 'pending', last_error = NULL, updated_at = ? "
            "WHERE status = 'failed' AND eligible = 1 AND in_category = 1",
            (utc_now(),),
        )
    connection.execute(
        "UPDATE images SET status = 'pending', updated_at = ? "
        "WHERE status = 'in_progress' AND eligible = 1 AND in_category = 1",
        (utc_now(),),
    )
    connection.commit()

    eligible = connection.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(byte_size), 0) AS bytes "
        "FROM images WHERE eligible = 1 AND in_category = 1"
    ).fetchone()
    downloaded_before = connection.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(downloaded_bytes), 0) AS bytes "
        "FROM images WHERE status = 'downloaded' AND in_category = 1"
    ).fetchone()
    LOGGER.info(
        "Eligible photographic originals: %s files, %s total",
        f"{int(eligible['count']):,}",
        format_bytes(int(eligible["bytes"])),
    )
    LOGGER.info(
        "Already downloaded: %s files, %s",
        f"{int(downloaded_before['count']):,}",
        format_bytes(int(downloaded_before["bytes"])),
    )
    LOGGER.info(
        "Download policy: %s concurrent requests, %.2f request starts/s maximum",
        workers,
        requests_per_second,
    )
    set_state(connection, "download_workers", str(workers))
    set_state(connection, "download_requests_per_second", str(requests_per_second))
    connection.commit()

    completed_this_run = 0
    bytes_this_run = 0
    started = time.monotonic()
    last_report_at = started
    request_pacer = RequestPacer(requests_per_second)
    futures: dict[Future[DownloadResult], DownloadRecord] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        while True:
            fill_blocked_by: str | None = None
            while not STOP_EVENT.is_set() and len(futures) < workers:
                if limit is not None and completed_this_run + len(futures) >= limit:
                    fill_blocked_by = "limit"
                    break
                row = connection.execute(
                    """
                    SELECT page_id, title, original_url, byte_size, mime_type,
                        sha1_hex
                    FROM images
                    WHERE status = 'pending' AND eligible = 1 AND in_category = 1
                    ORDER BY byte_size ASC, page_id ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    fill_blocked_by = "empty"
                    break
                record = rows_to_download_records([row])[0]
                available = shutil.disk_usage(root).free
                inflight_bytes = sum(item.byte_size for item in futures.values())
                budget = max(available - reserve_bytes - inflight_bytes, 0)
                if record.byte_size > budget:
                    fill_blocked_by = "space"
                    break

                now = utc_now()
                connection.execute(
                    "UPDATE images SET status = 'in_progress', "
                    "attempts = attempts + 1, download_started_at = ?, "
                    "updated_at = ? WHERE page_id = ? AND status = 'pending'",
                    (now, now, record.page_id),
                )
                connection.commit()
                future = executor.submit(
                    download_one,
                    root,
                    record,
                    user_agent,
                    request_pacer,
                )
                futures[future] = record

            if not futures:
                if fill_blocked_by == "space":
                    available = shutil.disk_usage(root).free
                    LOGGER.warning(
                        "Stopping before the free-space reserve: %s free, %s reserved",
                        format_bytes(available),
                        format_bytes(reserve_bytes),
                    )
                    set_state(
                        connection, "download_stopped_reason", "free-space reserve"
                    )
                    set_state(connection, "download_stopped_at", utc_now())
                    connection.commit()
                break

            done, _pending = wait(
                tuple(futures), timeout=1.0, return_when=FIRST_COMPLETED
            )
            if not done:
                continue
            for future in done:
                record = futures.pop(future)
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - preserve worker state.
                    result = DownloadResult(
                        record.page_id,
                        False,
                        None,
                        0,
                        None,
                        None,
                        f"unexpected worker error: {exc}"[:2000],
                        0,
                    )
                now = utc_now()
                if result.ok:
                    connection.execute(
                        """
                        UPDATE images SET status = 'downloaded', download_path = ?,
                            downloaded_bytes = ?, http_etag = ?,
                            http_last_modified = ?, last_error = NULL,
                            downloaded_at = ?, updated_at = ?
                        WHERE page_id = ?
                        """,
                        (
                            result.relative_path,
                            result.downloaded_bytes,
                            result.etag,
                            result.last_modified,
                            now,
                            now,
                            result.page_id,
                        ),
                    )
                    completed_this_run += 1
                    bytes_this_run += result.downloaded_bytes
                else:
                    status = "pending" if STOP_EVENT.is_set() else "failed"
                    connection.execute(
                        """
                        UPDATE images SET status = ?, downloaded_bytes = ?,
                            http_etag = ?, http_last_modified = ?, last_error = ?,
                            updated_at = ? WHERE page_id = ?
                        """,
                        (
                            status,
                            result.downloaded_bytes,
                            result.etag,
                            result.last_modified,
                            result.error,
                            now,
                            result.page_id,
                        ),
                    )
                    if not STOP_EVENT.is_set():
                        LOGGER.error("Failed %s: %s", record.title, result.error)
            connection.commit()

            now_monotonic = time.monotonic()
            if now_monotonic - last_report_at >= 60.0:
                elapsed = max(now_monotonic - started, 0.001)
                LOGGER.info(
                    "Downloaded %s files (%s) this run at %s/s; %s free",
                    f"{completed_this_run:,}",
                    format_bytes(bytes_this_run),
                    format_bytes(bytes_this_run / elapsed),
                    format_bytes(shutil.disk_usage(root).free),
                )
                last_report_at = now_monotonic

    if completed_this_run:
        elapsed = max(time.monotonic() - started, 0.001)
        LOGGER.info(
            "Download run ended after %s files (%s) at %s/s",
            f"{completed_this_run:,}",
            format_bytes(bytes_this_run),
            format_bytes(bytes_this_run / elapsed),
        )

    return completed_this_run


def database_status(db_path: Path, root: Path) -> dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(f"database does not exist: {db_path}")
    connection = connect_database(db_path, read_only=True)
    try:
        status_rows = connection.execute(
            "SELECT status, COUNT(*) AS count, COALESCE(SUM(byte_size), 0) AS bytes "
            "FROM images GROUP BY status ORDER BY status"
        ).fetchall()
        mime_rows = connection.execute(
            "SELECT COALESCE(mime_type, 'unknown') AS mime_type, COUNT(*) AS count, "
            "COALESCE(SUM(byte_size), 0) AS bytes FROM images "
            "GROUP BY mime_type ORDER BY count DESC"
        ).fetchall()
        state_rows = connection.execute("SELECT key, value FROM state").fetchall()
        disk = shutil.disk_usage(root)
        return {
            "root": str(root),
            "database": str(db_path),
            "disk": {
                "total": disk.total,
                "used": disk.used,
                "free": disk.free,
            },
            "status": {
                str(row["status"]): {
                    "count": int(row["count"]),
                    "bytes": int(row["bytes"]),
                }
                for row in status_rows
            },
            "mime_types": {
                str(row["mime_type"]): {
                    "count": int(row["count"]),
                    "bytes": int(row["bytes"]),
                }
                for row in mime_rows
            },
            "state": {str(row["key"]): str(row["value"]) for row in state_rows},
        }
    finally:
        connection.close()


def print_status(status: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return
    print(f"Root: {status['root']}")
    print(f"Database: {status['database']}")
    disk = status["disk"]
    print(f"Disk: {format_bytes(disk['free'])} free of {format_bytes(disk['total'])}")
    state = status["state"]
    print(
        "Discovery: "
        + ("complete" if state.get("discovery_complete") == "1" else "in progress")
    )
    print(
        "Attribution: "
        + (
            "complete"
            if state.get("metadata_enrichment_complete") == "1"
            else "in progress or not started"
        )
    )
    for name, values in status["status"].items():
        print(
            f"  {name:12s} {values['count']:>9,} files  "
            f"{format_bytes(values['bytes']):>12s}"
        )
    if status["mime_types"]:
        print("MIME types:")
        for name, values in status["mime_types"].items():
            print(
                f"  {name:24s} {values['count']:>9,} files  "
                f"{format_bytes(values['bytes']):>12s}"
            )


def install_signal_handlers() -> None:
    def handle_signal(signum: int, _frame: Any) -> None:
        LOGGER.warning("Received signal %s; stopping after active writes", signum)
        STOP_EVENT.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("run", "discover", "enrich", "download", "status"),
        nargs="?",
        default="run",
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--requests-per-second",
        type=float,
        default=DEFAULT_REQUESTS_PER_SECOND,
    )
    parser.add_argument("--reserve-gib", type=float, default=DEFAULT_RESERVE_GIB)
    parser.add_argument("--discover-limit", type=int)
    parser.add_argument("--enrich-limit", type=int)
    parser.add_argument("--download-limit", type=int)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--anonymous", action="store_true")
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--json", action="store_true", help="JSON output for status")
    parser.add_argument("--verbose", action="store_true")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.workers < 1 or args.workers > MAX_DOWNLOAD_WORKERS:
        raise ValueError(
            f"--workers must be between 1 and {MAX_DOWNLOAD_WORKERS} to comply "
            "with Wikimedia concurrency guidance"
        )
    if not 0 < args.requests_per_second <= MAX_REQUESTS_PER_SECOND:
        raise ValueError(
            "--requests-per-second must be greater than zero and no more than "
            f"{MAX_REQUESTS_PER_SECOND:g}"
        )
    if args.reserve_gib < 0:
        raise ValueError("--reserve-gib cannot be negative")
    for name in ("discover_limit", "enrich_limit", "download_limit"):
        value = getattr(args, name)
        if value is not None and value < 1:
            raise ValueError(f"--{name.replace('_', '-')} must be positive")


def run(args: argparse.Namespace) -> int:
    validate_args(args)
    root = args.root.expanduser().resolve()
    db_path = (
        args.database.expanduser().resolve()
        if args.database is not None
        else root / "quality_images.sqlite3"
    )
    log_file = args.log_file.expanduser().resolve() if args.log_file else None
    configure_logging(log_file, args.verbose)

    if args.command == "status":
        print_status(database_status(db_path, root), as_json=args.json)
        return 0

    root.mkdir(parents=True, exist_ok=True)
    (root / "images").mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_database(db_path)
    initialize_database(connection)
    install_signal_handlers()

    client: CommonsClient | None = None
    enrichment_thread: threading.Thread | None = None
    enrichment_errors: list[Exception] = []
    try:
        if args.command in {"run", "discover", "enrich"}:
            client = client_from_config(args.config, args.anonymous)
            client.login()
        if args.command in {"run", "discover"}:
            assert client is not None
            discover_images(
                connection,
                client,
                refresh=args.refresh,
                limit=args.discover_limit,
            )
        if args.command == "enrich" and not STOP_EVENT.is_set():
            assert client is not None
            enrich_metadata(connection, client, limit=args.enrich_limit)
        if (
            args.command == "run"
            and not STOP_EVENT.is_set()
            and get_state(connection, "discovery_complete", "0") == "1"
        ):
            assert client is not None

            def metadata_worker() -> None:
                metadata_connection = connect_database(db_path)
                try:
                    initialize_database(metadata_connection)
                    enrich_metadata(
                        metadata_connection,
                        client,
                        limit=args.enrich_limit,
                    )
                except InterruptedError as exc:
                    if not STOP_EVENT.is_set():
                        enrichment_errors.append(exc)
                        STOP_EVENT.set()
                except Exception as exc:  # noqa: BLE001 - propagate to main thread.
                    enrichment_errors.append(exc)
                    STOP_EVENT.set()
                finally:
                    metadata_connection.close()

            enrichment_thread = threading.Thread(
                target=metadata_worker,
                name="commons-attribution-enrichment",
            )
            enrichment_thread.start()
        if args.command in {"run", "download"} and not STOP_EVENT.is_set():
            if client is not None:
                user_agent = client.session.headers["User-Agent"]
            else:
                config = load_config(args.config).commons
                owner = config.username.split("@", 1)[0].strip() or "pupphoto"
                user_agent = (
                    "pupphoto-quality-image-archiver/1.0 "
                    f"(https://commons.wikimedia.org/wiki/User:{quote(owner)})"
                )
            download_images(
                connection,
                root,
                workers=args.workers,
                reserve_bytes=int(args.reserve_gib * GIB),
                limit=args.download_limit,
                user_agent=user_agent,
                retry_failed=args.retry_failed,
                requests_per_second=args.requests_per_second,
            )
        if enrichment_thread is not None:
            enrichment_thread.join()
        if enrichment_errors:
            raise RuntimeError("Commons attribution enrichment failed") from (
                enrichment_errors[0]
            )
        set_state(connection, "last_clean_exit_at", utc_now())
        connection.commit()
    finally:
        if enrichment_thread is not None and enrichment_thread.is_alive():
            STOP_EVENT.set()
            enrichment_thread.join()
        if client is not None:
            client.close()
        connection.close()

    if db_path.exists():
        print_status(database_status(db_path, root), as_json=False)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        STOP_EVENT.set()
        return 130
    except InterruptedError:
        if STOP_EVENT.is_set():
            LOGGER.info("Quality Image archiver stopped cleanly")
            return 0
        raise
    except Exception:
        if not LOGGER.handlers:
            configure_logging(None, False)
        LOGGER.exception("Quality Image archiver failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
