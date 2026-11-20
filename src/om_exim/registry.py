"""Entity-specific REST routes, projections and dependency rules for the baseline."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Relationship:
    field: str
    kinds: tuple[str, ...]
    required: bool = False


@dataclass(frozen=True)
class EntitySpec:
    route: str
    fields: tuple[str, ...] = ()
    dependencies: tuple[Relationship, ...] = ()
    domain_field: str | None = None


OWNER = Relationship("owners", ("team", "user"))
REGISTRY = {
    "domain": EntitySpec(
        "domains", ("owners", "tags"), (Relationship("parent", ("domain",)), OWNER)
    ),
    "table": EntitySpec(
        "tables",
        ("domains", "owners", "tags"),
        (
            Relationship("databaseSchema", ("databaseSchema",), required=True),
            Relationship("database", ("database",), required=True),
            Relationship("service", ("databaseService",), required=True),
            OWNER,
        ),
        domain_field="domains",
    ),
    "dataProduct": EntitySpec(
        "dataProducts", ("domains", "owners", "tags"), (OWNER,), domain_field="domains"
    ),
    "databaseSchema": EntitySpec(
        "databaseSchemas",
        dependencies=(
            Relationship("database", ("database",), required=True),
            Relationship("service", ("databaseService",), required=True),
        ),
    ),
    "database": EntitySpec(
        "databases",
        dependencies=(Relationship("service", ("databaseService",), required=True),),
    ),
    "databaseService": EntitySpec("services/databaseServices"),
    "team": EntitySpec("teams"),
    "user": EntitySpec("users"),
    "tag": EntitySpec(
        "tags",
        dependencies=(
            Relationship("classification", ("classification",), required=True),
            Relationship("parent", ("tag",)),
        ),
    ),
    "glossaryTerm": EntitySpec(
        "glossaryTerms",
        dependencies=(
            Relationship("glossary", ("glossary",), required=True),
            Relationship("parent", ("glossaryTerm",)),
        ),
    ),
    "glossary": EntitySpec("glossaries"),
    "classification": EntitySpec("classifications"),
}
PRIMARY_TYPES = tuple(kind for kind, spec in REGISTRY.items() if spec.domain_field)
