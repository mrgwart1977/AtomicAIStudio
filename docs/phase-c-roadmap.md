# Phase C — proposed roadmap (planning only, not authorized)

Phase C hardens the Phase A foundation toward a releasable desktop
application. Five milestones, each independently gated on owner
approval. **Nothing here is implemented until the roadmap is approved
and a milestone is individually authorized.**

Boundaries carried forward: Stage B2b ON HOLD; issue #96 mandatory
before live artifact retrieval; no live provider calls, credential
access, paid activity, provider CLIs, model downloads, or real
inference without separate approval.

## C1 — Reliability baseline

**Scope:** CI stability (flake inventory + bounds review); shutdown and
process lifecycle (orphan sidecar/WebView2 detection); durable job
recovery after crash/restart; migration safety (downgrade/backup-first
upgrade); backup/restore round-trip hardening; installer upgrade
reliability over populated installs.

**Dependencies:** Phase A merged. No new features.

**Risks:** lifecycle bugs are platform-specific (WebView2, sidecar);
job-recovery changes touch the durable-job core — regression surface
is wide.

**Acceptance criteria:** zero known flaky tests under repeated CI
re-runs (3× green streak); kill-9 recovery restores durable jobs to a
documented terminal or resumable state; upgrade install over each
supported prior release preserves all project data.

**Automated tests:** flake-quarantine list + retry-instrumented CI
report; crash-injection tests for job recovery; migration up/down
matrix tests; corrupted/missing package import tests.

**Installed tests:** kill app mid-generation → restart → verify job
state; upgrade install A7→C1 build; repeated install/uninstall cycles.

**Human tests:** none required (all automatable).

**Approval gate:** owner sign-off on scope before implementation.

## C2 — Security hardening

**Scope:** loopback auth review (token entropy, rotation, port
binding); credential isolation audit (vault boundaries, no secret
material in packages/logs/audit); project/package import validation
(schema version checks, manifest signature-lite integrity, settings
sanitizer coverage review); path traversal and archive safety (zip
slip, symlink, device paths); DNS/SSRF and redirect controls review;
issue #96 resolution design; provider dispatch authorization audit.

**Dependencies:** C1 recommended first (recovery paths interact with
import/restore). #96 design may spawn its own gated milestone.

**Risks:** auth changes can lock users out of their own installs;
overly tight import validation breaks legacy backups (compat matrix
required).

**Acceptance criteria:** threat-model doc for package import +
provider dispatch; fuzzed package inputs cannot write outside CAS or
inject settings/identity; auth model documented and pen-checked on
loopback; #96 either resolved behind an explicit owner gate or
carried with a written rationale.

**Automated tests:** malicious-package corpus (traversal, symlink,
oversized, wrong manifest); settings sanitizer property tests; auth
negative tests (no token, wrong token, replayed WS frames).

**Installed tests:** import hostile packages on the real install;
verify no writes outside the app data dir; offline launch behavior.

**Human tests:** review of auth UX (token prompts, error messages).

**Approval gate:** owner sign-off; #96 resolution needs its own
explicit approval.

## C3 — Performance and scalability

**Scope:** timeline performance with large clip counts; large-project
load (hundreds of scenes/shots/assets); CAS growth management (GC,
dedupe stats, compaction); memory profiling of backend + WebView2;
GPU scheduler throughput under queued load; job throughput and queue
latency; export performance; the ~750-unique-source export limitation
(root-cause + fix or documented ceiling).

**Dependencies:** C1 (reliable baselines to measure against).

**Risks:** perf work on the timeline/EDL path risks regressing the
A6 assembly correctness pins; CAS GC risks data loss if refcounting
is wrong — needs destructive-op care.

**Acceptance criteria:** documented perf envelope (project size,
timeline clip count, export duration vs source count) with CI
regression bounds tuned for loaded runners (fix the 1s-bound flake
class); 750-source limit either removed or bounded by an explicit,
tested cap.

**Automated tests:** perf-boundary suite with load-aware thresholds;
CAS growth/dedupe tests; synthetic large-project fixtures.

**Installed tests:** large-project timeline scrubbing and export on
real hardware; memory over long sessions.

**Human tests:** timeline responsiveness feel-check on the target
hardware profile.

**Approval gate:** owner sign-off on perf targets before
implementation.

## C4 — Quality and human acceptance

**Scope:** execute the authoritative 19-item human checklist
(`docs/n3-human-checklist.md`); audio listening tests (sync, fades,
mix levels, tone-vs-speech honesty); timeline usability pass; wizard
usability pass (beginner completion of the 15 stages unaided);
accessibility (contrast, keyboard nav, screen-reader labels, error
message clarity); error-recovery UX review; upgrade-path human test.

**Dependencies:** C1–C3 recommended so humans test the hardened build.

**Risks:** human findings create unbounded scope — the milestone needs
a triage protocol (blocker / fix-in-C / defer) defined up front.

**Acceptance criteria:** all 19 checks marked Human PASS/FAIL with
evidence; zero release-blocker findings unresolved; accessibility
audit checklist completed; wizard completable by a first-time user.

**Automated tests:** accessibility lint where automatable (ARIA,
contrast ratios); error-surface snapshot tests.

**Installed tests:** same checks run installed, not dev.

**Human tests:** this milestone *is* the human gate — scripted
sessions with notes and severity tags.

**Approval gate:** owner sign-off on the human-test protocol and
blocker triage rules before execution.

## C5 — Release engineering

**Scope:** NSIS installer reliability (upgrade/downgrade/repair/
uninstall matrix); frozen-backend verification (import surface,
missing-module detection in CI); Windows compatibility floor
(supported versions, WebView2 runtime checks); reproducible builds
(deterministic sidecar + installer hashes where feasible); signing
strategy (code-signing cert path, unsigned-build warnings);
documentation refresh (install, upgrade, recovery, known limits);
release-candidate gate definition.

**Dependencies:** C1–C4 (sign/release last — nothing to release until
quality gates pass).

**Risks:** reproducible PyInstaller+NSIS builds are hard; signing
requires real-world decisions (cert procurement) outside code.

**Acceptance criteria:** documented release pipeline from tag →
signed installer → verification checklist; CI `package` job extended
to verify frozen-backend completeness (import smoke test on the
artifact); reproducibility report.

**Automated tests:** installer-matrix harness (fresh/upgrade/
downgrade/repair); frozen-backend smoke suite run against the built
sidecar; hash-comparison for two clean builds (documented variance).

**Installed tests:** the full matrix on clean VMs/machines; offline
install; install without admin rights.

**Human tests:** install/uninstall UX review; first-run experience.

**Approval gate:** owner sign-off; signing/credential decisions are
explicitly out of code scope and need separate owner action.

## Suggested sequencing

C1 → C2 → C3 → C4 → C5, each gated. C4 may partially parallel C3
(human sessions can start on the reliability-hardened build while
perf work continues) if the owner approves overlapping execution.

## Explicitly out of scope for Phase C

- Live provider execution, real inference, credential retrieval,
  paid operations (separate authorization required; #96 gates
  artifact retrieval).
- Stage B2b (ON HOLD).
- Public release — Phase C ends at release-candidate readiness, not
  release.
