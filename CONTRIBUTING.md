# Contributing to Atomic Dream Engine

Thanks for helping build a free, open-source AI filmmaking suite.
This project values correctness and honest capability claims over speed.

## Development workflow

1. Fork/branch from `main`: `feat/<topic>`, `fix/<topic>`, `chore/<topic>`.
2. Make changes; keep them scoped to one concern.
3. Run the validation gate before opening a PR:
   `scripts\validate.ps1` (backend on Python 3.12 + 3.14, frontend build;
   `-Tauri`/`-Package` optional).
4. Open a PR — CI must be green; describe *why*, not just *what*.
5. One approval from the maintainer is required before merge (squash).

## Rules that protect the codebase

- **Spec of record:** `docs/handoff/` (v0.9). Amendments extend it —
  never silently remove a requirement.
- **Project isolation:** every project-owned row carries `project_id`;
  isolation is enforced in DB + API, not just UI.
- **Mutations go through the command system** (preview → commit →
  audit → undo). No direct state writes from UI paths.
- **Jobs are durable:** `generation_jobs` rows, idempotent via
  `idempotency_key`.
- **Mock ≠ inference:** mock adapters are labeled `adapter_kind="mock"`
  and must never be presented as real generation. Tests asserting
  otherwise will be rejected.
- **No secrets, ever:** no credentials in code, tests, logs, docs, or
  git history. Provider secrets live only in the DPAPI vault.
- **Zustand rule:** never construct new arrays/objects inside `useApp`
  selectors — select the stable field, derive outside.

## Tests

- New behavior → new tests. Bug fixes → regression tests.
- The backend suite must pass on **both** Python 3.12 and 3.14.
- Tests must not download models, call cloud APIs, or require a GPU.
  Worker/provider tests use mock workers and stub adapters.

## External integrations (models, providers, runtimes)

Before proposing a new AI model, runtime, or cloud provider integration:

1. Its license must permit our usage — document it in `model_catalog.py`
   / the provider catalog with honest `commercial_ok` metadata.
2. Weights are never vendored or bundled — user-approved downloads only.
3. Capabilities are labeled honestly (T1 API / T2 assisted / T3 manual /
   T4 external-only). Unsupported ≠ functional.
4. GPL-licensed runtimes (e.g., ComfyUI) may only be external
   user-installed tools — never copied into this codebase.
5. No paid cloud calls, browser automation, or real inference in CI —
  ever.

## Commit style

Short imperative summary + a paragraph on *why*. Reference issue/PR
numbers where relevant.

## License

Contributions are accepted under Apache-2.0 (inbound = outbound per
Apache-2.0 §5). By submitting a PR you certify you can license your
contribution this way.
