"""Plan and apply scoped target writes from a validated export package."""

import hashlib
import json
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from tempfile import NamedTemporaryFile

from .client import Document, ReadClient
from .package import Key, Package
from .registry import REGISTRY
from .target import TargetClient

PREEXISTING = {"databaseService", "team", "user"}
DESCRIBED = {
    "domain",
    "dataProduct",
    "classification",
    "tag",
    "glossary",
    "glossaryTerm",
}
DESCRIPTION = (
    "Imported by om-exim. The source description was omitted from the export package."
)
CREATE_PARENT = {
    "database": "service",
    "databaseSchema": "database",
    "table": "databaseSchema",
    "tag": "classification",
    "glossaryTerm": "glossary",
}
OPTIONAL_PARENT = {"domain", "tag", "glossaryTerm"}
TYPES = {"domain": "domainType", "dataProduct": "dataProductType", "table": "tableType"}
ANCESTOR_FIELDS = {
    "databaseSchema": ("service",),
    "table": ("database", "service"),
    "tag": ("classification",),
    "glossaryTerm": ("glossary",),
}


class MigrationImportError(ValueError):
    pass


@dataclass(frozen=True)
class Operation:
    key: Key
    action: str
    current: Document | None
    create: Document | None


def _quoted(name: str) -> str:
    if (
        name.startswith('"')
        and name.endswith('"')
        and '"' not in name[1:-1].replace('""', "")
    ):
        unquoted = name[1:-1].replace('""', '"')
        return name if any(char in unquoted for char in '."') else unquoted
    return (
        '"' + name.replace('"', '""') + '"'
        if any(char in name for char in '."')
        else name
    )


def _structure(package: Package, key: Key) -> tuple[Document, set[Key]]:
    kind = key[0]
    row = package.entities[key]
    edges = package.links.get(key, {})
    parents: set[Key] = set()
    body: Document = {}
    for rule in REGISTRY[kind].dependencies:
        if rule.required and rule.field not in edges:
            raise MigrationImportError(
                f"The package is missing a required {kind}.{rule.field} dependency."
            )
        if rule.field not in {"owners"}:
            parents.update(edges.get(rule.field, ()))
    if kind in PREEXISTING:
        return body, parents

    data = row["data"]
    name = data.get("name")
    if not name:
        raise MigrationImportError(
            f"The package is missing a name required to create a {kind}."
        )
    body["name"] = name
    if kind in DESCRIBED:
        body["description"] = DESCRIPTION
    field = TYPES.get(kind)
    if field is not None and field in data:
        body[field] = data[field]
    if kind == "domain" and body.get("domainType") not in {
        "Source-aligned",
        "Consumer-aligned",
        "Aggregate",
    }:
        raise MigrationImportError("The package is missing a valid domainType.")
    parent_field = CREATE_PARENT.get(kind)
    parent_fqn = None
    if parent_field:
        parent_fqn = package.entities[edges[parent_field][0]]["fqn"]
        body[parent_field] = parent_fqn
    if kind in OPTIONAL_PARENT and edges.get("parent"):
        parent_fqn = package.entities[edges["parent"][0]]["fqn"]
        body["parent"] = parent_fqn
    ancestor = (
        edges.get("parent")
        if kind in {"tag", "glossaryTerm"}
        else edges.get(parent_field)
    )
    if ancestor:
        ancestor_links = package.links.get(ancestor[0], {})
        for field in ANCESTOR_FIELDS.get(kind, ()):
            if ancestor_links.get(field) != edges.get(field):
                raise MigrationImportError(
                    f"The package has inconsistent {kind}.{field} ancestry."
                )
    if kind == "table":
        body["columns"] = data["columns"]
    if kind in {"table", "dataProduct"}:
        domains = edges["domains"]
        body["domains"] = [package.entities[target]["fqn"] for target in domains]
        parents.update(domains)

    expected = ".".join(part for part in (parent_fqn, _quoted(name)) if part)
    if expected != row["fqn"]:
        raise MigrationImportError(
            f"The package has a {kind} FQN that does not match its structural fields."
        )
    return body, parents


def _order(package: Package) -> tuple[list[Key], dict[Key, Document]]:
    payloads: dict[Key, Document] = {}
    graph: dict[Key, list[Key]] = {}
    for key in sorted(package.entities):
        body, required = _structure(package, key)
        graph[key] = sorted(required)
        payloads[key] = body
    try:
        return list(TopologicalSorter(graph).static_order()), payloads
    except CycleError:
        raise MigrationImportError(
            "The package contains a cycle in structural dependencies."
        ) from None


def _identity(key: Key, fqn: str, payload: Document) -> str:
    actual = payload.get("fullyQualifiedName")
    if actual is None and key[0] in {"team", "user"}:
        actual = payload.get("name")
    entity_id = payload.get("id")
    if (
        actual != fqn
        or not isinstance(entity_id, str)
        or not entity_id.strip()
        or payload.get("deleted") is True
    ):
        raise MigrationImportError(f"Target returned an unexpected {key[0]} identity.")
    return entity_id


def _fields(kind: str) -> tuple[str, ...]:
    fields = REGISTRY[kind].fields
    if kind in OPTIONAL_PARENT:
        fields = tuple(dict.fromkeys((*fields, "parent")))
    if kind == "table":
        fields = (*fields, "dataProducts")
    return fields


def _lookup(target: ReadClient, package: Package, key: Key) -> Document | None:
    fqn = package.entities[key]["fqn"]
    payload = target.detail(key[0], fqn, by_name=True, fields=_fields(key[0]))
    if payload is not None:
        _identity(key, fqn, payload)
    return payload


def _prepare(
    target: ReadClient, package: Package, policy: str, saved: dict[Key, str]
) -> list[Operation]:
    order, payloads = _order(package)
    operations = []
    conflicts: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    for key in order:
        current = _lookup(target, package, key)
        if key in saved:
            if current is None or current["id"] != saved[key]:
                raise MigrationImportError(
                    "A checkpointed entity is missing or has changed identity on the target."
                )
            action = "resume"
        elif key[0] in PREEXISTING:
            action = "reuse"
            if current is None:
                missing[key[0]] += 1
        elif current is None:
            action = "create"
        elif policy == "fail":
            conflicts[key[0]] += 1
            action = "conflict"
        else:
            action = policy
        operations.append(
            Operation(
                key, action, current, payloads[key] if action == "create" else None
            )
        )
    if missing or conflicts:
        problems = []
        if missing:
            problems.append(
                "Missing target prerequisites: "
                + ", ".join(
                    f"{kind} ({count})" for kind, count in sorted(missing.items())
                )
                + "."
            )
        if conflicts:
            problems.append(
                "Existing target entities: "
                + ", ".join(
                    f"{kind} ({count})" for kind, count in sorted(conflicts.items())
                )
                + ". Select skip or update to continue."
            )
        raise MigrationImportError(" ".join(problems))
    ids = {
        operation.key: operation.current["id"]
        for operation in operations
        if operation.current is not None
    }
    for operation in operations:
        current = operation.current
        if current is None:
            continue
        row = package.entities[operation.key]
        for field in ("serviceType", "teamType"):
            if field in row["data"] and current.get(field) != row["data"][field]:
                raise MigrationImportError(
                    f"A target {operation.key[0]} has incompatible {field}."
                )
        for field, targets in package.links.get(operation.key, {}).items():
            if field in {"domains", "owners", "tags", "assets"}:
                continue
            expected = ids.get(targets[0])
            ref = current.get(field)
            if (
                expected is None
                or not isinstance(ref, dict)
                or ref.get("id") != expected
            ):
                raise MigrationImportError(
                    f"A target {operation.key[0]} has a mismatched {field} relationship."
                )
    return operations


def _add(changes: list[Document], path: str, value: object) -> None:
    changes.append({"op": "add", "path": path, "value": value})


def _column_changes(
    wanted: list[Document], current: list[Document], path: str, changes: list[Document]
) -> None:
    by_name = {column["name"]: (i, column) for i, column in enumerate(current)}
    for column in wanted:
        match = by_name.get(column["name"])
        if match is None:
            _add(changes, f"{path}/-", column)
            continue
        index, old = match
        for field, value in column.items():
            if field == "children" and isinstance(old.get(field), list):
                _column_changes(value, old[field], f"{path}/{index}/children", changes)
            elif old.get(field) != value:
                _add(changes, f"{path}/{index}/{field}", value)


def _patch(
    package: Package, key: Key, current: Document, mapped: dict[Key, str]
) -> list[Document]:
    row = package.entities[key]
    changes: list[Document] = []
    field = TYPES.get(key[0])
    if (
        field is not None
        and field in row["data"]
        and row["data"][field] != current.get(field)
    ):
        _add(changes, f"/{field}", row["data"][field])
    if key[0] == "table":
        wanted = row["data"]["columns"]
        current_columns = current.get("columns")
        if isinstance(current_columns, list):
            _column_changes(wanted, current_columns, "/columns", changes)
        elif wanted:
            _add(changes, "/columns", wanted)
    links = package.links.get(key, {})
    for field in ("domains", "owners"):
        if field not in links:
            continue
        desired = [
            {"type": target[0], "id": mapped[target]}
            for target in links[field]
            if target in mapped
        ]
        old = [
            ref
            for ref in (current.get(field) or [])
            if ref.get("inherited") is not True
        ]
        seen = {(ref["type"], ref["id"]) for ref in old}
        additions = [ref for ref in desired if (ref["type"], ref["id"]) not in seen]
        if additions:
            existing = [{"type": ref["type"], "id": ref["id"]} for ref in old]
            _add(changes, f"/{field}", existing + additions)
    if links.get("tags"):
        old = current.get("tags") or []
        desired = [
            {
                "tagFQN": package.entities[target]["fqn"],
                "source": "Classification" if target[0] == "tag" else "Glossary",
                "labelType": "Manual",
                "state": "Suggested",
            }
            for target in links["tags"]
        ]
        seen = {(tag["source"], tag["tagFQN"]) for tag in old}
        additions = [
            tag for tag in desired if (tag["source"], tag["tagFQN"]) not in seen
        ]
        if additions:
            if isinstance(current.get("tags"), list):
                for tag in additions:
                    _add(changes, "/tags/-", tag)
            else:
                _add(changes, "/tags", additions)
    return changes


def _checkpoint_identity(target: ReadClient, package: Package) -> Document:
    url = target.connection.server_url.removesuffix("/v1")
    return {
        "package_sha256": package.fingerprint,
        "target_sha256": hashlib.sha256(url.encode()).hexdigest(),
    }


def _read_checkpoint(
    path: Path, identity: Document, package: Package
) -> dict[Key, str]:
    if path.is_symlink():
        raise MigrationImportError("The import checkpoint must be a regular file.")
    if not path.exists():
        return {}
    if not path.is_file():
        raise MigrationImportError("The import checkpoint must be a regular file.")
    try:
        with path.open(encoding="utf-8") as stream:
            state = json.loads(stream.readline())
            if state != identity:
                raise MigrationImportError(
                    "The import checkpoint belongs to a different package or target."
                )
            saved = {}
            for line in stream:
                kind, source_id, target_id = json.loads(line)
                key = kind, source_id
                if (
                    key not in package.entities
                    or key in saved
                    or not isinstance(target_id, str)
                    or not target_id
                ):
                    raise ValueError
                saved[key] = target_id
        return saved
    except (OSError, ValueError, TypeError, KeyError) as error:
        if isinstance(error, MigrationImportError):
            raise
        raise MigrationImportError(
            "Unable to read a valid import checkpoint."
        ) from None


def _initialize_checkpoint(path: Path, identity: Document) -> None:
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(identity) + "\n")
        temporary.replace(path)
    except OSError:
        raise MigrationImportError(
            "Unable to save the import checkpoint. Review the target before retrying."
        ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def _locked(path: Path):
    lock = path.with_name(path.name + ".lock")
    try:
        with lock.open("x"):
            pass
    except OSError:
        raise MigrationImportError(
            "Unable to acquire the import checkpoint lock."
        ) from None
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _append_checkpoint(path: Path, key: Key, target_id: str) -> None:
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps([*key, target_id]) + "\n")
    except OSError:
        raise MigrationImportError(
            "Unable to save the created entity in the checkpoint. Review the target before retrying."
        ) from None


def _products(table: Document) -> set[str]:
    return {
        ref["id"]
        for ref in (table.get("dataProducts") or [])
        if ref.get("type") == "dataProduct" and ref.get("inherited") is not True
    }


def import_package(
    target: TargetClient,
    package: Package,
    checkpoint: Path,
    *,
    policy: str,
    apply: bool,
) -> Document:
    identity = _checkpoint_identity(target, package)
    if not apply:
        operations = _prepare(
            target, package, policy, _read_checkpoint(checkpoint, identity, package)
        )
        return {
            "mode": "dry-run",
            "actions": dict(
                sorted(Counter(operation.action for operation in operations).items())
            ),
            "entities": [
                {
                    **{
                        field: package.entities[op.key][field]
                        for field in ("kind", "id", "fqn", "role")
                    },
                    "action": op.action,
                    "target_id": op.current["id"] if op.current is not None else None,
                }
                for op in operations
            ],
            "references_to_review": package.reference_count,
        }

    with _locked(checkpoint):
        saved = _read_checkpoint(checkpoint, identity, package)
        operations = _prepare(target, package, policy, saved)
        if not checkpoint.exists():
            _initialize_checkpoint(checkpoint, identity)
        mapped: dict[Key, str] = {}
        written = Counter()
        for operation in operations:
            key = operation.key
            current = operation.current
            if operation.create is not None:
                current = target.create(key[0], operation.create)
                entity_id = _identity(key, package.entities[key]["fqn"], current)
                _append_checkpoint(checkpoint, key, entity_id)
                written["created"] += 1
            if current is not None:
                mapped[key] = current["id"]
        direct_products: dict[Key, set[str]] = {}
        for operation in operations:
            key = operation.key
            if key[0] == "table" and operation.action == "skip":
                direct_products[key] = _products(operation.current)
            if operation.action in {"reuse", "skip"}:
                continue
            current, etag = target.detail_for_patch(
                key[0], mapped[key], _fields(key[0])
            )
            if (
                current is None
                or _identity(key, package.entities[key]["fqn"], current) != mapped[key]
            ):
                raise MigrationImportError(
                    "A target entity changed before relationship linking."
                )
            if key[0] == "table":
                direct_products[key] = _products(current)
            changes = _patch(package, key, current, mapped)
            if changes:
                response = target.patch(key[0], mapped[key], changes, etag=etag)
                if (
                    _identity(key, package.entities[key]["fqn"], response)
                    != mapped[key]
                ):
                    raise MigrationImportError(
                        "A target update returned an unexpected identity."
                    )
                written["patched"] += 1
        for operation in operations:
            key = operation.key
            assets = package.links.get(key, {}).get("assets", ())
            if assets and operation.action != "skip":
                additions = [
                    {"type": asset[0], "id": mapped[asset]}
                    for asset in assets
                    if mapped[key] not in direct_products[asset]
                ]
                for offset in range(0, len(additions), 1000):
                    target.add_product_assets(
                        package.entities[key]["fqn"], additions[offset : offset + 1000]
                    )
                    written["memberships_added"] += len(
                        additions[offset : offset + 1000]
                    )
        return {
            "mode": "apply",
            "actions": dict(
                sorted(Counter(operation.action for operation in operations).items())
            ),
            "writes": dict(written),
            "mapping": [
                {"kind": kind, "source_id": source_id, "target_id": value}
                for (kind, source_id), value in sorted(mapped.items())
            ],
            "references_to_review": package.reference_count,
        }


def validate_target(target: ReadClient, package: Package) -> Document:
    order, _ = _order(package)
    found = {
        key: payload
        for key in order
        if (payload := _lookup(target, package, key)) is not None
    }
    mapped = {key: payload["id"] for key, payload in found.items()}
    missing = [
        {"kind": key[0], "fqn": package.entities[key]["fqn"]}
        for key in order
        if key not in found
    ]
    mismatches = []
    for key, current in found.items():
        issues = set()
        data = package.entities[key]["data"]
        for field in ("serviceType", "teamType"):
            if field in data and current.get(field) != data[field]:
                issues.add(field)
        links = package.links.get(key, {})
        for field, objectives in links.items():
            if any(other not in mapped for other in objectives):
                issues.add(field)
                continue
            if field == "assets":
                if any(
                    mapped[key] not in _products(found[other]) for other in objectives
                ):
                    issues.add(field)
            elif field not in {"domains", "owners", "tags"}:
                ref = current.get(field)
                if not isinstance(ref, dict) or ref.get("id") != mapped[objectives[0]]:
                    issues.add(field)
        issues.update(
            change["path"].split("/")[1]
            for change in _patch(package, key, current, mapped)
        )
        if issues:
            mismatches.append(
                {
                    "kind": key[0],
                    "fqn": package.entities[key]["fqn"],
                    "fields": sorted(issues),
                }
            )
    return {
        "valid": not missing and not mismatches,
        "expected_counts": dict(sorted(Counter(key[0] for key in order).items())),
        "found_counts": dict(sorted(Counter(key[0] for key in found).items())),
        "missing": missing,
        "mismatches": mismatches,
        "references_to_review": package.reference_count,
    }
