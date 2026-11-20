# Codex instructions for om-exim

Read `PROJECT_PLAN.md` before making changes. The user's intent is selective OpenMetadata **same-exact-version 2.x** migration. Scope = directly domain-assigned primary entities + descendant subdomains + explicitly necessary dependencies. Avoid graph explosion.

## Engineering requirements
- Start by inspecting actual installed SDK/API models. The development baseline is OpenMetadata Server 2.0.0 with SDK 2.0.0.0. Use Python 3.12 for SDK model inspection. The REST CLI supports Python 3.12 through 3.14. Verify compatibility with the deployment target, Server 2.0.8, before enabling it. See `CAPABILITIES.md` for evidence and outstanding checks.
- Use Python with type hints, small testable modules, pytest, ruff, and no hard-coded secrets.
- For domain assignment, use entity-specific verified relationships, not a universal assumed `entity.domain` shape.
- Implement read-only plan/export before import. Do not issue writes in preflight, plan, export, validate, or import dry-run.
- Import uses API create-request schemas and maps IDs/references to target; never replay GET JSON blindly.
- Explicitly distinguish primary assets, required dependencies, and external references. Use typed allowlists and document dependency policies.
- No broad recursive traversal of all references. Never silently include unrelated assets from another domain.
- Support pagination and detail fetch; record unresolved references and failures.
- Fail closed on server version mismatch, missing configured domain, invalid package, or unsupported critical relationship.
- Do not commit credentials, `.env`, generated export data, or raw production metadata.
- Add unit tests for every scope and safety invariant in `PROJECT_PLAN.md`.

## Development workflow
- Use the existing checkout and the setup and test commands in `README.md`.
- Track supported entities and unverified assumptions in `CAPABILITIES.md`.
- Use synthetic fixtures for unit tests and disposable same-version instances for integration tests.
- Run lint and tests before committing changes on a feature branch. Prepare PRs for review and push or merge only with user authorization.
