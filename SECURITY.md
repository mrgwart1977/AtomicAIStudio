# Security Policy

## Supported versions

Atomic Dream Engine is pre-1.0 (`v0.x`). The `main` branch and the most
recent release are the versions that receive security fixes.

| Version | Supported |
|---|---|
| `main` | ✅ |
| Latest release tag | ✅ |
| Older releases | ❌ |

## Reporting a vulnerability

**Please use GitHub private vulnerability reporting** ("Security" tab →
"Report a vulnerability") so the report stays private until fixed.

Please do **not** open a public issue for security problems.

Include: affected version/commit, reproduction steps, impact, and any
mitigations you've found. Expect acknowledgment within ~7 days and a
fix-or-mitigation decision based on severity.

## Scope notes

- The DPAPI credential vault, project isolation (`project_id`), command
  audit trail, and backup/export paths are in scope.
- Model-download integrity (SHA verification) and runtime admission
  control are in scope.
- Third-party dependencies should be reported upstream when the issue
  lives in the dependency; report here if our usage creates the exposure.
- **Never** include real credentials, API keys, or private data in a
  report — describe the exposure mechanism instead.
