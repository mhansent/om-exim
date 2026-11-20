# Codex instructions for om-exim

Read `PROJECT_PLAN.md` before making changes. The user's intent is selective OpenMetadata **same-exact-version 2.x** migration. Scope = directly domain-assigned primary entities + descendant subdomains + explicitly necessary dependencies. Avoid graph explosion.

## Engineering requirements
- Start by inspecting actual installed SDK/API models. The exact OM 2.x patch and SDK version are not yet provided; do not invent them or assert compatibility. Request versions or make an explicitly mocked, version-agnostic scaffold first.
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

## First Codex task
1. Review this file and `PROJECT_PLAN.md`.
2. Inspect existing repository files and the exact pinned OpenMetadata version if provided.
3. Create a concise implementation plan and capability matrix; identify unverified assumptions.
4. Scaffold packaging, CLI, config example, and read-only planning interfaces.
5. Add synthetic fixtures/tests for domain descendants, direct asset inclusion, typed dependencies, no cross-domain asset leakage, and dry-run zero writes.
6. Run tests, commit on a feature branch, and prepare a PR. Do not push/merge unless the environment supports it and the user authorizes the action.

## Definition of done for first PR
- Installable package, documented CLI skeleton, safe config/env handling, automated tests passing, no live server required.
- Clear TODOs for API-model verification, with no fake working migration claims.
