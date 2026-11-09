# Release tracking — outstanding items

Central tracker for every known limitation, deferred improvement, and release
requirement. **Rules:** nothing here is removed or marked resolved without
test evidence or a documented resolution. Reviewed and updated at the end of
every milestone. v1.0-blockers are called out explicitly.

Status values: `open` · `in-progress` · `blocked` · `resolved`

| # | Item | Priority | Target | Status | Blocks v1.0 | Issue |
|---|------|----------|--------|--------|-------------|-------|
| 1 | Clean Windows VM install & upgrade verification | High | v1.0 | open | **yes** | #5 |
| 2 | Windows installer code signing & SmartScreen | High | v1.0 | open | **yes** | #6 |
| 3 | Real UNC/network-share backup stress test | Medium | v1.0 | open | **yes** (documented limitation acceptable short-term) | #7 |
| 4 | Interrupted import resume support | Medium | post-v1.0 | open | no | #8 |
| 5 | Security review of test-only env overrides | High | v1.0 | open | **yes** | #9 |
| 6 | Large-project performance & scalability validation | High | v1.0 | open | **yes** | #10 |
| 7 | Installer & DB migration compatibility matrix | Critical | v1.0 | open | **yes** | #11 |
| 8 | Model download approval workflow | High | M3 | open | **yes** (gate before any real adapter) | #12 |
| 9 | First verified local AI adapter | High | M3 | open | **yes** | #13 |
| 10 | Usability testing with target users | Medium | M9 | open | **yes** | #14 |
| 11 | FAT32 >4 GiB guard — edge coverage | Low | v1.0 | open | **yes** (defensible as-is; margin improvement) | #15 |
| 12 | Shot-level reference-ID picker UI | Low | M3+ | open | no (API+schema delivered M2.1; UI deferred) | — |

---

## 12. Shot-level reference-ID picker UI — (no issue yet)

- **What:** `shot_characters.reference_ids_json` is schema-validated,
  project-scope-checked, persisted, backup-remapped, and round-trips through
  the storyboard API — but the shot editor has no picker for it. Shot-level
  identity refs can only be set via API.
- **Why open:** deliberate deferral — the field exists for generation adapters
  (M3+); building a picker before anything consumes it would be dead UI.
- **Acceptance:** shot editor offers a reference-asset picker once a
  generation adapter reads `reference_ids_json` end-to-end.

---

## 1. Clean Windows VM install & upgrade verification — #5

- **What:** M0.1/M1/M1.1/M2 validated on the dev machine (env-scrubbed frozen
  exe + real NSIS install + observed 0001→0002→0003→0004 upgrades). Still needs
  a pristine VM/Hyper-V run: install → launch → all screens → upgrade → uninstall.
- **Why open:** dev-machine validation can't prove a bare Windows box works.
- **Acceptance:** documented VM run with screenshots; install-over-install
  from a previous release verified; zero dev dependencies.

## 2. Windows installer code signing & SmartScreen — #6

- **What:** unsigned NSIS installer → SmartScreen warning on first install.
- **Why open:** needs a certificate decision (EV/OV vs. self-sign + fingerprint doc).
- **Acceptance:** signed install with no SmartScreen, or a documented
  self-sign + fingerprint-verification flow.

## 3. Real UNC/network-share backup stress test — #7

- **What:** UNC paths validate and probe reachability (M1.1), but no real
  NAS/SMB share was soak-tested — large streamed export, mid-write disconnect,
  cancel, latency.
- **Why open:** requires an actual share; behavior under real network
  failure modes is unverified.
- **Acceptance:** export + cancel + mid-write disconnect on a real share;
  friendly errors verified and documented.

## 4. Interrupted import resume support — #8

- **What:** interrupted imports are safely cleaned (staging sweep + failed
  run row) but must restart from zero — verified blobs aren't reused.
- **Why open:** designed for correctness first; resume adds a checkpoint ledger.
- **Acceptance:** re-running an interrupted import skips already-verified
  blobs; crash-mid-import + resume covered by test.

## 5. Security review of test-only env overrides — #9

- **What:** `ATOMIC_SIDECAR_TOKEN` (fixed automation token) and
  `ATOMIC_PYTHON` (interpreter override) ship in the release binary.
- **Why open:** decision pending — keep documented / debug-gate / remove.
- **Acceptance:** decision recorded and enforced in the packaged build.

## 6. Large-project performance & scalability validation — #10

- **What:** architecture targets TB-scale projects (CAS, bounded-memory
  streaming, indexed queries, pagination) but was never load-tested at scale.
- **Why open:** requires a synthetic large fixture; M2 added storyboard
  queries that need the same validation.
- **Acceptance:** project with tens of thousands of assets + hundreds of
  shots; storyboard/assets/backup endpoints stay responsive; memory bounded;
  numbers documented.

## 7. Installer & DB migration compatibility matrix — #11

- **What:** pairwise upgrades verified live (0001→0002→0003→0004), but no
  scripted matrix over all migration pairs, old-installer→new-installer, or
  downgrade refusal.
- **Why open:** needs the scripted harness; highest-risk area for user data.
- **Acceptance:** scripted matrix covering every migration pair +
  installer-over-installer + downgrade behavior, run in CI or documented.

## 8. Model download approval workflow — #12

- **What:** before any model download: name/version/official
  source/license/size+VRAM/intended use/smaller-or-cloud alternatives, and
  explicit approval.
- **Why open:** M3 scope; no downloads are permitted until implemented.
- **Acceptance:** approval surface implemented + enforced on the download
  path; test evidence.

## 9. First verified local AI adapter — #13

- **What:** all generation is the mock adapter; the first real local adapter
  is chosen after a hardware survey in M3.
- **Why open:** deliberate sequencing — never pre-committed.
- **Acceptance:** one real local inference adapter verified end-to-end on a
  target hardware tier.

## 10. Usability testing with target users — #14

- **What:** beginner-friendliness is a core requirement; no moderated
  sessions yet.
- **Why open:** scheduled for late milestones (M9).
- **Acceptance:** moderated sessions; findings filed; critical UX issues fixed.

## 11. FAT32 >4 GiB backup guard — edge coverage — #15

- **What:** FAT32 destinations are rejected when the *estimated* package
  exceeds 4 GiB (unit-tested via monkeypatch). Edge cases: estimate could
  undershoot on text-heavy manifests + incompressible media; the G:\ test
  drive turned out to be a disconnected reader, not a formatted FAT32 volume.
- **Why open:** implemented and tested, but real-volume verification and an
  estimate safety margin are still open.
- **Acceptance:** margin added to the estimate, or a real FAT32 volume verified.

---

### Milestone notes

- **M1.1 →** items 1, 3, 4, 5, 6, 7, 11 carried forward; FAT32 guard (#11→#15)
  implemented with monkeypatched coverage.
- **M2 →** all items re-validated; nothing closed (no new evidence). No new
  issues discovered during M2 beyond those listed. M2 storyboard queries are
  in scope of #10 (scalability validation).
- **M2.1 →** all items re-validated; nothing closed. One new tracked item
  (#12 above): `reference_asset_ids` is validated + persisted at the command
  boundary but has no shot-editor picker yet — deliberately deferred to M3+
  when generation consumes it. Migration chain now runs through `0005_m21`;
  upgrade matrix (#7) must cover `0004_m2 → 0005_m21`.
- **Integration →** PRs #2/#3/#4/#16/#17 merged into `main` as merge commits
  in dependency order (`9263024`, `e5c0294`, `00d42c7`, `0229e52`, `449ce0a`).
  Verified on integrated `main`: 89/89 tests, frontend build, fresh migration
  to `0005_m21`, packaged install + API smoke. One CI gap found and fixed:
  `tauri-check` never staged the sidecar resource dir, so it failed on every
  PR; the job now stubs the directory (bundling still uses the real sidecar
  via the `package` job). All tracked items above remain open as recorded.

---

### Milestone notes (M3 Phase 1)

- **M3 Phase 1 →** inference foundation landed on `feat/m3-generation`:
  model catalog, compatibility engine, GPU detection + scheduler, Model
  Manager rewrite, Generate workspace, `SdCppAdapter` + `generate.image`,
  migration `0006_m3`. **No model weights or runtime binaries downloaded —
  `docs/plans/M3_MODEL_PROPOSAL.md` is the approval gate for Phase 2.**
- Suite now **115/115** (26 new M3 tests). Multi-GPU parallelism is proven on
  simulated probes only; clearly labelled as such — the physical
  single-GPU path needs real weights (Phase 2).
- Item **#8 (approval surface)** now has an implemented enforcement path
  (install endpoint + confirm dialog stating size/destination/license) —
  keep open until exercised with a real download.
- Item **#13 (first verified real local adapter)** — the `SdCppAdapter`
  exists and is fake-worker-verified; stays open until real inference is
  demonstrated on the RTX 3060 Ti.
- All other tracked items remain open as recorded.

