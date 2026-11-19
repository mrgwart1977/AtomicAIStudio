# C2 — Security Hardening: closeout record

## Merge

- **PR:** #158 — final reviewed head `18d7c99`
- **Merge SHA:** `fb501e6cea212c19faf75006dfede2b38d06b17b` (squash)
- **Post-merge main CI:** run `44458206432` (see note — verify all six
  jobs including `package` before C3 proceeds)
- **Migration head:** `0026_a7` (unchanged — no schema work in C2)

## Review-gate resolutions (Phase 1 verification)

- **F1 CI/test accounting:** final suite **1,948 passed + 11 skipped**.
  Pre-existing 1,860 (C1 head) + **88 new** (55 SSRF gate, 23 package
  security after F2 additions, 12 credential isolation — earlier
  "1,947/1,900/121" report was an arithmetic slip, not a test loss).
  Zero tests removed, renamed, or suppressed; `test_m11` gained a
  `size mismatch` acceptance alongside `hash mismatch` because the
  hardened importer now refuses earlier.
- **F2 archive defenses:** verified and extended — traversal both
  separators, absolute/drive/UNC/space-padded names on every member,
  duplicates refused (exact-match), canonical `files/<sha256>` arcnames,
  sha256 format + non-negative integer sizes, decompressed-size bound
  enforced mid-stream, member-count bound added (manifest cap + 8
  slack), CRC/truncated-member `BadZipFile` → `PackageError`, staging
  removed in `finally`, session opens only after all verification,
  single `session.commit`. Same defenses on every import path —
  `/api/packages/import`, `backup.import`, and `backup.restore` all
  funnel through `import_package`; preview only reads the manifest.
- **F3 issue #96:** gate verified — every resolved address checked
  (v4+v6), `::ffff:` mapped and `64:ff9b::/96` NAT64 embedded addresses
  unwrapped and re-checked, all blocked ranges incl. metadata IPs,
  TOCTOU/rebinding closed by construction (transport dials pinned
  literal IP, never re-resolves), `sni_host` preserves original name
  for TLS verification, redirects fail closed (none followed; per-hop
  `validate_redirect` exists for a future transport), `trust_env=False`
  + literal-IP dialing removes proxy bypass, unmarked/unpinned
  transports fail closed, fresh `FetchPlan` per call means no pool can
  reuse an unvalidated destination. **No live `stream_pinned`
  implemented — issue #96 stays OPEN** pending a separately authorized
  real transport.
- **F4 credential isolation:** synthetic secrets verified absent from
  API bodies, log records, package bytes + member names, vault
  plaintext; `providers` settings key stripped wholesale on import
  (enablement can't ride a package); secret-shaped key parts stripped;
  budget admission and dispatch authorization remain separate gates;
  same-user token exposure documented as a **residual local threat**
  (single-user/single-host scope only).
- **F5 installed provenance:** sidecar + NSIS rebuilt on `18d7c99`
  after the F2 code changes; full driver re-run — `C2 ACCEPTANCE:
  12/12 PASS` (`docs/c2-evidence/acceptance.txt`). Frozen backend
  carries `app.net`; bounded shutdown 1.1s; zero orphans.

## What shipped

- `app/net/egress.py` — resolved-IP egress gate (349 lines).
- `backup.py` — manifest structural validation, member-name safety on
  every entry, duplicate + member-count bounds, streaming decompressed-
  size cap, corrupt-member error hygiene.
- `rest_providers.py` / `rest_image_providers.py` — `fetch_artifact`
  requires a validated plan; live transports must implement
  `stream_pinned` (none exists → live fetch unreachable).
- Tests: `test_c2_ssrf_gate.py` (55), `test_c2_package_security.py`
  (23), `test_c2_credentials.py` (12).
- `docs/c2-threat-model.md`, `scripts/cdp-c2-acceptance.cjs`.

## Remaining risks / non-goals

- **Issue #96 implemented but NOT activated** — the live lane requires
  a separately authorized `stream_pinned` transport with real TLS
  verification; keep the issue open until then.
- Same-user local token exposure — residual threat, C4 review.
- Windows logoff — C4 human checklist.
- Stage B2b ON HOLD; all adapters remain `adapter_kind="mock"`.
