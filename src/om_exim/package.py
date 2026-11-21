"""Validate the versioned export package before contacting a target."""

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from .access import TEAM_FIELDS, policy_rules
from .client import Document
from .exporter import COLUMN_FIELDS, ENTITY_FIELDS
from .registry import PRIMARY_TYPES, REGISTRY
from .settings import valid_email

Key = tuple[str, str]
ROLES = {"selected_domain", "primary", "required_dependency"}


class PackageError(ValueError):
    pass


@dataclass(frozen=True)
class Package:
    fingerprint: str
    version: str
    entities: dict[Key, Document]
    links: dict[Key, dict[str, list[Key]]]
    reference_count: int
    team_access: bool = False


def _read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise PackageError(f"Missing or invalid package file {path.name}.")
    return path.read_bytes()


def _unique_object(pairs: list[tuple[str, object]]) -> Document:
    result: Document = {}
    for key, value in pairs:
        if key in result:
            raise PackageError("The package contains duplicate JSON object keys.")
        result[key] = value
    return result


def _json(data: bytes) -> Document:
    document = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(document, dict):
        raise PackageError(
            "The package contains a JSON value where an object was expected."
        )
    return document


def _string(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not any(ord(c) < 32 for c in value)
    )


def _validate_columns(value: object) -> None:
    if not isinstance(value, list):
        raise PackageError("An exported table has invalid columns.")
    names: set[str] = set()
    for column in value:
        if (
            not isinstance(column, dict)
            or set(column) - (set(COLUMN_FIELDS) | {"children"})
            or not _string(column.get("name"))
            or not _string(column.get("dataType"))
            or column["name"] in names
        ):
            raise PackageError("An exported table has an invalid or duplicate column.")
        names.add(column["name"])
        for field in set(column) - {"children"}:
            expected = (
                int
                if field in {"dataLength", "precision", "scale", "ordinalPosition"}
                else str
            )
            if type(column[field]) is not expected:
                raise PackageError("An exported column has an invalid field value.")
        if "children" in column:
            _validate_columns(column["children"])


def _validate_entity(row: Document, kind: str) -> None:
    role = row.get("role")
    if (
        set(row) != {"kind", "id", "fqn", "role", "data"}
        or row.get("kind") != kind
        or not _string(row.get("id"))
        or not _string(row.get("fqn"))
        or not isinstance(role, str)
        or role not in ROLES
        or (role == "selected_domain" and kind != "domain")
        or (role == "primary" and kind not in PRIMARY_TYPES)
        or (role == "required_dependency" and kind in PRIMARY_TYPES)
    ):
        raise PackageError("A package entity has an invalid identity or role.")
    data = row["data"]
    allowed = {"name", *ENTITY_FIELDS.get(kind, ())}
    if kind == "table":
        allowed.add("columns")
    if kind == "policy":
        allowed.add("rules")
    if not isinstance(data, dict) or set(data) - allowed:
        raise PackageError("A package entity contains unsupported fields.")
    for field in set(data) - {"columns", "rules"}:
        if field in {"enabled", "allowEdit", "isBot"}:
            if type(data[field]) is not bool:
                raise PackageError("A package entity contains an invalid boolean.")
        elif not _string(data[field]):
            raise PackageError("A package entity contains an invalid field value.")
    if "email" in data and not valid_email(data["email"]):
        raise PackageError("A package user has an invalid email address.")
    if "provider" in data and data["provider"] not in {"system", "user", "automation"}:
        raise PackageError("A package entity has an unsupported provider.")
    if kind == "policy":
        try:
            rules = policy_rules(data.get("rules"))
        except ValueError as error:
            raise PackageError(str(error)) from None
        if rules != data["rules"]:
            raise PackageError("A package policy contains unsupported rule fields.")
    if len(data.get("name", "")) > 256:
        raise PackageError("A package entity name exceeds the OpenMetadata limit.")
    if kind == "table":
        _validate_columns(data.get("columns"))


def _links(
    raw: object, entities: dict[Key, Document]
) -> dict[Key, dict[str, list[Key]]]:
    if not isinstance(raw, list):
        raise PackageError("The package relationships must be a list.")
    result: dict[Key, dict[str, list[Key]]] = {}
    for edge in raw:
        if (
            not isinstance(edge, dict)
            or set(edge)
            != {"source_kind", "source_id", "field", "target_kind", "target_id"}
            or not all(_string(value) for value in edge.values())
        ):
            raise PackageError("The package contains a malformed relationship.")
        source = edge["source_kind"], edge["source_id"]
        target = edge["target_kind"], edge["target_id"]
        field = edge["field"]
        if source not in entities or target not in entities:
            raise PackageError("A package relationship points outside the package.")
        spec = REGISTRY[source[0]]
        allowed = {rule.field: rule.kinds for rule in spec.dependencies}
        if spec.domain_field:
            allowed[spec.domain_field] = ("domain",)
        if "tags" in spec.fields:
            allowed["tags"] = ("tag", "glossaryTerm")
        if source[0] == "dataProduct":
            allowed["assets"] = ("table",)
        if source[0] == "user":
            allowed["teams"] = ("team",)
        if target[0] not in allowed.get(field, ()):
            raise PackageError("A package relationship is unsupported.")
        if field == "teams" and entities[target]["data"].get("teamType") != "Group":
            raise PackageError("A scoped user membership must point to a Group team.")
        targets = result.setdefault(source, {}).setdefault(field, [])
        if target in targets or (
            targets
            and field
            not in {"owners", "domains", "tags", "assets", "teams", *TEAM_FIELDS}
        ):
            raise PackageError("A package relationship is duplicate or ambiguous.")
        targets.append(target)
    return result


def load_package(directory: Path) -> Package:
    try:
        raw = _read(directory / "manifest.json")
        manifest = _json(raw)
        version, files, refs = (
            manifest.get("openmetadata_version"),
            manifest.get("files"),
            manifest.get("references"),
        )
        if (
            type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1
            or not isinstance(version, str)
            or re.fullmatch(r"2\.0\.(?:0|[1-9][0-9]*)", version) is None
            or not isinstance(files, dict)
            or not files
            or not isinstance(refs, list)
            or any(not isinstance(ref, dict) for ref in refs)
        ):
            raise PackageError("Unsupported or malformed package manifest.")
        entities: dict[Key, Document] = {}
        names: set[Key] = set()
        counts: dict[str, int] = {}
        by_role: dict[str, Counter[str]] = defaultdict(Counter)
        for kind, metadata in sorted(files.items()):
            expected_path = f"{kind}.ndjson"
            if (
                kind not in REGISTRY
                or not isinstance(metadata, dict)
                or metadata.get("path") != expected_path
                or type(metadata.get("count")) is not int
                or metadata["count"] < 1
            ):
                raise PackageError("A package file entry is invalid.")
            content = _read(directory / expected_path)
            if hashlib.sha256(content).hexdigest() != metadata.get("sha256"):
                raise PackageError(f"SHA-256 mismatch for {expected_path}.")
            lines = content.split(b"\n")
            if lines.pop() or len(lines) != metadata["count"]:
                raise PackageError(
                    f"Invalid NDJSON framing or row count for {expected_path}."
                )
            counts[kind] = len(lines)
            for line in lines:
                row = _json(line)
                _validate_entity(row, kind)
                key, named = (kind, row["id"]), (kind, row["fqn"])
                if key in entities or named in names:
                    raise PackageError(
                        "The package contains duplicate source IDs or FQNs."
                    )
                entities[key] = row
                names.add(named)
                by_role[row["role"]][kind] += 1
        if counts != manifest.get("counts") or {
            role: dict(by_role[role]) for role in ROLES
        } != manifest.get("counts_by_role"):
            raise PackageError(
                "The manifest entity counts do not match the package files."
            )
        if not by_role["selected_domain"]:
            raise PackageError("The package has no selected domains.")
        links = _links(manifest.get("relationships"), entities)
        team_access = manifest.get("team_access", False)
        if type(team_access) is not bool:
            raise PackageError("The package team access marker must be a boolean.")
        if not team_access and (
            any(key[0] in {"role", "policy"} for key in entities)
            or any(
                set(edges).intersection((*TEAM_FIELDS, "teams"))
                for edges in links.values()
            )
        ):
            raise PackageError("The package does not declare its team access data.")
        if team_access and any(
            isinstance(ref.get("entity"), dict)
            and ref["entity"].get("kind") in {"team", "role"}
            and ref.get("relationship") in TEAM_FIELDS
            for ref in refs
        ):
            raise PackageError(
                "The package contains unresolved team hierarchy or permission references."
            )
        selection = manifest.get("selection")
        roots = selection.get("domains") if isinstance(selection, dict) else None
        if (
            not isinstance(roots, list)
            or not roots
            or not all(_string(root) for root in roots)
        ):
            raise PackageError("The package is missing its configured domain FQNs.")
        selected = {
            key for key, row in entities.items() if row["role"] == "selected_domain"
        }
        root_keys = {key for key in selected if entities[key]["fqn"] in roots}
        if {entities[key]["fqn"] for key in root_keys} != set(roots):
            raise PackageError("A configured domain is missing from the package scope.")
        children: dict[Key, list[Key]] = defaultdict(list)
        for key in selected - root_keys:
            for parent in links.get(key, {}).get("parent", []):
                children[parent].append(key)
        reachable = set(root_keys)
        pending = list(root_keys)
        while pending:
            for child in children.get(pending.pop(), ()):
                if child not in reachable:
                    reachable.add(child)
                    pending.append(child)
        if reachable != selected or (
            selection.get("include_subdomains") is False and selected != root_keys
        ):
            raise PackageError("A selected domain is outside the configured roots.")
        for key, row in entities.items():
            edges = links.get(key, {})
            if row["role"] == "primary" and (
                not edges.get("domains")
                or any(domain not in selected for domain in edges["domains"])
            ):
                raise PackageError(
                    "A primary asset is missing a selected domain assignment."
                )
        reachable = set(selected) | {
            key for key, row in entities.items() if row["role"] == "primary"
        }
        pending = list(reachable)
        while pending:
            for targets in links.get(pending.pop(), {}).values():
                for target in targets:
                    if target not in reachable:
                        reachable.add(target)
                        pending.append(target)
        if reachable != entities.keys():
            raise PackageError("The package contains an unreferenced dependency.")
        return Package(
            hashlib.sha256(raw).hexdigest(),
            version,
            entities,
            links,
            len(refs),
            team_access,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise PackageError(
            "Unable to read the export package or decode its JSON."
        ) from None
