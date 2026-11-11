"""Program C1 — Unified AI Provider Manager domain package.

Declarative 60-entry provider catalog + config registry + DPAPI
credential vault + mock connection testing. Documentation of record:
docs/plans/PROGRAM_C_CLOUD_PROVIDERS.md, C0_PROVIDER_FEASIBILITY.md,
C1_5_ACCESS_BRIDGE.md.

Non-secret config lives in app_settings (JSON). Secrets NEVER touch the
database, backups, logs, or the frontend — they live in a DPAPI-protected
credentials.vault under the data dir.
"""
