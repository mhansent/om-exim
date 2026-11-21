"""Portable OpenMetadata team permission fields."""

from .client import Document

TEAM_FIELDS = ("parents", "defaultRoles", "policies")
RULE_FIELDS = ("name", "effect", "operations", "resources", "condition")
TEAM_TYPES = ("Group", "Department", "Division", "BusinessUnit", "Organization")


def policy_rules(value: object) -> list[Document]:
    """Project rule semantics without copying descriptive or server-only fields."""
    if not isinstance(value, list):
        raise ValueError("A team policy is missing its rules.")
    rules: list[Document] = []
    names: set[str] = set()
    for rule in value:
        if not isinstance(rule, dict):
            raise ValueError("A team policy contains an invalid rule.")
        name = rule.get("name")
        if (
            not isinstance(name, str)
            or not name.strip()
            or name in names
            or rule.get("effect") not in ("allow", "deny")
            or any(
                not isinstance(rule.get(field), list)
                or not rule[field]
                or not all(
                    isinstance(item, str) and item.strip() for item in rule[field]
                )
                for field in ("operations", "resources")
            )
            or (
                rule.get("condition") is not None
                and not isinstance(rule["condition"], str)
            )
        ):
            raise ValueError("A team policy contains an invalid or duplicate rule.")
        names.add(name)
        rules.append(
            {field: rule[field] for field in RULE_FIELDS if rule.get(field) is not None}
        )
    return rules
