# om-exim

Plan selective OpenMetadata migrations between two instances running the same exact server version. The current planner uses read-only REST calls. It selects directly assigned tables and data products from exact domain FQNs and their descendants.

## Install

The REST CLI supports Python 3.12, 3.13, and 3.14. Use an installed interpreter from that range to create the virtual environment.

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Use Python 3.12 with `.[dev,sdk]` to inspect the pinned OpenMetadata SDK models locally. That SDK pins Pydantic below 2.12, and its required `pydantic-core` releases have no Python 3.14 wheels. The REST CLI runs with the dependencies installed above. See the [capability matrix](CAPABILITIES.md) for the verified baseline and coverage.

## Configure and run

Copy `config.example.yaml` to `config.yaml`, then update the API base URLs and domain FQNs. Use HTTPS for live connections and tokens scoped for read access. Copy `.env.example` to `.env` alongside the YAML and fill in `OM_SOURCE_TOKEN` and `OM_TARGET_TOKEN`, or set those variables in the process environment. Each YAML `token_env` field contains the name of the variable that holds the token. Keep local configuration, credentials, and generated plans with sensitive metadata out of version control.

The process environment takes precedence over `.env`. Either can override the YAML connection settings through `OM_SOURCE_SERVER_URL`, `OM_TARGET_SERVER_URL`, `OM_SOURCE_TOKEN_ENV`, and `OM_TARGET_TOKEN_ENV`. Use `--config PATH` to select a different YAML file. Its sibling `.env` is used if present.

```sh
.venv/bin/om-exim preflight
.venv/bin/om-exim plan
```

Both commands require the source and target servers to report the same exact stable OpenMetadata `2.0.x` version. For example, two `2.0.8` servers pass the version check. The plan is JSON on standard output. It reports selected domains, primary entities, required dependencies, their known relationships, and external, missing, unsupported, or excluded references. Membership in a selected data product does not add an asset to the plan. OpenMetadata permissions can limit the entities visible to the source token.

`export`, `import`, and `validate` are reserved for later phases and exit before issuing requests. The example's `migration.output_dir` and `import.on_conflict` document settings for those phases.

The planner includes all descendants of the configured domain FQNs by default. They appear alongside the configured roots as `selected_domain` entries in `entities`, with totals under `counts.selected_domain`. The planner also includes supported required dependencies by default. They appear as `required_dependency` entries, with totals under `counts.required_dependency`. Use `migration.include_subdomains` and `migration.include_dependencies` to control these options.

For large catalogs, `plan` reads table relationships from paginated REST responses and fetches individual details when the response omits required planning fields. For large sets of required schemas and databases, it also reads their details from paginated collections. Only referenced schemas and databases enter the plan. `migration.batch_size` controls the page size and defaults to 100. Higher values reduce requests and increase the size of each response.

Set `plan.show_progress: true` in `config.yaml` to show the current stage and running counts in the console. Running counts are reported in increments of `migration.batch_size`. The example configuration enables progress. Configurations that omit the setting default to `false`. Progress goes to stderr, so `om-exim plan > plan.json` writes only JSON to the file.

## Development checks

```sh
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

Tests use synthetic metadata and a local REST fixture.
