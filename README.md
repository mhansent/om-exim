# om-exim

Plan selective OpenMetadata migrations between two instances running the same exact server version. The current planner uses read-only REST calls. It selects directly assigned tables and data products from exact domain FQNs and their descendants.

## Install

Use Python 3.12.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Install `.[dev,sdk]` to inspect the optional OpenMetadata SDK models locally. See the [capability matrix](CAPABILITIES.md) for the verified baseline and coverage.

## Configure and run

Copy `config.example.yaml` to `config.yaml`, then update the API base URLs and domain FQNs. Use HTTPS for live connections and tokens scoped for read access. Copy `.env.example` to `.env` alongside the YAML and fill in `OM_SOURCE_TOKEN` and `OM_TARGET_TOKEN`, or set those variables in the process environment. Each YAML `token_env` field contains the name of the variable that holds the token. Keep local configuration, credentials, and generated plans with sensitive metadata out of version control.

The process environment takes precedence over `.env`. Either can override the YAML connection settings through `OM_SOURCE_SERVER_URL`, `OM_TARGET_SERVER_URL`, `OM_SOURCE_TOKEN_ENV`, and `OM_TARGET_TOKEN_ENV`. Use `--config PATH` to select a different YAML file. Its sibling `.env` is used if present.

```sh
.venv/bin/om-exim preflight
.venv/bin/om-exim plan
```

Both commands check the exact source and target versions first. The current baseline is server `2.0.0`. The plan is JSON on standard output. It reports selected domains, primary entities, required dependencies, their known relationships, and external, missing, unsupported, or excluded references. Membership in a selected data product does not add an asset to the plan. OpenMetadata permissions can limit the entities visible to the source token.

`export`, `import`, and `validate` are reserved for later phases and exit before issuing requests. The example's `migration.output_dir` and `import.on_conflict` document settings for those phases.

## Development checks

```sh
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

Tests use synthetic metadata and a local REST fixture.
