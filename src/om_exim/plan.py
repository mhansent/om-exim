"""Build a plan using only the source's read interface."""

from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass

from .client import Document, ReadClient, ReadError
from .registry import PRIMARY_TYPES, REGISTRY
from .settings import Selection

Key = tuple[str, str]
STRUCTURAL_TYPES = ("databaseSchema", "database")
MIN_BULK_DEPENDENCIES = 32
Progress = Callable[[str], None]


def _track[T](
    rows: Iterator[T], label: str, progress: Progress | None, *, batch_size: int
) -> Iterator[T]:
    if progress is None:
        yield from rows
        return
    progress(f"{label}...")
    count = 0
    for count, row in enumerate(rows, start=1):
        yield row
        if count % batch_size == 0:
            progress(f"{label}: {count:,} processed.")
    progress(f"{label}: {count:,} processed in total.")


@dataclass(frozen=True)
class Entity:
    kind: str
    id: str
    fqn: str

    @property
    def key(self) -> Key:
        return self.kind, self.id


@dataclass(frozen=True)
class Reference:
    entity: Entity
    relationship: str
    target_kind: str
    target: str
    reason: str


@dataclass(frozen=True, order=True)
class Relationship:
    source_kind: str
    source_id: str
    field: str
    target_kind: str
    target_id: str


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReadError(f"The source returned an invalid {label}.")
    return value


def _read_entity(
    kind: str, payload: Document, expected_id: str | None = None
) -> Entity:
    entity_id = _text(payload.get("id"), f"{kind} ID")
    if expected_id is not None and expected_id != entity_id:
        raise ReadError(f"The {kind} detail did not match the requested ID.")
    fqn = payload.get("fullyQualifiedName")
    if fqn is None and kind in {"team", "user"}:
        fqn = payload.get("name")
    return Entity(kind, entity_id, _text(fqn, f"{kind} FQN"))


def _items(value: object, label: str) -> list[Document]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ReadError(f"The source returned an invalid {label} relationship list.")
    return value


def _reference(raw: object, field: str) -> Key:
    if not isinstance(raw, dict):
        raise ReadError(f"The source returned an invalid {field} reference.")
    return _text(raw.get("type"), f"{field} type"), _text(raw.get("id"), f"{field} ID")


def _details(
    client: ReadClient,
    kind: str,
    batch_size: int,
    *,
    export_columns: bool = False,
) -> Iterator[tuple[Entity, Document]]:
    spec = REGISTRY[kind]
    required = tuple(rule.field for rule in spec.dependencies if rule.required)
    projected = kind == "table"
    retained = (
        ("name", "tableType", "deleted", "columns")
        if export_columns and projected
        else ()
    )
    for row in client.list_entities(kind, batch_size, project_fields=projected):
        entity_id = row["id"]
        if projected and spec.domain_field in row and _has_projection(row, required):
            yield _read_entity(kind, row), _planning_fields(kind, row, retained)
            continue
        payload = client.detail(kind, entity_id)
        if payload is None:
            raise ReadError(
                f"A listed {kind} disappeared before its detail could be read."
            )
        yield (
            _read_entity(kind, payload, entity_id),
            (_planning_fields(kind, payload, retained) if projected else payload),
        )


def _has_projection(row: Document, required: tuple[str, ...]) -> bool:
    return bool(row.get("fullyQualifiedName")) and all(
        row.get(field) is not None for field in required
    )


def _planning_fields(kind: str, row: Document, extra: tuple[str, ...] = ()) -> Document:
    spec = REGISTRY[kind]
    fields = (*spec.fields, *(rule.field for rule in spec.dependencies), *extra)
    return {field: row[field] for field in fields if field in row}


def _domains(
    client: ReadClient, selection: Selection, progress: Progress | None
) -> dict[Key, tuple[Entity, Document]]:
    domains = {
        entity.key: (entity, payload)
        for entity, payload in _track(
            _details(client, "domain", selection.batch_size),
            "Scanning domains",
            progress,
            batch_size=selection.batch_size,
        )
    }
    by_name = {entity.fqn: key for key, (entity, _) in domains.items()}
    if len(by_name) != len(domains):
        raise ReadError("The domain listing contains duplicate FQNs.")
    roots = [by_name.get(fqn) for fqn in selection.domains]
    if None in roots:
        raise ReadError("A configured domain FQN was not found in the source.")

    children: dict[Key, list[Key]] = defaultdict(list)
    parents: dict[Key, Key] = {}
    for key, (_, payload) in domains.items():
        if payload.get("parent") is not None:
            parent = _reference(payload["parent"], "domain parent")
            if parent[0] != "domain":
                raise ReadError("A domain has an unsupported parent relationship.")
            children[parent].append(key)
            parents[key] = parent

    for key in domains:
        visited: set[Key] = set()
        while key in parents:
            if key in visited:
                raise ReadError("The domain hierarchy contains a cycle.")
            visited.add(key)
            key = parents[key]

    selected: dict[Key, tuple[Entity, Document]] = {}
    pending = deque(root for root in roots if root is not None)
    while pending:
        key = pending.popleft()
        if key in selected:
            continue
        selected[key] = domains[key]
        if selection.include_subdomains:
            pending.extend(children[key])
    return selected


def _assignments(payload: Document, field: str) -> set[Key]:
    assignments: set[Key] = set()
    for raw in _items(payload.get(field), field):
        key = _reference(raw, field)
        inherited = raw.get("inherited")
        if key[0] != "domain" or (
            inherited is not None and type(inherited) is not bool
        ):
            raise ReadError("The source returned an unsupported domain assignment.")
        if inherited is not True:
            assignments.add(key)
    return assignments


def _dependencies(
    entity: Entity,
    payload: Document,
) -> Iterator[tuple[str, str, str, bool, bool]]:
    for rule in REGISTRY[entity.kind].dependencies:
        raw = payload.get(rule.field)
        if rule.required and raw is None:
            raise ReadError(
                f"The source omitted the required {entity.kind}.{rule.field} reference."
            )
        values = raw if isinstance(raw, list) else ([] if raw is None else [raw])
        if rule.required and len(values) != 1:
            raise ReadError(
                f"The source returned an invalid {entity.kind}.{rule.field} reference."
            )
        for item in values:
            kind, value = _reference(item, rule.field)
            if rule.field == "owners" and item.get("inherited") is True:
                continue
            if rule.required and kind not in rule.kinds:
                raise ReadError(
                    f"The source returned an unsupported {entity.kind}.{rule.field} type."
                )
            yield rule.field, kind, value, False, kind in rule.kinds
    if "tags" in REGISTRY[entity.kind].fields:
        for label in _items(payload.get("tags"), "tags"):
            fqn = _text(label.get("tagFQN"), "tag FQN")
            source = label.get("source")
            if source == "Classification":
                yield "tags", "tag", fqn, True, True
            elif source == "Glossary":
                yield "tags", "glossaryTerm", fqn, True, True
            else:
                yield "tags", "unknown", fqn, True, False


def _structural_details(
    client: ReadClient,
    primaries: dict[Key, tuple[Entity, Document]],
    limit: int,
    progress: Progress | None,
) -> dict[Key, Document]:
    wanted: dict[str, set[str]] = {kind: set() for kind in STRUCTURAL_TYPES}
    for entity, payload in primaries.values():
        if entity.kind != "table":
            continue
        for kind in STRUCTURAL_TYPES:
            ref_kind, entity_id = _reference(payload.get(kind), f"table.{kind}")
            if ref_kind != kind:
                raise ReadError(
                    f"The source returned an unsupported table.{kind} type."
                )
            wanted[kind].add(entity_id)

    found = {}
    for kind, remaining in wanted.items():
        if len(remaining) < MIN_BULK_DEPENDENCIES:
            continue
        required = tuple(
            rule.field for rule in REGISTRY[kind].dependencies if rule.required
        )
        # Leave room for targeted fallback when the wanted IDs are sparse in the collection.
        rows = client.list_entities(
            kind, limit, optional=True, max_pages=len(remaining) // 2
        )
        for row in _track(
            rows, f"Reading {REGISTRY[kind].route} in bulk", progress, batch_size=limit
        ):
            entity_id = row["id"]
            if entity_id in remaining and _has_projection(row, required):
                found[kind, entity_id] = _planning_fields(
                    kind, row, ("name", "serviceType", "deleted")
                ) | {
                    "id": entity_id,
                    "fullyQualifiedName": row["fullyQualifiedName"],
                }
                remaining.remove(entity_id)
                if not remaining:
                    break
    return found


def build_plan(
    client: ReadClient,
    selection: Selection,
    version: str,
    *,
    progress: Progress | None = None,
    capture: Callable[[Document, Document], None] | None = None,
) -> Document:
    domains = _domains(client, selection, progress)
    if progress:
        progress(f"Selected {len(domains):,} domains.")
    primaries: dict[Key, tuple[Entity, Document]] = {}
    references: set[Reference] = set()
    relationships: set[Relationship] = set()
    for kind in PRIMARY_TYPES:
        field = REGISTRY[kind].domain_field
        assert field is not None
        batch_size = selection.batch_size
        if kind == "table" and selection.table_batch_size is not None:
            batch_size = selection.table_batch_size
        rows = _details(client, kind, batch_size, export_columns=capture is not None)
        for entity, payload in _track(
            rows,
            f"Scanning {REGISTRY[kind].route}",
            progress,
            batch_size=batch_size,
        ):
            assigned = _assignments(payload, field)
            selected = assigned.intersection(domains)
            if selected:
                if (
                    capture is not None
                    and kind == "table"
                    and payload.get("columns") is None
                ):
                    # Requesting both tags and columns also loads unused column tags.
                    detail = client.detail(kind, entity.id, fields=(field, "columns"))
                    if (
                        detail is None
                        or detail.get("deleted") is True
                        or _read_entity(kind, detail, entity.id) != entity
                        or _assignments(detail, field) != assigned
                    ):
                        raise ReadError(
                            "A selected table changed before its columns could be read."
                        )
                    payload["columns"] = detail.get("columns")
                primaries[entity.key] = entity, payload
                for domain_kind, domain_id in selected:
                    relationships.add(
                        Relationship(
                            entity.kind, entity.id, field, domain_kind, domain_id
                        )
                    )
                for other_kind, other_id in assigned.difference(domains):
                    references.add(
                        Reference(
                            entity, field, other_kind, other_id, "external_reference"
                        )
                    )

    if progress:
        progress(
            f"Selected {len(primaries):,} primary entities. Checking data product memberships."
        )
    for entity, _ in primaries.values():
        if entity.kind != "dataProduct":
            continue
        members = client.product_members(entity.id, selection.batch_size)
        for member in _track(
            members,
            "Reading data product members",
            progress,
            batch_size=selection.batch_size,
        ):
            kind, entity_id = _reference(member, "assets")
            if (kind, entity_id) in primaries:
                relationships.add(
                    Relationship(entity.kind, entity.id, "assets", kind, entity_id)
                )
            else:
                references.add(
                    Reference(entity, "assets", kind, entity_id, "external_reference")
                )

    nodes = {**domains, **primaries}
    roles = dict.fromkeys(domains, "selected_domain") | dict.fromkeys(
        primaries, "primary"
    )
    pending = deque(nodes.values())
    resolved: dict[tuple[str, str, bool], Entity | None] = {}
    structural = (
        _structural_details(client, primaries, selection.batch_size, progress)
        if selection.include_dependencies
        else {}
    )

    def queued_entities() -> Iterator[tuple[Entity, Document]]:
        while pending:
            yield pending.popleft()

    for entity, payload in _track(
        queued_entities(),
        "Resolving dependencies and references",
        progress,
        batch_size=selection.batch_size,
    ):
        for field, kind, value, by_name, supported in _dependencies(entity, payload):
            if not supported:
                references.add(
                    Reference(entity, field, kind, value, "unsupported_reference")
                )
                continue
            if not by_name and (kind, value) in nodes:
                relationships.add(
                    Relationship(entity.kind, entity.id, field, kind, value)
                )
                continue
            if not selection.include_dependencies:
                references.add(
                    Reference(entity, field, kind, value, "excluded_dependency")
                )
                continue
            lookup = kind, value, by_name
            if lookup not in resolved:
                details = structural.get((kind, value)) if not by_name else None
                if details is None:
                    details = client.detail(kind, value, by_name=by_name)
                dependency = (
                    _read_entity(kind, details, None if by_name else value)
                    if details is not None
                    else None
                )
                if dependency is not None and details is not None:
                    if by_name and dependency.fqn != value:
                        raise ReadError(
                            "A dependency detail did not match the requested FQN."
                        )
                    if dependency.key not in nodes:
                        nodes[dependency.key] = dependency, details
                        roles[dependency.key] = "required_dependency"
                        pending.append((dependency, details))
                resolved[lookup] = dependency
            dependency = resolved[lookup]
            if dependency is None:
                references.add(
                    Reference(entity, field, kind, value, "missing_reference")
                )
            else:
                relationships.add(
                    Relationship(entity.kind, entity.id, field, kind, dependency.id)
                )
    if progress:
        progress(f"Building the JSON report for {len(nodes):,} entities.")
    entries = []
    for key, (entity, payload) in sorted(
        nodes.items(),
        key=lambda entry: (entry[1][0].kind, entry[1][0].fqn, entry[1][0].id),
    ):
        entry = {**asdict(entity), "role": roles[key]}
        if capture is not None:
            capture(entry, payload)
        entries.append(entry)
    return {
        "schema_version": 1,
        "openmetadata_version": version,
        "selection": asdict(selection),
        "coverage": list(PRIMARY_TYPES),
        "counts": {
            role: dict(
                sorted(
                    Counter(
                        entry["kind"] for entry in entries if entry["role"] == role
                    ).items()
                )
            )
            for role in ("selected_domain", "primary", "required_dependency")
        },
        "entities": entries,
        "relationships": [
            asdict(relationship) for relationship in sorted(relationships)
        ],
        "references": [
            asdict(ref)
            for ref in sorted(
                references,
                key=lambda ref: (
                    ref.entity.kind,
                    ref.entity.fqn,
                    ref.entity.id,
                    ref.relationship,
                    ref.target_kind,
                    ref.target,
                    ref.reason,
                ),
            )
        ],
    }
