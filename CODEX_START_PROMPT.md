# First prompt to paste into Codex

We are building a new GitHub project named `om-exim`: a safe selective export/import utility for OpenMetadata 2.x between two instances running the **exact same server version**.

Read `AGENTS.md` and `PROJECT_PLAN.md` in full and treat them as the source of requirements. Scope: selected domain FQN(s), recursively include subdomains, include primary entities **only if directly assigned** to selected domains, and include only necessary typed dependencies. Never pull unrelated assets through data product membership, schema membership, or generic recursive references.

First task: create a feature branch and implement **P0/P1 only**: project scaffold, package/CLI, safe configuration, version preflight interface, capability matrix documenting which OpenMetadata 2.x entity models/relations still need verification, read-only domain-scope planner with synthetic fixtures, and tests. Do not implement or execute writes to target. Do not claim production compatibility until exact server and SDK versions are supplied and checked. Run lint/tests and report results. Prepare a PR for review if GitHub is connected; otherwise leave a clean local commit and instructions.

Use Python type hints, pytest, ruff, and avoid hardcoded credentials. Include a concise README with installation, CLI usage, and security precautions. Ask for exact OM server/SDK versions before live integration.
