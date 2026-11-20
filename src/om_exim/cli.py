"""Command line entry point."""

import argparse
import json
import sys
from pathlib import Path

import requests

from .client import BASELINE_SDK, ReadClient, ReadError, preflight
from .plan import build_plan
from .settings import ConfigError, load_settings


def _print_progress(message: str) -> None:
    print(f"om-exim: {message}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="om-exim")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "plan", "export", "import", "validate"):
        deferred = name in {"export", "import", "validate"}
        command = commands.add_parser(
            name, help="reserved for a later phase" if deferred else None
        )
        command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        if name in {"import", "validate"}:
            command.add_argument("--package", type=Path, required=True)
        if name == "import":
            mode = command.add_mutually_exclusive_group()
            mode.add_argument("--apply", action="store_true")
            mode.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.command not in {"preflight", "plan"}:
        print(f"{args.command} is reserved for a later phase.", file=sys.stderr)
        return 2

    try:
        settings = load_settings(args.config, require_selection=args.command == "plan")
        progress = (
            _print_progress if settings.selection and settings.show_progress else None
        )
        with requests.Session() as source_session, requests.Session() as target_session:
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
            else:
                result = build_plan(
                    source, settings.selection, version, progress=progress
                )
    except (ConfigError, ReadError) as error:
        print(f"om-exim: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if progress:
        progress("Plan complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
