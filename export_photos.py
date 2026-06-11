#!/usr/bin/env python3

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from config import PhotoLibraryConfig, load_config


@dataclass(frozen=True)
class LibraryRoot:
    name: str
    path: Path
    raw_subdir: str
    cooked_subdir: str

    @property
    def raw_dir(self) -> Path:
        return self.path / self.raw_subdir

    @property
    def cooked_dir(self) -> Path:
        return self.path / self.cooked_subdir


@dataclass(frozen=True)
class ExportJob:
    root: LibraryRoot
    raw_path: Path
    sidecar_path: Path
    output_path: Path
    reason: str


@dataclass
class ScanResult:
    jobs: list[ExportJob]
    up_to_date: int
    missing_raw: list[Path]


def raw_path_for_sidecar(sidecar_path: Path) -> Path:
    return sidecar_path.with_suffix("")


def output_path_for_raw(raw_path: Path, cooked_dir: Path) -> Path:
    return cooked_dir / raw_path.with_suffix(".jpg").name


def export_reason(sidecar_path: Path, output_path: Path) -> str | None:
    if not output_path.exists():
        return "missing"
    if sidecar_path.stat().st_mtime_ns > output_path.stat().st_mtime_ns:
        return "stale"
    return None


def scan_root(root: LibraryRoot, raw_extensions: set[str]) -> ScanResult:
    jobs: list[ExportJob] = []
    missing_raw: list[Path] = []
    up_to_date = 0

    if not root.raw_dir.is_dir():
        raise FileNotFoundError(f"{root.raw_dir} is not a directory")

    for sidecar_path in sorted(root.raw_dir.iterdir()):
        if sidecar_path.suffix.lower() != ".xmp":
            continue

        raw_path = raw_path_for_sidecar(sidecar_path)
        if raw_path.suffix.lower() not in raw_extensions:
            continue
        if not raw_path.is_file():
            missing_raw.append(sidecar_path)
            continue

        output_path = output_path_for_raw(raw_path, root.cooked_dir)
        reason = export_reason(sidecar_path, output_path)
        if reason is None:
            up_to_date += 1
            continue

        jobs.append(
            ExportJob(
                root=root,
                raw_path=raw_path,
                sidecar_path=sidecar_path,
                output_path=output_path,
                reason=reason,
            )
        )

    return ScanResult(jobs=jobs, up_to_date=up_to_date, missing_raw=missing_raw)


def resolve_darktable_cli(configured_path: str) -> str:
    configured = Path(configured_path)
    if configured.is_file():
        return str(configured)

    path_command = shutil.which("darktable-cli")
    if path_command is not None:
        return path_command

    return configured_path


def run_export(job: ExportJob, darktable_cli: str, dry_run: bool) -> bool:
    relative_output = job.output_path.relative_to(job.root.path)
    print(f"[{job.root.name}] {job.reason}: {relative_output}")
    if dry_run:
        return True

    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output = job.output_path.with_name(
        f".{job.output_path.stem}.tmp{job.output_path.suffix}"
    )
    if temp_output.exists():
        temp_output.unlink()

    try:
        subprocess.run(
            [
                darktable_cli,
                str(job.raw_path),
                str(job.sidecar_path),
                str(temp_output),
            ],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(
            f"[{job.root.name}] failed to export {job.sidecar_path.name}: {exc}",
            file=sys.stderr,
        )
        return False

    temp_output.replace(job.output_path)
    return True


def configured_roots(config: PhotoLibraryConfig) -> dict[str, LibraryRoot]:
    return {
        "hot": LibraryRoot(
            "hot", config.hot_root, config.raw_subdir, config.cooked_subdir
        ),
        "cold": LibraryRoot(
            "cold", config.cold_root, config.raw_subdir, config.cooked_subdir
        ),
    }


def selected_roots(
    root_choice: str, roots: dict[str, LibraryRoot]
) -> list[LibraryRoot]:
    if root_choice == "all":
        return [roots["hot"], roots["cold"]]
    return [roots[root_choice]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export darktable sidecars from the hot or cold photo library."
    )
    parser.add_argument(
        "--root",
        choices=["hot", "cold", "all"],
        default="hot",
        help="Photo library root to scan. Defaults to hot.",
    )
    parser.add_argument(
        "--darktable-cli",
        help="Override the configured darktable-cli executable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show exports that would run without writing files.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app_config = load_config()
    photo_library_config = app_config.photo_library
    roots = configured_roots(photo_library_config)
    raw_extensions = {
        suffix.lower() for suffix in app_config.import_config.supported_raw_formats
    }
    darktable_cli = resolve_darktable_cli(
        args.darktable_cli or photo_library_config.darktable_cli
    )
    failed = 0

    for root in selected_roots(args.root, roots):
        try:
            result = scan_root(root, raw_extensions)
        except FileNotFoundError as exc:
            print(f"[{root.name}] {exc}", file=sys.stderr)
            failed += 1
            continue

        for sidecar_path in result.missing_raw:
            print(
                f"[{root.name}] missing raw for sidecar: {sidecar_path.name}",
                file=sys.stderr,
            )
        failed += len(result.missing_raw)

        exported = 0
        for job in result.jobs:
            if run_export(job, darktable_cli, args.dry_run):
                exported += 1
            else:
                failed += 1

        action = "would export" if args.dry_run else "exported"
        print(
            f"[{root.name}] {action} {exported}; "
            f"up to date {result.up_to_date}; missing raw {len(result.missing_raw)}"
        )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
