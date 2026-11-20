# om-exim — Project Plan

## Purpose
Build a safe, auditable, selective export/import CLI for **OpenMetadata 2.x → exactly the same OpenMetadata 2.x server version**. Source and destination are separate instances. Use the official OpenMetadata Python SDK and/or documented REST APIs. Do not modify OpenMetadata's database directly.

## Agreed scope
- Select root domain(s) by exact FQN, include all descendant subdomains by the authoritative domain hierarchy (not naive prefix matching).
- Select **primary entities only when directly assigned** to any selected domain: data products and supported assets (tables, topics, dashboards, charts, pipelines, ML models, containers, stored procedures, dashboard data models, search indexes, etc., subject to verified OM 2.x schema/API support).
- Do **not** transitively include all assets of a selected data product, database, schema, lineage, or neighboring domain. A referenced asset outside scope must not silently become a primary asset.
- Include **required dependencies** for creating selected entities, such as parent service/database/schema/container, owning team/user, tags/classifications, glossary/terms, policies/roles as applicable. Dependency inclusion is **typed and allowlisted**, not a generic recursive crawl of all entity references. Report references that are external, missing, unsupported, or intentionally excluded. Preserve direct domain scope.
- Preserve relationships where supported, with explicit second-pass relationship restoration and validation when necessary.
- Export package is reviewable offline and importable later; no implicit source-to-target live copy.
- Dry-run by default for import; explicit apply flag required to mutate target.

## Source context
The previous `omd_migrate` experiment did not filter by configured domain despite README claims. The attached legacy `config.yaml` had `export.selective.domains: []`, `linked_data_products_only: true`, `linked_assets_only: true`, 19 entity categories, and `batch_size: 100`. It is reference input only, **not** the new CLI's schema. Do not reuse its tokens or server URLs.

## Non-goals for MVP
- Cross-version migrations or schema conversion.
- Database dump/restore or search-index migration.
- Migrating entity history, activity feed, audit logs, ingestion run history, secrets/credentials, or all lineage unless explicitly designed and tested later.
- Bulk import of unrelated assets via broad graph traversal.
- Automatically overwriting destination entities.

## Functional requirements
1. Source/target connections via separate environment variables; redact credentials from logs and manifests. Preflight verify source and target exact server version and SDK compatibility. Fail closed if version cannot be verified or mismatches.
2. `plan`: resolve exact FQNs, descendants, primary entities, typed dependencies, unsupported references; produce counts and reviewable manifest. A missing configured domain is fatal.
3. `export`: use pagination; honor API's include-deleted and field expansion semantics; retrieve full entity representations where list responses omit relationships. Serialize deterministic UTF-8 NDJSON, include `manifest.json` with schema version, OM version, timestamps, selection rules, per-type counts, hashes, warnings, and unresolved refs. Atomic output writes.
4. `import`: preflight package hashes and server version; build source-ID/FQN-to-target-ID mapping; use schema-valid create/update requests rather than blindly POSTing exported GET entities; topological dependency order and two-pass relationship linking. Never assume source UUID persists. Handle existing entities with explicit `skip|update|fail` policy, default `fail` for conflicts; apply only with explicit `--apply`.
5. `validate`: compare entity counts, FQNs, selected domain assignments, parent hierarchy, owners, glossary/tag relations, and known asset references. Report mismatches and unsupported fields.
6. Support resumable runs via checkpoint/state, idempotent re-runs, bounded retries with backoff, rate limiting, structured logs, and actionable errors.
7. Tests use mocked API fixtures and small synthetic domain graph; integration tests opt-in and never target production by default.

## Critical correctness rules
- Domain `parent`/hierarchy and domain assignments can vary by entity/model; inspect generated OM 2.x models and API responses before coding. Some APIs may use arrays or references not a scalar `domain` field. Do not guess.
- List endpoints can omit relationship fields. Fetch detail and verify field projection.
- Typed dependency graph: separate `primary`, `required_dependency`, `external_reference`, `unsupported_reference`; only add approved required dependencies. Preserve role of each entity in manifest.
- Avoid FQN-only cross-type collisions: index by `(entity_type, id)` and `(entity_type, fqn)`.
- Cyclic relations may require a second pass. Do not use `skip_on_error` as a default silent behavior.
- Never export secrets, passwords, connection credentials, JWTs, or PII by accident. Audit sensitive fields and make service config export opt-in and sanitized.
- Domain selection is not a security boundary; authorization on source and target still applies.

## Proposed repository layout
```
om-exim/
  AGENTS.md
  PROJECT_PLAN.md
  README.md
  pyproject.toml
  .gitignore
  config.example.yaml
  src/om_exim/
    __init__.py
    cli.py
    settings.py
    client.py
    registry.py
    models.py
    plan.py
    exporter.py
    importer.py
    dependencies.py
    mapping.py
    validator.py
    package.py
  tests/
    fixtures/
    test_domain_scope.py
    test_dependencies.py
    test_export_package.py
    test_import_dry_run.py
    test_id_mapping.py
```

## Configuration and suggested CLI contract

By default, load `config.yaml` from the current working directory. Use the optional `--config PATH` to select a different YAML file. Load the optional `.env` from the same directory as the selected YAML file.

Resolve configurable values in this order, from lowest to highest precedence: YAML, supported environment overrides, then explicitly supplied CLI overrides. For environment values, use the current process environment over `.env`. Only the connection fields below can be overridden through the environment. Define the CLI override flags after the YAML schema is complete.

| YAML field | Environment override |
| --- | --- |
| `source.server_url` | `OM_SOURCE_SERVER_URL` |
| `source.token_env` | `OM_SOURCE_TOKEN_ENV` |
| `target.server_url` | `OM_TARGET_SERVER_URL` |
| `target.token_env` | `OM_TARGET_TOKEN_ENV` |

A `token_env` value names the variable that holds the token. Resolve that name after applying the overrides, then read the token from the process environment or `.env`. The example YAML uses `OM_SOURCE_TOKEN` and `OM_TARGET_TOKEN` as the default names. Store token values in those variables or their configured replacements. Keep token values out of YAML, CLI arguments, logs, and version control. Fail before making an API request when a connection required by the command has a missing or empty URL or token.

The following examples use the default configuration path. `--package` selects an input package, and `--apply` explicitly authorizes target writes.

```
om-exim preflight
om-exim plan
om-exim export
om-exim import --package ./out/finance
om-exim import --package ./out/finance --apply
om-exim validate --package ./out/finance
```

Import runs in dry-run mode unless `--apply` is supplied. The exact server version match and the explicit apply requirement are enforced regardless of configuration.

## Implementation phases
**P0 — Verify:** inspect pinned OM version, SDK generated model fields, actual REST routes and create request models, relationship fields and pagination; write capability matrix for each of 19 requested types. Use Server 2.0.0 and SDK 2.0.0.0 as the development baseline. See the [capability matrix](CAPABILITIES.md) for coverage and outstanding live checks.

**P1 — Scaffold and plan:** CLI, config, preflight, entity registry, domain/subdomain resolver, direct-relationship selection, typed dependency plan, fixtures/tests. Produce read-only plan report. No target mutations.

**P2 — Export:** paginated collection, detailed entity fetch, NDJSON package, deterministic manifest and checksums, warnings and tests.

**P3 — Import:** dry-run, create/update mapping, dependency order, relationship pass, checkpoint and validation, safe apply mode.

**P4 — Harden:** integration tests on disposable equal-version OM instances, retry/resume, audit, documentation, GitHub Actions CI.

## Acceptance tests
- Domain A selected, A.child included, B and B.child excluded.
- A directly assigned table included; table assigned only to B excluded, even if in same database/schema.
- A data product included; its B-only asset excluded as primary.
- Parent database/schema required for selected table included as structural dependencies, but unrelated sibling tables excluded.
- Owner/tag/glossary refs resolved where supported; unsupported/missing references explicitly reported.
- Missing domain, version mismatch, invalid package hash, and failed preflight all fail closed.
- Import dry-run makes zero target writes; `--apply` writes only planned entities; re-run is idempotent.
- UUIDs differ between instances yet references resolve correctly via mapping.
- Secrets never appear in artifacts or logs.

## Decisions pending verification
- Compatibility with the deployment target, OpenMetadata Server 2.0.8, remains pending live API verification.
- Precise list of entity categories supported in the chosen SDK and how their domain relations are represented.
- Treatment of referenced assets outside selected domains: default preserve as external references/report, not migrate automatically.
- Service-entity creation: sanitize and avoid credentials; verify target pre-existence or require explicit opt-in.
- Whether lineage, tests, custom properties, and dashboards' chart references are in a future phase.

## GitHub workflow
Repository `om-exim`; implement on feature branches, tests before PR, review then merge. Never commit `.env`, tokens, source exports, or real metadata containing secrets. Use CI lint/type/test and protected `main` when available.
