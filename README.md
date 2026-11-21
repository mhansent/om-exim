# om-exim

Plan and export selective OpenMetadata migrations between two instances running the same exact server version. The planner and exporter use read-only REST calls. It selects directly assigned tables and data products from exact domain FQNs and their descendants.

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
.venv/bin/om-exim export
```

These commands require the source and target servers to report the same exact stable OpenMetadata `2.0.x` version. For example, two `2.0.8` servers pass the version check. The plan is JSON on standard output. It reports selected domains, primary entities, required dependencies, their known relationships, and external, missing, unsupported, or excluded references. Membership in a selected data product does not add an asset to the plan. OpenMetadata permissions can limit the entities visible to the source token.

`import` and `validate` are reserved for later phases and exit before issuing requests.

The planner includes all descendants of the configured domain FQNs by default. They appear alongside the configured roots as `selected_domain` entries in `entities`, with totals under `counts.selected_domain`. The planner also includes supported required dependencies by default. They appear as `required_dependency` entries, with totals under `counts.required_dependency`. Use `migration.include_subdomains` and `migration.include_dependencies` to control these options.

### Discover assets by domain

For a small selection in a large catalog, set the following option under your existing `migration` settings.

```yaml
migration:
  discovery: domain
```

Both `plan` and `export` resolve the configured domains and their subdomains, then query the selected domains' asset lists for table candidates. Data product listing uses the domain filter. The planner deduplicates candidates, verifies direct assignment from source entity details, and resolves required dependencies by ID or FQN. This mode skips the global table, schema, and database scans. It still reads the domain hierarchy and pages through memberships of selected data products for reference reporting.

Domain asset discovery depends on the OpenMetadata search index. A stale or unavailable index can omit tables, including returning an empty page, so this mode reports a warning on stderr and in the plan or export manifest. Compare the resulting entity IDs against `migration.discovery: full_scan` on your server before relying on the indexed mode for a complete migration. `full_scan` is the default.

### Pagination and progress

In full scan mode, `plan` reads table relationships from paginated REST responses and fetches individual details when the response omits required planning fields. For large sets of required schemas and databases, it also reads their details from paginated collections. Only referenced schemas and databases enter the plan. `migration.batch_size` defaults to 100 and accepts 1–1,000. It controls pagination for general reads, including data product membership. Set `migration.table_batch_size` to use a separate page size for full table scans in both `plan` and `export`. Domain asset pages use `migration.batch_size`. It accepts 1–1,000,000 and inherits `migration.batch_size` when omitted. Larger table pages reduce requests and increase response size and memory use.

```yaml
migration:
  # Keep your existing domain selection and migration options here.
  batch_size: 1000
  table_batch_size: 2500
```

Set `plan.show_progress: true` in `config.yaml` to show the current stage and running counts in the console. Table scan progress follows the effective table batch size. Other running counts use `migration.batch_size`. The example configuration enables progress. Configurations that omit the setting default to `false`. Progress goes to stderr, so `om-exim plan > plan.json` writes only JSON to the file.

## Export package

Set `migration.output_dir` to a new directory. Relative paths use the current working directory. The default is `./out`. The exporter publishes the directory only after writing all files and refuses to overwrite an existing directory. On success it prints a table of exported entity counts and the number of references to review. `plan.show_progress` also controls the planning and writing progress shown during export.

Export uses the table columns already present in paginated REST responses. When a selected table is missing its columns, export fetches them separately. SHA-256 checksums are computed while writing the NDJSON files.

The package contains one UTF-8 NDJSON file per exported entity type. Each row records the source identity, role, and allowlisted structural metadata, including table columns. `manifest.json` records the package schema version, UTC creation time, OpenMetadata version, selection rules, counts, planned relationships, references to review, and SHA-256 hashes of the NDJSON files. Rows and checksums are deterministic for the same source metadata. The creation time reflects the current run.

The exporter uses the active entity scope and omits free-text descriptions, service connection settings, user contact fields, profiles, sample data, column tags, and unregistered relationships. Identifiers and ownership relationships can still contain sensitive information. Store the package privately.

## Development checks

```sh
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

Tests use synthetic metadata and a local REST fixture.
