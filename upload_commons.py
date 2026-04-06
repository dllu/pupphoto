#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import socket
import subprocess
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from fractions import Fraction
from html import escape
from io import BytesIO
from functools import lru_cache
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request
from openai import OpenAI
from PIL import ExifTags, Image, ImageOps

from config import CommonsConfig, OpenAIConfig, load_config
from gps import is_in_banned_area


Image.MAX_IMAGE_PIXELS = None

EXIF_TAGS = ExifTags.TAGS
GPS_TAGS = ExifTags.GPSTAGS
RAW_SHA1_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}_.+_([0-9a-f]{40})\.[^.]+$",
    re.IGNORECASE,
)


@dataclass
class PhotoMetadata:
    width: int
    height: int
    captured_at: str | None
    captured_on: str | None
    captured_year: int | None
    make: str | None
    model: str | None
    lens_model: str | None
    focal_length_mm: float | None
    exposure_time_seconds: float | None
    exposure_time_label: str | None
    f_number: float | None
    f_number_label: str | None
    iso: int | None
    latitude: float | None
    longitude: float | None
    location_allowed: bool
    raw_sha1sum: str | None

    def to_model_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["location_allowed"] = self.location_allowed
        if not self.location_allowed:
            data["latitude"] = None
            data["longitude"] = None
        return data


@dataclass
class CategoryNode:
    title: str
    parents: list[str]
    query_hits: list[str]
    source: str
    file_count: int
    subcategory_count: int


def _get_exif_value(exif: dict[int, Any], tag_name: str) -> Any:
    for tag, value in exif.items():
        if EXIF_TAGS.get(tag) == tag_name:
            return value
    return None


def _rational_to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        pass
    if isinstance(value, tuple) and len(value) == 2 and value[1]:
        return value[0] / value[1]
    return None


def _gps_to_decimal(values: Any, ref: str | None) -> float | None:
    if not values:
        return None
    parts = [_rational_to_float(item) for item in values]
    if any(part is None for part in parts[:3]):
        return None
    degrees, minutes, seconds = parts[:3]
    decimal = degrees + minutes / 60 + seconds / 3600
    if ref and ref.upper() in {"S", "W"}:
        decimal = -decimal
    return decimal


def _format_fraction(seconds: float) -> str:
    if seconds <= 0:
        return f"{seconds:g} sec"
    if seconds >= 1:
        if abs(seconds - round(seconds)) < 1e-9:
            return f"{int(round(seconds))} sec"
        return f"{seconds:.1f} sec".rstrip("0").rstrip(".")
    frac = Fraction(seconds).limit_denominator(8000)
    if frac.numerator == 1:
        return f"1/{frac.denominator} sec"
    return f"{frac.numerator}/{frac.denominator} sec"


def _format_f_number(value: float | None) -> str | None:
    if value is None:
        return None
    if value <= 0:
        return None
    if abs(value - round(value)) < 0.05:
        return f"f/{int(round(value))}"
    rounded = round(value, 1)
    return f"f/{rounded:g}"


def _normalize_camera_make_model(make: str | None, model: str | None) -> str | None:
    if not make and not model:
        return None
    make_clean = (make or "").strip().title()
    model_clean = (model or "").strip()
    model_clean = re.sub(r"\s+", " ", model_clean)
    if make_clean and model_clean.upper().startswith(make_clean.upper()):
        return model_clean
    if make_clean and model_clean:
        return f"{make_clean} {model_clean}"
    return make_clean or model_clean


@lru_cache(maxsize=128)
def _exiv2_metadata_map(image_path: str) -> dict[str, str]:
    try:
        output = subprocess.check_output(
            ["exiv2", "-pa", image_path],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {}
    metadata: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"^(\S+)\s+(\S+)\s+\d+\s+(.*)$", line)
        if not match:
            continue
        tag, _type_name, value = match.groups()
        metadata[tag] = value.strip()
    return metadata


def _exiv2_tag_value(image_path: Path, tag: str) -> str | None:
    return _exiv2_metadata_map(str(image_path)).get(tag)


def _parse_exiv2_float(value: str | None, suffix: str = "") -> float | None:
    if not value:
        return None
    cleaned = value.removeprefix("F").strip()
    if suffix and cleaned.endswith(suffix):
        cleaned = cleaned[: -len(suffix)].strip()
    rational_match = re.search(
        r"([-+]?\d+(?:\.\d+)?)\s*/\s*([-+]?\d+(?:\.\d+)?)", cleaned
    )
    if rational_match:
        numerator = float(rational_match.group(1))
        denominator = float(rational_match.group(2))
        if denominator != 0:
            return numerator / denominator
    match = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _parse_exiv2_int(value: str | None) -> int | None:
    parsed = _parse_exiv2_float(value)
    if parsed is None:
        return None
    return int(round(parsed))


def _supplement_metadata_with_exiv2(
    image_path: Path, metadata: PhotoMetadata
) -> PhotoMetadata:
    if metadata.focal_length_mm is None:
        metadata.focal_length_mm = _parse_exiv2_float(
            _exiv2_tag_value(image_path, "Exif.Photo.FocalLength"),
            "mm",
        )
    if metadata.exposure_time_seconds is None:
        metadata.exposure_time_seconds = _parse_exiv2_float(
            _exiv2_tag_value(image_path, "Exif.Photo.ExposureTime"),
            "s",
        )
    if metadata.f_number is None:
        metadata.f_number = _parse_exiv2_float(
            _exiv2_tag_value(image_path, "Exif.Photo.FNumber")
        )
    if metadata.lens_model is None:
        metadata.lens_model = _exiv2_tag_value(image_path, "Exif.Photo.LensModel")
    if metadata.iso is None:
        metadata.iso = _parse_exiv2_int(
            _exiv2_tag_value(image_path, "Exif.Photo.ISOSpeedRatings")
        )
    if metadata.make is None:
        metadata.make = _exiv2_tag_value(image_path, "Exif.Image.Make")
    if metadata.model is None:
        metadata.model = _exiv2_tag_value(image_path, "Exif.Image.Model")
    if (
        metadata.captured_on is None
        or metadata.captured_at is None
        or metadata.captured_year is None
    ):
        captured_raw = _exiv2_tag_value(image_path, "Exif.Photo.DateTimeOriginal")
        if captured_raw:
            try:
                captured_dt = datetime.strptime(captured_raw, "%Y:%m:%d %H:%M:%S")
                metadata.captured_at = captured_dt.isoformat()
                metadata.captured_on = captured_dt.date().isoformat()
                metadata.captured_year = captured_dt.year
            except ValueError:
                pass
    if (
        metadata.exposure_time_label is None
        and metadata.exposure_time_seconds is not None
    ):
        metadata.exposure_time_label = _format_fraction(metadata.exposure_time_seconds)
    if metadata.f_number_label is None and metadata.f_number is not None:
        metadata.f_number_label = _format_f_number(metadata.f_number)
    if metadata.focal_length_mm is not None:
        metadata.focal_length_mm = round(metadata.focal_length_mm, 2)
    return metadata


def _read_metadata(image_path: Path) -> PhotoMetadata:
    with Image.open(image_path) as img:
        img = ImageOps.exif_transpose(img)
        width, height = img.size
        exif = img.getexif()

    make = _get_exif_value(exif, "Make")
    model = _get_exif_value(exif, "Model")
    lens_model = _get_exif_value(exif, "LensModel")
    focal_length = _rational_to_float(_get_exif_value(exif, "FocalLength"))
    exposure_time = _rational_to_float(_get_exif_value(exif, "ExposureTime"))
    f_number = _rational_to_float(_get_exif_value(exif, "FNumber"))
    iso_value = _get_exif_value(exif, "ISOSpeedRatings")
    if iso_value is None:
        iso_value = _get_exif_value(exif, "PhotographicSensitivity")
    iso = None
    if isinstance(iso_value, (list, tuple)):
        iso = int(iso_value[0]) if iso_value else None
    elif iso_value is not None:
        iso = int(iso_value)

    captured_raw = _get_exif_value(exif, "DateTimeOriginal") or _get_exif_value(
        exif, "DateTime"
    )
    captured_at = None
    captured_on = None
    captured_year = None
    if captured_raw:
        try:
            captured_dt = datetime.strptime(str(captured_raw), "%Y:%m:%d %H:%M:%S")
            captured_at = captured_dt.isoformat()
            captured_on = captured_dt.date().isoformat()
            captured_year = captured_dt.year
        except ValueError:
            pass

    gps_info: dict[Any, Any] = {}
    gps_tag = next((tag for tag, name in EXIF_TAGS.items() if name == "GPSInfo"), None)
    if gps_tag is not None and hasattr(exif, "get_ifd"):
        try:
            gps_candidate = exif.get_ifd(gps_tag)
            if isinstance(gps_candidate, dict):
                gps_info = gps_candidate
        except Exception:
            pass
    if not gps_info:
        gps_candidate = _get_exif_value(exif, "GPSInfo")
        if isinstance(gps_candidate, dict):
            gps_info = gps_candidate
    gps = {GPS_TAGS.get(tag, tag): value for tag, value in gps_info.items()}
    latitude = _gps_to_decimal(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
    longitude = _gps_to_decimal(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
    location_allowed = True
    if latitude is not None and longitude is not None:
        location_allowed = not is_in_banned_area(latitude, longitude)

    raw_sha1sum = None
    match = RAW_SHA1_RE.match(image_path.name)
    if match:
        raw_sha1sum = match.group(1)

    metadata = PhotoMetadata(
        width=width,
        height=height,
        captured_at=captured_at,
        captured_on=captured_on,
        captured_year=captured_year,
        make=str(make).strip() if make else None,
        model=str(model).strip() if model else None,
        lens_model=str(lens_model).strip() if lens_model else None,
        focal_length_mm=round(focal_length, 2) if focal_length is not None else None,
        exposure_time_seconds=exposure_time,
        exposure_time_label=_format_fraction(exposure_time) if exposure_time else None,
        f_number=f_number,
        f_number_label=_format_f_number(f_number),
        iso=iso,
        latitude=latitude,
        longitude=longitude,
        location_allowed=location_allowed,
        raw_sha1sum=raw_sha1sum,
    )
    return _supplement_metadata_with_exiv2(image_path, metadata)


def _downsize_image(image_path: Path, max_dimension: int) -> tuple[bytes, str]:
    with Image.open(image_path) as img:
        img = ImageOps.exif_transpose(img)
        img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        output = BytesIO()
        img.save(output, format="JPEG", quality=92)
    return output.getvalue(), "image/jpeg"


def _image_data_url(image_bytes: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _json_schema(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": name,
        "strict": True,
        "schema": schema,
    }


class CommonsApi:
    _TITLE_BATCH_SIZE = 50
    _MAX_RETRIES = 4
    _RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(self, config: CommonsConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "pupphoto/0.1 Wikimedia Commons uploader",
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
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload_params = {**(params or {}), "format": "json", "formatversion": "2"}
        for attempt in range(self._MAX_RETRIES + 1):
            response = self.session.request(
                method,
                self.config.api_url,
                params=payload_params,
                data=data,
                files=files,
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

    def post_upload(
        self, params: dict[str, Any], files: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request_json(
            "POST",
            data={**params, "format": "json", "formatversion": "2"},
            files=files,
            timeout=300,
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

    def search_categories(self, query: str, limit: int) -> list[str]:
        print(f"Searching Commons categories for query: {query}", flush=True)
        data = self.get(
            action="query",
            list="search",
            srsearch=query,
            srnamespace="14",
            srwhat="text",
            srlimit=limit,
        )
        return [item["title"] for item in data.get("query", {}).get("search", [])]

    def search_files_by_raw_sha1(self, raw_sha1sum: str, limit: int = 10) -> list[str]:
        print(
            f"Searching Commons files for raw SHA1 sum: {raw_sha1sum}", flush=True
        )
        data = self.get(
            action="query",
            list="search",
            srsearch=f"\"{raw_sha1sum}\"",
            srnamespace="6",
            srwhat="text",
            srlimit=limit,
        )
        return [item["title"] for item in data.get("query", {}).get("search", [])]

    def category_exists(self, title: str) -> bool:
        pages = self.get(action="query", titles=title)["query"]["pages"]
        return bool(pages) and "missing" not in pages[0]

    def category_details(self, titles: list[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for batch_start in range(0, len(titles), self._TITLE_BATCH_SIZE):
            batch = titles[batch_start : batch_start + self._TITLE_BATCH_SIZE]
            pages = self.get(
                action="query",
                titles="|".join(batch),
                prop="categories|categoryinfo",
                cllimit="max",
                clshow="!hidden",
            )["query"]["pages"]
            for page in pages:
                info = page.get("categoryinfo", {})
                result[page["title"]] = {
                    "parents": [
                        category["title"] for category in page.get("categories", [])
                    ],
                    "file_count": int(info.get("files", 0)),
                    "subcategory_count": int(info.get("subcats", 0)),
                }
        return result

    def category_counts(self, titles: list[str]) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for batch_start in range(0, len(titles), self._TITLE_BATCH_SIZE):
            batch = titles[batch_start : batch_start + self._TITLE_BATCH_SIZE]
            pages = self.get(
                action="query",
                titles="|".join(batch),
                prop="categoryinfo",
            )["query"]["pages"]
            for page in pages:
                info = page.get("categoryinfo", {})
                result[page["title"]] = {
                    "file_count": int(info.get("files", 0)),
                    "subcategory_count": int(info.get("subcats", 0)),
                }
        return result

    def upload_file(
        self,
        image_path: Path,
        filename: str,
        description_wikitext: str,
        summary: str,
    ) -> str:
        self.login()
        token = self.csrf_token()
        print(f"Uploading file to Commons as {filename}...", flush=True)
        with image_path.open("rb") as f:
            data = self.post_upload(
                params={
                    "action": "upload",
                    "filename": filename,
                    "comment": summary,
                    "text": description_wikitext,
                    "ignorewarnings": "0",
                    "token": token,
                },
                files={
                    "file": (
                        filename,
                        f,
                        mimetypes.guess_type(filename)[0] or "application/octet-stream",
                    )
                },
            )
        upload = data["upload"]
        if upload["result"] != "Success":
            raise RuntimeError(f"Commons upload failed: {upload}")
        return f"https://commons.wikimedia.org/wiki/File:{filename.replace(' ', '_')}"

    def overwrite_file(
        self,
        image_path: Path,
        title: str,
        summary: str,
    ) -> str:
        self.login()
        token = self.csrf_token()
        filename = title.removeprefix("File:")
        print(f"Overwriting Commons file {title}...", flush=True)
        with image_path.open("rb") as f:
            data = self.post_upload(
                params={
                    "action": "upload",
                    "filename": filename,
                    "comment": summary,
                    "ignorewarnings": "1",
                    "token": token,
                },
                files={
                    "file": (
                        filename,
                        f,
                        mimetypes.guess_type(filename)[0] or "application/octet-stream",
                    )
                },
            )
        upload = data["upload"]
        if upload["result"] != "Success":
            raise RuntimeError(f"Commons overwrite failed: {upload}")
        return _commons_file_url(title)


class VisionClient:
    def __init__(self, config: OpenAIConfig):
        self.config = config
        self.client = OpenAI(api_key=config.api_key)

    def propose_metadata(
        self,
        image_data_url: str,
        metadata: PhotoMetadata,
        suffix_hint: str,
        user_hint: str,
    ) -> dict[str, Any]:
        print(
            "Requesting filename, caption, and category search hints from OpenAI...",
            flush=True,
        )
        schema = {
            "type": "object",
            "properties": {
                "filename_stem": {"type": "string"},
                "caption_en": {"type": "string"},
                "keywords": {"type": "array", "items": {"type": "string"}},
                "search_queries": {"type": "array", "items": {"type": "string"}},
                "visual_summary": {"type": "string"},
            },
            "required": [
                "filename_stem",
                "caption_en",
                "keywords",
                "search_queries",
                "visual_summary",
            ],
            "additionalProperties": False,
        }
        instructions = (
            "You are preparing a Wikimedia Commons upload. "
            "Return JSON only. "
            "Write a concise English filename stem using plain ASCII words separated by spaces, "
            "without file extension and without the configured suffix. "
            "Write a concise, factual, neutral, encyclopedic English caption. "
            "Treat the optional user hint as high-priority factual context when it provides names, locations, dates, events, or the shooting viewpoint. "
            "Silently correct likely misspellings in the user hint when generating the caption, keywords, and category search queries. "
            "Generate search queries for Wikimedia Commons categories that emphasize specific depicted subjects, "
            "locations, events, operators, object classes, and year if relevant. "
            "When the image is described as a subject seen from a named place or viewpoint, include highly specific queries for that relationship, such as "
            '"<subject> from <viewpoint>", "views of <subject> from <viewpoint>", and other exact proper-name combinations that could match an existing Commons category. '
            "Prefer exact proper names over generic descriptions in search queries. "
            "When the time of day or lighting is visually clear and categorically useful, include search queries for that too, such as dusk, sunset, dawn, night, or blue hour, combined with the location if appropriate. "
            "Do not include photographic equipment categories in the search queries; those are handled separately. "
            "Avoid speculation and avoid promotional language. "
            f"The upload workflow will append this suffix later if non-empty: {suffix_hint!r}."
        )
        response = self.client.responses.create(
            model=self.config.vision_model,
            reasoning={"effort": "high"},
            input=[
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": instructions}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Image metadata JSON:\n"
                            + json.dumps(
                                metadata.to_model_dict(), indent=2, sort_keys=True
                            )
                            + "\n\nOptional user hint:\n"
                            + (user_hint or "(none)"),
                        },
                        {
                            "type": "input_image",
                            "image_url": image_data_url,
                            "detail": self.config.image_detail,
                        },
                    ],
                },
            ],
            text={"format": _json_schema("commons_upload_proposal", schema)},
        )
        return json.loads(response.output_text)

    def choose_categories(
        self,
        image_data_url: str,
        metadata: PhotoMetadata,
        proposed_caption: str,
        proposed_summary: str,
        category_graph: dict[str, Any],
        user_hint: str,
    ) -> dict[str, Any]:
        print("Requesting final category selection from OpenAI...", flush=True)
        schema = {
            "type": "object",
            "properties": {
                "selected_categories": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "reasoning_summary": {"type": "string"},
            },
            "required": ["selected_categories", "reasoning_summary"],
            "additionalProperties": False,
        }
        instructions = (
            "You are selecting Wikimedia Commons topical categories for a single image. "
            "Return JSON only. "
            "Choose only from the supplied candidate graph. "
            "Prefer the most specific applicable categories. "
            "Treat the optional user hint as high-priority factual context when it identifies the subject, place, event, or named viewpoint, even if the image alone would be ambiguous. "
            "If the hint implies a viewpoint or depiction relationship such as a landmark seen from a named overlook, prefer a candidate category that captures that exact relationship. "
            "Each candidate includes file_count and subcategory_count from Commons. "
            "In general, do not select categories with file_count = 0 for a photo upload, even if they have subcategories. "
            "Prefer categories that already contain files when an otherwise-similar empty category is available. "
            "Do not select both a category and its ancestor when the child already fully covers the image. "
            "Avoid maintenance, creator, user, and campaign categories. "
            "Do not add photographic equipment or exposure parameter categories; those are handled separately. "
            "Choose categories that are directly supported by the image and metadata. "
            "If the image clearly shows a meaningful time-of-day or lighting condition such as dusk, sunset, dawn, or night, include an appropriate category for that when it exists in the candidate graph. "
            "If a place, operator, event, station, vehicle type, or year category is clearly applicable, prefer the specific leaf category over a broad regional or parent category."
        )
        response = self.client.responses.create(
            model=self.config.vision_model,
            reasoning={"effort": "high"},
            input=[
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": instructions}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Known metadata JSON:\n"
                            + json.dumps(
                                metadata.to_model_dict(), indent=2, sort_keys=True
                            )
                            + "\n\nProposed caption:\n"
                            + proposed_caption
                            + "\n\nVisual summary:\n"
                            + proposed_summary
                            + "\n\nOptional user hint:\n"
                            + (user_hint or "(none)")
                            + "\n\nCandidate category graph JSON:\n"
                            + json.dumps(category_graph, indent=2, sort_keys=True),
                        },
                        {
                            "type": "input_image",
                            "image_url": image_data_url,
                            "detail": self.config.image_detail,
                        },
                    ],
                },
            ],
            text={"format": _json_schema("commons_category_selection", schema)},
        )
        return json.loads(response.output_text)


def _preferred_filename(stem: str, suffix: str, ext: str) -> str:
    sanitized = re.sub(r"\s+", " ", stem.strip())
    sanitized = re.sub(r"[\\/:*?\"<>|#%{}\[\]]+", " ", sanitized)
    sanitized = re.sub(r"\s+", " ", sanitized).strip(" .")
    if not sanitized:
        sanitized = "Untitled"
    if suffix.strip():
        sanitized = f"{sanitized} {suffix.strip()}".strip()
    return f"{sanitized}{ext.lower()}"


def _equipment_category_candidates(metadata: PhotoMetadata) -> list[str]:
    candidates: list[str] = []
    camera_label = _normalize_camera_make_model(metadata.make, metadata.model)
    if camera_label:
        candidates.append(f"Category:Taken with {camera_label}")
    if metadata.focal_length_mm is not None:
        candidates.append(
            f"Category:Lens focal length {int(round(metadata.focal_length_mm))} mm"
        )
    if metadata.exposure_time_label:
        candidates.append(f"Category:Exposure time {metadata.exposure_time_label}")
    if metadata.f_number_label:
        candidates.append(f"Category:F-number {metadata.f_number_label}")
    return candidates


def _resolve_equipment_categories(
    commons_api: CommonsApi, metadata: PhotoMetadata
) -> list[str]:
    categories: list[str] = []
    print("Resolving equipment and exposure categories from EXIF...", flush=True)
    for title in _equipment_category_candidates(metadata):
        if commons_api.category_exists(title):
            categories.append(title.removeprefix("Category:"))
    return categories


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _build_category_graph(
    commons_api: CommonsApi,
    queries: list[str],
    limit_per_query: int,
    max_candidate_categories: int,
    max_parent_depth: int,
) -> dict[str, Any]:
    print("Building candidate Commons category graph...", flush=True)
    seed_nodes: dict[str, CategoryNode] = {}
    for query in _dedupe_preserve_order(queries):
        for title in commons_api.search_categories(query, limit_per_query):
            node = seed_nodes.setdefault(
                title,
                CategoryNode(
                    title=title,
                    parents=[],
                    query_hits=[],
                    source="search",
                    file_count=0,
                    subcategory_count=0,
                ),
            )
            node.query_hits.append(query)
    limited_titles = list(seed_nodes)[:max_candidate_categories]
    graph_nodes: dict[str, CategoryNode] = {
        title: seed_nodes[title] for title in limited_titles
    }
    frontier = list(graph_nodes)
    detailed_titles: set[str] = set()
    depth = 0
    while frontier and depth < max_parent_depth:
        details_map = commons_api.category_details(frontier)
        next_frontier: list[str] = []
        for title in frontier:
            details = details_map.get(title, {})
            parents = details.get("parents", [])
            graph_nodes[title].parents = parents
            graph_nodes[title].file_count = int(details.get("file_count", 0))
            graph_nodes[title].subcategory_count = int(
                details.get("subcategory_count", 0)
            )
            detailed_titles.add(title)
            for parent in parents:
                if parent not in graph_nodes:
                    graph_nodes[parent] = CategoryNode(
                        title=parent,
                        parents=[],
                        query_hits=[],
                        source="ancestor",
                        file_count=0,
                        subcategory_count=0,
                    )
                    next_frontier.append(parent)
        frontier = next_frontier
        depth += 1
    remaining_titles = [title for title in graph_nodes if title not in detailed_titles]
    counts_map = commons_api.category_counts(remaining_titles)
    for title, node in graph_nodes.items():
        counts = counts_map.get(title, {})
        if counts:
            node.file_count = int(counts.get("file_count", 0))
            node.subcategory_count = int(counts.get("subcategory_count", 0))
    return {
        "nodes": {
            title: {
                "parents": node.parents,
                "query_hits": _dedupe_preserve_order(node.query_hits),
                "source": node.source,
                "file_count": node.file_count,
                "subcategory_count": node.subcategory_count,
            }
            for title, node in sorted(graph_nodes.items())
        }
    }


def _remove_ancestor_duplicates(
    selected_categories: list[str], graph: dict[str, Any]
) -> list[str]:
    node_map = graph.get("nodes", {})

    def ancestors_of(title: str) -> set[str]:
        stack = list(node_map.get(f"Category:{title}", {}).get("parents", []))
        seen: set[str] = set()
        while stack:
            parent = stack.pop()
            if parent in seen:
                continue
            seen.add(parent)
            stack.extend(node_map.get(parent, {}).get("parents", []))
        return {item.removeprefix("Category:") for item in seen}

    selected_set = set(selected_categories)
    filtered: list[str] = []
    for category in selected_categories:
        if selected_set.intersection(ancestors_of(category)):
            continue
        filtered.append(category)
    return filtered


def _remove_empty_categories(
    selected_categories: list[str], graph: dict[str, Any]
) -> list[str]:
    node_map = graph.get("nodes", {})
    filtered: list[str] = []
    removed: list[str] = []
    for category in selected_categories:
        node = node_map.get(f"Category:{category}", {})
        if int(node.get("file_count", 0)) <= 0:
            removed.append(category)
            continue
        filtered.append(category)
    if removed:
        print(
            "Dropping zero-file topical categories: "
            + json.dumps(removed, ensure_ascii=True),
            flush=True,
        )
    return filtered


def _wikitext_categories(categories: list[str]) -> str:
    return "\n".join(f"[[Category:{category}]]" for category in categories)


def _wikitext_location(metadata: PhotoMetadata) -> str:
    if (
        metadata.location_allowed
        and metadata.latitude is not None
        and metadata.longitude is not None
    ):
        return f"\n{{{{Location|{metadata.latitude:.7f}|{metadata.longitude:.7f}}}}}"
    return ""


def _raw_sha1_field(metadata: PhotoMetadata) -> str:
    if not metadata.raw_sha1sum:
        return ""
    return (
        "\n|other fields = "
        "{{Information field | name = Raw file SHA1 sum | value = "
        f"{metadata.raw_sha1sum}"
        "}}"
    )


def build_description_wikitext(
    caption: str,
    metadata: PhotoMetadata,
    author: str,
    license_wikitext: str,
    categories: list[str],
) -> str:
    date_value = metadata.captured_on or datetime.now(timezone.utc).date().isoformat()
    return (
        "=={{int:filedesc}}==\n"
        "{{Information\n"
        "|description =\n"
        f"{{{{en|1 = {caption}}}}}\n"
        f"|date = {date_value}\n"
        "|source = {{own}}\n"
        f"|author = {author}"
        f"{_raw_sha1_field(metadata)}\n"
        "}}\n"
        f"{_wikitext_location(metadata)}\n\n"
        "=={{int:license-header}}==\n"
        f"{license_wikitext}\n\n"
        f"{_wikitext_categories(categories)}\n"
    )


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _html_page(state: dict[str, Any]) -> str:
    warning_banner = ""
    if state.get("sha1_matches"):
        sha1_line = ""
        if state.get("raw_sha1sum"):
            sha1_line = f'<p><code>{escape(state["raw_sha1sum"])}</code></p>'
        match_items = "".join(
            f'<li><a href="{escape(item["url"])}" target="_blank" rel="noreferrer">{escape(item["title"])}</a>'
            f'<button type="button" class="overwrite-btn" data-file-title="{escape(item["title"], quote=True)}">Overwrite this file</button></li>'
            for item in state.get("sha1_matches", [])
        )
        warning_banner = (
            '<div class="warning-banner">'
            "<h2>Possible Existing Uploads</h2>"
            "<p>This photo's raw SHA1 sum was found on Commons. These may be existing uploads of the same source file or variants derived from it. You can still continue if this upload is intentionally different, or overwrite one of these files if this is a minor improved version.</p>"
            + sha1_line
            + "<ul>"
            + match_items
            + "</ul></div>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Commons Upload Review</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f2ea;
      --panel: #fffaf0;
      --ink: #181510;
      --accent: #8e3b1b;
      --line: #d8ccb8;
    }}
    body {{
      margin: 0;
      font-family: Georgia, "Iowan Old Style", serif;
      background: var(--bg);
      color: var(--ink);
    }}
    .page {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px;
      display: grid;
      gap: 24px;
      grid-template-columns: minmax(320px, 1fr) minmax(360px, 460px);
    }}
    .card {{
      background: rgba(255,250,240,.92);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 10px 30px rgba(70,50,30,.08);
      overflow: hidden;
    }}
    .image-card img {{
      display: block;
      width: 100%;
      height: auto;
      background: #ddd4c5;
    }}
    .meta {{
      padding: 18px 20px;
      font-size: 14px;
      line-height: 1.5;
      border-top: 1px solid var(--line);
    }}
    .form-card {{
      padding: 20px;
    }}
    .warning-banner {{
      margin: 0 0 18px;
      padding: 14px 16px;
      border: 1px solid #d5b16d;
      border-radius: 14px;
      background: #fff1cf;
      color: #5d3d0c;
    }}
    .warning-banner h2 {{
      margin: 0 0 8px;
      font-size: 18px;
      line-height: 1.2;
    }}
    .warning-banner p {{
      margin: 0 0 10px;
      font-size: 14px;
      line-height: 1.45;
    }}
    .warning-banner ul {{
      margin: 0;
      padding-left: 18px;
    }}
    .warning-banner li {{
      display: flex;
      gap: 10px;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
    }}
    .warning-banner li + li {{
      margin-top: 6px;
    }}
    .warning-banner a {{
      color: inherit;
    }}
    .warning-banner code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px;
    }}
    .warning-banner .overwrite-btn {{
      padding: 8px 12px;
      background: #eadfcd;
      color: #3d2d20;
      white-space: nowrap;
    }}
    h1 {{
      margin: 0 0 16px;
      font-size: 28px;
      line-height: 1.1;
    }}
    label {{
      display: block;
      margin: 16px 0 8px;
      font-weight: 700;
    }}
    input[type="text"], textarea {{
      width: 100%;
      box-sizing: border-box;
      padding: 12px 14px;
      border: 1px solid #b9ab92;
      border-radius: 10px;
      background: #fff;
      font: inherit;
      color: inherit;
    }}
    textarea {{
      min-height: 96px;
      resize: vertical;
    }}
    .category-row {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      margin-bottom: 8px;
    }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: 11px 16px;
      font: inherit;
      cursor: pointer;
    }}
    .primary {{
      background: var(--accent);
      color: #fff;
      font-weight: 700;
    }}
    .secondary {{
      background: #eadfcd;
      color: #3d2d20;
    }}
    .actions {{
      display: flex;
      gap: 10px;
      margin-top: 18px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .spinner {{
      width: 18px;
      height: 18px;
      border: 3px solid rgba(142,59,27,.2);
      border-top-color: var(--accent);
      border-radius: 50%;
      display: none;
      animation: spin 0.8s linear infinite;
    }}
    .spinner.visible {{
      display: inline-block;
    }}
    .status {{
      min-height: 1.2em;
      font-size: 14px;
      color: #5f4635;
      overflow-wrap: anywhere;
    }}
    .status.error {{
      width: 100%;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #f8efe4;
      white-space: pre-wrap;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    @media (max-width: 900px) {{
      .page {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <section class="card image-card">
      <img src="/image" alt="Upload preview">
      <div class="meta"><strong>Metadata</strong><pre>{escape(json.dumps(state["metadata"], indent=2, sort_keys=True))}</pre></div>
    </section>
    <section class="card form-card">
      <div id="hint-stage">
        <h1>Prepare Commons Upload</h1>
        {warning_banner}
        <p>Add an optional hint if the model may struggle to identify the subject, location, event, or other context from the image alone.</p>
        <label for="hint">Hint</label>
        <textarea id="hint" placeholder="Optional: identify the subject, place, event, or any other context for the AI."></textarea>
        <div class="actions">
          <button type="button" class="primary" id="analyze">Generate proposal</button>
        </div>
      </div>
      <div id="review-stage" style="display:none">
        <h1>Review Commons Upload</h1>
        <label for="review-hint">Hint</label>
        <textarea id="review-hint" placeholder="Optional hint for re-analysis."></textarea>
        <div class="actions">
          <button type="button" class="secondary" id="redo">Redo with hint</button>
        </div>
        <label for="filename">Filename</label>
        <input id="filename" type="text" value="">
        <label for="caption">Caption</label>
        <textarea id="caption"></textarea>
        <label>Categories</label>
        <div id="categories"></div>
        <button type="button" class="secondary" id="add-category">Add category</button>
        <div class="actions">
          <button type="button" class="primary" id="submit">Save and upload</button>
        </div>
      </div>
      <div class="actions">
        <div class="spinner" id="spinner"></div>
        <div class="status" id="status"></div>
      </div>
    </section>
  </div>
  <script>
    const categories = document.getElementById("categories");
    const spinner = document.getElementById("spinner");
    const status = document.getElementById("status");
    const hintStage = document.getElementById("hint-stage");
    const reviewStage = document.getElementById("review-stage");
    function wireRemoveButtons(root) {{
      root.querySelectorAll(".remove-btn").forEach((button) => {{
        button.onclick = () => button.parentElement.remove();
      }});
    }}
    function addCategory(value = "") {{
      const row = document.createElement("div");
      row.className = "category-row";
      row.innerHTML = '<input type="text" class="category-input"><button type="button" class="remove-btn">Remove</button>';
      row.querySelector(".category-input").value = value;
      categories.appendChild(row);
      wireRemoveButtons(row);
    }}
    wireRemoveButtons(document);
    document.getElementById("add-category").onclick = () => addCategory("");
    document.querySelectorAll(".overwrite-btn").forEach((button) => {{
      button.onclick = async () => {{
        const title = button.dataset.fileTitle;
        setBusy(`Overwriting ${{title}}...`);
        const response = await fetch("/overwrite", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ title }}),
        }});
        const data = await response.json();
        if (!response.ok) {{
          clearBusy(data.error || "Overwrite failed", true);
          return;
        }}
        clearBusy("Overwrite complete. Redirecting...");
        window.location.href = data.redirect_url;
      }};
    }});
    function setBusy(message) {{
      spinner.classList.add("visible");
      status.classList.remove("error");
      status.textContent = message;
    }}
    function clearBusy(message = "", isError = false) {{
      spinner.classList.remove("visible");
      status.classList.toggle("error", isError);
      status.textContent = message;
    }}
    function populateReview(data) {{
      document.getElementById("review-hint").value = data.hint || "";
      document.getElementById("filename").value = data.filename;
      document.getElementById("caption").value = data.caption;
      categories.innerHTML = "";
      data.categories.forEach((category) => addCategory(category));
      hintStage.style.display = "none";
      reviewStage.style.display = "block";
    }}
    async function analyzeWithHint(hintValue) {{
      setBusy("Generating proposal...");
      const response = await fetch("/analyze", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ hint: hintValue }}),
      }});
      const data = await response.json();
      if (!response.ok) {{
        clearBusy(data.error || "Analysis failed", true);
        return null;
      }}
      populateReview(data);
      clearBusy("Proposal ready.");
      return data;
    }}
    document.getElementById("analyze").onclick = async () => {{
      const hintValue = document.getElementById("hint").value.trim();
      await analyzeWithHint(hintValue);
    }};
    document.getElementById("redo").onclick = async () => {{
      const hintValue = document.getElementById("review-hint").value.trim();
      await analyzeWithHint(hintValue);
    }};
    document.getElementById("submit").onclick = async () => {{
      setBusy("Uploading...");
      const payload = {{
        filename: document.getElementById("filename").value,
        caption: document.getElementById("caption").value,
        categories: Array.from(document.querySelectorAll(".category-input"))
          .map((node) => node.value.trim())
          .filter(Boolean),
      }};
      const response = await fetch("/submit", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(payload),
      }});
      const data = await response.json();
      if (!response.ok) {{
        clearBusy(data.error || "Upload failed", true);
        return;
      }}
      clearBusy("Upload complete. Redirecting...");
      window.location.href = data.redirect_url;
    }};
  </script>
</body>
</html>"""


def _schedule_exit() -> None:
    threading.Thread(target=lambda: (time.sleep(1.0), os._exit(0)), daemon=True).start()


def _validate_required_config(
    commons_config: CommonsConfig, openai_config: OpenAIConfig
) -> None:
    missing: list[str] = []
    if not openai_config.api_key.strip():
        missing.append("openai.api_key")
    if not commons_config.username.strip():
        missing.append("commons.username")
    if not commons_config.password.strip():
        missing.append("commons.password")
    if not commons_config.author.strip():
        missing.append("commons.author")
    if missing:
        raise SystemExit(
            "Missing required config values in config.toml: " + ", ".join(missing)
        )


def _commons_file_url(title: str) -> str:
    return "https://commons.wikimedia.org/wiki/" + title.replace(" ", "_")


def run_app(image_path: Path) -> None:
    app_config = load_config()
    commons_config = app_config.commons
    openai_config = app_config.openai
    _validate_required_config(commons_config, openai_config)
    image_path = image_path.resolve()
    print(f"Preparing Commons upload for {image_path}", flush=True)

    print("Reading image metadata...", flush=True)
    metadata = _read_metadata(image_path)
    print(
        "Metadata summary: "
        + json.dumps(metadata.to_model_dict(), sort_keys=True, ensure_ascii=True),
        flush=True,
    )
    print("Creating downsized preview for vision analysis...", flush=True)
    downsized_bytes, mime_type = _downsize_image(
        image_path, openai_config.downsized_max_dimension
    )
    image_data_url = _image_data_url(downsized_bytes, mime_type)

    vision = VisionClient(openai_config)
    commons_api = CommonsApi(commons_config)
    sha1_matches: list[dict[str, str]] = []
    if metadata.raw_sha1sum:
        sha1_matches = [
            {"title": title, "url": _commons_file_url(title)}
            for title in commons_api.search_files_by_raw_sha1(metadata.raw_sha1sum)
        ]
        if sha1_matches:
            print(
                "Found Commons files with matching raw SHA1 sum: "
                + json.dumps([item["title"] for item in sha1_matches], ensure_ascii=True),
                flush=True,
            )

    state = {
        "metadata": metadata.to_model_dict(),
        "raw_sha1sum": metadata.raw_sha1sum,
        "sha1_matches": sha1_matches,
    }

    def generate_review_state(user_hint: str) -> dict[str, Any]:
        print(f"Starting AI analysis pipeline. Hint: {user_hint!r}", flush=True)
        proposal = vision.propose_metadata(
            image_data_url=image_data_url,
            metadata=metadata,
            suffix_hint=commons_config.filename_suffix,
            user_hint=user_hint,
        )
        print("OpenAI proposal received.", flush=True)
        category_queries = _dedupe_preserve_order(
            proposal["search_queries"] + proposal["keywords"]
        )
        print(
            "Category search queries: "
            + json.dumps(category_queries, ensure_ascii=True),
            flush=True,
        )
        category_graph = _build_category_graph(
            commons_api=commons_api,
            queries=category_queries,
            limit_per_query=commons_config.search_limit_per_query,
            max_candidate_categories=commons_config.max_candidate_categories,
            max_parent_depth=commons_config.max_parent_depth,
        )
        selected = vision.choose_categories(
            image_data_url=image_data_url,
            metadata=metadata,
            proposed_caption=proposal["caption_en"],
            proposed_summary=proposal["visual_summary"],
            category_graph=category_graph,
            user_hint=user_hint,
        )
        print("OpenAI category selection received.", flush=True)
        model_categories = [
            title.removeprefix("Category:") for title in selected["selected_categories"]
        ]
        model_categories = _remove_empty_categories(model_categories, category_graph)
        model_categories = _remove_ancestor_duplicates(model_categories, category_graph)
        equipment_categories = _resolve_equipment_categories(commons_api, metadata)
        all_categories = _dedupe_preserve_order(model_categories + equipment_categories)
        print(
            "Initial proposed categories: "
            + json.dumps(all_categories, ensure_ascii=True),
            flush=True,
        )
        return {
            "hint": user_hint,
            "filename": _preferred_filename(
                proposal["filename_stem"],
                commons_config.filename_suffix,
                image_path.suffix,
            ),
            "caption": proposal["caption_en"],
            "categories": all_categories,
        }

    app = Flask(__name__)

    @app.get("/")
    def index() -> str:
        return _html_page(state)

    @app.get("/image")
    def image() -> tuple[bytes, int, dict[str, str]]:
        return downsized_bytes, 200, {"Content-Type": mime_type}

    @app.post("/analyze")
    def analyze() -> tuple[Any, int]:
        payload = request.get_json(force=True)
        hint = payload.get("hint", "").strip()
        try:
            review_state = generate_review_state(hint)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify(review_state), 200

    @app.post("/submit")
    def submit() -> tuple[Any, int]:
        payload = request.get_json(force=True)
        filename_input = payload["filename"].strip()
        stem = Path(filename_input).stem if filename_input else image_path.stem
        filename = _preferred_filename(stem, "", image_path.suffix)
        caption = payload["caption"].strip()
        categories = _dedupe_preserve_order(
            [item.strip() for item in payload["categories"] if item.strip()]
        )
        description = build_description_wikitext(
            caption=caption,
            metadata=metadata,
            author=commons_config.author,
            license_wikitext=commons_config.license_wikitext,
            categories=categories,
        )
        try:
            redirect_url = commons_api.upload_file(
                image_path=image_path,
                filename=filename,
                description_wikitext=description,
                summary=f"Uploading {filename} via pupphoto",
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        _schedule_exit()
        return jsonify({"redirect_url": redirect_url}), 200

    @app.post("/overwrite")
    def overwrite() -> tuple[Any, int]:
        payload = request.get_json(force=True)
        title = payload.get("title", "").strip()
        if not title.startswith("File:"):
            return jsonify({"error": "Invalid file title"}), 400
        valid_titles = {item["title"] for item in sha1_matches}
        if title not in valid_titles:
            return jsonify({"error": "File is not in the matching SHA1 list"}), 400
        try:
            redirect_url = commons_api.overwrite_file(
                image_path=image_path,
                title=title,
                summary=f"Uploading new version of {title} via pupphoto",
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        _schedule_exit()
        return jsonify({"redirect_url": redirect_url}), 200

    port = _free_port(commons_config.ui_host)
    url = f"http://{commons_config.ui_host}:{port}/"
    print(f"Opening review UI at {url}", flush=True)
    threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    print(url)
    app.run(host=commons_config.ui_host, port=port, debug=False, use_reloader=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Review and upload a photo to Wikimedia Commons."
    )
    parser.add_argument("image_path", type=Path)
    args = parser.parse_args()
    run_app(args.image_path)


if __name__ == "__main__":
    main()
