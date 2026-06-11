#!/usr/bin/env python3

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

from config import load_config


def directory_arg(path: Path) -> str:
    return f"{path}/"


def build_rsync_command(
    hot_root: Path,
    cold_root: Path,
    *,
    dry_run: bool,
    rsync_command: str = "rsync",
) -> list[str]:
    command = [
        rsync_command,
        "-a",
        "--update",
        "--partial",
        "--human-readable",
        "--itemize-changes",
        "--info=stats2,progress2",
    ]
    if dry_run:
        command.append("--dry-run")

    command.extend([directory_arg(hot_root), directory_arg(cold_root)])
    return command


def validate_roots(hot_root: Path, cold_root: Path) -> None:
    if not hot_root.is_dir():
        raise ValueError(f"Hot photo root is not a directory: {hot_root}")

    resolved_hot = hot_root.resolve()
    resolved_cold = cold_root.resolve(strict=False)
    if resolved_hot == resolved_cold:
        raise ValueError("Hot and cold photo roots point to the same directory")
    if resolved_cold.is_relative_to(resolved_hot):
        raise ValueError("Cold photo root is inside the hot photo root")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rsync the configured hot photo root into the cold photo root."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what rsync would copy without writing files.",
    )
    parser.add_argument(
        "--rsync",
        default="rsync",
        help="rsync executable to run. Defaults to rsync.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config().photo_library

    try:
        validate_roots(config.hot_root, config.cold_root)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    config.cold_root.mkdir(parents=True, exist_ok=True)
    command = build_rsync_command(
        config.hot_root,
        config.cold_root,
        dry_run=args.dry_run,
        rsync_command=args.rsync,
    )
    print(shlex.join(command))

    try:
        return subprocess.run(command, check=False).returncode
    except OSError as exc:
        print(f"Failed to run rsync: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
