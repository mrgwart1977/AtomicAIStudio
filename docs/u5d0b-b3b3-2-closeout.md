# U-5d-0b Phase β3-B3.2 closeout — verified synthetic runtime acquisition foundation (racq:v0)

Status: **technically accepted, merged, and approved for formal closure**
— repository-recorded closure becomes effective upon acceptance and
merge of this documentation-only closeout PR. This is a milestone
closeout of a **synthetic, non-production** foundation, **not** a
release, qualification, enrollment, installation-publication, or
runtime-execution claim.

## 1. Implementation history and exact identities

| Stage | Commit / identity |
|---|---|
| Original implementation | `77bd2c450751cb121da0fbb7fff80ea5b7118e3a` (direct child of accepted baseline `a9e3a5590325beba20b52f06b8a38e7e7f287363`, baseline tree `4b7c1cc02510aca46d59c3727d3919f7800ceb0c`) |
| C1–C6 bounded correction | `6f6ce7e06ecc2cd28169fb6d061d2f6d55b612f1` (direct child of `77bd2c4`) |
| N1/N2 final acceptance hardening | `d5e5b760b12ac12e773ddb994d32c49c7cf3cc67` (direct child of `6f6ce7e`, tree `827d6c23cd3149bd88af600e92b843f72ae41602`) |
| PR #197 merge commit | `5b0998db10b629b0c86ea949dd3e532b2b66b281`, merged 2026-11-28T03:48:33Z, merge-commit method, first parent `a9e3a55`, second parent `d5e5b76`, merged-main tree `827d6c23` (byte-identical to the authorized head tree) |

All three implementation commits remain in merged-main ancestry. The
final accepted implementation is `d5e5b76`; earlier heads are
superseded history, not accepted implementations.

Review sequence: original synthetic acquisition foundation → initial
technical acceptance findings → C1–C6 bounded corrections →
post-correction V1–V7 technical acceptance review → N1/N2 final
acceptance hardening → final technical acceptance → exact-head merge
authorization → PR #197 merge → merged-main verification.

Changed-file boundary (aggregate vs accepted baseline, preserved
through all stages): exactly `backend/app/domain/runtime_acquisition.py`
and `backend/tests/test_runtime_acquisition_u5d0b3b2.py`.

## 2. Established capabilities (synthetic foundation)

- Isolated `racq:v0` acquisition evidence envelope referencing the
  immutable `rref:v0` reference digest; canonical acquisition identity
  over `{contract, reference_digest, runtime_key, flavor, selected,
  omitted, assets}`.
- Supplied synthetic acquisition authority (acquire vs extract
  operations); known-revocation check precedes permit matching;
  immutable bounded frozenset snapshots of revoked identities.
- Exact asset-identity uniqueness across the entire supplied release
  inventory — name, id and url indexes refuse duplicates including
  unselected assets.
- Bounded injected synthetic transport (no real network); archive
  SHA-256 verification on streamed bytes.
- Verified file-backed archive association: baseline stat on the open
  write handle, raw-fd (`os.read`) rehash bypassing buffered caches,
  same-handle preflight and extraction, final persistent-association
  check before completion.
- Complete metadata-detectable preflight of every selected archive
  before the first managed-member write.
- Windows junction/reparse-point refusal on staging roots, archive
  paths, member directories and destinations.
- Managed-member path and structural validation (flat archives,
  `valid_member_name`, NUL/drive/parent-traversal refusal).
- Bounded EOCD pre-assessment before parser materialization (tail
  scan, comment-length-to-EOF check, entry/directory bounds); declared
  vs actual entry-count reconciliation.
- Aggregate archive-buffer and central-directory accounting; exact
  integer compression-ratio checks.
- **Explicit general-purpose bit-3 (data-descriptor) refusal** —
  see §4. Encryption, strong-encryption, masked-directory, ZIP64 and
  multidisk structures refuse deterministically.
- **Deterministic archive resource ownership** via
  `contextlib.ExitStack` — see §5.
- Cancellation checkpoints (stream, rehash, EOCD, preflight,
  extraction, final) and controlled refusal semantics; partial staging
  evidence preserved on every controlled outcome.

None of this establishes production runtime acquisition: every
envelope remains non-executable and the module has no production
consumers.

## 3. C1–C6 findings disposition — all accepted

- **C1** verified archive content association: accepted.
- **C2** known revocation and synthetic authority: accepted.
- **C3** Windows junction and reparse-point safety: accepted within
  documented limitations (metadata-based checks; symlink-privilege
  environments exercise the real path).
- **C4** exact asset identity uniqueness: accepted.
- **C5** complete metadata-detectable preflight: accepted.
- **C6** bounded resource and ZIP parser hardening: accepted within
  the supported ZIP subset.

## 4. N1 disposition — resolved via explicit bit-3 refusal

ZIP data-descriptor (general-purpose bit 3) entries are **unsupported**
and refused during metadata preflight:

- Central flag check refuses bit 3 on every surviving entry —
  managed **and** declared-skipped alike
  (`member_flags_unsupported:<name>`).
- Local/central flag disagreement refuses via the retained compare
  mask (`archive_local_central_mismatch`).
- Refusal is metadata-detectable and precedes all managed-member
  writes; descriptor-form fixtures (including descriptor-tail overlap
  and central-directory intrusion fixtures) fail closed on the flag
  check before range assessment.
- The descriptor-specific local-size zero-or-match exception was
  removed — committed local/central sizes now always match exactly.
- Supported-subset narrowing documented in-module and here; one
  authorized test adjustment: `test_c6_20_size_semantics` now asserts
  descriptor refusal (previously asserted bounded acceptance). The
  underlying requirement — local/central size-consistency screening —
  is preserved in the same test's first half. Zero cases removed.

Member SHA-256 verification is **not** claimed to establish
descriptor structural integrity; the concern is resolved by refusing
the descriptor form outright. Future data-descriptor support requires
separate compatibility review and authorization.

## 5. N2 disposition — resolved via deterministic ownership

All archive resources retained during acquisition are explicitly
owned by a single `contextlib.ExitStack` covering the
stream/preflight/extraction/final-verification region:

- Owned `BytesIO` buffers registered at creation; verified
  file-backed handles registered immediately upon acquisition —
  before preflight, never deferred to `prepared` insertion.
- ZIP parser objects registered after their underlying handles, so
  LIFO unwinding closes parser-before-handle on every exit path.
- Cleanup occurs across success, refusal, cancellation and failure
  exits; required cleanup completes before a successful envelope is
  constructed. A close failure during unwind cannot produce a
  completed acquisition — it propagates to the fail-closed boundary.
- Streaming write handles keep explicit `finally` cleanup; raw-fd
  verification sequence, cancellation ordering and partial staging
  evidence preserved.
- Instrumented `_TrackedFh`/`_TrackedIO`/`_TrackedZip` tests with
  retained external references assert explicit `close()` ordering —
  interpreter teardown cannot satisfy the assertions.

Interpreter teardown is **not** the primary cleanup mechanism on any
path.

## 6. Regression evidence

Acquisition suite progression: 108 collected (original) → 172 (C1–C6)
→ **194** (N1/N2). 86 additional cases; **zero removed, zero newly
introduced skips**.

| Evidence | Result |
|---|---|
| Focused acquisition suite | 194 collected, 194 passed, 0 skipped, 0 failed — actual Windows junction test executed (real `mklink /J`) |
| Protected regression batches | 563 passed/3 skipped + 208 passed/1 skipped — all skips pre-existing (symlink privilege) |
| Full local backend suite | 3858 collected = 3664 + 194; 3844 passed, 14 skipped, 0 failed, 0 teardown errors |
| Exact-head PR CI `46099189015` (attempt 1, head `d5e5b76`) | changes: success; backend 3.12 + 3.14: success on `windows-latest` (3848p/10s each); frontend, tauri-check, package: **skipped** by legitimate path filtering (not executed) |
| Merged-main CI `46104725217` (attempt 1, push, head `5b0998d`) | **all six jobs executed and succeeded**: changes (ubuntu-latest); backend 3.12 + 3.14 (windows-latest) 3848p/10s each; frontend (ubuntu-latest); tauri-check (windows-latest); package (windows-latest) |
| Ruff | clean on both implementation files |

## 7. Reporting accuracy reconciliation (F8)

- Local skip count: **14**; CI skip count: **10** per backend job.
- CI individual skip identities: **unestablished** (logs report
  counts only); the exact causes of the four-test local-versus-CI
  difference are **unestablished** — the difference is consistent
  with the runners' environment but no individual-test evidence
  proves a specific cause.
- Successful `package` CI on merged main is a build job result only.
  It does **not** constitute installer-artifact production evidence,
  successful installer execution, installed-application acceptance,
  or human acceptance qualification. No new human installer
  qualification or C4 human acceptance completion is established by
  this milestone.

## 8. Protected regression baseline — proposed promotion

- Official baseline (unchanged): **1308**.
- Proposed post-closure baseline: **1308 + 194 = 1502**.
- Newly protected component: the complete 194-case acquisition
  regression suite `backend/tests/test_runtime_acquisition_u5d0b3b2.py`
  — protected for subsequent runtime acquisition, installation,
  enrollment and execution development.
- All existing 1308 protected cases and their established skip
  dispositions preserved; none removed, replaced, weakened or
  reclassified.
- The promotion is a documentation record only — no executable
  configuration exists for the baseline. The effective accepted
  baseline remains **1308** until this closeout PR is separately
  authorized, merged and verified.

## 9. Contract and canonical identity preservation

`rref:v0`, `racq:v0`, `rtq:v1`, `lcfg:v1`, `hwid:nv/v1`,
`execbind:v1`, `identity_cached()`, `evidence_cached()`, all ten
fingerprints, canonical acquisition digest semantics, and migration
head `0030_u4` are preserved.

Every acquisition outcome declares: `production_trust:false`,
`execution_eligible:false`,
`operational_qualification:"unestablished"`,
`trusted_enrollment:"unestablished"`,
`installation_publication:"not_performed"`.

## 10. Production non-activation record

β3-B3.2 introduced **no**: production importer of
`runtime_acquisition`, production router, production job, scheduler
change, watchdog change, GPU reservation change, runtime launch
path, registry integration, new dependency, database migration, real
provider call, API expenditure, real runtime acquisition, trusted
installation enrollment, or installation publication. Synthetic
acquisition remains isolated.

## 11. Accepted bounded limitations and deferred functionality

- Residual concurrent filesystem modification/restoration races
  (no verification-to-launch guarantee).
- Inode-unavailable filesystem identity degradation — never bypasses
  content verification.
- Supplied authority snapshots rather than live revocation; the
  synthetic authority is not a security boundary against arbitrary
  in-process Python.
- Blocking iterators are not necessarily interruptible.
- No production network deadline/timeout, disk-budget, or total
  process RSS enforcement.
- No exhaustive ZIP forensics; ZIP64, multidisk and data-descriptor
  entries unsupported within the accepted subset.
- Declared-skipped content not independently member-verified.
- Shortened staging-digest collision fails closed.
- No versioned installation publication, no restart reconciliation.
- Logical staging ownership does not establish production tamper
  resistance.
- F7 staging-ownership limitations and F8 reporting qualifications
  preserved.

None of these limitations may be silently removed from the roadmap;
none are implemented under this closeout.

## 12. Repository governance observation (non-blocking)

During the β3-B3.2 merge operation the traditional branch-protection
endpoint for `main` returned HTTP 404 — i.e. `main` had no classic
branch protection at merge time. This does **not** establish that all
repository rulesets or inherited organization-level protections were
exhaustively verified. A future, separately authorized governance
review is recommended covering: main branch protection, repository
and inherited rulesets, required CI checks, pull-request
requirements, required approvals, force-push and branch-deletion
protection, bypass permissions, and preservation of merge-commit
support. No repository settings were modified under this work.

## 13. Holds preserved

The following remain held pending separate explicit owner
authorization: subsequent β3-B3 implementation; β3-B4 explicit
reference enrollment; C4 Human Acceptance Readiness (all 47 human
checks); C5 Release Readiness; DeepSeek B2b live validation; real
provider calls and API expenditure; real runtime acquisition;
production runtime execution; trusted installation enrollment;
installation publication; and all other previously held work.
