"""Command line entry point."""

import argparse
import json
import sys
from pathlib import Path

import requests

from .client import BASELINE_SDK, ReadClient, ReadError, preflight
from .exporter import ExportError, export_package, format_summary
from .importer import MigrationImportError, import_package, validate_target
from .package import PackageError, load_package
from .plan import DOMAIN_DISCOVERY_WARNING, build_plan
from .settings import ConfigError, load_settings
from .target import TargetClient


def _print_progress(message: str) -> None:
    print(f"om-exim: {message}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="om-exim")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "plan", "export", "import", "validate"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        if name in {"import", "validate"}:
            command.add_argument("--package", type=Path, required=True)
        if name == "import":
            mode = command.add_mutually_exclusive_group()
            mode.add_argument("--apply", action="store_true")
            mode.add_argument("--dry-run", action="store_true")
            command.add_argument("--on-conflict", choices=("fail", "skip", "update"))
            command.add_argument("--checkpoint", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.command in {"import", "validate"}:
            package = load_package(args.package)
            settings = load_settings(
                args.config, require_selection=False, require_source=False
            )
            with requests.Session() as target_session:
                target = TargetClient(settings.target, "Target", target_session)
                if target.version() != package.version:
                    raise MigrationImportError(
                        "The target server must report exactly the package's OpenMetadata version."
                    )
                if args.command == "import":
                    package_directory = args.package.resolve()
                    checkpoint = args.checkpoint or (
                        package_directory.parent
                        / f".{package_directory.name}.import-state.ndjson"
                    )
                    result = import_package(
                        target,
                        package,
                        checkpoint,
                        policy=args.on_conflict or settings.on_conflict,
                        apply=args.apply,
                        create_users=settings.create_users,
                        user_emails=settings.user_emails,
                    )
                else:
                    result = validate_target(target, package)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return int(args.command == "validate" and not result["valid"])
        settings = load_settings(
            args.config, require_selection=args.command != "preflight"
        )
        progress = (
            _print_progress if settings.selection and settings.show_progress else None
        )
        if settings.selection and settings.selection.discovery == "domain":
            print(f"om-exim: {DOMAIN_DISCOVERY_WARNING}", file=sys.stderr)
        with requests.Session() as source_session, requests.Session() as target_session:
            assert settings.source is not None
            source = ReadClient(settings.source, "Source", source_session)
            target = ReadClient(settings.target, "Target", target_session)
            if progress:
                progress("Checking source and target OpenMetadata versions.")
            version = preflight(source, target)
            if settings.selection is None:
                result = {
                    "openmetadata_version": version,
                    "sdk_schema_baseline": BASELINE_SDK,
                }
            elif args.command == "plan":
                result = build_plan(
                    source, settings.selection, version, progress=progress
                )
            else:
                counts, reference_count = export_package(
                    source,
                    settings.selection,
                    version,
                    settings.output_dir,
                    tokens=(settings.source.token, settings.target.token),
                    progress=progress,
                )
    except (
        ConfigError,
        ReadError,
        ExportError,
        PackageError,
        MigrationImportError,
    ) as error:
        print(f"om-exim: {error}", file=sys.stderr)
        return 1
    if args.command == "export":
        print(f"Export written to {settings.output_dir}")
        print(format_summary(counts))
        print(f"References to review: {reference_count:,}")
        return 0
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if progress:
        progress("Plan complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
