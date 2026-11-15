#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sysconfig
import tempfile
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from fractions import Fraction
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request
from jinja2 import Environment, FileSystemLoader, select_autoescape
from openai import OpenAI
from PIL import ExifTags, Image

from commons_categories import (
    _build_category_graph,
    _category_intersection_query,
    _dedupe_preserve_order,
    _refine_category_intersections,
    _remove_ancestor_duplicates,
    _remove_empty_categories,
)
from config import CommonsConfig, OpenAIConfig, load_config
from gps import is_in_banned_area
from upload_photo import (
    apply_exif_orientation,
    image_path_needs_orientation_normalization,
    is_heif_path,
    save_image_with_metadata,
    save_jpeg_with_metadata,
    upload_temp_filename,
)

Image.MAX_IMAGE_PIXELS = None

EXIF_TAGS = ExifTags.TAGS
GPS_TAGS = ExifTags.GPSTAGS
RAW_SHA1_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}_.+_([0-9a-f]{40})\.[^.]+$",
    re.IGNORECASE,
)
MAX_CATEGORY_SEARCH_QUERIES = 8
MAX_CATEGORY_RELATIONSHIP_QUERIES = 2
DECISIONS_MODEL = "gpt-6-luna"
CATEGORY_SELECTION_PROBABILITY_THRESHOLD = 0.5
MAX_DECISIONS_IMAGES = 128
MAX_DECISIONS_QUESTIONS = 400
QUALITY_IMAGE_CANDIDATE_LIST_TITLE = "Commons:Quality images candidates/candidate list"
QUALITY_IMAGE_DAILY_NOMINATION_LIMIT = 5
GPS_EXIF_KEYS = tuple(
    f"Exif.GPSInfo.{name}"
    for name in GPS_TAGS.values()
    if isinstance(name, str) and name.startswith("GPS")
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
class PreparedPhoto:
    source_path: Path
    upload_path: Path
    upload_temp_dir: tempfile.TemporaryDirectory[str] | None
    metadata: PhotoMetadata
    preview_bytes: bytes
    preview_mime_type: str
    image_data_url: str
    sha1_matches: list[dict[str, str]]


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
        if not is_heif_path(image_path):
            img = apply_exif_orientation(img)
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
        if not is_heif_path(image_path):
            img = apply_exif_orientation(img)
        img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        output = BytesIO()
        img.save(output, format="JPEG", quality=92)
    return output.getvalue(), "image/jpeg"


def _remove_gps_metadata(image_path: Path) -> None:
    metadata_keys = set(_exiv2_metadata_map(str(image_path)))
    keys_to_delete = sorted(
        key
        for key in metadata_keys
        if key == "Exif.Image.GPSTag"
        or key.startswith("Exif.GPSInfo.")
        or key.startswith("Xmp.exif.GPS")
    )
    keys_to_delete.extend(key for key in GPS_EXIF_KEYS if key not in metadata_keys)

    commands = []
    for key in keys_to_delete:
        commands.extend(["-M", f"del {key}"])
    if commands:
        subprocess.run(
            ["exiv2", *commands, str(image_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    _exiv2_metadata_map.cache_clear()


def _upload_safe_image_path(
    image_path: Path, metadata: PhotoMetadata
) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    needs_conversion = is_heif_path(image_path)
    needs_orientation_normalization = (
        not needs_conversion and image_path_needs_orientation_normalization(image_path)
    )
    needs_gps_scrub = not metadata.location_allowed
    if (
        not needs_conversion
        and not needs_orientation_normalization
        and not needs_gps_scrub
    ):
        return image_path, None

    temp_dir = tempfile.TemporaryDirectory(prefix="pupphoto-commons-")
    safe_path = Path(temp_dir.name) / (
        upload_temp_filename(image_path) if needs_conversion else image_path.name
    )
    if needs_conversion:
        with Image.open(image_path) as img:
            save_jpeg_with_metadata(img, safe_path)
        print(
            f"HEIF input detected; using converted JPEG upload copy {safe_path}",
            flush=True,
        )
    elif needs_orientation_normalization:
        with Image.open(image_path) as img:
            save_image_with_metadata(img, safe_path)
        print(
            f"EXIF orientation detected; using normalized upload copy {safe_path}",
            flush=True,
        )
    else:
        shutil.copy2(image_path, safe_path)

    if needs_gps_scrub:
        _remove_gps_metadata(safe_path)

        safe_metadata = _read_metadata(safe_path)
        if not safe_metadata.location_allowed:
            temp_dir.cleanup()
            raise RuntimeError(
                "Refusing to upload: banned-area GPS metadata is still present after "
                "scrubbing the upload copy."
            )

        print(
            f"GPS metadata is inside a banned area; using scrubbed upload copy {safe_path}",
            flush=True,
        )
    return safe_path, temp_dir


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


def _botpassword_owner_username(username: str) -> str:
    return username.split("@", 1)[0].strip()


def _utc_date_label(value: date) -> str:
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def _utc_signature(username: str, timestamp: datetime | None = None) -> str:
    signed_at = timestamp or datetime.now(timezone.utc)
    return (
        f"--[[User:{username}|{username}]] "
        f"{signed_at:%H:%M}, {_utc_date_label(signed_at.date())} (UTC)"
    )


def _quality_image_description(caption: str, filename: str) -> str:
    description = re.sub(r"\s+", " ", caption).strip()
    description = re.sub(r"[\[\]{}|<>]", " ", description)
    description = re.sub(r"\s+", " ", description).strip(" .,:;")
    if not description:
        description = Path(filename).stem.replace("_", " ").strip()

    words = description.split()
    if len(words) > 14:
        description = " ".join(words[:14]).strip(" .,:;")
    if len(description) > 120:
        description = description[:120].rsplit(" ", 1)[0].strip(" .,:;")
    return description or Path(filename).stem.replace("_", " ").strip()


def _user_signature_patterns(username: str) -> tuple[str, str]:
    return (f"--[[User:{username}|", f"--[[User:{username}]]")


def _section_bounds(wikitext: str, heading: str) -> tuple[int, int] | None:
    pattern = re.compile(rf"^==\s+{re.escape(heading)}\s+==\s*$", re.MULTILINE)
    match = pattern.search(wikitext)
    if not match:
        return None
    next_match = re.search(r"^==\s+.+?\s+==\s*$", wikitext[match.end() :], re.MULTILINE)
    end = match.end() + next_match.start() if next_match else len(wikitext)
    return match.start(), end


def _count_quality_image_nominations(
    wikitext: str, username: str, date_label: str
) -> int:
    bounds = _section_bounds(wikitext, date_label)
    if bounds is None:
        return 0
    section = wikitext[bounds[0] : bounds[1]]
    signature_patterns = _user_signature_patterns(username)
    count = 0
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("File:") or "{{/" not in stripped:
            continue
        nomination_text = re.split(r"<br\s*/?>", stripped, maxsplit=1)[0]
        if any(pattern in nomination_text for pattern in signature_patterns):
            count += 1
    return count


def _insert_quality_image_nomination(
    wikitext: str, date_label: str, nomination_line: str
) -> str:
    bounds = _section_bounds(wikitext, date_label)
    if bounds is not None:
        section_start, section_end = bounds
        section = wikitext[section_start:section_end]
        gallery_match = re.search(r"^<gallery>\s*$", section, re.MULTILINE)
        if gallery_match:
            insert_at = section_start + gallery_match.end()
            return (
                wikitext[:insert_at]
                + "\n"
                + nomination_line
                + "\n\n"
                + wikitext[insert_at:].lstrip("\n")
            )
        heading_end = wikitext.find("\n", section_start)
        insert_at = heading_end + 1 if heading_end != -1 else section_end
        return (
            wikitext[:insert_at]
            + "<gallery>\n"
            + nomination_line
            + "\n</gallery>\n\n"
            + wikitext[insert_at:].lstrip("\n")
        )

    new_section = f"== {date_label} ==\n<gallery>\n{nomination_line}\n</gallery>\n\n"
    marker = "new nominations -->"
    marker_index = wikitext.find(marker)
    if marker_index != -1:
        insert_at = wikitext.find("\n", marker_index)
        if insert_at == -1:
            return wikitext.rstrip() + "\n" + new_section
        insert_at += 1
        return wikitext[:insert_at] + new_section + wikitext[insert_at:].lstrip("\n")

    first_date_heading = re.search(r"^==\s+.+?\s+==\s*$", wikitext, re.MULTILINE)
    if first_date_heading:
        return (
            wikitext[: first_date_heading.start()]
            + new_section
            + wikitext[first_date_heading.start() :]
        )
    return wikitext.rstrip() + "\n\n" + new_section.rstrip() + "\n"


class CommonsApi:
    _TITLE_BATCH_SIZE = 50
    _MAX_RETRIES = 4
    _MAX_RETRY_DELAY_SECONDS = 30.0
    _MAX_CHILD_CATEGORY_LOOKUPS = 12
    _RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
    _CHUNKED_UPLOAD_THRESHOLD_BYTES = 95 * 1024 * 1024
    _UPLOAD_CHUNK_SIZE_BYTES = 20 * 1024 * 1024

    def __init__(self, config: CommonsConfig):
        self.config = config
        self._logged_in = False
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
                    return min(
                        max(float(retry_after), 0.5),
                        self._MAX_RETRY_DELAY_SECONDS,
                    )
                except ValueError:
                    pass
        return min(1.5 * (2**attempt), self._MAX_RETRY_DELAY_SECONDS)

    def _retry_message(
        self, response: requests.Response | None, delay: float, reason: str
    ) -> str:
        retry_after = (
            response.headers.get("Retry-After") if response is not None else None
        )
        if retry_after:
            try:
                requested_delay = float(retry_after)
            except ValueError:
                requested_delay = None
            if requested_delay is not None and requested_delay > delay:
                return (
                    f"Commons API returned {reason}; server requested "
                    f"{requested_delay:.1f}s, retrying in capped {delay:.1f}s..."
                )
        return f"Commons API returned {reason}; retrying in {delay:.1f}s..."

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
                        self._retry_message(
                            response, delay, f"HTTP {response.status_code}"
                        ),
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
                            self._retry_message(response, delay, error.get("code")),
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
        self,
        params: dict[str, Any],
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request_json(
            "POST",
            data={**params, "format": "json", "formatversion": "2"},
            files=files,
            timeout=300,
        )

    def login(self) -> None:
        if self._logged_in:
            return
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
        self._logged_in = True

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

    def search_category_intersections(
        self,
        category_pairs: list[tuple[str, str]],
        limit: int,
    ) -> dict[str, dict[str, Any]]:
        query = _category_intersection_query(category_pairs)
        if not query:
            return {}
        print("Searching Commons for shared child categories...", flush=True)
        data = self.get(
            action="query",
            generator="search",
            gsrsearch=query,
            gsrnamespace="14",
            gsrwhat="text",
            gsrlimit=limit,
            prop="categories|categoryinfo",
            cllimit="max",
            clshow="!hidden",
        )
        pages = sorted(
            data.get("query", {}).get("pages", []),
            key=lambda page: int(page.get("index", 0)),
        )
        result: dict[str, dict[str, Any]] = {}
        for page in pages:
            if "missing" in page or page.get("ns") != 14:
                continue
            info = page.get("categoryinfo", {})
            result[page["title"]] = {
                "parents": [
                    category["title"] for category in page.get("categories", [])
                ],
                "file_count": int(info.get("files", 0)),
                "subcategory_count": int(info.get("subcats", 0)),
            }
        return result

    def existing_category_titles(self, titles: list[str]) -> list[str]:
        if not titles:
            return []
        result: list[str] = []
        for batch_start in range(0, len(titles), self._TITLE_BATCH_SIZE):
            batch = titles[batch_start : batch_start + self._TITLE_BATCH_SIZE]
            pages = self.get(
                action="query",
                titles="|".join(batch),
                prop="categoryinfo",
            )["query"]["pages"]
            for page in pages:
                if "missing" not in page and page.get("ns") == 14:
                    result.append(page["title"])
        return result

    def search_files_by_raw_sha1(self, raw_sha1sum: str, limit: int = 10) -> list[str]:
        print(f"Searching Commons files for raw SHA1 sum: {raw_sha1sum}", flush=True)
        data = self.get(
            action="query",
            list="search",
            srsearch=f'"{raw_sha1sum}"',
            srnamespace="6",
            srwhat="text",
            srlimit=limit,
        )
        return [item["title"] for item in data.get("query", {}).get("search", [])]

    def page_wikitext(self, title: str) -> tuple[str, str, str]:
        data = self.get(
            action="query",
            titles=title,
            prop="revisions",
            rvprop="content|timestamp",
            rvslots="main",
            curtimestamp="1",
        )
        page = data["query"]["pages"][0]
        if "missing" in page:
            raise RuntimeError(f"Commons page does not exist: {title}")
        revision = page["revisions"][0]
        content = revision["slots"]["main"]["content"]
        return content, revision["timestamp"], data["curtimestamp"]

    def edit_page(
        self,
        title: str,
        text: str,
        summary: str,
        basetimestamp: str,
        starttimestamp: str,
    ) -> None:
        self.login()
        token = self.csrf_token()
        data = self.post(
            action="edit",
            title=title,
            text=text,
            summary=summary,
            token=token,
            basetimestamp=basetimestamp,
            starttimestamp=starttimestamp,
            notminor="1",
        )
        result = data.get("edit", {})
        if result.get("result") != "Success":
            raise RuntimeError(f"Commons edit failed: {result}")

    def quality_image_nomination_count(
        self, username: str, nomination_date: date
    ) -> int:
        wikitext, _basetimestamp, _starttimestamp = self.page_wikitext(
            QUALITY_IMAGE_CANDIDATE_LIST_TITLE
        )
        return _count_quality_image_nominations(
            wikitext, username, _utc_date_label(nomination_date)
        )

    def nominate_quality_image(
        self,
        file_title: str,
        description: str,
        username: str,
        timestamp: datetime | None = None,
    ) -> int:
        now = timestamp or datetime.now(timezone.utc)
        date_label = _utc_date_label(now.date())
        wikitext, basetimestamp, starttimestamp = self.page_wikitext(
            QUALITY_IMAGE_CANDIDATE_LIST_TITLE
        )
        existing_count = _count_quality_image_nominations(
            wikitext, username, date_label
        )
        if existing_count >= QUALITY_IMAGE_DAILY_NOMINATION_LIMIT:
            raise RuntimeError(
                "Refusing Quality Image nomination: "
                f"{username} already has {existing_count} nominations on "
                f"{date_label} UTC."
            )

        if f"{file_title}|" in wikitext:
            raise RuntimeError(
                f"Refusing Quality Image nomination: {file_title} is already listed."
            )

        nomination_line = (
            f"{file_title}|{{{{/Nomination|{description} "
            f"{_utc_signature(username, now)}|}}}}"
        )
        updated_wikitext = _insert_quality_image_nomination(
            wikitext, date_label, nomination_line
        )
        self.edit_page(
            QUALITY_IMAGE_CANDIDATE_LIST_TITLE,
            updated_wikitext,
            f"Nominate {file_title} for Quality Image",
            basetimestamp,
            starttimestamp,
        )
        return existing_count + 1

    def category_exists(self, title: str) -> bool:
        pages = self.get(action="query", titles=title)["query"]["pages"]
        return bool(pages) and "missing" not in pages[0]

    def category_details(self, titles: list[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for batch_start in range(0, len(titles), self._TITLE_BATCH_SIZE):
            batch = titles[batch_start : batch_start + self._TITLE_BATCH_SIZE]
            continuation: dict[str, Any] = {}
            while True:
                data = self.get(
                    action="query",
                    titles="|".join(batch),
                    prop="categories|categoryinfo",
                    cllimit="max",
                    clshow="!hidden",
                    **continuation,
                )
                for page in data["query"]["pages"]:
                    details = result.setdefault(
                        page["title"],
                        {"parents": [], "file_count": 0, "subcategory_count": 0},
                    )
                    details["parents"] = _dedupe_preserve_order(
                        details["parents"]
                        + [category["title"] for category in page.get("categories", [])]
                    )
                    if "categoryinfo" in page:
                        info = page["categoryinfo"]
                        details["file_count"] = int(info.get("files", 0))
                        details["subcategory_count"] = int(info.get("subcats", 0))
                continuation = data.get("continue", {})
                if not continuation:
                    break
        return result

    def category_subcategories(
        self, titles: list[str], limit_per_category: int
    ) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for title in titles:
            data = self.get(
                action="query",
                list="categorymembers",
                cmtitle=title,
                cmnamespace="14",
                cmtype="subcat",
                cmlimit=limit_per_category,
            )
            result[title] = [
                item["title"]
                for item in data.get("query", {}).get("categorymembers", [])
            ]
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

    def _stash_file_in_chunks(
        self,
        image_path: Path,
        filename: str,
        token: str,
    ) -> str:
        filesize = image_path.stat().st_size
        chunk_count = (
            filesize + self._UPLOAD_CHUNK_SIZE_BYTES - 1
        ) // self._UPLOAD_CHUNK_SIZE_BYTES
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        print(
            f"Using chunked upload for {filesize / (1024 * 1024):.1f} MiB file "
            f"({chunk_count} chunks)...",
            flush=True,
        )

        offset = 0
        filekey: str | None = None
        last_result: str | None = None
        with image_path.open("rb") as source:
            for chunk_number in range(1, chunk_count + 1):
                source.seek(offset)
                chunk = source.read(self._UPLOAD_CHUNK_SIZE_BYTES)
                if not chunk:
                    raise RuntimeError(
                        f"Could not read Commons upload chunk at offset {offset}"
                    )

                params: dict[str, Any] = {
                    "action": "upload",
                    "filename": filename,
                    "filesize": filesize,
                    "offset": offset,
                    "stash": "1",
                    "ignorewarnings": "1",
                    "token": token,
                }
                if filekey is not None:
                    params["filekey"] = filekey

                data = self.post_upload(
                    params=params,
                    files={"chunk": (filename, chunk, mime_type)},
                )
                upload = data.get("upload", {})
                last_result = upload.get("result")
                returned_filekey = upload.get("filekey")
                if not isinstance(returned_filekey, str) or not returned_filekey:
                    raise RuntimeError(
                        "Commons chunk upload did not return a file key: "
                        f"{upload}"
                    )
                filekey = returned_filekey

                expected_offset = offset + len(chunk)
                if last_result == "Continue":
                    returned_offset = upload.get("offset")
                    if returned_offset != expected_offset:
                        raise RuntimeError(
                            "Commons chunk upload returned an unexpected offset: "
                            f"expected {expected_offset}, got {returned_offset}"
                        )
                    offset = returned_offset
                elif last_result == "Success":
                    if expected_offset != filesize:
                        raise RuntimeError(
                            "Commons reported a completed chunk upload before the "
                            f"whole file was sent ({expected_offset} of {filesize} bytes)"
                        )
                    offset = expected_offset
                else:
                    raise RuntimeError(
                        f"Commons chunk upload failed at offset {offset}: {upload}"
                    )

                print(
                    f"Uploaded chunk {chunk_number}/{chunk_count} "
                    f"({offset / filesize:.0%}).",
                    flush=True,
                )

        if last_result != "Success" or offset != filesize or filekey is None:
            raise RuntimeError(
                "Commons chunk upload ended before the complete file was stashed"
            )
        return filekey

    def _upload_file_contents(
        self,
        image_path: Path,
        filename: str,
        token: str,
        comment: str,
        ignorewarnings: bool = False,
        text: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "action": "upload",
            "filename": filename,
            "comment": comment,
            "token": token,
        }
        # MediaWiki treats any supplied boolean parameter as true, even "0".
        # New uploads must omit this flag so filename collisions stop the upload.
        if ignorewarnings:
            params["ignorewarnings"] = "1"
        if text is not None:
            params["text"] = text

        if image_path.stat().st_size >= self._CHUNKED_UPLOAD_THRESHOLD_BYTES:
            params["filekey"] = self._stash_file_in_chunks(
                image_path=image_path,
                filename=filename,
                token=token,
            )
            return self.post_upload(params=params)

        with image_path.open("rb") as source:
            return self.post_upload(
                params=params,
                files={
                    "file": (
                        filename,
                        source,
                        mimetypes.guess_type(filename)[0]
                        or "application/octet-stream",
                    )
                },
            )

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
        data = self._upload_file_contents(
            image_path=image_path,
            filename=filename,
            token=token,
            comment=summary,
            text=description_wikitext,
        )
        upload = data["upload"]
        if upload["result"] != "Success":
            if "exists" in upload.get("warnings", {}):
                raise RuntimeError(
                    f"Refusing to overwrite existing Commons file {filename}. "
                    "Choose a different filename and submit again."
                )
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
        data = self._upload_file_contents(
            image_path=image_path,
            filename=filename,
            token=token,
            comment=summary,
            ignorewarnings=True,
        )
        upload = data["upload"]
        if upload["result"] != "Success":
            raise RuntimeError(f"Commons overwrite failed: {upload}")
        return _commons_file_url(title)


class VisionClient:
    def __init__(self, config: OpenAIConfig):
        self.config = config
        self.client = OpenAI(api_key=config.api_key)

    def _ordered_image_content(self, image_data_urls: list[str]) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        for index, image_data_url in enumerate(image_data_urls, start=1):
            content.extend(
                [
                    {"type": "input_text", "text": f"Photo {index} of {len(image_data_urls)}:"},
                    {
                        "type": "input_image",
                        "image_url": image_data_url,
                        "detail": self.config.image_detail,
                    },
                ]
            )
        return content

    def propose_group_metadata(
        self,
        image_data_urls: list[str],
        metadatas: list[PhotoMetadata],
        suffix_hint: str,
        user_hint: str,
        source_names: list[str] | None = None,
    ) -> dict[str, Any]:
        photo_count = len(image_data_urls)
        if photo_count == 0 or len(metadatas) != photo_count:
            raise ValueError("Image data and metadata must describe the same photo group")
        if source_names is not None and len(source_names) != photo_count:
            raise ValueError("Source names must describe the same photo group")

        print(
            "Requesting a shared filename, consistent captions, and category search "
            "hints from OpenAI...",
            flush=True,
        )
        photo_schema = {
            "type": "object",
            "properties": {
                "caption_en": {"type": "string"},
                "visual_summary": {"type": "string"},
            },
            "required": ["caption_en", "visual_summary"],
            "additionalProperties": False,
        }
        schema = {
            "type": "object",
            "properties": {
                "group_filename_stem": {"type": "string"},
                "photos": {
                    "type": "array",
                    "items": photo_schema,
                    "minItems": photo_count,
                    "maxItems": photo_count,
                },
                "keywords": {"type": "array", "items": {"type": "string"}},
                "search_queries": {"type": "array", "items": {"type": "string"}},
                "relationship_queries": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "group_summary": {"type": "string"},
                "research_summary": {"type": "string"},
            },
            "required": [
                "group_filename_stem",
                "photos",
                "keywords",
                "search_queries",
                "relationship_queries",
                "group_summary",
                "research_summary",
            ],
            "additionalProperties": False,
        }
        instructions = (
            f"You are preparing an ordered group of {photo_count} highly related "
            "photo(s) for Wikimedia Commons. Return JSON only. Analyze every photo "
            "together before describing any one of them. "
            "Use web search when the photos or hint may involve a recently released "
            "product, vehicle, train, station, event, public artwork, building, route, "
            "operator, or another named subject whose identity or terminology may have "
            "changed after model training. Search sparingly and only enough to identify "
            "current factual names and common terms. "
            "Write one concise English group_filename_stem using plain ASCII words "
            "separated by spaces, without a number, file extension, or configured suffix. "
            "It must accurately describe every photo; the upload workflow will append "
            "ordered numbers. "
            "Return exactly one photos entry per input photo in the original order. Write "
            "a concise, factual, neutral, encyclopedic English caption for each. Keep the "
            "captions related and stylistically consistent: use the same exact proper "
            "names, terminology, tense, and sentence pattern where appropriate, while "
            "mentioning the view, action, or detail that distinguishes each photo. Do not "
            "refer to photo numbers, 'this image', another photo, or an unsupported "
            "temporal sequence in a caption. "
            "In each visual_summary, record the main subject's visible color, material, "
            "body form, activity or state (such as parked versus moving), and setting "
            "when clear. These observations support category discovery even when they "
            "do not belong in the concise caption. "
            "Treat the optional user hint as high-priority factual context when it gives "
            "names, locations, dates, events, or the shooting viewpoint. Silently correct "
            "likely misspellings in the hint. "
            "Actively inspect all photos for discriminating text, model or serial numbers, "
            "logos, badges, signage, inscriptions, physical features, and location markers. "
            "Use cross-photo evidence to resolve identities, but only state details that "
            "are supported for the individual caption. Verify a resolvable specific "
            "identity with web search instead of stopping at a generic class or guessing. "
            "Identify visually central relationships, combinations, or states involving "
            "the subjects. Put no more than two high-value, plausible Commons category "
            "titles for such combinations in relationship_queries. "
            "Generate category lookup queries for the group as a whole, emphasizing "
            "specific subjects, locations, events, operators, object classes, and year "
            "when relevant. Generate no more than 8 queries total across search_queries "
            "and relationship_queries. Prioritize a standalone canonical-name query for "
            "each central identified subject, and use separate queries for useful "
            "subject/place or subject/state combinations. Cover independent useful "
            "facets rather than spending the query budget on near-duplicate identity "
            "queries. Search for visible color combined with manufacturer and body "
            "form or object subtype, as well as color combined with location. Useful "
            "titles can follow '<color> <manufacturer> <body form or objects>'; a "
            "broad class such as sedans alone is less useful than that intersection. "
            "Also search for visible color plus object class and location, and "
            "activity or state plus object class and location, using plausible Commons "
            "titles such as '<color> <objects> in <state or region>' and "
            "'<activity/state> <objects> in <state or region>'. Use the general object "
            "class when Commons is more likely to organize that facet by class than "
            "by the specific model. Prefer a subject-specific place query such as "
            "'<topic> in <city>' over a bare city name. A color or state query can use "
            "a broader verified region if the city-specific intersection is unlikely "
            "to exist. Reserve room for these facets whenever visibly supported; "
            "do not let model names, years, or repeated place queries crowd them out. "
            "Every query should contain a "
            "named subject, named place, model, operator, route, event, year, or similarly "
            "concrete context; do not output bare visual descriptors. Prefer exact proper "
            "names. Include a time-of-day query only when it is visually clear and useful. "
            "Do not include photographic equipment categories; those are handled from "
            "each photo's EXIF. In research_summary, briefly state facts found via web "
            "search that affected the result, or return an empty string. Avoid speculation "
            "and promotional language. "
            f"The workflow will append this suffix after each number: {suffix_hint!r}."
        )
        metadata_payload = [
            {
                "photo_number": index + 1,
                "source_filename": (
                    source_names[index] if source_names is not None else None
                ),
                "metadata": metadata.to_model_dict(),
            }
            for index, metadata in enumerate(metadatas)
        ]
        user_content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": "Ordered photo metadata JSON:\n"
                + json.dumps(metadata_payload, indent=2, sort_keys=True)
                + "\n\nOptional user hint for the whole group:\n"
                + (user_hint or "(none)"),
            }
        ]
        user_content.extend(self._ordered_image_content(image_data_urls))
        response = self.client.responses.create(
            model=self.config.vision_model,
            reasoning={"effort": "low"},
            tools=[{"type": "web_search", "search_context_size": "low"}],
            max_tool_calls=4,
            input=[
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": instructions}],
                },
                {"role": "user", "content": user_content},
            ],
            text={"format": _json_schema("commons_group_upload_proposal", schema)},
        )
        proposal = json.loads(response.output_text)
        if len(proposal.get("photos", [])) != photo_count:
            raise RuntimeError(
                "OpenAI returned metadata for a different number of photos "
                f"({len(proposal.get('photos', []))} instead of {photo_count})"
            )
        return proposal

    def propose_metadata(
        self,
        image_data_url: str,
        metadata: PhotoMetadata,
        suffix_hint: str,
        user_hint: str,
    ) -> dict[str, Any]:
        """Backward-compatible single-photo wrapper."""
        proposal = self.propose_group_metadata(
            [image_data_url], [metadata], suffix_hint, user_hint
        )
        photo = proposal["photos"][0]
        return {
            "filename_stem": proposal["group_filename_stem"],
            "caption_en": photo["caption_en"],
            "visual_summary": photo["visual_summary"],
            "keywords": proposal["keywords"],
            "search_queries": proposal["search_queries"],
            "relationship_queries": proposal["relationship_queries"],
            "research_summary": proposal["research_summary"],
        }

    def choose_group_categories(
        self,
        image_data_urls: list[str],
        metadatas: list[PhotoMetadata],
        proposed_photos: list[dict[str, str]],
        group_summary: str,
        research_summary: str,
        category_graph: dict[str, Any],
        user_hint: str,
    ) -> dict[str, Any]:
        photo_count = len(image_data_urls)
        if not (
            photo_count
            and len(metadatas) == photo_count
            and len(proposed_photos) == photo_count
        ):
            raise ValueError("Category inputs must describe the same photo group")

        if photo_count > MAX_DECISIONS_IMAGES:
            raise ValueError(
                f"Decisions category selection supports at most {MAX_DECISIONS_IMAGES} "
                "photos per group"
            )
        category_titles = [
            title
            for title, node in category_graph.get("nodes", {}).items()
            if int(node.get("file_count", 0)) > 0
        ]
        photo_categories: list[list[str]] = [[] for _ in range(photo_count)]
        if not category_titles:
            return {"photo_categories": photo_categories, "reasoning_summary": ""}

        print("Evaluating Commons categories with the OpenAI Decisions API...", flush=True)
        photo_context = [
            {
                "photo_number": index + 1,
                "metadata": metadatas[index].to_model_dict(),
                "proposed_caption": proposed_photos[index]["caption_en"],
                "visual_summary": proposed_photos[index]["visual_summary"],
            }
            for index in range(photo_count)
        ]
        user_content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": "Ordered photo context JSON:\n"
                + json.dumps(photo_context, indent=2, sort_keys=True)
                + "\n\nGroup visual summary:\n"
                + group_summary
                + "\n\nCurrent/researched context:\n"
                + (research_summary or "(none)")
                + "\n\nOptional user hint:\n"
                + (user_hint or "(none)")
                + "\n\nCandidate category graph JSON:\n"
                + json.dumps(category_graph, indent=2, sort_keys=True),
            }
        ]
        user_content.extend(self._ordered_image_content(image_data_urls))
        questions: list[dict[str, str]] = []
        question_targets: dict[str, tuple[int, str]] = {}
        for photo_index in range(photo_count):
            for category_index, title in enumerate(category_titles):
                name = f"photo_{photo_index + 1}_category_{category_index + 1}"
                question_targets[name] = (photo_index, title)
                questions.append(
                    {
                        "type": "predicate",
                        "name": name,
                        "instructions": (
                            f"Is {json.dumps(title)} an appropriate Wikimedia Commons "
                            f"topical category for photo {photo_index + 1}? Require "
                            "support from this photo, its metadata, researched context, "
                            "or the user hint. Treat the user hint as high-priority "
                            "factual context. Use related photos as supporting context, "
                            "but do not transfer visible features found only in another "
                            "photo. Require direct relevance to a main depicted subject, "
                            "activity, place, or clearly identifiable feature. Prefer "
                            "specific categories in the graph. A bare city, state, "
                            "country, or geographic-region category is not justified "
                            "merely because it contains the "
                            "shooting location; the named region or landform must itself "
                            "be a depicted subject supported by the evidence. Unidentified "
                            "distant hills do not establish a particular mountain range. "
                            "For a subject-in-place category, a verified location of "
                            "the depicted subject is sufficient; the place need not "
                            "itself be a recognizable landscape. Color combined with "
                            "manufacturer/body form, color combined with location, and "
                            "activity or parked/moving state combined with location "
                            "are useful independent facets when supported by the photo, "
                            "even if its specific model identity is already known. "
                            "Exclude maintenance, creator, user, campaign, "
                            "equipment, and exposure categories. Include lighting or "
                            "time-of-day categories only when visually clear in this "
                            "photo. Assess applicability independently; redundant "
                            "ancestor categories are removed separately."
                        ),
                    }
                )
        refused_count = 0
        # Graph expansion and photo groups can exceed the per-request question
        # limit. Keep the full shared context in every batch.
        for start in range(0, len(questions), MAX_DECISIONS_QUESTIONS):
            question_batch = questions[start : start + MAX_DECISIONS_QUESTIONS]
            expected_names = {question["name"] for question in question_batch}
            if len(questions) > MAX_DECISIONS_QUESTIONS:
                print(
                    f"Evaluating category questions {start + 1}-"
                    f"{start + len(question_batch)} of {len(questions)}...",
                    flush=True,
                )
            # The installed SDK has no Decisions resource yet. Its HTTP client keeps
            # the existing API credentials, base URL, retries, and error handling.
            decision = self.client.post(
                "/decisions",
                cast_to=dict[str, Any],
                body={
                    "model": DECISIONS_MODEL,
                    "input": [{"role": "user", "content": user_content}],
                    "questions": question_batch,
                },
            )
            answers = decision.get("answers", [])
            if not isinstance(answers, list) or len(answers) != len(question_batch):
                raise RuntimeError(
                    "OpenAI Decisions returned an unexpected number of category answers"
                )
            seen_names: set[str] = set()
            for answer in answers:
                name = answer.get("name") if isinstance(answer, dict) else None
                if (
                    not isinstance(name, str)
                    or name not in expected_names
                    or name in seen_names
                ):
                    raise RuntimeError("OpenAI Decisions returned an unexpected category answer")
                seen_names.add(name)
                if answer.get("type") == "refusal":
                    refused_count += 1
                    continue
                probability = answer.get("probability")
                if (
                    answer.get("type") != "predicate"
                    or isinstance(probability, bool)
                    or not isinstance(probability, (int, float))
                    or not 0 <= probability <= 1
                ):
                    raise RuntimeError("OpenAI Decisions returned an invalid category probability")
                photo_index, title = question_targets[name]
                if probability >= CATEGORY_SELECTION_PROBABILITY_THRESHOLD:
                    photo_categories[photo_index].append(title)
        reasoning_summary = (
            "Selected categories with applicability probability "
            f">= {CATEGORY_SELECTION_PROBABILITY_THRESHOLD:g}."
        )
        if refused_count:
            reasoning_summary += f" Skipped {refused_count} refused category question(s)."
            print(reasoning_summary, flush=True)
        return {
            "photo_categories": photo_categories,
            "reasoning_summary": reasoning_summary,
        }

    def review_group_categories(
        self,
        image_data_urls: list[str],
        metadatas: list[PhotoMetadata],
        proposed_photos: list[dict[str, str]],
        photo_categories: list[list[str]],
        group_summary: str,
        research_summary: str,
        category_graph: dict[str, Any],
        user_hint: str,
    ) -> dict[str, Any]:
        """Review independently applicable categories as a coherent set per photo."""
        photo_count = len(image_data_urls)
        if not (
            photo_count
            and len(metadatas) == photo_count
            and len(proposed_photos) == photo_count
            and len(photo_categories) == photo_count
        ):
            raise ValueError("Category review inputs must describe the same photo group")
        if not any(photo_categories):
            return {"photo_categories": photo_categories, "reasoning_summary": ""}

        print("Reviewing category specificity and redundancy across each photo...", flush=True)
        schema = {
            "type": "object",
            "properties": {
                "photos": {
                    "type": "array",
                    "minItems": photo_count,
                    "maxItems": photo_count,
                    "items": {
                        "type": "object",
                        "properties": {
                            "selected_categories": {
                                "type": "array",
                                "items": {"type": "string"},
                            }
                        },
                        "required": ["selected_categories"],
                        "additionalProperties": False,
                    },
                },
                "reasoning_summary": {"type": "string"},
            },
            "required": ["photos", "reasoning_summary"],
            "additionalProperties": False,
        }
        instructions = (
            "Review Wikimedia Commons topical categories as a set for each photo. "
            "These categories were scored independently for applicability; now remove "
            "over-categorization and categories too broad to be useful. Return one "
            "photos entry per input photo in the same order. Select only from that "
            "photo's supplied categories; never invent a title or transfer a category "
            "from another photo. Keep the most specific supported identity. Remove "
            "generic object-class or body-form categories when they merely describe "
            "the same main subject already covered by a specific named model or taxon. "
            "The supplied category graph is incomplete: a missing ancestor path does "
            "not establish that two categories provide independent information. Use "
            "the photo and subject identity to recognize semantic redundancy too. "
            "Keep independent, supported dimensions such as color, material, activity, "
            "parked/moving state, and subject-in-place categories; a model or taxon "
            "does not imply the depicted individual's color, activity, or location. "
            "Prefer the most specific available intersection for each such dimension. "
            "Do not collapse incomparable intersections across different axes: a "
            "color/manufacturer/body-form category and a color/place category can "
            "both be useful. A specific intersection replaces its broader class, "
            "not other independent intersections. "
            "Remove bare city, county, state, country, or geographic-region categories "
            "when the place is only the shooting location for a close view of an "
            "object. A topic-in-place category can be useful in that case. There is "
            "no need to retain a city category if no useful subject-specific one was "
            "found. Retain a place category when the place itself is a main depicted "
            "subject, such as a recognizable cityscape or landscape. Do not remove "
            "an independent category for a second main subject merely because it "
            "overlaps the first subject's identity. Ignore incidental background "
            "objects. Treat the user hint as factual context, and use each photo's "
            "own visible evidence for its color, state, and depicted subjects."
        )
        context = {
            "photos": [
                {
                    "photo_number": index + 1,
                    "metadata": metadatas[index].to_model_dict(),
                    **proposed_photos[index],
                    "categories": categories,
                }
                for index, categories in enumerate(photo_categories)
            ],
            "group_summary": group_summary,
            "research_summary": research_summary,
            "user_hint": user_hint,
            "category_parents": {
                title: node.get("parents", [])
                for title, node in category_graph.get("nodes", {}).items()
            },
        }
        user_content: list[dict[str, Any]] = [
            {"type": "input_text", "text": json.dumps(context, indent=2, sort_keys=True)}
        ]
        user_content.extend(self._ordered_image_content(image_data_urls))
        response = self.client.responses.create(
            model=self.config.vision_model,
            reasoning={"effort": "low"},
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": instructions}]},
                {"role": "user", "content": user_content},
            ],
            text={"format": _json_schema("commons_group_category_review", schema)},
        )
        review = json.loads(response.output_text)
        reviewed_photos = review.get("photos")
        if not isinstance(reviewed_photos, list) or len(reviewed_photos) != photo_count:
            raise RuntimeError("OpenAI returned an unexpected number of category reviews")
        reviewed_categories: list[list[str]] = []
        for original, photo in zip(photo_categories, reviewed_photos):
            retained = photo.get("selected_categories") if isinstance(photo, dict) else None
            if not isinstance(retained, list) or any(
                not isinstance(title, str) or title not in original for title in retained
            ):
                raise RuntimeError("OpenAI category review returned an unselected category")
            reviewed_categories.append([title for title in original if title in retained])
        return {
            "photo_categories": reviewed_categories,
            "reasoning_summary": review["reasoning_summary"],
        }

    def choose_categories(
        self,
        image_data_url: str,
        metadata: PhotoMetadata,
        proposed_caption: str,
        proposed_summary: str,
        research_summary: str,
        category_graph: dict[str, Any],
        user_hint: str,
    ) -> dict[str, Any]:
        """Backward-compatible single-photo wrapper."""
        selection = self.choose_group_categories(
            image_data_urls=[image_data_url],
            metadatas=[metadata],
            proposed_photos=[
                {
                    "caption_en": proposed_caption,
                    "visual_summary": proposed_summary,
                }
            ],
            group_summary=proposed_summary,
            research_summary=research_summary,
            category_graph=category_graph,
            user_hint=user_hint,
        )
        return {
            "selected_categories": selection["photo_categories"][0],
            "reasoning_summary": selection["reasoning_summary"],
        }


def _preferred_filename(stem: str, suffix: str, ext: str) -> str:
    sanitized = re.sub(r"\s+", " ", stem.strip())
    sanitized = re.sub(r"[\\/:*?\"<>|#%{}\[\]]+", " ", sanitized)
    sanitized = re.sub(r"\s+", " ", sanitized).strip(" .")
    if not sanitized:
        sanitized = "Untitled"
    if suffix.strip():
        sanitized = f"{sanitized} {suffix.strip()}".strip()
    return f"{sanitized}{ext.lower()}"


def _numbered_filenames(
    group_stem: str, suffix: str, extensions: list[str]
) -> list[str]:
    """Build stable, ordered names; a single-photo upload keeps its old name shape."""
    photo_count = len(extensions)
    if photo_count == 0:
        return []
    number_width = max(2, len(str(photo_count)))
    return [
        _preferred_filename(
            (
                group_stem
                if photo_count == 1
                else f"{group_stem} {index:0{number_width}d}"
            ),
            suffix,
            extension,
        )
        for index, extension in enumerate(extensions, start=1)
    ]


def _commons_filename_key(filename: str) -> str:
    return re.sub(r"[ _]+", " ", filename).strip().casefold()


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
    return _resolve_group_equipment_categories(commons_api, [metadata])[0]


def _resolve_group_equipment_categories(
    commons_api: CommonsApi, metadatas: list[PhotoMetadata]
) -> list[list[str]]:
    print("Resolving equipment and exposure categories from group EXIF...", flush=True)
    candidates_by_photo = [
        _equipment_category_candidates(metadata) for metadata in metadatas
    ]
    all_candidates = _dedupe_preserve_order(
        [title for candidates in candidates_by_photo for title in candidates]
    )
    existing_titles = set(commons_api.existing_category_titles(all_candidates))
    return [
        [
            title.removeprefix("Category:")
            for title in candidates
            if title in existing_titles
        ]
        for candidates in candidates_by_photo
    ]


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


def _wikitext_batch_gallery(filenames: list[str]) -> str:
    normalized_filenames: list[str] = []
    for filename in filenames:
        normalized = str(filename).strip().removeprefix("File:").strip()
        normalized = re.sub(r"[\r\n\[\]{}|<>#%]+", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if normalized:
            normalized_filenames.append(normalized)
    normalized_filenames = _dedupe_preserve_order(normalized_filenames)
    if len(normalized_filenames) <= 1:
        return ""
    gallery_files = "\n".join(
        f"File:{filename}" for filename in normalized_filenames
    )
    return (
        '\n<gallery mode="packed" heights="120" showfilename="yes" '
        'caption="Related images in this batch">\n'
        f"{gallery_files}\n"
        "</gallery>"
    )


def build_description_wikitext(
    caption: str,
    metadata: PhotoMetadata,
    author: str,
    license_wikitext: str,
    categories: list[str],
    batch_filenames: list[str] | None = None,
) -> str:
    date_value = metadata.captured_on or datetime.now(timezone.utc).date().isoformat()
    batch_gallery = _wikitext_batch_gallery(batch_filenames or [])
    return (
        "=={{int:filedesc}}==\n"
        "{{Information\n"
        "|description =\n"
        f"{{{{en|1 = {caption}}}}}\n"
        f"|date = {date_value}\n"
        "|source = {{own}}\n"
        f"|author = {author}"
        f"{_raw_sha1_field(metadata)}\n"
        "}}"
        f"{batch_gallery}\n"
        f"{_wikitext_location(metadata)}\n\n"
        "=={{int:license-header}}==\n"
        f"{license_wikitext}\n\n"
        f"{_wikitext_categories(categories)}\n"
    )


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _review_static_dir() -> Path:
    source_dir = Path(__file__).with_name("static")
    if (source_dir / "commons_upload.html").is_file():
        return source_dir
    return Path(sysconfig.get_path("data")) / "share" / "pupphoto" / "static"


@lru_cache(maxsize=1)
def _review_templates() -> Environment:
    return Environment(
        loader=FileSystemLoader(_review_static_dir()),
        autoescape=select_autoescape(["html"]),
    )


def _html_page(state: dict[str, Any]) -> str:
    photos = state.get("photos")
    if not isinstance(photos, list) or not photos:
        photos = [
            {
                "index": 0,
                "source_name": "Photo",
                "metadata": state.get("metadata", {}),
                "raw_sha1sum": state.get("raw_sha1sum"),
                "sha1_matches": state.get("sha1_matches", []),
            }
        ]
    qi_count = int(state.get("quality_image_nominations_today", 0))
    qi_limit = int(state.get("quality_image_nomination_limit", 5))
    return _review_templates().get_template("commons_upload.html").render(
        photos=photos,
        qi_count=qi_count,
        qi_limit=qi_limit,
        qi_date=state.get("quality_image_nomination_date", "today"),
        qi_disabled=qi_count >= qi_limit,
    )


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


def _normalize_image_paths(image_paths: Path | list[Path]) -> list[Path]:
    paths = [image_paths] if isinstance(image_paths, Path) else list(image_paths)
    if not paths:
        raise SystemExit("At least one image path is required")

    resolved_paths: list[Path] = []
    seen: set[Path] = set()
    for image_path in paths:
        resolved = image_path.expanduser().resolve()
        if not resolved.is_file():
            raise SystemExit(f"Image path is not a file: {resolved}")
        if resolved in seen:
            raise SystemExit(f"Image path was supplied more than once: {resolved}")
        seen.add(resolved)
        resolved_paths.append(resolved)
    return resolved_paths


def run_app(image_paths: Path | list[Path]) -> None:
    app_config = load_config()
    commons_config = app_config.commons
    openai_config = app_config.openai
    _validate_required_config(commons_config, openai_config)
    resolved_paths = _normalize_image_paths(image_paths)
    photo_count = len(resolved_paths)
    print(
        f"Preparing Commons upload for {photo_count} photo"
        + ("" if photo_count == 1 else "s"),
        flush=True,
    )

    prepared_photos: list[PreparedPhoto] = []
    try:
        for index, image_path in enumerate(resolved_paths, start=1):
            print(
                f"Reading image metadata ({index}/{photo_count}): {image_path}",
                flush=True,
            )
            metadata = _read_metadata(image_path)
            upload_image_path, upload_temp_dir = _upload_safe_image_path(
                image_path, metadata
            )
            try:
                print(
                    "Metadata summary: "
                    + json.dumps(
                        metadata.to_model_dict(), sort_keys=True, ensure_ascii=True
                    ),
                    flush=True,
                )
                print(
                    f"Creating downsized preview ({index}/{photo_count})...",
                    flush=True,
                )
                preview_bytes, preview_mime_type = _downsize_image(
                    image_path, openai_config.downsized_max_dimension
                )
                prepared_photos.append(
                    PreparedPhoto(
                        source_path=image_path,
                        upload_path=upload_image_path,
                        upload_temp_dir=upload_temp_dir,
                        metadata=metadata,
                        preview_bytes=preview_bytes,
                        preview_mime_type=preview_mime_type,
                        image_data_url=_image_data_url(
                            preview_bytes, preview_mime_type
                        ),
                        sha1_matches=[],
                    )
                )
            except Exception:
                if upload_temp_dir is not None:
                    upload_temp_dir.cleanup()
                raise
    except Exception:
        for photo in prepared_photos:
            if photo.upload_temp_dir is not None:
                photo.upload_temp_dir.cleanup()
        raise

    vision = VisionClient(openai_config)
    commons_api = CommonsApi(commons_config)
    commons_api.login()
    commons_username = _botpassword_owner_username(commons_config.username)
    quality_image_nomination_date = datetime.now(timezone.utc).date()
    quality_image_nominations_today = commons_api.quality_image_nomination_count(
        commons_username, quality_image_nomination_date
    )
    print(
        "Quality Image nominations today: "
        f"{quality_image_nominations_today}/"
        f"{QUALITY_IMAGE_DAILY_NOMINATION_LIMIT}",
        flush=True,
    )
    for index, photo in enumerate(prepared_photos, start=1):
        if not photo.metadata.raw_sha1sum:
            continue
        photo.sha1_matches = [
            {"title": title, "url": _commons_file_url(title)}
            for title in commons_api.search_files_by_raw_sha1(
                photo.metadata.raw_sha1sum
            )
        ]
        if photo.sha1_matches:
            print(
                f"Found Commons files matching photo {index}'s raw SHA1 sum: "
                + json.dumps(
                    [item["title"] for item in photo.sha1_matches],
                    ensure_ascii=True,
                ),
                flush=True,
            )

    photo_state = [
        {
            "index": index,
            "source_name": photo.source_path.name,
            "metadata": photo.metadata.to_model_dict(),
            "raw_sha1sum": photo.metadata.raw_sha1sum,
            "sha1_matches": photo.sha1_matches,
        }
        for index, photo in enumerate(prepared_photos)
    ]
    state = {
        "photos": photo_state,
        "photo_count": photo_count,
        # Keep the single-photo keys for callers that render _html_page directly.
        "metadata": photo_state[0]["metadata"],
        "raw_sha1sum": photo_state[0]["raw_sha1sum"],
        "sha1_matches": photo_state[0]["sha1_matches"],
        "quality_image_nominations_today": quality_image_nominations_today,
        "quality_image_nomination_limit": QUALITY_IMAGE_DAILY_NOMINATION_LIMIT,
        "quality_image_nomination_date": _utc_date_label(quality_image_nomination_date),
    }

    def generate_review_state(user_hint: str) -> dict[str, Any]:
        print(f"Starting AI analysis pipeline. Hint: {user_hint!r}", flush=True)
        image_data_urls = [photo.image_data_url for photo in prepared_photos]
        metadatas = [photo.metadata for photo in prepared_photos]
        proposal = vision.propose_group_metadata(
            image_data_urls=image_data_urls,
            metadatas=metadatas,
            suffix_hint=commons_config.filename_suffix,
            user_hint=user_hint,
            source_names=[photo.source_path.name for photo in prepared_photos],
        )
        print("OpenAI group proposal received.", flush=True)
        relationship_queries = _dedupe_preserve_order(
            [
                re.sub(r"\s+", " ", str(query).removeprefix("Category:").strip())
                for query in proposal["relationship_queries"]
                if str(query).strip()
            ]
        )[:MAX_CATEGORY_RELATIONSHIP_QUERIES]
        print(
            "Category relationship queries: "
            + json.dumps(relationship_queries, ensure_ascii=True),
            flush=True,
        )
        uncapped_category_queries = _dedupe_preserve_order(
            relationship_queries
            + [
                re.sub(r"\s+", " ", str(query).removeprefix("Category:").strip())
                for query in proposal["search_queries"]
                if str(query).strip()
            ]
        )
        category_queries = uncapped_category_queries[:MAX_CATEGORY_SEARCH_QUERIES]
        if len(uncapped_category_queries) > len(category_queries):
            print(
                "Dropping low-priority category search queries: "
                + json.dumps(
                    uncapped_category_queries[MAX_CATEGORY_SEARCH_QUERIES:],
                    ensure_ascii=True,
                ),
                flush=True,
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
        selected = vision.choose_group_categories(
            image_data_urls=image_data_urls,
            metadatas=metadatas,
            proposed_photos=proposal["photos"],
            group_summary=proposal["group_summary"],
            research_summary=proposal["research_summary"],
            category_graph=category_graph,
            user_hint=user_hint,
        )
        print("OpenAI group category selection received.", flush=True)
        selected = vision.review_group_categories(
            image_data_urls=image_data_urls,
            metadatas=metadatas,
            proposed_photos=proposal["photos"],
            photo_categories=selected["photo_categories"],
            group_summary=proposal["group_summary"],
            research_summary=proposal["research_summary"],
            category_graph=category_graph,
            user_hint=user_hint,
        )
        filenames = _numbered_filenames(
            proposal["group_filename_stem"],
            commons_config.filename_suffix,
            [photo.upload_path.suffix for photo in prepared_photos],
        )
        equipment_categories_by_photo = _resolve_group_equipment_categories(
            commons_api, metadatas
        )
        intersection_cache: dict[
            tuple[tuple[str, str], ...], dict[str, dict[str, Any]]
        ] = {}
        review_photos: list[dict[str, Any]] = []
        for index, photo in enumerate(prepared_photos):
            photo_proposal = proposal["photos"][index]
            model_categories = [
                str(title).removeprefix("Category:")
                for title in selected["photo_categories"][index]
            ]
            model_categories = _remove_empty_categories(
                model_categories, category_graph
            )
            model_categories = _remove_ancestor_duplicates(
                model_categories, category_graph
            )
            model_categories = _refine_category_intersections(
                commons_api,
                model_categories,
                category_graph,
                evidence=[
                    photo_proposal["caption_en"],
                    photo_proposal["visual_summary"],
                    proposal["group_summary"],
                    proposal["research_summary"],
                    user_hint,
                    *proposal["keywords"],
                    *relationship_queries,
                ],
                intersection_cache=intersection_cache,
            )
            all_categories = _dedupe_preserve_order(
                model_categories + equipment_categories_by_photo[index]
            )
            print(
                f"Initial proposed categories for photo {index + 1}: "
                + json.dumps(all_categories, ensure_ascii=True),
                flush=True,
            )
            review_photos.append(
                {
                    "index": index,
                    "filename": filenames[index],
                    "caption": photo_proposal["caption_en"],
                    "categories": all_categories,
                }
            )

        review_state: dict[str, Any] = {
            "hint": user_hint,
            "photos": review_photos,
        }
        if photo_count == 1:
            review_state.update(
                {
                    "filename": review_photos[0]["filename"],
                    "caption": review_photos[0]["caption"],
                    "categories": review_photos[0]["categories"],
                }
            )
        return review_state

    batch_filenames_by_index: dict[int, str] = {}
    app = Flask(__name__, static_folder=str(_review_static_dir()))

    @app.get("/")
    def index() -> str:
        return _html_page(state)

    @app.get("/image")
    def image() -> tuple[bytes, int, dict[str, str]]:
        photo = prepared_photos[0]
        return photo.preview_bytes, 200, {"Content-Type": photo.preview_mime_type}

    @app.get("/image/<int:photo_index>")
    def group_image(photo_index: int) -> tuple[bytes, int, dict[str, str]]:
        if photo_index < 0 or photo_index >= photo_count:
            return b"Image not found", 404, {"Content-Type": "text/plain"}
        photo = prepared_photos[photo_index]
        return photo.preview_bytes, 200, {"Content-Type": photo.preview_mime_type}

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
        raw_photo_items = payload.get("photos")
        if raw_photo_items is None and photo_count == 1:
            raw_photo_items = [
                {
                    "index": 0,
                    "filename": payload.get("filename", ""),
                    "caption": payload.get("caption", ""),
                    "categories": payload.get("categories", []),
                    "nominate_quality_image": payload.get(
                        "nominate_quality_image", False
                    ),
                }
            ]
        if not isinstance(raw_photo_items, list) or not raw_photo_items:
            return jsonify({"error": "No photos were submitted for upload"}), 400

        submitted_photos: list[dict[str, Any]] = []
        submitted_indexes: set[int] = set()
        try:
            for raw_item in raw_photo_items:
                if not isinstance(raw_item, dict):
                    raise ValueError("Each submitted photo must be an object")
                photo_index = int(raw_item.get("index", -1))
                if photo_index < 0 or photo_index >= photo_count:
                    raise ValueError(f"Invalid photo index: {photo_index}")
                if photo_index in submitted_indexes:
                    raise ValueError(f"Photo {photo_index + 1} was submitted twice")
                submitted_indexes.add(photo_index)
                prepared = prepared_photos[photo_index]
                filename_input = str(raw_item.get("filename", "")).strip()
                stem = (
                    Path(filename_input).stem
                    if filename_input
                    else prepared.source_path.stem
                )
                filename = _preferred_filename(stem, "", prepared.upload_path.suffix)
                raw_categories = raw_item.get("categories", [])
                if not isinstance(raw_categories, list):
                    raise ValueError(
                        f"Categories for photo {photo_index + 1} must be a list"
                    )
                category_names: list[str] = []
                for category_item in raw_categories:
                    category = (
                        str(category_item)
                        .strip()
                        .removeprefix("Category:")
                        .strip()
                    )
                    if category:
                        category_names.append(category)
                submitted_photos.append(
                    {
                        "index": photo_index,
                        "filename": filename,
                        "caption": str(raw_item.get("caption", "")).strip(),
                        "categories": _dedupe_preserve_order(category_names),
                        "nominate_quality_image": bool(
                            raw_item.get("nominate_quality_image")
                        ),
                    }
                )
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

        proposed_batch_filenames = dict(batch_filenames_by_index)
        proposed_batch_filenames.update(
            {item["index"]: item["filename"] for item in submitted_photos}
        )
        filename_keys = [
            _commons_filename_key(filename)
            for filename in proposed_batch_filenames.values()
        ]
        if len(set(filename_keys)) != len(filename_keys):
            return jsonify({"error": "Every photo must have a unique filename"}), 400
        batch_filenames_by_index.update(proposed_batch_filenames)
        batch_filenames = [
            filename
            for _index, filename in sorted(batch_filenames_by_index.items())
        ]

        quality_image_count = sum(
            bool(item["nominate_quality_image"]) for item in submitted_photos
        )
        uploaded_files: list[dict[str, Any]] = []
        try:
            if quality_image_count:
                nominations_today = commons_api.quality_image_nomination_count(
                    commons_username, datetime.now(timezone.utc).date()
                )
                if (
                    nominations_today + quality_image_count
                    > QUALITY_IMAGE_DAILY_NOMINATION_LIMIT
                ):
                    return (
                        jsonify(
                            {
                                "error": (
                                    "Quality Image nomination limit would be exceeded: "
                                    f"{nominations_today} already listed plus "
                                    f"{quality_image_count} selected, with a daily limit "
                                    f"of {QUALITY_IMAGE_DAILY_NOMINATION_LIMIT}."
                                )
                            }
                        ),
                        400,
                    )

            for item in submitted_photos:
                prepared = prepared_photos[item["index"]]
                description = build_description_wikitext(
                    caption=item["caption"],
                    metadata=prepared.metadata,
                    author=commons_config.author,
                    license_wikitext=commons_config.license_wikitext,
                    categories=item["categories"],
                    batch_filenames=batch_filenames,
                )
                redirect_url = commons_api.upload_file(
                    image_path=prepared.upload_path,
                    filename=item["filename"],
                    description_wikitext=description,
                    summary=f"Uploading {item['filename']} via pupphoto",
                )
                uploaded_files.append(
                    {
                        "index": item["index"],
                        "filename": item["filename"],
                        "url": redirect_url,
                    }
                )
                if item["nominate_quality_image"]:
                    commons_api.nominate_quality_image(
                        file_title=f"File:{item['filename']}",
                        description=_quality_image_description(
                            item["caption"], item["filename"]
                        ),
                        username=commons_username,
                    )
        except Exception as exc:
            completed = len(uploaded_files)
            prefix = ""
            if completed:
                completed_names = ", ".join(
                    item["filename"] for item in uploaded_files
                )
                prefix = (
                    f"Uploaded {completed} of {len(submitted_photos)} photos before "
                    f"the batch stopped ({completed_names}). "
                )
            return (
                jsonify(
                    {
                        "error": prefix + str(exc),
                        "uploaded_files": uploaded_files,
                    }
                ),
                500,
            )

        for photo in prepared_photos:
            if photo.upload_temp_dir is not None:
                photo.upload_temp_dir.cleanup()
        _schedule_exit()
        return (
            jsonify(
                {
                    "redirect_url": uploaded_files[0]["url"],
                    "uploaded_files": uploaded_files,
                }
            ),
            200,
        )

    @app.post("/overwrite")
    def overwrite() -> tuple[Any, int]:
        payload = request.get_json(force=True)
        title = payload.get("title", "").strip()
        if not title.startswith("File:"):
            return jsonify({"error": "Invalid file title"}), 400
        try:
            photo_index = int(payload.get("photo_index", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid photo index"}), 400
        if photo_index < 0 or photo_index >= photo_count:
            return jsonify({"error": "Invalid photo index"}), 400
        prepared = prepared_photos[photo_index]
        valid_titles = {item["title"] for item in prepared.sha1_matches}
        if title not in valid_titles:
            return jsonify({"error": "File is not in the matching SHA1 list"}), 400
        try:
            redirect_url = commons_api.overwrite_file(
                image_path=prepared.upload_path,
                title=title,
                summary=f"Uploading new version of {title} via pupphoto",
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        batch_filenames_by_index[photo_index] = title.removeprefix("File:")
        if photo_count == 1:
            if prepared.upload_temp_dir is not None:
                prepared.upload_temp_dir.cleanup()
            _schedule_exit()
        return (
            jsonify(
                {"redirect_url": redirect_url, "photo_index": photo_index}
            ),
            200,
        )

    port = _free_port(commons_config.ui_host)
    url = f"http://{commons_config.ui_host}:{port}/"
    print(f"Opening review UI at {url}", flush=True)
    threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    print(url)
    app.run(host=commons_config.ui_host, port=port, debug=False, use_reloader=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Review and upload one photo or an ordered group of related photos to "
            "Wikimedia Commons."
        )
    )
    parser.add_argument(
        "image_paths",
        type=Path,
        nargs="+",
        help="Photo paths; multiple paths are analyzed and uploaded as one group",
    )
    args = parser.parse_args()
    run_app(args.image_paths)


if __name__ == "__main__":
    main()
