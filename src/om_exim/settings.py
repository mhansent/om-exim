"""Load and validate local migration settings."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
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
    include_user_emails: bool = False


@dataclass(frozen=True)
class HttpSettings:
    timeout_seconds: float = 30
    read_retries: int = 2
    requests_per_second: float = 0


@dataclass(frozen=True)
class Settings:
    source: Connection | None
    target: Connection
    selection: Selection | None
    show_progress: bool = False
    output_dir: Path = Path("out")
    on_conflict: str = "update"
    create_users: bool = True
    user_emails: dict[str, str] = field(default_factory=dict, repr=False)
    http: HttpSettings = HttpSettings()
    audit_file: Path | None = None


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
            "include_user_emails",
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
    include_user_emails = settings.get("include_user_emails", False)
    if type(include_user_emails) is not bool:
        raise ConfigError("migration.include_user_emails must be a boolean.")
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
        include_user_emails=include_user_emails,
    )


def load_settings(
    path: Path,
    *,
    require_selection: bool,
    require_source: bool = True,
    environ: Mapping[str, str] | None = None,
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
        raw,
        "Configuration",
        {"source", "target", "migration", "plan", "import", "http"},
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
    import_config = _mapping(
        config.get("import", {}),
        "import",
        {"on_conflict", "create_users", "user_emails", "audit_file"},
    )
    on_conflict = import_config.get("on_conflict", "update")
    if on_conflict not in ("fail", "skip", "update"):
        raise ConfigError("import.on_conflict must be fail, skip, or update.")
    create_users = import_config.get("create_users", True)
    if type(create_users) is not bool:
        raise ConfigError("import.create_users must be a boolean.")
    user_emails = import_config.get("user_emails", {})
    if not isinstance(user_emails, dict) or any(
        not isinstance(key, str) or not key.strip() or not valid_email(value)
        for key, value in user_emails.items()
    ):
        raise ConfigError(
            "import.user_emails must map user FQNs to valid email addresses."
        )
    audit_file = import_config.get("audit_file")
    if audit_file is not None and (
        not isinstance(audit_file, str)
        or not audit_file.strip()
        or "\x00" in audit_file
    ):
        raise ConfigError("import.audit_file must be a non-empty path.")
    return Settings(
        _connection("source", config.get("source", {}), values)
        if require_source
        else None,
        _connection("target", config.get("target", {}), values),
        selection,
        show_progress,
        Path(output_dir),
        on_conflict,
        create_users,
        user_emails,
        _http_settings(config.get("http", {})),
        Path(audit_file) if audit_file is not None else None,
    )


def valid_email(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) is not None
    )


def _http_settings(raw: object) -> HttpSettings:
    config = _mapping(
        raw, "http", {"timeout_seconds", "read_retries", "requests_per_second"}
    )
    timeout = config.get("timeout_seconds", 30)
    retries = config.get("read_retries", 2)
    rate = config.get("requests_per_second", 0)
    try:
        valid_timeout = (
            type(timeout) in {int, float} and isfinite(timeout) and timeout > 0
        )
        valid_rate = type(rate) in {int, float} and (
            rate == 0 or rate > 0 and isfinite(rate) and isfinite(1 / rate)
        )
    except OverflowError:
        raise ConfigError("HTTP timeout and rate must be finite numbers.") from None
    if not valid_timeout:
        raise ConfigError("http.timeout_seconds must be a positive finite number.")
    if type(retries) is not int or not 0 <= retries <= 10:
        raise ConfigError("http.read_retries must be an integer from 0 to 10.")
    if not valid_rate:
        raise ConfigError(
            "http.requests_per_second must be zero or a positive finite rate."
        )
    return HttpSettings(timeout, retries, rate)
