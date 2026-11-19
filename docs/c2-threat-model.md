# C2 — Security Baseline and Threat Model

Branch `feat/c2-security`. Scope: desktop app + frozen FastAPI sidecar +
project data. **No live providers, no real inference, Stage B2b on hold,
issue #96 gate implemented but not enabled.**

## Trust boundaries

| # | Boundary | Crossing data | Controls |
|---|----------|--------------|----------|
| B1 | Tauri WebView2 → loopback FastAPI | commands, media bytes, job events | `X-Atomic-Token` header (no query tokens since A3), `127.0.0.1` bind, CORS allowlist, WS first-frame auth + origin check + bounded unauthenticated slots |
| B2 | Sidecar → project DB (SQLite) | all project rows | `project_id` FK enforced in DB+API; every mutation via command preview→commit→audit |
| B3 | Sidecar → CAS on disk | media blobs | `safe_join` rejects `..`/absolute; content-addressed names; hash-verified ingest |
| B4 | `.atomicpkg` import → project | manifest + blobs | **C2 hardened**: structural manifest validation, canonical `files/<sha256>` arcnames, traversal/absolute refusal on *every* member, duplicate-member refusal, decompressed-size bound during streaming, hash+size verify, refusal before DB session opens, staging cleanup |
| B5 | Sidecar → provider endpoints | prompts, artifact refs | **mock-only**; `live_gate.dispatch_admission` intact; **C2 added** `app/net/egress.py` — resolved-IP gate for any future live path |
| B6 | Credential vault (DPAPI) | provider secrets | ciphertext-only file; metadata-only API; never in DB/logs/backups/diagnostics; fail-closed (`VaultUnavailable`, `SecretUnreadable`) |
| B7 | Sidecar → FFmpeg/subprocess | media argv | argv-array `Popen` only — no `shell=True` anywhere; bounded shutdown kills process trees |
| B8 | Model/artifact downloads | weights, artifacts | **disabled** — no live retrieval; #96 gate is the prerequisite |

## Attack surfaces and controls

### AS-1 Hostile `.atomicpkg` (imported by the user or a tool)
- **Vectors:** path traversal (`../`, `..\\`, `C:/`, `/abs`, `\abs`, leading/trailing spaces), zip bombs (declared small, decompress huge), duplicate members (parser confusion), hash/size lies, manifest schema abuse, cross-project reference injection, credential-shaped fields in settings.
- **Controls (new this milestone):** member-name validation runs on *every* archive entry before the whitelist skip — `..` on either separator, drive-letter, leading `/`/`\`, and space-padded names all refuse. Manifest validated structurally (sha256 format, integer non-negative size, canonical `files/<sha256>` arcname equal to `files/{sha256}`, no duplicate hashes). Duplicate zip members refused. Per-file decompressed bytes bounded while streaming. All refusals occur before a DB session opens (proven by session-probe test) and staging is cleaned on every failure.
- **Residual:** none identified for the extraction path; `zipfile.extract()` is not used.

### AS-2 SSRF via future artifact retrieval (issue #96)
- **Vectors:** DNS rebinding/TOCTOU, private/loopback/link-local/CGNAT/metadata IPs, IPv6 (`::1`, `fec0::/10` site-local), redirect chains to internal hosts, proxy bypass, userinfo smuggling, non-443 ports.
- **Controls (new):** `app/net/egress.py` resolves the hostname, validates **every** resolved address against a blocked-range table, and returns a `FetchPlan` pinned to the validated IP. Live-marked transports must implement `stream_pinned` — a plain/default `httpx` transport is refused at construction. Redirects are refused (no silent follow); proxies/env routing are excluded from the pinned path. Existing checks retained: HTTPS-only, exact/subdomain artifact-host allowlist, no userinfo, port 443 only, no provider auth headers to artifact hosts, streaming byte cap, image-decode limits.
- **Status:** gate implemented and covered by 88 deterministic tests with fake resolver/connector objects. **Not enabled** — no live transport exists; `verification_evidence_valid` remains unreachable by design.

### AS-3 Loopback API abuse (other local processes)
- **Vectors:** stolen token, cross-project access, unauthenticated WS, hostile origin.
- **Controls:** `X-Atomic-Token` required on all protected routers; `?token=` removed (A3); WS closes non-auth first frames; host/origin allowlists in `app/security.py`; every project row carries `project_id` enforced in DB+API queries.
- **Residual:** a local process that can read the token from the spawned sidecar's environment can impersonate the frontend — accepted for a single-user desktop app; documented for C4 human review.

### AS-4 Credential disclosure
- **Vectors:** secrets in logs, backups, API bodies, crash dumps, frontend store.
- **Controls:** DPAPI CurrentUser encryption; API returns `{configured, fields, set_at, readable}` only; `backup.py` strips credential/secret/token/password-shaped keys on import **and** export; vault file sits outside export scope. C2 tests assert synthetic secrets appear in none of: API responses, log records, package bytes, package member names, vault plaintext.
- **Residual:** secrets exist in backend memory during provider calls — inherent; cleared by process exit.

### AS-5 Provider dispatch without authorization
- **Vectors:** planning treated as approval, budget bypass, credential presence implying permission, alternate dispatch routes.
- **Controls:** `live_gate.dispatch_admission` is the sole gate, invoked at the send boundary in `live_transport.py` and `live_permit.py` — not at plan time. Grant requires explicit approval + fresh evidence + budget admission + readable credential. Mock adapters (`adapter_kind="mock"`) cannot reach the live lane (unmarked transport refused).
- **Residual:** none identified; all dispatch is simulated.

### AS-6 Subprocess/filesystem abuse
- **Vectors:** command injection via filenames, shell metacharacters in prompts, temp-file residue, symlink races.
- **Controls:** every subprocess spawn is argv-array (`proc_guard.Popen(argv)`); `shell=True`/`os.system` absent from `app/`. FFmpeg filter strings are constructed from typed fields, not concatenated user input. `safe_join` confines CAS and file-serving paths. Staging/temp dirs cleaned on failure paths.
- **Residual:** operator-provided `ATOMIC_SDCPP_CMD` override splits via `shlex` — deliberate power-user feature, still argv-form.

## Severity register

| ID | Severity | Finding | Disposition |
|----|----------|---------|-------------|
| C2-F1 | High | `.atomicpkg` decompressed-size unbounded → zip bomb could fill disk before hash check | **Fixed** — streaming bound + manifest structural validation |
| C2-F2 | High | Traversal members lacking `files/` prefix were silently skipped | **Fixed** — `_unsafe_member_name` checked on every member first |
| C2-F3 | High | Issue #96: no DNS/IP validation — future live fetch could hit private/metadata IPs | **Fixed** (gate implemented, still disabled) |
| C2-F4 | Medium | Duplicate zip members collapsed silently | **Fixed** — refused |
| C2-F5 | Low | Token readable by local processes | Accepted — single-user desktop; documented |
| C2-F6 | Info | Windows logoff behavior | Deferred to C4 human checklist |

## Evidence

- `tests/test_c2_ssrf_gate.py` — 88 tests (blocked ranges, rebinding, pinning, redirects, proxies, size caps, mock vs live transport).
- `tests/test_c2_package_security.py` — 21 tests (all refusal vectors + no-DB-mutation probe + end-to-end API).
- `tests/test_c2_credentials.py` — 12 tests (ciphertext-only vault, no leaks to API/logs/packages, fail-closed).
- Prior suites retained: `test_a2_ws_auth`, `test_a3_query_token_removed`, `test_c2_live_gate` (dispatch gates, credential invalidation, audit has no secrets), `test_m1` backup roundtrip.
