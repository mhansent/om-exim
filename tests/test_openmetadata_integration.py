"""Opt-in end-to-end migration on two disposable OpenMetadata servers."""

import json
import os
from time import monotonic, sleep
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import requests
import yaml

from om_exim.cli import main
from om_exim.client import preflight
from om_exim.settings import Connection
from om_exim.target import TargetClient

pytestmark = [
    pytest.mark.openmetadata_integration,
    pytest.mark.skipif(
        os.environ.get("OM_EXIM_RUN_INTEGRATION") != "1",
        reason="Requires an explicitly enabled pair of disposable OpenMetadata servers.",
    ),
]


def _endpoint(side):
    base = os.environ.get(f"OM_EXIM_TEST_{side}_URL", "")
    token = os.environ.get(f"OM_EXIM_TEST_{side}_TOKEN", "")
    url = urlsplit(base)
    if (
        url.scheme not in {"http", "https"}
        or url.hostname not in {"127.0.0.1", "::1"}
        or url.port is None
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or url.path.rstrip("/") not in {"/api", "/api/v1"}
        or not token
    ):
        pytest.fail(
            f"Set the {side} test URL to a disposable loopback API with an explicit port and supply its token."
        )
    return Connection(base.rstrip("/"), token), url.port


def _await_member(fetch, entity_id):
    deadline = monotonic() + 45
    while monotonic() < deadline:
        if any(row.get("id") == entity_id for row in fetch()):
            return
        sleep(1)
    pytest.fail(
        "The disposable source search index did not show the seeded member in time."
    )


def test_selective_migration_between_disposable_servers(tmp_path, monkeypatch, capsys):
    source_connection, source_port = _endpoint("SOURCE")
    target_connection, target_port = _endpoint("TARGET")
    assert source_port != target_port, "Use two different disposable server ports."
    name = f"om_exim_{uuid4().hex[:12]}"
    child = f"{name}.child"
    included = f"{name}.catalog.schema.selected"
    excluded = f"{name}.catalog.schema.outside"

    with requests.Session() as source_session, requests.Session() as target_session:
        source = TargetClient(source_connection, "Disposable source", source_session)
        target = TargetClient(target_connection, "Disposable target", target_session)
        version = preflight(source, target)
        for data in (
            {"name": name},
            {"name": "child", "parent": name},
            {"name": f"{name}_outside"},
        ):
            source.create(
                "domain",
                {
                    "domainType": "Aggregate",
                    "description": "Disposable migration test.",
                    **data,
                },
            )
        source.create("databaseService", {"name": name, "serviceType": "Postgres"})
        source.create("database", {"name": "catalog", "service": name})
        source.create(
            "databaseSchema", {"name": "schema", "database": f"{name}.catalog"}
        )
        policy = source.create(
            "policy",
            {
                "name": name,
                "rules": [
                    {
                        "name": "read",
                        "effect": "allow",
                        "operations": ["ViewBasic"],
                        "resources": ["table"],
                    }
                ],
            },
        )
        role = source.create("role", {"name": name, "policies": [name]})
        division = source.create("team", {"name": name, "teamType": "BusinessUnit"})
        team = source.create(
            "team",
            {
                "name": f"{name}_group",
                "teamType": "Group",
                "parents": [division["id"]],
                "policies": [policy["id"]],
                "defaultRoles": [role["id"]],
            },
        )
        users = {
            username: source.create(
                "user",
                {
                    "name": username,
                    "email": f"{username}@example.com",
                    "teams": [team["id"]],
                    "createPasswordType": "USER_CREATE",
                },
            )
            for username in (name, f"{name}_outside")
        }
        table = source.create(
            "table",
            {
                "name": "selected",
                "databaseSchema": f"{name}.catalog.schema",
                "columns": [{"name": "id", "dataType": "BIGINT"}],
                "domains": [child],
                "owners": [{"type": "team", "id": team["id"]}],
            },
        )
        source.create(
            "table",
            {
                "name": "outside",
                "databaseSchema": f"{name}.catalog.schema",
                "columns": [{"name": "id", "dataType": "BIGINT"}],
                "domains": [f"{name}_outside"],
            },
        )
        product = source.create(
            "dataProduct",
            {
                "name": name,
                "description": "Disposable migration test.",
                "domains": [child],
                "owners": [{"type": "user", "id": users[name]["id"]}],
            },
        )
        source.add_product_assets(
            product["fullyQualifiedName"], [{"type": "table", "id": table["id"]}]
        )
        _await_member(lambda: source.product_members(product["id"], 100), table["id"])
        child_domain = source.detail("domain", child, by_name=True)
        assert child_domain is not None
        _await_member(
            lambda: source.domain_assets(child_domain["id"], 100), table["id"]
        )

        for prefix, connection in (
            ("SOURCE", source_connection),
            ("TARGET", target_connection),
        ):
            monkeypatch.setenv(f"OM_{prefix}_SERVER_URL", connection.server_url)
            monkeypatch.setenv(f"OM_{prefix}_TOKEN", connection.token)
            monkeypatch.setenv(f"OM_{prefix}_TOKEN_ENV", f"OM_{prefix}_TOKEN")
        output = tmp_path / "package"
        path = tmp_path / "config.yaml"
        config = {
            "source": {},
            "target": {},
            "migration": {
                "domains": [name],
                "output_dir": str(output),
                "include_user_emails": True,
            },
        }
        path.write_text(yaml.safe_dump(config))
        args = ["--config", str(path)]
        assert main([*args, "plan"]) == 0
        full = json.loads(capsys.readouterr().out)

        config["migration"]["discovery"] = "domain"
        path.write_text(yaml.safe_dump(config))
        assert main([*args, "plan"]) == 0
        indexed = json.loads(capsys.readouterr().out)
        for field in ("entities", "counts", "relationships", "references"):
            assert full[field] == indexed[field]
        assert full["counts"]["primary"] == {"table": 1, "dataProduct": 1}

        assert main([*args, "export"]) == 0
        capsys.readouterr()
        assert (
            json.loads((output / "manifest.json").read_text())["openmetadata_version"]
            == version
        )
        import_args = [*args, "import", "--package", str(output)]
        assert main(import_args) == 0
        assert json.loads(capsys.readouterr().out)["mode"] == "dry-run"
        assert target.detail("domain", name, by_name=True) is None

        assert main([*import_args, "--apply"]) == 0
        applied = json.loads(capsys.readouterr().out)
        assert any(
            row["source_id"] == table["id"] and row["target_id"] != table["id"]
            for row in applied["mapping"]
        )
        assert target.detail("table", included, by_name=True) is not None
        assert target.detail("table", excluded, by_name=True) is None
        assert target.detail("user", name, by_name=True) is not None
        assert target.detail("user", f"{name}_outside", by_name=True) is None
        assert main([*args, "validate", "--package", str(output)]) == 0
        assert json.loads(capsys.readouterr().out)["valid"] is True
        assert main([*import_args, "--apply"]) == 0
        assert json.loads(capsys.readouterr().out)["writes"] == {}
