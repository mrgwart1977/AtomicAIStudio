# Phase A closeout — A1–A8 integrated filmmaking

Status: **Phase A complete — A8 merged `8fbdf96`** (final reviewed head
`a3161f9`; post-merge main CI verified). This is a milestone closeout,
**not** a public-release claim. Phase C (reliability, security,
performance, human acceptance, release engineering) is planned in
`docs/phase-c-roadmap.md` but not authorized.

## 1. Milestones and PRs

| # | Scope | PR | Merge SHA | Migration |
|---|-------|----|-----------|-----------|
| A1 | Integrated screenplay + deterministic shot-list scaffold | #141 | `7464d61` | `0020_a1` |
| A2 | Shot planning + storyboard media board (10 camera-follow modes, multi-character blocking, refs) | #143 | `30a16a8` | `0021_a2` |
| A3 | Shot keyframes + continuity prompts + mock image generation | #144 | `8fc1910` | `0022_a3` |
| A4 | Motion clips + mock video generation + timeline placement | #146 | `9a7c072` | `0023_a4` |
| A5 | Dialogue segments + mock audio (speakers, voice resolution) | #149 | `ad1013a` | `0024_a5` |
| A6 | First-cut assembly — deterministic plan → review → non-destructive apply | #151 | `fa23db9` | `0025_a6` |
| A7 | Multi-GPU scheduling + provider-neutral cloud planning gates | #153 | `5d8129c` | `0026_a7` |
| A8 | Filmmaking wizard — 15-stage guided pipeline over A1–A7 | #155 | `8fbdf96` | — (rides `settings_json`) |

Post-merge docs PRs: #142 (A1), #145 (A2), #147 (A3), #148 (A4),
#150 (A5), #152 (A6), #154 (A7).

## 2. Final migration chain

`0026_a7` → `0025_a6` → `0024_a5` → `0023_a4` → `0022_a3` → `0021_a2`
→ `0020_a1` → `0019_n3a` (pre-Phase-A base). A8 adds **no** migration —
wizard progress persists via `settings_json` merge semantics, verified
against populated pre-A8 databases in installed acceptance B2.

## 3. Test totals (final reviewed head `a3161f9`)

| Suite | Result |
|-------|--------|
| Backend pytest (full) | **1851 passed**, 11 skipped, 0 failures |
| Frontend vitest | **411 passed** (29 files) |
| A8 backend wizard tests | 20/20 |
| A8 frontend wizard tests | 23 |

## 4. Installed Windows acceptance (per milestone)

| Milestone | Steps | Result |
|-----------|-------|--------|
| A1 | 20/20 | `docs/a1-evidence/a1-acceptance.txt` |
| A2 | 16/16 | `docs/a2-evidence/a2-acceptance.txt` |
| A3 | 17/17 | `docs/a3-evidence/a3-acceptance.txt` |
| A4 | 15/15 | `docs/a4-evidence/a4-acceptance.txt` |
| A5 | 17/17 | `docs/a5-evidence/a5-acceptance.txt` |
| A6 | 19/19 | `docs/a6-evidence/` |
| A7 | 17/17 | `docs/a7-evidence/a7-acceptance.txt` |
| A8 | **34/34** on `4e6fda3` — NSIS 76.43 MiB, PyInstaller-frozen Python 3.14.7, no system Python, bundled-ffprobe export validation, restart persistence, backup/restore, populated-DB upgrade, clean shutdown | `docs/a8-evidence/` |

## 5. End-to-end workflow capabilities (implemented + tested)

- Projects with per-project isolation (`project_id` everywhere);
  settings, wizard progress, provider overrides in `settings_json`.
- Screenplay: structured create/edit + documented plain-text import;
  deterministic shot-list scaffold.
- Storyboard: scenes, cast, locations, continuity refs; shot planning
  with angle/framing/movement and all ten camera-follow modes;
  multi-character targeting.
- Keyframes: start/end/reference/intermediate, continuity prompts,
  editable prompt + negative prompt, mock image generation.
- Motion clips: keyframe-driven mock video with timeline placement.
- Dialogue/audio: speaker lines, character voice resolution, mock
  segment generation, lane mixing + automation.
- First-cut assembly: deterministic plan → fingerprint-pinned review →
  scoped approval → non-destructive timeline apply.
- Timeline: multi-track edit, trims, gain, automation, audio mix.
- Export: FFmpeg H.264/AAC pipeline, bundled ffprobe validation.
- Wizard (A8): 15 guided stages, Beginner/Advanced views over the same
  rows, progress save/resume, derived warnings and honesty labels.
- Scheduling (A7): simulated multi-GPU reservation scheduling, CPU
  lane, hetero devices, queue reasons, device-loss handling.
- Backups: export/import package with CAS dedupe; restore of project's
  own runs; settings import sanitized at the package boundary.

## 6. Mock-versus-real matrix

| Capability | Status |
|------------|--------|
| Image generation | **Simulated only** (`adapter_kind="mock"`, labeled `simulated`) |
| Video/motion generation | **Simulated only** |
| Speech/audio generation | **Simulated only** — tones, never presented as speech |
| Cloud provider planning | Read-only `cloud.plan`, mocked providers, no network |
| Live provider dispatch | **Blocked** — `live_gate.dispatch_admission` intact; issue #96 mandatory before live retrieval |
| GPU execution | **Simulated reservations only** — no real inference hardware use |
| Timeline/export/render | **Real** — local FFmpeg pipeline produces valid media |

## 7. Known limitations

- ~750-unique-source export limitation (documented; C3 scope).
- No real AI inference of any kind; all generated content is
  deterministic mock output.
- Screenplay import is plain text only — no Fountain/FDX.
- No lip synchronization; mock speech is not spoken dialogue.
- Perf-boundary flake class observed twice (`test_edl_v2_…`, ~1s bound
  on loaded runners) — documented in A6/A7 hardening notes.
- The 19-item human audio/editing checklist is **NOT RUN**.

## 8. Outstanding human acceptance

`docs/n3-human-checklist.md` — 19 checks (A1–A8, B1–B2, C1–C6, D1–D3),
all **NOT RUN**. Earlier loose references to "24" were reconciled in
the doc header. C4 proposes executing this checklist.

## 9. Security and provider authorization status

- Credential values live in the DPAPI CredentialVault only — never in
  packages, exports, or `settings_json`.
- `settings_json.providers` (enablement/endpoint/region overrides) is
  stripped at package import; imported projects re-derive provider
  state from app config + explicit user approval. Secret-shaped keys
  are defensively stripped too.
- Budget holds, live-gate state, and provider credentials are separate
  rows/config — they never cross a package boundary.
- Loopback API auth via `X-Atomic-Token`; WS first-frame auth.
- DNS/SSRF restrictions, redirect validation, audit history,
  idempotency keys, and fail-closed dispatch preserved end to end.

## 10. Phase C readiness

Ready for planning only: `docs/phase-c-roadmap.md`. Remaining work
before any release claim: human checklist execution, reliability
baseline, security hardening, performance ceilings, installer/release
engineering. Stage B2b remains ON HOLD; issue #96 gates live artifact
retrieval.
