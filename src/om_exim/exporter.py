"""Write a versioned package from the read-only migration plan."""

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from .access import policy_rules
from .client import Document, ReadClient
from .plan import Progress, build_plan
from .settings import Selection, valid_email

ENTITY_FIELDS = {
    "domain": ("domainType",),
    "dataProduct": ("dataProductType",),
    "table": ("tableType",),
    "database": ("serviceType",),
    "databaseSchema": ("serviceType",),
    "databaseService": ("serviceType",),
    "team": ("teamType",),
    "role": ("provider", "allowEdit"),
    "policy": ("provider", "allowEdit", "enabled"),
    "user": ("email", "isBot"),
}
COLUMN_FIELDS = (
    "name",
    "dataType",
    "arrayDataType",
    "dataLength",
    "precision",
    "scale",
    "ordinalPosition",
    "constraint",
)
TYPE_LABELS = {
    "classification": "Classifications",
    "database": "Databases",
    "databaseSchema": "Database Schemas",
    "databaseService": "Database Services",
    "dataProduct": "Data Products",
    "domain": "Domains",
    "glossary": "Glossaries",
    "glossaryTerm": "Glossary Terms",
    "table": "Tables",
    "tag": "Tags",
    "team": "Teams",
    "user": "Users",
    "role": "Roles",
    "policy": "Policies",
}


class ExportError(ValueError):
    pass


def _scalars(payload: Document, fields: tuple[str, ...]) -> Document:
    return {
        field: payload[field]
        for field in fields
        if type(payload.get(field)) in {str, int, bool}
    }


def _columns(raw: object) -> list[Document]:
    if not isinstance(raw, list) or any(not isinstance(column, dict) for column in raw):
        raise ExportError(
            "The source omitted table columns or returned invalid column metadata."
        )
    columns = []
    for column in raw:
        if not isinstance(column.get("name"), str) or not isinstance(
            column.get("dataType"), str
        ):
            raise ExportError(
                "The source returned a column without a name or data type."
            )
        clean = _scalars(column, COLUMN_FIELDS)
        if column.get("children") is not None:
            clean["children"] = _columns(column["children"])
        columns.append(clean)
    return columns


def _encode(
    document: Document, token_patterns: tuple[bytes, ...], *, indent: int | None = None
) -> bytes:
    serialized = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        indent=indent,
        separators=(",", ":") if indent is None else None,
    )
    line = (serialized + "\n").encode("utf-8")
    if any(pattern in line for pattern in token_patterns):
        raise ExportError(
            "A connection token matched export data. The package was not written."
        )
    return line


def export_package(
    client: ReadClient,
    selection: Selection,
    server_version: str,
    output: Path,
    *,
    tokens: tuple[str, ...],
    progress: Progress | None = None,
) -> tuple[dict[str, int], int]:
    if output.exists() or output.is_symlink():
        raise ExportError(
            "The export directory already exists. Choose a new migration.output_dir."
        )

    records: dict[str, list[Document]] = defaultdict(list)

    def capture(entry: Document, payload: Document) -> None:
        kind = entry["kind"]
        if payload.get("deleted") is True:
            raise ExportError(
                "The source returned a deleted entity in the active entity scope."
            )
        data = _scalars(payload, ("name", *ENTITY_FIELDS.get(kind, ())))
        if kind == "user":
            email = data.pop("email", None)
            if selection.include_user_emails and valid_email(email):
                data["email"] = email
        if kind == "policy":
            if payload.get("location") is not None:
                raise ExportError(
                    "This export includes a location-scoped team policy. Its scope cannot be transferred."
                )
            try:
                data["rules"] = policy_rules(payload.get("rules"))
            except ValueError as error:
                raise ExportError(str(error)) from None
        if kind == "table":
            data["columns"] = _columns(payload.get("columns"))
        records[kind].append({**entry, "data": data})

    plan = build_plan(
        client, selection, server_version, progress=progress, capture=capture
    )
    counts = {kind: len(rows) for kind, rows in records.items()}
    references = plan["references"]
    warnings = [
        *plan["warnings"],
        "The package contains allowlisted structural metadata and planned relationships.",
        "Free-text descriptions, profile data, and service connection settings are omitted.",
    ]
    if references:
        warnings.append(
            "Review reference reasons before using the package for migration."
        )
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(timespec="microseconds"),
        "openmetadata_version": server_version,
        "coverage": plan["coverage"],
        "team_access": True,
        "selection": {
            **plan["selection"],
            "primary_relationship": "direct",
            "dependency_policy": "required_only",
            "external_reference_policy": "report",
            "include_deleted": False,
        },
        "counts": counts,
        "counts_by_role": plan["counts"],
        "relationships": plan["relationships"],
        "references": references,
        "warnings": warnings,
        "files": {},
    }

    if progress:
        progress("Writing and hashing export files...")
    patterns = []
    for token in tokens:
        quoted = json.dumps(token, ensure_ascii=False)
        patterns.append((quoted[1:-1] if len(token) >= 8 else quoted).encode("utf-8"))
    token_patterns = tuple(patterns)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".om-exim-", dir=output.parent) as directory:
            staging = Path(directory) / "package"
            staging.mkdir(mode=0o700)
            for kind, rows in records.items():
                path = staging / f"{kind}.ndjson"
                checksum = hashlib.sha256()
                with path.open("wb") as stream:
                    for row in rows:
                        line = _encode(row, token_patterns)
                        stream.write(line)
                        checksum.update(line)
                manifest["files"][kind] = {
                    "path": path.name,
                    "count": counts[kind],
                    "sha256": checksum.hexdigest(),
                }

            (staging / "manifest.json").write_bytes(
                _encode(manifest, token_patterns, indent=2)
            )
            if output.exists() or output.is_symlink():
                raise ExportError(
                    "The export directory already exists. Choose a new migration.output_dir."
                )
            staging.rename(output)
    except OSError:
        raise ExportError(
            "Unable to write the export package. Check migration.output_dir."
        ) from None

    return counts, len(references)


def format_summary(counts: dict[str, int]) -> str:
    rows = sorted((TYPE_LABELS[kind], f"{count:,}") for kind, count in counts.items())
    rows.append(("Total", f"{sum(counts.values()):,}"))
    width = max(len("Entity Type"), *(len(label) for label, _ in rows))
    number_width = max(len("Count"), *(len(value) for _, value in rows))
    left, right = "━" * (width + 2), "━" * (number_width + 2)
    return "\n".join(
        [
            f"┏{left}┳{right}┓",
            f"┃ {'Entity Type':<{width}} ┃ {'Count':>{number_width}} ┃",
            f"┡{left}╇{right}┩",
            *(
                f"│ {label:<{width}} │ {value:>{number_width}} │"
                for label, value in rows
            ),
            f"└{'─' * (width + 2)}┴{'─' * (number_width + 2)}┘",
        ]
    )
