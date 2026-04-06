from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any
import tomllib


def _expand_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


def _section_kwargs(dataclass_type: type, data: dict[str, Any]) -> dict[str, Any]:
    allowed_fields = {field.name for field in fields(dataclass_type)}
    return {key: value for key, value in data.items() if key in allowed_fields}


@dataclass
class BannedArea:
    name: str
    latitude: float
    longitude: float
    radius_meters: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BannedArea":
        return cls(**_section_kwargs(cls, data))


@dataclass
class ImportConfig:
    camera_dir: Path
    photo_destination: Path
    video_destination: Path
    supported_raw_formats: list[str]
    supported_video_formats: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any], base_dir: Path) -> "ImportConfig":
        kwargs = _section_kwargs(cls, data)
        return cls(
            camera_dir=_expand_path(kwargs["camera_dir"], base_dir),
            photo_destination=_expand_path(kwargs["photo_destination"], base_dir),
            video_destination=_expand_path(kwargs["video_destination"], base_dir),
            supported_raw_formats=[
                suffix.lower() for suffix in kwargs["supported_raw_formats"]
            ],
            supported_video_formats=[
                suffix.lower() for suffix in kwargs["supported_video_formats"]
            ],
        )


@dataclass
class UploadConfig:
    pictures_dir: Path
    thumb_dir: Path
    rclone_destination: str
    public_base_url: str
    blog_image_dir: Path

    @classmethod
    def from_dict(cls, data: dict[str, Any], base_dir: Path) -> "UploadConfig":
        kwargs = _section_kwargs(cls, data)
        return cls(
            pictures_dir=_expand_path(kwargs["pictures_dir"], base_dir),
            thumb_dir=_expand_path(kwargs["thumb_dir"], base_dir),
            rclone_destination=kwargs["rclone_destination"],
            public_base_url=kwargs["public_base_url"].rstrip("/"),
            blog_image_dir=_expand_path(kwargs["blog_image_dir"], base_dir),
        )


@dataclass
class AlbumConfig:
    template_path: Path
    output_dir: Path
    rsync_destination: str
    public_base_url: str
    max_workers: int

    @classmethod
    def from_dict(cls, data: dict[str, Any], base_dir: Path) -> "AlbumConfig":
        kwargs = _section_kwargs(cls, data)
        return cls(
            template_path=_expand_path(kwargs["template_path"], base_dir),
            output_dir=_expand_path(kwargs["output_dir"], base_dir),
            rsync_destination=kwargs["rsync_destination"],
            public_base_url=kwargs["public_base_url"].rstrip("/"),
            max_workers=kwargs["max_workers"],
        )


@dataclass
class OpenAIConfig:
    api_key: str
    vision_model: str
    image_detail: str
    downsized_max_dimension: int

    @classmethod
    def from_dict(cls, data: dict[str, Any], base_dir: Path) -> "OpenAIConfig":
        kwargs = _section_kwargs(cls, data)
        return cls(
            api_key=kwargs["api_key"],
            vision_model=kwargs["vision_model"],
            image_detail=kwargs["image_detail"],
            downsized_max_dimension=kwargs["downsized_max_dimension"],
        )


@dataclass
class CommonsConfig:
    api_url: str
    username: str
    password: str
    author: str
    license_wikitext: str
    filename_suffix: str
    search_limit_per_query: int
    max_candidate_categories: int
    max_parent_depth: int
    ui_host: str
    quality_images_category: str
    quality_images_scan_limit: int

    @classmethod
    def from_dict(cls, data: dict[str, Any], base_dir: Path) -> "CommonsConfig":
        kwargs = _section_kwargs(cls, data)
        return cls(
            api_url=kwargs["api_url"],
            username=kwargs["username"],
            password=kwargs["password"],
            author=kwargs["author"],
            license_wikitext=kwargs["license_wikitext"],
            filename_suffix=kwargs["filename_suffix"],
            search_limit_per_query=kwargs["search_limit_per_query"],
            max_candidate_categories=kwargs["max_candidate_categories"],
            max_parent_depth=kwargs["max_parent_depth"],
            ui_host=kwargs["ui_host"],
            quality_images_category=kwargs["quality_images_category"],
            quality_images_scan_limit=kwargs["quality_images_scan_limit"],
        )


@dataclass
class AppConfig:
    import_config: ImportConfig
    upload: UploadConfig
    album: AlbumConfig
    openai: OpenAIConfig
    commons: CommonsConfig
    banned_areas: list[BannedArea]

    @classmethod
    def from_dict(cls, data: dict[str, Any], base_dir: Path) -> "AppConfig":
        return cls(
            import_config=ImportConfig.from_dict(data["import"], base_dir),
            upload=UploadConfig.from_dict(data["upload"], base_dir),
            album=AlbumConfig.from_dict(data["album"], base_dir),
            openai=OpenAIConfig.from_dict(data["openai"], base_dir),
            commons=CommonsConfig.from_dict(data["commons"], base_dir),
            banned_areas=[BannedArea.from_dict(item) for item in data["banned_areas"]],
        )

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        return {
            "import": {
                **raw["import_config"],
                "camera_dir": str(self.import_config.camera_dir),
                "photo_destination": str(self.import_config.photo_destination),
                "video_destination": str(self.import_config.video_destination),
            },
            "upload": {
                **raw["upload"],
                "pictures_dir": str(self.upload.pictures_dir),
                "thumb_dir": str(self.upload.thumb_dir),
                "blog_image_dir": str(self.upload.blog_image_dir),
            },
            "album": {
                **raw["album"],
                "template_path": str(self.album.template_path),
                "output_dir": str(self.album.output_dir),
            },
            "openai": raw["openai"],
            "commons": raw["commons"],
            "banned_areas": raw["banned_areas"],
        }


def default_config_path() -> Path:
    return Path(__file__).with_name("config.toml")


def load_config(config_path: Path | None = None) -> AppConfig:
    path = default_config_path() if config_path is None else Path(config_path)
    with path.open("rb") as f:
        data = tomllib.load(f)
    return AppConfig.from_dict(data, path.parent)
