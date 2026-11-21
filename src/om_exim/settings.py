"""Load and validate local migration settings."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from dotenv import dotenv_values


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Connection:
    server_url: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class Selection:
    domains: tuple[str, ...]
    batch_size: int = 100
    include_subdomains: bool = True
    include_dependencies: bool = True
    table_batch_size: int | None = None
    discovery: str = "full_scan"


@dataclass(frozen=True)
class Settings:
    source: Connection
    target: Connection
    selection: Selection | None
    show_progress: bool = False
    output_dir: Path = Path("out")


def _mapping(value: object, location: str, keys: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{location} must be a mapping.")
    if value.keys() - keys:
        raise ConfigError(f"{location} contains an unsupported setting.")
    return value


def _connection(name: str, raw: object, values: Mapping[str, str | None]) -> Connection:
    config = _mapping(raw, name, {"server_url", "token_env"})
    prefix = f"OM_{name.upper()}"
    url = values.get(f"{prefix}_SERVER_URL", config.get("server_url"))
    if not isinstance(url, str) or not url or any(char.isspace() for char in url):
        raise ConfigError(f"{name}.server_url must be a valid API base URL.")
    try:
        parsed = urlsplit(url)
        valid_url = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.port != 0
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ConfigError(f"{name}.server_url must be a valid API base URL.")

    token_env = values.get(f"{prefix}_TOKEN_ENV", config.get("token_env"))
    if not isinstance(token_env, str) or not re.fullmatch(
        r"[a-zA-Z_]\w*", token_env, re.ASCII
    ):
        raise ConfigError(f"{name}.token_env must be an environment variable name.")
    token = values.get(token_env)
    if not token or not token.isascii() or not token.isprintable() or " " in token:
        raise ConfigError(f"The token for {name} is missing or invalid.")
    return Connection(url.rstrip("/"), token)


def _selection(raw: object) -> Selection:
    settings = _mapping(
        raw,
        "migration",
        {
            "domains",
            "batch_size",
            "table_batch_size",
            "discovery",
            "include_subdomains",
            "include_dependencies",
            "primary_relationship",
            "dependency_policy",
            "external_reference_policy",
            "output_dir",
        },
    )
    domains = settings.get("domains")
    if (
        not isinstance(domains, list)
        or not domains
        or any(
            not isinstance(domain, str)
            or not domain.strip()
            or domain.strip() != domain
            for domain in domains
        )
    ):
        raise ConfigError(
            "migration.domains must contain at least one exact domain FQN."
        )
    for name, expected in (
        ("primary_relationship", "direct"),
        ("dependency_policy", "required_only"),
        ("external_reference_policy", "report"),
    ):
        if settings.get(name, expected) != expected:
            raise ConfigError(f"migration.{name} must be {expected}.")
    size = settings.get("batch_size", 100)
    if type(size) is not int or not 1 <= size <= 1000:
        raise ConfigError("migration.batch_size must be an integer from 1 to 1000.")
    table_size = settings.get("table_batch_size", size)
    if type(table_size) is not int or not 1 <= table_size <= 1_000_000:
        raise ConfigError(
            "migration.table_batch_size must be an integer from 1 to 1000000."
        )
    discovery = settings.get("discovery", "full_scan")
    if discovery not in ("full_scan", "domain"):
        raise ConfigError("migration.discovery must be full_scan or domain.")
    subdomains = settings.get("include_subdomains", True)
    dependencies = settings.get("include_dependencies", True)
    if type(subdomains) is not bool or type(dependencies) is not bool:
        raise ConfigError("Migration inclusion settings must be booleans.")
    return Selection(
        tuple(dict.fromkeys(domains)),
        batch_size=size,
        include_subdomains=subdomains,
        include_dependencies=dependencies,
        table_batch_size=table_size,
        discovery=discovery,
    )


def load_settings(
    path: Path, *, require_selection: bool, environ: Mapping[str, str] | None = None
) -> Settings:
    try:
        with path.open(encoding="utf-8") as config_file:
            raw = yaml.safe_load(config_file)
        values = dict(dotenv_values(path.with_name(".env"), interpolate=False))
    except (OSError, ValueError, yaml.YAMLError):
        raise ConfigError(
            "Unable to read the selected YAML configuration or .env."
        ) from None
    values.update(os.environ if environ is None else environ)
    config = _mapping(
        raw, "Configuration", {"source", "target", "migration", "plan", "import"}
    )
    plan = _mapping(config.get("plan", {}), "plan", {"show_progress"})
    show_progress = plan.get("show_progress", False)
    if type(show_progress) is not bool:
        raise ConfigError("plan.show_progress must be a boolean.")
    migration = config.get("migration")
    selection = _selection(migration) if require_selection else None
    output_dir = migration.get("output_dir", "out") if selection else "out"
    if (
        not isinstance(output_dir, str)
        or not output_dir.strip()
        or "\x00" in output_dir
    ):
        raise ConfigError("migration.output_dir must be a non-empty path.")
    return Settings(
        _connection("source", config.get("source", {}), values),
        _connection("target", config.get("target", {}), values),
        selection,
        show_progress,
        Path(output_dir),
    )
