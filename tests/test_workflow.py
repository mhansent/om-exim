import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from om_exim.cli import main
from om_exim.settings import load_settings


def ref(kind, entity_id, **fields):
    return {"type": kind, "id": entity_id, **fields}


def entity(entity_id, fqn, **fields):
    return {"id": entity_id, "fullyQualifiedName": fqn, **fields}


@pytest.fixture
def api():
    parent_refs = {
        "databaseSchema": ref("databaseSchema", "schema"),
        "database": ref("database", "db"),
        "service": ref("databaseService", "service"),
    }
    routes = {
        "domains": {
            "a": entity("a", "A"),
            "child": entity("child", "A.child", parent=ref("domain", "a")),
            "b": entity("b", "B"),
            "b-child": entity("b-child", "B.child", parent=ref("domain", "b")),
        },
        "tables": {
            "selected": entity(
                "selected",
                "service.db.schema.selected",
                **parent_refs,
                domains=[ref("domain", "child"), ref("domain", "b")],
                owners=[
                    ref("team", "team"),
                    ref("policy", "wrong-type"),
                    ref("user", "missing"),
                ],
                tags=[
                    {"source": "Classification", "tagFQN": "Sensitivity.Public"},
                    {"source": "Glossary", "tagFQN": "Catalog.Standard"},
                    {"source": "Unknown", "tagFQN": "Unknown.Item"},
                ],
            ),
            "unrelated": entity(
                "unrelated",
                "service.db.schema.other",
                **parent_refs,
                domains=[ref("domain", "b")],
            ),
            "inherited": entity(
                "inherited",
                "service.db.schema.inherited",
                **parent_refs,
                domains=[ref("domain", "a", inherited=True)],
            ),
        },
        "dataProducts": {
            "product": entity(
                "product", "A.product", domains=[ref("domain", "a", inherited=False)]
            ),
            "other-product": entity(
                "other-product", "B.product", domains=[ref("domain", "b")]
            ),
        },
        "databaseSchemas": {
            "schema": entity(
                "schema",
                "service.db.schema",
                database=parent_refs["database"],
                service=parent_refs["service"],
            )
        },
        "databases": {"db": entity("db", "service.db", service=parent_refs["service"])},
        "services/databaseServices": {
            "service": entity("service", "service", connection={"secret": "REDACT_ME"})
        },
        "teams": {"team": entity("team", "Analysts")},
        "tags": {
            "name/Sensitivity.Public": entity(
                "tag",
                "Sensitivity.Public",
                classification=ref("classification", "classification"),
            )
        },
        "classifications": {"classification": entity("classification", "Sensitivity")},
        "glossaryTerms": {
            "name/Catalog.Standard": entity(
                "term", "Catalog.Standard", glossary=ref("glossary", "glossary")
            )
        },
        "glossaries": {"glossary": entity("glossary", "Catalog")},
        "users": {},
    }
    versions = {"source": {"version": "2.0.0"}, "target": {"version": "2.0.0"}}
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            url = urlsplit(self.path)
            side, _, _, route = url.path.lstrip("/").split("/", 3)
            params = parse_qs(url.query)
            calls.append((self.command, side, route, params))
            assert self.headers.get("Authorization") == f"Bearer {side}-token"
            if route == "system/version":
                self.send(200, versions[side])
                return
            assert side == "source"
            if route == "dataProducts/product/assets":
                items = [ref("table", "selected"), ref("table", "unrelated")]
                offset, limit = int(params["offset"][0]), int(params["limit"][0])
                self.send(
                    200,
                    {
                        "data": items[offset : offset + limit],
                        "paging": {"total": len(items)},
                    },
                )
                return
            if route in routes:
                rows = list(routes[route])
                after = int(params.get("after", ["0"])[0])
                end = min(after + int(params["limit"][0]), len(rows))
                data = []
                for key in rows[after:end]:
                    row = routes[route][key]
                    if "fields" in params or route in {"databaseSchemas", "databases"}:
                        omitted = row.get("_omit_from_listing", [])
                        data.append(
                            {
                                field: value
                                for field, value in row.items()
                                if field not in omitted
                            }
                        )
                    else:
                        data.append({"id": key})
                self.send(
                    200,
                    {
                        "data": data,
                        "paging": {"after": str(end) if end < len(rows) else None},
                    },
                )
                return
            base = next((p for p in routes if route.startswith(p + "/")), None)
            payload = routes[base].get(route.removeprefix(base + "/")) if base else None
            self.send(200 if payload else 404, payload or {"message": "REDACT_ME"})

        def send(self, status, payload):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_POST(self):
            calls.append((self.command, self.path))
            self.send(405, {})

        do_PUT = do_PATCH = do_DELETE = do_POST

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", routes, versions, calls
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture
def config(tmp_path, monkeypatch, api):
    base, *_ = api
    monkeypatch.chdir(tmp_path)
    for name in ("SOURCE", "TARGET"):
        for suffix in ("SERVER_URL", "TOKEN_ENV", "TOKEN"):
            monkeypatch.delenv(f"OM_{name}_{suffix}", raising=False)
    (tmp_path / ".env").write_text(
        "OM_SOURCE_TOKEN=source-token\nOM_TARGET_TOKEN=target-token\n"
    )
    config = {
        name: {
            "server_url": f"{base}/{name}/api",
            "token_env": f"OM_{name.upper()}_TOKEN",
        }
        for name in ("source", "target")
    }
    config["migration"] = {"domains": ["A"], "batch_size": 1}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


@pytest.mark.parametrize("server_version", ["2.0.0", "2.0.8"])
def test_plan_does_not_expand_through_product_members_or_inherited_domains(
    config, api, capsys, server_version
):
    _, _, versions, calls = api
    versions["source"] = versions["target"] = {"version": server_version}
    assert main(["plan"]) == 0
    stdout = capsys.readouterr().out
    result = json.loads(stdout)
    assert result["openmetadata_version"] == server_version
    assert "REDACT_ME" not in stdout
    assert "-token" not in stdout
    by_role = {
        role: {e["id"] for e in result["entities"] if e["role"] == role}
        for role in ("selected_domain", "primary", "required_dependency")
    }
    assert by_role == {
        "selected_domain": {"a", "child"},
        "primary": {"selected", "product"},
        "required_dependency": {
            "schema",
            "db",
            "service",
            "team",
            "tag",
            "classification",
            "term",
            "glossary",
        },
    }
    assert result["counts"]["primary"] == {"dataProduct": 1, "table": 1}
    assert {
        (edge["source_id"], edge["field"], edge["target_id"])
        for edge in result["relationships"]
        if edge["source_id"] == "selected"
    } == {
        ("selected", "domains", "child"),
        ("selected", "databaseSchema", "schema"),
        ("selected", "database", "db"),
        ("selected", "service", "service"),
        ("selected", "owners", "team"),
        ("selected", "tags", "tag"),
        ("selected", "tags", "term"),
    }
    assert {
        tuple(r[k] for k in ("relationship", "target", "reason"))
        for r in result["references"]
    } == {
        ("assets", "unrelated", "external_reference"),
        ("domains", "b", "external_reference"),
        ("owners", "wrong-type", "unsupported_reference"),
        ("owners", "missing", "missing_reference"),
        ("tags", "Unknown.Item", "unsupported_reference"),
    }
    assert (
        "GET",
        "source",
        "tables",
        {"limit": ["1"], "after": ["1"], "fields": ["domains,owners,tags"]},
    ) in calls
    assert not any(path.startswith("tables/") for _, _, path, _ in calls)
    assert (
        "GET",
        "source",
        "dataProducts/product/assets",
        {"limit": ["1"], "offset": ["1"]},
    ) in calls
    assert all(
        method == "GET" and (side == "source" or path == "system/version")
        for method, side, path, _ in calls
    )


def test_plan_falls_back_when_table_list_omits_projected_fields(config, api, capsys):
    api[1]["tables"]["selected"]["_omit_from_listing"] = ["domains", "databaseSchema"]
    assert main(["plan"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["primary"]["table"] == 1
    assert (
        "GET",
        "source",
        "tables/selected",
        {"fields": ["domains,owners,tags"]},
    ) in api[3]


def test_plan_batches_large_table_and_dependency_collections(config, api, capsys):
    _, routes, _, calls = api
    config.write_text(config.read_text().replace("batch_size: 1", "batch_size: 100"))
    service = ref("databaseService", "service")
    routes["databases"] = {
        f"db-{i}": entity(f"db-{i}", f"service.db{i}", service=service)
        for i in range(344)
    }
    schemas = {
        f"schema-{i}": entity(
            f"schema-{i}",
            f"service.db{i % 344}.schema{i}",
            database=ref("database", f"db-{i % 344}"),
            service=service,
        )
        for i in range(2040)
    }
    routes["databaseSchemas"] = schemas
    schemas["schema-0"]["_omit_from_listing"] = ["database"]
    routes["tables"] = {
        f"table-{i}": entity(
            f"table-{i}",
            f"{schemas[f'schema-{i % 2040}']['fullyQualifiedName']}.table{i}",
            domains=[ref("domain", "b" if i < 500 else "child")],
            databaseSchema=ref("databaseSchema", f"schema-{i % 2040}"),
            database=schemas[f"schema-{i % 2040}"]["database"],
            service=service,
        )
        for i in range(10_000)
    }
    assert main(["plan"]) == 0
    counts = json.loads(capsys.readouterr().out)["counts"]
    assert counts["primary"]["table"] == 9500
    assert counts["required_dependency"]["databaseSchema"] == 2040
    assert counts["required_dependency"]["database"] == 344
    paths = [path for _, _, path, _ in calls]
    assert paths.count("tables") == 100
    assert paths.count("databaseSchemas") == 21
    assert paths.count("databases") == 4
    assert [path for path in paths if path.startswith("databaseSchemas/")] == [
        "databaseSchemas/schema-0"
    ]
    assert not any(path.startswith(("tables/", "databases/")) for path in paths)
    assert len(calls) < 150


@pytest.mark.parametrize(
    "version, message",
    [
        ({"version": "2.0.8"}, "same exact server version"),
        ({}, "version could not be verified"),
    ],
)
def test_preflight_fails_closed(config, api, capsys, version, message):
    _, _, versions, calls = api
    versions["target"] = version
    assert main(["--config", str(config), "plan"]) == 1
    result = capsys.readouterr()
    assert not result.out
    assert message in result.err
    assert all(call[2] == "system/version" for call in calls)


@pytest.mark.parametrize("server_version", ["2.1.0", "2.0", "2.0.8-rc1"])
def test_matching_version_outside_stable_20_is_rejected(
    config, api, capsys, server_version
):
    _, _, versions, calls = api
    versions["source"] = versions["target"] = {"version": server_version}
    assert main(["preflight", "--config", str(config)]) == 1
    result = capsys.readouterr()
    assert not result.out
    assert "stable OpenMetadata 2.0.x" in result.err
    assert all(call[2] == "system/version" for call in calls)


def test_missing_domain_fails_before_asset_reads(config, api, capsys):
    config.write_text(config.read_text().replace("- A", "- a"))
    assert main(["plan"]) == 1
    assert "FQN was not found" in capsys.readouterr().err
    assert not any(call[2] in {"tables", "dataProducts"} for call in api[3])


def test_unsupported_domain_reference_fails_closed(config, api, capsys):
    api[1]["tables"]["selected"]["domains"] = [{"type": "table", "id": "child"}]
    assert main(["plan"]) == 1
    assert "unsupported domain assignment" in capsys.readouterr().err


def test_install_and_config_defaults_are_independent(config, api, monkeypatch, capsys):
    folder = config.parent / "alternate"
    folder.mkdir()
    custom = folder / "project.yaml"
    custom.write_text(config.read_text())
    (folder / ".env").write_text(
        f"OM_SOURCE_SERVER_URL={api[0]}/source/api/v1\n"
        "OM_TARGET_SERVER_URL=http://unused.example/api\n"
        "OM_SOURCE_TOKEN_ENV=CUSTOM_SOURCE\nOM_TARGET_TOKEN_ENV=CUSTOM_TARGET\n"
        "CUSTOM_SOURCE=source-token\nCUSTOM_TARGET=unused\nOM_MIGRATION_DOMAINS=B\n"
    )
    monkeypatch.setenv("OM_TARGET_SERVER_URL", f"{api[0]}/target/api")
    monkeypatch.setenv("CUSTOM_TARGET", "target-token")
    result = load_settings(custom, require_selection=True)
    assert result.selection.domains == ("A",)
    assert "token" not in repr(result.source)
    versions = api[2]
    versions["source"] = versions["target"] = {"version": "2.0.8"}
    assert main(["preflight", "--config", str(custom)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "openmetadata_version": "2.0.8",
        "sdk_schema_baseline": "2.0.0.0",
    }


def test_invalid_token_fails_before_any_network_request(
    config, api, monkeypatch, capsys
):
    monkeypatch.setenv("OM_SOURCE_TOKEN", "")
    assert main(["preflight"]) == 1
    assert "token for source" in capsys.readouterr().err
    assert not api[3]


@pytest.mark.parametrize("mode", [[], ["--dry-run"], ["--apply"]])
def test_deferred_import_cannot_write(api, capsys, mode):
    assert main(["import", "--package", "package", *mode]) == 2
    assert "reserved for a later phase" in capsys.readouterr().err
    assert not api[3]
